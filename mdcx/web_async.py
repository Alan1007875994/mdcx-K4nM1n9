import asyncio
import contextlib
import json
import os
import random
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiofiles
import aiofiles.os
import httpx
from aiolimiter import AsyncLimiter
from curl_cffi import AsyncSession, Response
from curl_cffi.requests.exceptions import ConnectionError, ProxyError, RequestException, Timeout
from curl_cffi.requests.session import HttpMethod
from curl_cffi.requests.utils import not_set
from PIL import Image

from .models.crawl_status import CrawlStatus, batch_diagnostics, check_service_stop, observe_http
from .network_fingerprint import (
    BrowserFingerprint,
    RequestPurpose,
    build_fingerprint_headers,
    infer_request_purpose,
    merge_headers,
    select_fingerprint,
    should_apply_fingerprint,
)
from .utils import collapse_inline_script_splits

# Shared by all clients, including clients retained across configuration reloads.
# A thread lock protects the FIFO so separate event loops also share one slot.
_html_queue: deque[object] = deque()
_html_queue_lock = threading.Lock()


class SiteCircuitOpen(Exception):
    status = CrawlStatus.SKIPPED


class SiteWallTimeout(TimeoutError):
    """A round has expired; request retries must not consume this exception."""


BAN_RETRY_SLEEP_SECONDS = 15.0
BAN_SITE_PAUSE_SECONDS = 60.0
BAN_MAX_CONSECUTIVE_FAILURES = 3
# request() 里网络类异常 (非 HTTP 状态码) 的错误信息前缀, 用于区分「网络类失败」与「HTTP 状态耗尽」
_NETWORK_ERROR_PREFIXES = ("连接超时", "连接错误", "请求异常", "请求等待超时", "curl-cffi 异常")


@dataclass
class _SiteBanState:
    failure_count: int = 0
    pause_until: float = 0.0
    last_failure_trigger_time: float = 0.0
    circuit_open: bool = False
    last_status_code: int | None = 403


@dataclass
class CrawlRequestState:
    is_blocked: Callable[[], bool]
    request_failed: bool = False
    timeout_ctx: asyncio.Timeout | None = None
    budget: Any = None
    site_name: str = ""
    open_circuit: Callable[[str], None] | None = None
    is_ban_failure: bool = False
    ban_failure_recorded: bool = False
    abandoned_error: str = ""
    # 最近一次请求失败是网络类异常 (连接/TLS/超时/HTTP2 等), 而非 HTTP 状态码
    network_failure: bool = False


crawl_request_state: ContextVar[CrawlRequestState | None] = ContextVar("crawl_request_state", default=None)


@contextmanager
def movie_request_scope(site_name: str, state: CrawlRequestState | None = None) -> Iterator[CrawlRequestState | None]:
    """为不经过 BaseCrawler.run 的请求 (如 DMM 图片校验) 提供“一部片”的请求状态.

    已处于爬虫请求状态内时直接沿用 (同一部片共用 1 个失败记录); 否则新建或使用传入的状态,
    使该范围内的外层重试最多只向断路器记 1 次失败.
    """
    if crawl_request_state.get() is not None:
        yield crawl_request_state.get()
        return
    scope_state = state or CrawlRequestState(is_blocked=lambda: False, site_name=site_name)
    token = crawl_request_state.set(scope_state)
    try:
        yield scope_state
    finally:
        crawl_request_state.reset(token)


def extend_crawl_timeout(seconds: float) -> None:
    if seconds <= 0:
        return
    state = crawl_request_state.get()
    if state is None:
        return
    budget = getattr(state, "budget", None)
    if budget is not None:
        budget.deadline += seconds
        timeout_ctx = getattr(state, "timeout_ctx", None)
        if timeout_ctx is not None and not timeout_ctx.expired() and timeout_ctx.when() is not None:
            timeout_ctx.reschedule(budget.deadline)


@contextlib.asynccontextmanager
async def exempt_crawl_timeout():
    state = crawl_request_state.get()
    budget = getattr(state, "budget", None)
    timeout_ctx = getattr(state, "timeout_ctx", None)
    # Only the outer exemption owns the timer; nested waits must not double-count.
    if budget is None or timeout_ctx is None or timeout_ctx.expired() or timeout_ctx.when() is None:
        yield
        return
    loop = asyncio.get_running_loop()
    start_time = loop.time()
    timeout_ctx.reschedule(None)
    try:
        yield
    finally:
        budget.deadline += loop.time() - start_time
        if not timeout_ctx.expired():
            timeout_ctx.reschedule(budget.deadline)


def _check_site_circuit() -> None:
    state = crawl_request_state.get()
    budget = getattr(state, "budget", None)
    if budget is not None and asyncio.get_running_loop().time() >= budget.wall_deadline:
        raise SiteWallTimeout("单站单番号墙钟超限")
    if state is not None and state.is_blocked():
        raise SiteCircuitOpen("站点已断路，本次运行不再请求")


@contextlib.asynccontextmanager
async def _html_slot():
    ticket = object()
    with _html_queue_lock:
        _html_queue.append(ticket)
    try:
        while True:
            with _html_queue_lock:
                ready = _html_queue[0] is ticket
            if ready:
                break
            await asyncio.sleep(0.05)
        yield
    finally:
        with _html_queue_lock:
            _html_queue.remove(ticket)


class AsyncWebLimiters:
    def __init__(self):
        self.limiters: dict[str, AsyncLimiter] = {
            "127.0.0.1": AsyncLimiter(300, 1),
            "localhost": AsyncLimiter(300, 1),
        }

    def get(self, key: str, rate: float = 8, period: float = 1) -> AsyncLimiter:
        """默认对所有域名启用 8 req/s 的速率限制"""
        return self.limiters.setdefault(key, AsyncLimiter(rate, period))

    def remove(self, key: str):
        if key in self.limiters:
            del self.limiters[key]


@dataclass
class _FingerprintState:
    fingerprint: BrowserFingerprint
    created_at: float
    expires_at: float
    request_count: int
    max_requests: int


class HostConnectionPool:
    def __init__(
        self,
        *,
        key: str,
        session_factory: Callable[[BrowserFingerprint | None], AsyncSession],
        log_fn: Callable[[str], None],
        max_clients: int,
        fingerprint: BrowserFingerprint | None = None,
    ):
        self.key = key
        self._session_factory = session_factory
        self._log = log_fn
        self.fingerprint = fingerprint
        self._request_slots = asyncio.BoundedSemaphore(max(int(max_clients), 1))
        self.session = self._new_session()
        self._sessions: dict[int, AsyncSession] = {0: self.session}
        self._active_by_generation: dict[int, int] = {}
        self._retired_generations: set[int] = set()
        self._waiting_requests = 0
        self._session_lock = asyncio.Lock()
        self._closed = False
        self._generation = 0
        self.last_used_at = time.monotonic()

    def _new_session(self) -> AsyncSession:
        return self._session_factory(self.fingerprint)

    async def begin_request(self) -> tuple[AsyncSession, int]:
        waiting_registered = False
        slot_acquired = False
        async with self._session_lock:
            if self._closed:
                raise RuntimeError("网络连接池已关闭")
            self._waiting_requests += 1
            waiting_registered = True

        try:
            await self._request_slots.acquire()
            slot_acquired = True

            async with self._session_lock:
                if waiting_registered:
                    self._waiting_requests -= 1
                    waiting_registered = False
                if self._closed:
                    raise RuntimeError("网络连接池已关闭")
                generation = self._generation
                self._active_by_generation[generation] = self._active_by_generation.get(generation, 0) + 1
                self.last_used_at = time.monotonic()
                session = self.session
            return session, generation
        except BaseException:
            if slot_acquired:
                self._request_slots.release()
            if waiting_registered:
                async with self._session_lock:
                    self._waiting_requests = max(self._waiting_requests - 1, 0)
                    self.last_used_at = time.monotonic()
            raise

    async def end_request(self, generation: int) -> None:
        sessions_to_close: list[AsyncSession] = []
        async with self._session_lock:
            current_count = self._active_by_generation.get(generation, 0)
            if current_count <= 1:
                self._active_by_generation.pop(generation, None)
                if generation in self._retired_generations:
                    self._retired_generations.remove(generation)
                    old_session = self._sessions.pop(generation, None)
                    if old_session is not None:
                        sessions_to_close.append(old_session)
            else:
                self._active_by_generation[generation] = current_count - 1
            self.last_used_at = time.monotonic()
        self._request_slots.release()
        await self._close_sessions(sessions_to_close)

    async def is_idle(self) -> bool:
        async with self._session_lock:
            return not self._active_by_generation and self._waiting_requests == 0

    async def reset(self, reason: str) -> None:
        if self._closed:
            return
        sessions_to_close: list[AsyncSession] = []
        async with self._session_lock:
            if self._closed:
                return
            sessions_to_close = self._rotate_locked(reason)
        await self._close_sessions(sessions_to_close)

    def _rotate_locked(self, reason: str) -> list[AsyncSession]:
        old_generation = self._generation
        self.session = self._new_session()
        self._generation += 1
        self._sessions[self._generation] = self.session
        self.last_used_at = time.monotonic()
        old_session = self._sessions.get(old_generation)
        if old_session is None:
            return []
        if self._active_by_generation.get(old_generation, 0) > 0:
            self._retired_generations.add(old_generation)
            return []
        self._sessions.pop(old_generation, None)
        self._retired_generations.discard(old_generation)
        return [old_session]

    async def _close_sessions(self, sessions: list[AsyncSession]) -> None:
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.close()

    async def _close_all_sessions(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        self._active_by_generation.clear()
        self._retired_generations.clear()
        await self._close_sessions(sessions)

    async def close(self) -> None:
        if self._closed:
            return
        async with self._session_lock:
            if self._closed:
                return
            self._closed = True
            await self._close_all_sessions()


class HostPoolManager:
    def __init__(
        self,
        *,
        session_factory: Callable[[BrowserFingerprint | None], AsyncSession],
        log_fn: Callable[[str], None],
        max_clients: int,
        idle_ttl: float = 600.0,
    ):
        self._session_factory = session_factory
        self._log = log_fn
        self._max_clients = max(int(max_clients), 1)
        self._idle_ttl = idle_ttl
        self._pools: dict[str, HostConnectionPool] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def key_for_url(url: str, proxy: str | None = None) -> str:
        parsed = httpx.URL(url)
        scheme = (parsed.scheme or "https").lower()
        host = (parsed.host or "").lower()
        port = parsed.port
        default_port = 443 if scheme == "https" else 80
        port_part = "" if port is None or port == default_port else f":{port}"
        proxy_part = (proxy or "").strip()
        return f"{scheme}://{host}{port_part}|proxy={proxy_part}"

    @classmethod
    def key_for_request(
        cls,
        url: str,
        proxy: str | None = None,
        fingerprint: BrowserFingerprint | None = None,
    ) -> str:
        key = cls.key_for_url(url, proxy)
        if fingerprint is None:
            return key
        return f"{key}|fp={fingerprint.fingerprint_id}"

    async def get(self, key: str, *, fingerprint: BrowserFingerprint | None = None) -> HostConnectionPool:
        if self._closed:
            raise RuntimeError("网络连接池管理器已关闭")
        async with self._lock:
            if self._closed:
                raise RuntimeError("网络连接池管理器已关闭")
            await self._cleanup_idle_locked()
            pool = self._pools.get(key)
            if pool is None:
                pool = HostConnectionPool(
                    key=key,
                    session_factory=self._session_factory,
                    log_fn=self._log,
                    max_clients=self._max_clients,
                    fingerprint=fingerprint,
                )
                self._pools[key] = pool
            return pool

    async def reset(self, key: str, reason: str) -> None:
        async with self._lock:
            pool = self._pools.get(key)
            if pool is None:
                matched = [
                    each_pool for each_key, each_pool in self._pools.items() if each_key.startswith(f"{key}|fp=")
                ]
            else:
                matched = []
        if pool is not None:
            await pool.reset(reason)
        elif matched:
            await asyncio.gather(*(each_pool.reset(reason) for each_pool in matched), return_exceptions=True)

    async def reset_all(self, reason: str) -> None:
        async with self._lock:
            pools = list(self._pools.values())
        await asyncio.gather(*(pool.reset(reason) for pool in pools), return_exceptions=True)

    async def is_idle(self) -> bool:
        async with self._lock:
            pools = list(self._pools.values())
        results = await asyncio.gather(*(pool.is_idle() for pool in pools), return_exceptions=True)
        return all(result is True for result in results)

    async def close(self) -> None:
        if self._closed:
            return
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            pools = list(self._pools.values())
            self._pools.clear()
        await asyncio.gather(*(pool.close() for pool in pools), return_exceptions=True)

    async def _cleanup_idle_locked(self) -> None:
        now = time.monotonic()
        expired: list[str] = []
        for key, pool in self._pools.items():
            if now - pool.last_used_at <= self._idle_ttl:
                continue
            if await pool.is_idle():
                expired.append(key)
        for key in expired:
            pool = self._pools.pop(key, None)
            if pool is not None:
                await pool.close()


CF_MIRROR_TIMEOUT = 45.0


def is_cf_challenge_response(response: Any) -> bool:
    if response is None:
        return False
    status = getattr(response, "status_code", None)
    raw_headers = getattr(response, "headers", None) or {}
    headers = {str(k): v for k, v in raw_headers.items()}
    server = AsyncWebClient._extract_header_case_insensitive(headers, "server").lower()
    cf_ray = AsyncWebClient._extract_header_case_insensitive(headers, "cf-ray")

    content_type = AsyncWebClient._extract_header_case_insensitive(headers, "content-type").lower()
    body_text = ""
    if "text/html" in content_type or not content_type:
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            try:
                body_text = bytes(content[:8192]).decode("utf-8", errors="ignore").lower()
            except Exception:
                body_text = ""
        else:
            try:
                body_text = str(getattr(response, "text", "") or "")[:8192].lower()
            except Exception:
                body_text = ""

    challenge_markers = (
        "just a moment",
        "cf-chl",
        "cdn-cgi/challenge-platform",
        "attention required",
        "enable javascript and cookies",
        "checking your browser before accessing",
    )
    has_marker = any(marker in body_text for marker in challenge_markers)

    # 规则1: 明确 header + 挑战文案
    if status in (403, 429, 503) and ("cloudflare" in server or bool(cf_ray)) and has_marker:
        return True
    # 规则2: 挑战文案足够明确时，允许无 header 命中
    if has_marker and ("cf-chl" in body_text or "cdn-cgi/challenge-platform" in body_text):
        return True
    return False


class AsyncWebClient:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        retry: int = 3,
        timeout: float,
        cf_bypass_url: str = "",
        cf_bypass_proxy: str | None = None,
        cf_bypass_timeout: float = 90,
        site_total_timeout: float = 120,
        site_wall_timeout: float = 180,
        request_interval: float | None = None,
        site_request_intervals: Mapping[str, float] | None = None,
        log_fn: Callable[[str], None] | None = None,
        limiters: AsyncWebLimiters | None = None,
    ):
        self.retry = retry
        self.proxy = proxy
        self.timeout = timeout
        self.site_total_timeout = float(site_total_timeout)
        self.site_wall_timeout = float(site_wall_timeout)
        self.max_clients = 100
        self._session_kwargs = {
            "max_clients": self.max_clients,
            "verify": False,
            "max_redirects": 20,
            "timeout": timeout,
        }
        self._closed = False
        self._close_requested = False
        self._lease_lock = threading.Lock()
        self._leases = 0

        self.log_fn = log_fn if log_fn is not None else lambda _: None
        self.limiters = limiters if limiters is not None else AsyncWebLimiters()
        self._pool_manager = HostPoolManager(
            session_factory=self._new_curl_session,
            log_fn=self._log,
            max_clients=self.max_clients,
        )

        self.cf_bypass_url = cf_bypass_url.strip().rstrip("/")
        self.cf_bypass_proxy = (cf_bypass_proxy or "").strip()
        self._cf_bypass_enabled = bool(self.cf_bypass_url)
        self._cf_known_bypass_hosts: set[str] = set()
        self._cf_host_locks: dict[str, asyncio.Lock] = {}
        self._cf_force_refresh_locks: dict[str, asyncio.Lock] = {}
        self._cf_host_retry_semaphores: dict[str, asyncio.Semaphore] = {}
        self._cf_locks_guard = asyncio.Lock()
        self._cf_last_bypass_attempt_at: dict[str, float] = {}
        self._cf_host_challenge_hits: dict[str, int] = {}
        self._cf_bypass_min_interval = 2.0
        self._cf_site_min_intervals: dict[str, float] = {
            "javlibrary": 3.0,
        }
        # 各站最小请求间隔: None 表示未配置公共默认值 (不限制); 站点覆盖值同时作用于 bypass 冷却
        self._request_interval: float | None = None if request_interval is None else max(float(request_interval), 0.0)
        self._site_request_intervals: dict[str, float] = {
            str(k).strip().lower(): max(float(v), 0.0) for k, v in (site_request_intervals or {}).items()
        }
        self._cf_site_min_intervals.update(self._site_request_intervals)
        self._site_last_request_at: dict[str, float] = {}
        self._cf_bypass_timeout = float(cf_bypass_timeout)
        self._cf_mirror_timeout = CF_MIRROR_TIMEOUT
        self._cf_bypass_retries = 2
        self._cf_mirror_max_redirects = 8
        self._cf_request_bypass_rounds = 1
        self._cf_retry_max_concurrent_per_host = 4
        self._cf_retry_after_bypass_base_delay = 1.2
        self._cf_retry_after_bypass_jitter = 1.3
        self._retry_sleep_jitter = 0.4
        self._fingerprint_states_by_pool_base: dict[str, _FingerprintState] = {}
        self._excluded_fingerprint_by_pool_base: dict[str, str] = {}
        self._fingerprint_default_lifetime_range = (20 * 60.0, 45 * 60.0)
        self._fingerprint_default_request_range = (120, 240)
        self._fingerprint_amazon_lifetime_range = (8 * 60.0, 18 * 60.0)
        self._fingerprint_amazon_request_range = (60, 140)
        self._site_ban_states: dict[str, _SiteBanState] = {}
        self._site_ban_lock = asyncio.Lock()
        self._sleep: Callable[[float], Any] | None = None
        self._time_fn: Callable[[], float] | None = None

    def _new_curl_session(self, fingerprint: BrowserFingerprint | None = None) -> AsyncSession:
        impersonate = (
            fingerprint.impersonate
            if fingerprint is not None
            else random.choice(["chrome123", "chrome124", "chrome131", "chrome136", "firefox133", "firefox135"])
        )
        return AsyncSession(
            **self._session_kwargs,
            impersonate=impersonate,
        )

    def retain(self) -> None:
        """声明一个长生命周期使用方正在持有客户端，避免配置重载时提前关闭连接池。"""
        with self._lease_lock:
            if self._closed:
                raise RuntimeError("网络客户端已关闭")
            self._leases += 1

    async def release(self) -> None:
        """释放长生命周期使用方。若已请求关闭，则在空闲后关闭底层连接池。"""
        with self._lease_lock:
            if self._leases > 0:
                self._leases -= 1
            remaining = self._leases
        if remaining == 0:
            self._cf_known_bypass_hosts.clear()
            self._site_ban_states.clear()
        if self._close_requested:
            await self._close_if_idle()

    def _lease_count(self) -> int:
        with self._lease_lock:
            return self._leases

    def _request_timeout_seconds(self, timeout: float | httpx.Timeout | None) -> float | None:
        if timeout is None or timeout is not_set:
            return float(self.timeout) + 5.0
        if isinstance(timeout, (int, float)):
            return float(timeout) + 5.0
        return None

    async def _is_idle(self) -> bool:
        return self._lease_count() == 0 and await self._pool_manager.is_idle()

    async def _close_if_idle(self) -> bool:
        if not await self._is_idle():
            return False
        await self.close()
        return True

    async def close_when_idle(self, *, poll_interval: float = 0.2) -> None:
        """等待所有持有方与进行中的请求结束后关闭连接池。"""
        self._close_requested = True
        while not await self._is_idle():
            await asyncio.sleep(poll_interval)
        await self.close()

    async def close(self) -> None:
        """关闭底层连接池。关闭后的客户端不可继续使用。"""
        if self._closed:
            return
        self._close_requested = True
        self._closed = True
        self._cf_known_bypass_hosts.clear()
        self._site_ban_states.clear()
        await self._pool_manager.close()

    async def reset_connections(self, reason: str, *, pool_key: str | None = None) -> None:
        """重建底层连接池，用于代理/节点不稳定后丢弃可能失效的连接。"""
        if self._closed:
            return
        if pool_key is None:
            self._fingerprint_states_by_pool_base.clear()
            self._excluded_fingerprint_by_pool_base.clear()
            await self._pool_manager.reset_all(reason)
        else:
            pool_base_key, _, failed_fingerprint_id = pool_key.partition("|fp=")
            if failed_fingerprint_id:
                self._excluded_fingerprint_by_pool_base[pool_base_key] = failed_fingerprint_id
            self._fingerprint_states_by_pool_base.pop(pool_base_key, None)
            await self._pool_manager.reset(pool_key, reason)

    async def _record_transport_failure(self, error_msg: str, *, pool_key: str) -> None:
        await self.reset_connections(error_msg, pool_key=pool_key)

    async def _record_transport_success(self, *, pool_key: str) -> None:
        return

    async def _record_retryable_response_failure(self, error_msg: str, *, pool_key: str) -> None:
        await self.reset_connections(error_msg, pool_key=pool_key)

    def _get_fingerprint_for_request(
        self,
        url: str,
        proxy: str | None,
        host: str,
        purpose: RequestPurpose,
        allow_lifetime_rotation: bool = True,
    ) -> BrowserFingerprint | None:
        if not host:
            return None
        pool_base_key = HostPoolManager.key_for_url(url, proxy)
        state = self._fingerprint_states_by_pool_base.get(pool_base_key)
        now = time.monotonic()
        if state is not None and allow_lifetime_rotation and self._is_fingerprint_state_expired(state, now=now):
            self._excluded_fingerprint_by_pool_base[pool_base_key] = state.fingerprint.fingerprint_id
            self._fingerprint_states_by_pool_base.pop(pool_base_key, None)
            state = None
        if state is None:
            state = self._new_fingerprint_state(
                host,
                purpose=purpose,
                pool_base_key=pool_base_key,
                now=now,
            )
            self._fingerprint_states_by_pool_base[pool_base_key] = state
        state.request_count += 1
        return state.fingerprint

    def _is_fingerprint_state_expired(self, state: _FingerprintState, *, now: float) -> bool:
        return now >= state.expires_at or state.request_count >= state.max_requests

    def _new_fingerprint_state(
        self,
        host: str,
        *,
        purpose: RequestPurpose,
        pool_base_key: str,
        now: float,
    ) -> _FingerprintState:
        fingerprint = select_fingerprint(
            host,
            purpose=purpose,
            exclude_fingerprint_id=self._excluded_fingerprint_by_pool_base.pop(pool_base_key, ""),
        )
        lifetime_min, lifetime_max = self._fingerprint_default_lifetime_range
        request_min, request_max = self._fingerprint_default_request_range
        if host.lower().endswith("amazon.co.jp"):
            lifetime_min, lifetime_max = self._fingerprint_amazon_lifetime_range
            request_min, request_max = self._fingerprint_amazon_request_range

        lifetime = random.uniform(max(lifetime_min, 1.0), max(lifetime_max, lifetime_min, 1.0))
        max_requests = random.randint(max(int(request_min), 1), max(int(request_max), int(request_min), 1))
        return _FingerprintState(
            fingerprint=fingerprint,
            created_at=now,
            expires_at=now + lifetime,
            request_count=0,
            max_requests=max_requests,
        )

    async def _curl_request(self, *, fingerprint: BrowserFingerprint | None = None, **kwargs) -> Response:
        check_service_stop()
        _check_site_circuit()
        url = str(kwargs.get("url") or "")
        site_key = self._resolve_site_key(httpx.URL(url).host or "")
        await self._wait_for_site_pause_if_needed(site_key)
        proxy = kwargs.get("proxy")
        pool_key = HostPoolManager.key_for_request(url, str(proxy) if proxy else None, fingerprint)
        if self._closed or (self._close_requested and self._lease_count() == 0):
            raise RuntimeError("网络客户端已关闭")
        request_loop = asyncio.get_running_loop()
        pool = await self._pool_manager.get(pool_key, fingerprint=fingerprint)
        session, generation = await pool.begin_request()
        release_now = True
        try:
            await self._wait_for_site_pause_if_needed(site_key)
            _check_site_circuit()
            timeout_seconds = self._request_timeout_seconds(kwargs.get("timeout"))
            state = crawl_request_state.get()
            budget = getattr(state, "budget", None)
            if budget is not None:
                remaining = budget.wall_deadline - asyncio.get_running_loop().time()
                timeout_seconds = min(timeout_seconds, remaining) if timeout_seconds is not None else remaining
            check_service_stop()
            coro = session.request(**kwargs)
            if timeout_seconds is None:
                response = await coro
            else:
                response = await asyncio.wait_for(coro, timeout=timeout_seconds)
            check_service_stop()
            if kwargs.get("stream"):
                release_now = False
                return self._attach_stream_release(
                    response, pool=pool, generation=generation, request_loop=request_loop
                )
            return response
        except (SiteCircuitOpen, SiteWallTimeout):
            raise
        except Exception as exc:
            # A transport timeout at the hard deadline is not a new site failure.
            _check_site_circuit()
            # Log metadata only: exception strings can contain credentials or proxy addresses.
            with contextlib.suppress(Exception):
                host = urlsplit(url).hostname or "未知"
                if host == (urlsplit(self.cf_bypass_url).hostname or ""):
                    host = "bypass服务"
                self._log(
                    f"🧾 [传输诊断] 时间={datetime.now().astimezone().isoformat(timespec='seconds')} "
                    f"主机={host} 路由={'代理' if proxy else '直连'} "
                    f"错误码={getattr(exc, 'code', '未知')} 类型={type(exc).__name__}"
                )
            raise
        finally:
            if release_now:
                await pool.end_request(generation)

    def _attach_stream_release(
        self,
        response: Response,
        *,
        pool: HostConnectionPool,
        generation: int,
        request_loop: asyncio.AbstractEventLoop,
    ) -> Response:
        if getattr(response, "_mdcx_release_attached", False):
            return response

        released = False
        original_close = getattr(response, "close", None)
        original_aclose = getattr(response, "aclose", None)

        async def release_once() -> None:
            nonlocal released
            if released:
                return
            released = True
            await pool.end_request(generation)

        def close_wrapper(*args, **kwargs):
            try:
                if original_close is not None:
                    return original_close(*args, **kwargs)
                return None
            finally:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(release_once())
                except RuntimeError:
                    if not request_loop.is_closed():
                        asyncio.run_coroutine_threadsafe(release_once(), request_loop)

        async def aclose_wrapper(*args, **kwargs):
            try:
                if original_aclose is not None:
                    return await original_aclose(*args, **kwargs)
                if original_close is not None:
                    return original_close()
                return None
            finally:
                await release_once()

        try:
            response.close = close_wrapper  # type: ignore[method-assign]
            response.aclose = aclose_wrapper  # type: ignore[method-assign]
            response._mdcx_release_attached = True  # type: ignore[attr-defined]
        except Exception:
            pass
        return response

    async def _close_response(self, response: Response | None) -> None:
        if response is None:
            return
        if hasattr(response, "aclose"):
            with contextlib.suppress(Exception):
                await response.aclose()
            return
        with contextlib.suppress(Exception):
            response.close()

    def _log(self, message: str) -> None:
        try:
            self.log_fn(message)
            return
        except UnicodeEncodeError:
            pass
        except Exception:
            return

        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_message = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
        try:
            self.log_fn(safe_message)
        except Exception:
            pass

    @staticmethod
    def _diagnostic_url(url: str) -> str:
        """Keep routing evidence, excluding accounts, credentials and arbitrary query values."""
        parsed = urlsplit(str(url))
        host = parsed.hostname or ""
        path = re.sub(r"(?i)(/(?:users?|accounts?|profiles?)/)[^/]+", r"\1[已隐藏]", parsed.path)
        path = re.sub(r"[^/]+@[^/]+", "[已隐藏]", path)
        query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(parsed.query)
                if key.lower() in {"id", "cid", "content", "seasonid", "searchstr", "keyword", "locale"}
            ]
        )
        return urlunsplit((parsed.scheme, host, path, query, ""))

    def _log_dmm_response(self, response: Response, request_url: str, *, method: str, stream: bool) -> None:
        state = crawl_request_state.get()
        if state is None or state.site_name != "dmm" or stream or method.upper() != "GET":
            return
        if infer_request_purpose(request_url) != "document":
            return
        try:
            host = urlsplit(request_url).hostname or ""
            if not any(host == domain or host.endswith("." + domain) for domain in ("dmm.co.jp", "dmm.com")):
                return  # Do not expose the bypass service address from nested requests.
            final_url = str(getattr(response, "url", "") or "")
            headers = {str(key): str(value) for key, value in response.headers.items()}
            bypass_mode = self._extract_header_case_insensitive(headers, "x-mdcx-bypass-mode")
            if bypass_mode == "html":
                # /html falls back to the requested URL for parsing; that is not redirect evidence.
                final_url = self._extract_header_case_insensitive(headers, "x-cf-bypasser-final-url")
            self._log(
                f"🧾 [DMM响应诊断] 阶段={'搜索' if '/search/' in urlsplit(request_url).path else '详情'} "
                f"请求={self._diagnostic_url(request_url)} "
                f"最终URL={self._diagnostic_url(final_url) if final_url else '未记录'} "
                f"HTTP状态={response.status_code} 响应字节数={len(response.content)} 来源={bypass_mode or '普通请求'}"
            )
        except Exception:
            # Diagnostics must never change request/decoding behavior.
            pass

    def _prepare_headers(
        self,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        *,
        fingerprint: BrowserFingerprint | None = None,
        purpose: RequestPurpose = "document",
        apply_fingerprint: bool = True,
    ) -> dict[str, str]:
        """预处理请求头"""
        explicit_headers = dict(headers or {})
        site_headers: dict[str, str] = {}

        # 根据URL设置特定的Referer
        if url:
            if "getchu" in url:
                site_headers.update({"Referer": "http://www.getchu.com/top.html"})
            elif "xcity" in url:
                site_headers.update(
                    {"referer": "https://xcity.jp/result_published/?genre=%2Fresult_published%2F&q=2&sg=main&num=60"}
                )
            elif "javbus" in url:
                site_headers.update({"Referer": "https://www.javbus.com/"})
            elif "giga" in url and "cookie_set.php" not in url:
                site_headers.update({"Referer": "https://www.giga-web.jp/top.html"})

        fingerprint_headers = (
            build_fingerprint_headers(url or "", fingerprint=fingerprint, purpose=purpose)
            if apply_fingerprint and fingerprint is not None
            else None
        )
        return merge_headers(fingerprint_headers, site_headers, explicit_headers)

    async def _get_cf_host_lock(self, host: str) -> asyncio.Lock:
        async with self._cf_locks_guard:
            return self._cf_host_locks.setdefault(host, asyncio.Lock())

    async def _get_cf_force_refresh_lock(self, host: str) -> asyncio.Lock:
        async with self._cf_locks_guard:
            return self._cf_force_refresh_locks.setdefault(host, asyncio.Lock())

    async def _get_cf_host_retry_semaphore(self, host: str) -> asyncio.Semaphore:
        async with self._cf_locks_guard:
            if host not in self._cf_host_retry_semaphores:
                self._cf_host_retry_semaphores[host] = asyncio.Semaphore(
                    max(int(self._cf_retry_max_concurrent_per_host), 1)
                )
            return self._cf_host_retry_semaphores[host]

    def _calc_retry_sleep_seconds(self, attempt: int, *, after_cf_bypass: bool = False) -> float:
        if after_cf_bypass:
            base_delay = max(float(self._cf_retry_after_bypass_base_delay), 0.0)
            jitter = random.uniform(0.0, max(float(self._cf_retry_after_bypass_jitter), 0.0))
            return base_delay + jitter

        base_delay = max(float(attempt * 3 + 2), 0.0)
        jitter = random.uniform(0.0, max(float(self._retry_sleep_jitter), 0.0))
        return base_delay + jitter

    def _merge_cookies(
        self,
        cookies: dict[str, str] | None,
        bypass_cookies: dict[str, str] | None = None,
    ) -> dict[str, str] | None:
        base = dict(cookies or {})
        if bypass_cookies:
            base.update(bypass_cookies)
        return base or None

    @staticmethod
    def _extract_header_case_insensitive(headers: dict[str, Any], key: str) -> str:
        key_lower = key.lower()
        for k, v in headers.items():
            if str(k).lower() == key_lower:
                return str(v)
        return ""

    def _set_header_case_insensitive(self, headers: dict[str, str], key: str, value: str) -> None:
        key_lower = key.lower()
        for k in list(headers):
            if str(k).lower() == key_lower:
                headers.pop(k, None)
        headers[key] = value

    def _pop_header_case_insensitive(self, headers: dict[str, str], key: str) -> str:
        key_lower = key.lower()
        for k in list(headers):
            if str(k).lower() == key_lower:
                return headers.pop(k, "")
        return ""

    def _build_cookie_header(self, cookies: dict[str, str] | None) -> str:
        if not cookies:
            return ""
        pairs = [f"{str(k)}={str(v)}" for k, v in cookies.items() if k]
        return "; ".join(pairs)

    def _parse_cookie_header(self, cookie_header: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for item in (cookie_header or "").split(";"):
            part = item.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            parsed[name.strip()] = value.strip()
        return parsed

    def _merge_url_params(self, url: str, params: dict[str, Any] | list[tuple[str, Any]] | None) -> str:
        if not params:
            return url

        split_result = urlsplit(url)
        existing_items = parse_qsl(split_result.query, keep_blank_values=True)
        new_items = list(httpx.QueryParams(params).multi_items())
        merged_query = urlencode(existing_items + new_items, doseq=True)
        return urlunsplit(
            (
                split_result.scheme,
                split_result.netloc,
                split_result.path,
                merged_query,
                split_result.fragment,
            )
        )

    def _build_mirror_url(self, target_url: str) -> str:
        split_result = urlsplit(target_url)
        raw_path = split_result.path or "/"
        path = re.sub(r"/{2,}", "/", raw_path)
        mirror_url = f"{self.cf_bypass_url}{path}"
        if split_result.query:
            mirror_url = f"{mirror_url}?{split_result.query}"
        return mirror_url

    def _is_dmm_image_url(self, url: str) -> bool:
        normalized = str(url or "").strip()
        if normalized.startswith("//"):
            normalized = "https:" + normalized
        try:
            split_result = urlsplit(normalized)
        except Exception:
            return False

        host = split_result.netloc.lower()
        path = split_result.path.lower()
        if not host or not (host.endswith("dmm.co.jp") or host.endswith("dmm.com")):
            return False
        return path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"))

    def _is_redirect_response(self, response: Response) -> bool:
        if response.status_code not in (301, 302, 303, 307, 308):
            return False
        headers = {str(k): str(v) for k, v in response.headers.items()}
        return bool(self._extract_header_case_insensitive(headers, "location").strip())

    def _get_cf_bypass_min_interval(self, host: str = "") -> float:
        normalized = (host or "").lower()
        for site_key, interval in self._cf_site_min_intervals.items():
            if site_key in normalized:
                return float(interval)
        return float(self._cf_bypass_min_interval)

    def _get_site_request_interval(self, site_key: str, host: str = "") -> float:
        key = (site_key or "").strip().lower()
        if key in self._site_request_intervals:
            return self._site_request_intervals[key]
        normalized_host = (host or "").lower()
        for name, interval in self._site_request_intervals.items():
            if name in normalized_host:
                return interval
        return self._request_interval or 0.0

    async def _wait_for_site_request_interval(self, site_key: str, host: str) -> None:
        """同一站点任意两次请求之间至少间隔 N 秒 (所有线程合计), 复用 #14 的按键加锁与冷却机制."""
        interval = self._get_site_request_interval(site_key, host)
        if interval <= 0:
            return
        key = (site_key or host or "default").lower()
        lock = await self._get_cf_host_lock(f"__interval__:{key}")
        async with exempt_crawl_timeout():
            await lock.acquire()
        try:
            last = self._site_last_request_at.get(key)
            if last is not None:
                wait_seconds = interval - (self._get_time() - last)
                if wait_seconds > 0:
                    async with exempt_crawl_timeout():
                        await self._do_sleep(wait_seconds)
            self._site_last_request_at[key] = self._get_time()
        finally:
            lock.release()

    def _parse_retry_after(self, headers: dict[str, Any] | None) -> float | None:
        if not headers:
            return None
        value = self._extract_header_case_insensitive(headers, "retry-after").strip()
        if not value:
            return None
        try:
            sec = float(value)
            if sec >= 0:
                return min(sec, 30.0)
            return None
        except ValueError:
            pass
        try:
            import email.utils
            from datetime import UTC, datetime

            dt = email.utils.parsedate_to_datetime(value)
            now = datetime.now(UTC)
            sec = (dt - now).total_seconds()
            if sec >= 0:
                return min(sec, 30.0)
            return 0.0
        except Exception:
            pass
        return None

    def _is_retryable_status_code(self, status_code: int) -> bool:
        return status_code in (
            500,  # Internal Server Error
            502,  # Bad Gateway
            503,  # Service Unavailable
            403,  # Forbidden
            408,  # Request Timeout
            429,  # Too Many Requests
            504,  # Gateway Timeout
        )

    def _extract_http_status_from_bypass_error(self, error: str, *, prefix: str) -> int | None:
        if not error:
            return None
        matched = re.match(rf"^{re.escape(prefix)}\s+(\d{{3}})\b", error.strip())
        if not matched:
            return None
        try:
            return int(matched.group(1))
        except Exception:
            return None

    def _extract_terminal_bypass_status(self, error: str) -> int | None:
        if not error:
            return None

        terminal_match = re.search(r"终态 HTTP (\d{3})", error)
        if terminal_match:
            try:
                return int(terminal_match.group(1))
            except Exception:
                return None

        mirror_status = self._extract_http_status_from_bypass_error(error, prefix="mirror HTTP")
        if mirror_status is not None:
            return mirror_status

        return self._extract_http_status_from_bypass_error(error, prefix="HTTP")

    def _is_mirror_cf_challenge_error(self, error: str) -> bool:
        if not error:
            return False
        normalized = error.strip()
        return normalized.startswith("mirror 返回 Cloudflare 挑战页") or "Cloudflare 挑战页" in normalized

    def _request_has_user_cookie(
        self,
        *,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
    ) -> bool:
        """Whether this request carries a caller-supplied Cookie (never inspect/log its value)."""
        if cookies and any(str(value).strip() for value in cookies.values() if value is not None):
            return True
        return bool(self._extract_header_case_insensitive(headers or {}, "cookie").strip())

    def _is_javdb_host(self, host: str) -> bool:
        normalized = (host or "").lower()
        if "javdb" in normalized:
            return True
        state = crawl_request_state.get()
        return bool(state and str(getattr(state, "site_name", "")).lower() == "javdb")

    def _html_fallback_drops_javdb_cookie(
        self,
        *,
        host: str,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
    ) -> bool:
        """The bypass /html endpoint has no Cookie parameter; never probe javdb without its session."""
        return self._is_javdb_host(host) and self._request_has_user_cookie(headers=headers, cookies=cookies)

    def _bind_response_effective_url(self, response: Response, final_url: str) -> None:
        normalized = (final_url or "").strip()
        if not normalized:
            return
        try:
            response.url = normalized
        except Exception:
            pass
        try:
            response.headers["x-mdcx-final-url"] = normalized
        except Exception:
            pass

    def _resolve_cf_bypass_proxy(self, *, use_proxy: bool) -> str:
        if not use_proxy:
            return ""
        return (self.cf_bypass_proxy or "").strip()

    def _prepare_mirror_headers(
        self,
        *,
        headers: dict[str, str] | None,
        target_host: str,
        cookies: dict[str, str] | None,
        use_proxy: bool,
        bypass_cache: bool = False,
    ) -> dict[str, str]:
        mirror_headers = dict(headers or {})
        self._pop_header_case_insensitive(mirror_headers, "host")
        self._set_header_case_insensitive(mirror_headers, "x-hostname", target_host)
        self._pop_header_case_insensitive(mirror_headers, "x-proxy")
        self._pop_header_case_insensitive(mirror_headers, "x-bypass-cache")
        bypass_proxy = self._resolve_cf_bypass_proxy(use_proxy=use_proxy)
        if bypass_proxy:
            self._set_header_case_insensitive(mirror_headers, "x-proxy", bypass_proxy)
        if bypass_cache:
            self._set_header_case_insensitive(mirror_headers, "x-bypass-cache", "true")

        header_cookie_map = self._parse_cookie_header(self._extract_header_case_insensitive(mirror_headers, "cookie"))
        merged_cookie_map = dict(cookies or {})
        merged_cookie_map.update(header_cookie_map)
        merged_cookie_header = self._build_cookie_header(merged_cookie_map)
        if merged_cookie_header:
            self._set_header_case_insensitive(mirror_headers, "Cookie", merged_cookie_header)
        elif header_cookie_map:
            self._set_header_case_insensitive(mirror_headers, "Cookie", self._build_cookie_header(header_cookie_map))
        else:
            self._pop_header_case_insensitive(mirror_headers, "cookie")

        return mirror_headers

    def _sanitize_url(self, url: str) -> tuple[str, bool]:
        cleaned = (url or "").strip()
        if not cleaned:
            return cleaned, False
        candidates: list[str] = []
        collapsed = collapse_inline_script_splits(cleaned).strip()
        if collapsed:
            candidates.append(collapsed)
        if cleaned not in candidates:
            candidates.append(cleaned)

        # 过滤类似 https://x.com?a=1">https://x.com?a=1 这类污染字符串，也兼容 Next.js 流式脚本分片插入。
        # 允许保留空格，随后交给 URL 解析器做编码，避免查询参数在空格处被截断。
        for source in candidates:
            source_matches: list[str] = []
            if match := re.match(r"^(https?://[^\"'<>]+)", source):
                source_matches.append(match.group(1).strip())
            if not source_matches:
                source_matches.extend(match.group(0).strip() for match in re.finditer(r"https?://[^\s\"'<>]+", source))

            for normalized in source_matches:
                try:
                    parsed = httpx.URL(normalized)
                except Exception:
                    continue
                if not parsed.host:
                    continue
                normalized = str(parsed)
                return normalized, normalized != cleaned

        return cleaned, False

    def _log_cf(self, message: str, host: str = "") -> None:
        host_prefix = f"{host} " if host else ""
        self._log(f"🛡️ [CF] {host_prefix}{message}")

    def _is_cf_challenge_response(self, response: Response) -> bool:
        return is_cf_challenge_response(response)

    def _is_cf_1015_response(self, response: Response | None) -> bool:
        if response is None:
            return False
        if response.status_code not in (403, 429):
            return False
        try:
            content = response.content[:8192].decode("utf-8", errors="ignore").lower()
        except Exception:
            return False
        markers = (
            "error 1015",
            "error code: 1015",
            "error code 1015",
            "you are being rate limited",
        )
        return any(m in content for m in markers)

    def _is_ban_response(self, response: Response | None) -> bool:
        if response is None:
            return False
        if self._is_cf_challenge_response(response):
            return False
        if response.status_code == 403:
            return True
        if self._is_cf_1015_response(response):
            return True
        return False

    def _resolve_site_key(self, host: str) -> str:
        state = crawl_request_state.get()
        if state and state.site_name:
            return state.site_name
        normalized = (host or "").lower()
        if normalized.endswith(("dmm.co.jp", "dmm.com")):
            return "dmm"
        return host or "default"

    def _is_site_circuit_open(self, site_key: str) -> bool:
        state = crawl_request_state.get()
        if state is not None and state.is_blocked():
            return True
        ban_state = self._site_ban_states.get(site_key)
        if ban_state and ban_state.circuit_open:
            return True
        return False

    def _get_time(self) -> float:
        if self._time_fn is not None:
            return self._time_fn()
        return time.time()

    async def _do_sleep(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
        else:
            await asyncio.sleep(seconds)

    async def _wait_for_site_pause_if_needed(self, site_key: str) -> None:
        while True:
            if self._is_site_circuit_open(site_key):
                raise SiteCircuitOpen(f"站点 {site_key} 已断路，本次运行不再请求")
            ban_state = self._site_ban_states.get(site_key)
            if not ban_state:
                break
            now = self._get_time()
            wait_seconds = ban_state.pause_until - now
            if wait_seconds <= 0:
                break
            async with exempt_crawl_timeout():
                await self._do_sleep(wait_seconds)

    async def _record_site_success(self, site_key: str) -> None:
        async with self._site_ban_lock:
            req_state = crawl_request_state.get()
            if req_state is not None:
                req_state.ban_failure_recorded = False
            state = self._site_ban_states.get(site_key)
            if state and state.failure_count > 0:
                state.failure_count = 0
                self._log(f"✅ [封禁] {site_key} 恢复正常，失败计数清零")

    async def _record_site_ban_failure(
        self, site_key: str, *, status_code: int | None, request_start_time: float, network: bool = False
    ) -> None:
        async with self._site_ban_lock:
            state = self._site_ban_states.setdefault(site_key, _SiteBanState())
            state.last_status_code = status_code
            if state.circuit_open:
                return
            req_state = crawl_request_state.get()
            # 同一部片 (同一 CrawlRequestState) 无论外层重试多少次, 最多记 1 次失败
            if req_state is not None and req_state.ban_failure_recorded:
                return
            now = self._get_time()

            if now < state.pause_until or request_start_time <= state.last_failure_trigger_time:
                if req_state is not None:
                    req_state.ban_failure_recorded = True
                return
            if req_state is not None:
                req_state.ban_failure_recorded = True

            state.failure_count += 1
            state.last_failure_trigger_time = now
            state.pause_until = now + BAN_SITE_PAUSE_SECONDS

            failure = "被拒" if status_code is not None else ("网络错误" if network else "CF/bypass 失败")
            self._log(
                f"⏸️ [封禁] {site_key} 第 {state.failure_count}/{int(BAN_MAX_CONSECUTIVE_FAILURES)} 次{failure}，暂停该站 {int(BAN_SITE_PAUSE_SECONDS)}s"
            )

            if state.failure_count >= BAN_MAX_CONSECUTIVE_FAILURES:
                state.circuit_open = True
                req_state = crawl_request_state.get()
                reason = f"连续 {int(BAN_MAX_CONSECUTIVE_FAILURES)} 部片被拒绝 ({status_code})，可能已被限制访问"
                if status_code is None:
                    reason = f"连续 {int(BAN_MAX_CONSECUTIVE_FAILURES)} 部片请求失败（CF/bypass 或访问受限）"
                    if network:
                        reason = f"连续 {int(BAN_MAX_CONSECUTIVE_FAILURES)} 部片网络请求失败（连接、超时或重试耗尽）"
                if req_state and req_state.open_circuit:
                    req_state.open_circuit(reason)
                else:
                    self._log(f"🚫 [断路器] {site_key} 本次运行停用：{reason}")

    async def _handle_ban_action(
        self,
        *,
        host: str,
        site_key: str,
        status_code: int,
        is_1015: bool,
        ban_retried: bool,
        request_start_time: float,
        responses_to_close: list[Response | None] = (),
        stream: bool = False,
    ) -> bool:
        status_text = f"{status_code} (1015)" if is_1015 else f"{status_code}"
        if not ban_retried:
            self._log(f"⚠️ [封禁] {host} 返回 {status_text}，等待 {int(BAN_RETRY_SLEEP_SECONDS)}s 后重试 1 次")
            if stream:
                for r in responses_to_close:
                    if r is not None:
                        await self._close_response(r)
            async with exempt_crawl_timeout():
                await self._do_sleep(BAN_RETRY_SLEEP_SECONDS)
            if self._is_site_circuit_open(site_key):
                raise SiteCircuitOpen(f"站点 {site_key} 已断路，本次运行不再请求")
            await self._wait_for_site_pause_if_needed(site_key)
            return True
        else:
            self._log(f"⚠️ [封禁] {host} 重试返回 {status_text}")
            req_state = crawl_request_state.get()
            if req_state:
                req_state.is_ban_failure = True
            await self._record_site_ban_failure(
                site_key,
                status_code=status_code,
                request_start_time=request_start_time,
            )
            if stream:
                for r in responses_to_close:
                    if r is not None:
                        await self._close_response(r)
            return False

    async def _call_bypass_mirror(
        self,
        *,
        method: HttpMethod,
        target_url: str,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        use_proxy: bool,
        bypass_cache: bool = False,
        data: dict[str, str] | list[tuple] | str | BytesIO | bytes | None = None,
        json_data: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
        allow_redirects: bool = True,
    ) -> tuple[Response | None, str]:
        if not self._cf_bypass_enabled:
            return None, "未配置 bypass 地址"

        current_url = target_url
        current_method = str(method).upper()
        current_data = data
        current_json_data = json_data

        for redirect_index in range(self._cf_mirror_max_redirects + 1):
            try:
                target = httpx.URL(current_url)
                target_host = target.host or ""
            except Exception as exc:
                return None, f"mirror 目标 URL 解析失败: {exc}"

            if not target_host:
                return None, "mirror 目标 URL 缺少 host"

            if redirect_index == 0 and self._resolve_cf_bypass_proxy(use_proxy=use_proxy):
                self._log_cf("🌐 mirror bypass 将使用独立代理", target_host)
            if redirect_index == 0 and bypass_cache:
                self._log_cf("♻️ mirror bypass 将强制刷新 cookies", target_host)

            mirror_url = self._build_mirror_url(current_url)
            mirror_headers = self._prepare_mirror_headers(
                headers=headers,
                target_host=target_host,
                cookies=cookies,
                use_proxy=use_proxy,
                bypass_cache=bypass_cache,
            )
            mirror_pool_key = HostPoolManager.key_for_url(mirror_url, None)
            try:
                limiter = self.limiters.get("127.0.0.1")
                await limiter.acquire()
                response = await self._curl_request(
                    method=current_method,
                    url=mirror_url,
                    proxy=None,
                    headers=mirror_headers,
                    data=current_data,
                    json=current_json_data,
                    # Preserve the pre-existing mirror timeout; only /html changes.
                    timeout=timeout or self._cf_mirror_timeout,
                    stream=False,
                    allow_redirects=False,
                )
                error = ""
            except (SiteCircuitOpen, SiteWallTimeout):
                raise
            except Timeout:
                response = None
                error = "mirror 请求超时"
                await self._record_transport_failure(error, pool_key=mirror_pool_key)
            except ConnectionError as exc:
                response = None
                error = f"mirror 连接错误: {exc}"
                await self._record_transport_failure(error, pool_key=mirror_pool_key)
            except RequestException as exc:
                response = None
                error = f"mirror 请求异常: {exc}"
                await self._record_transport_failure(error, pool_key=mirror_pool_key)
            except TimeoutError:
                response = None
                error = "mirror 请求等待超时"
                await self._record_transport_failure(error, pool_key=mirror_pool_key)
            except Exception as exc:
                response = None
                error = f"mirror 未知错误: {exc}"
                await self._record_transport_failure(error, pool_key=mirror_pool_key)
            if response is None:
                return None, error

            self._bind_response_effective_url(response, current_url)
            response.headers["x-mdcx-bypass-mode"] = "mirror"

            if response.status_code == 429:
                max_429_retries = 2
                for retry_k in range(1, max_429_retries + 1):
                    headers_map = {str(k): str(v) for k, v in response.headers.items()}
                    retry_after = self._parse_retry_after(headers_map)
                    if retry_after is not None:
                        wait_seconds = retry_after
                    else:
                        wait_seconds = 5.0 if retry_k == 1 else 10.0
                    wait_seconds = min(wait_seconds, getattr(self, "site_total_timeout", 120.0))
                    site_name = "javlibrary" if "javlibrary" in target_host.lower() else (target_host or "目标站点")
                    self._log(
                        f"⏳ [限流] {site_name} 返回 429，等待 {wait_seconds:g}s 后重试 ({retry_k}/{max_429_retries})"
                    )
                    async with exempt_crawl_timeout():
                        await asyncio.sleep(wait_seconds)

                    try:
                        limiter = self.limiters.get("127.0.0.1")
                        async with exempt_crawl_timeout():
                            await limiter.acquire()
                        response = await self._curl_request(
                            method=current_method,
                            url=mirror_url,
                            proxy=None,
                            headers=mirror_headers,
                            data=current_data,
                            json=current_json_data,
                            timeout=timeout or self._cf_mirror_timeout,
                            stream=False,
                            allow_redirects=False,
                        )
                    except (SiteCircuitOpen, SiteWallTimeout):
                        raise
                    except Timeout:
                        response = None
                        error = "mirror 请求超时"
                        await self._record_transport_failure(error, pool_key=mirror_pool_key)
                        break
                    except ConnectionError as exc:
                        response = None
                        error = f"mirror 连接错误: {exc}"
                        await self._record_transport_failure(error, pool_key=mirror_pool_key)
                        break
                    except RequestException as exc:
                        response = None
                        error = f"mirror 请求异常: {exc}"
                        await self._record_transport_failure(error, pool_key=mirror_pool_key)
                        break
                    except TimeoutError:
                        response = None
                        error = "mirror 请求等待超时"
                        await self._record_transport_failure(error, pool_key=mirror_pool_key)
                        break
                    except Exception as exc:
                        response = None
                        error = f"mirror 未知错误: {exc}"
                        await self._record_transport_failure(error, pool_key=mirror_pool_key)
                        break

                    if response is None:
                        break
                    self._bind_response_effective_url(response, current_url)
                    response.headers["x-mdcx-bypass-mode"] = "mirror"
                    if response.status_code != 429:
                        break

            if response is None:
                return None, error

            if self._is_cf_challenge_response(response):
                return None, "mirror 返回 Cloudflare 挑战页"

            if response.status_code >= 400:
                if 400 <= response.status_code < 500:
                    # Only known service validation messages are safe to report.
                    # Never log arbitrary upstream bodies (which may contain credentials).
                    try:
                        payload = json.loads(response.content)
                        detail = payload.get("detail") if isinstance(payload, dict) else None
                    except (ValueError, TypeError, UnicodeError):
                        detail = None
                    if detail in (
                        "Invalid or unsafe hostname - localhost and private IPs are not allowed",
                        "x-hostname header is required for request mirroring",
                        "x-proxy must start with http://, https://, socks4://, or socks5://",
                    ):
                        return None, (
                            f"mirror 服务拒绝（HTTP {response.status_code}）: {detail}；"
                            "请检查 bypass 部署的 DNS、真实公网解析及请求头"
                        )
                return None, f"mirror HTTP {response.status_code}"

            if not allow_redirects or not self._is_redirect_response(response):
                return response, ""

            response_headers = {str(k): str(v) for k, v in response.headers.items()}
            location = self._extract_header_case_insensitive(response_headers, "location").strip()
            if not location:
                return response, ""

            next_url = urljoin(current_url, location)
            if not next_url:
                return None, "mirror 重定向 Location 为空"
            self._log_cf(f"➡️ mirror 跟随重定向: {current_url} -> {next_url}", target_host)

            if current_method not in ("GET", "HEAD") and response.status_code in (301, 302, 303):
                current_method = "GET"
                current_data = None
                current_json_data = None

            current_url = next_url
            if redirect_index >= self._cf_mirror_max_redirects:
                break

        return None, f"mirror 重定向超过上限 ({self._cf_mirror_max_redirects})"

    async def _call_bypass_html(
        self,
        target_url: str,
        *,
        use_proxy: bool,
        bypass_cache: bool = False,
    ) -> tuple[Response | None, str]:
        if not self._cf_bypass_enabled:
            return None, "未配置 bypass 地址"

        params: dict[str, Any] = {"url": target_url}
        bypass_proxy = self._resolve_cf_bypass_proxy(use_proxy=use_proxy)
        if bypass_proxy:
            params["proxy"] = bypass_proxy
            self._log_cf("🌐 /html bypass 将使用独立代理")
        if bypass_cache:
            params["bypassCookieCache"] = "true"
            self._log_cf("♻️ /html bypass 将强制刷新 cookies")

        host = urlsplit(target_url).hostname or ""
        queued_at = time.monotonic()
        async with _html_slot():
            self._log_cf(f"/html 开始，在途=1，排队={time.monotonic() - queued_at:.2f}s", host)
            try:
                async with asyncio.timeout(self._cf_bypass_timeout):
                    response, error = await self.request(
                        "GET",
                        f"{self.cf_bypass_url}/html",
                        use_proxy=False,
                        allow_redirects=True,
                        timeout=self._cf_bypass_timeout,
                        params=params,
                        enable_cf_bypass=False,
                        retry_count=1,
                    )
            except SiteWallTimeout:
                raise
            except TimeoutError:
                response, error = None, f"/html 等待超时 ({self._cf_bypass_timeout:g}s)"
            finally:
                self._log_cf("/html 结束，在途=0", host)

        if response is None:
            return None, error

        if response.status_code >= 400:
            return None, f"HTTP {response.status_code}"

        if not response.content:
            return None, "bypass 返回空 HTML"

        response_headers = {str(k): str(v) for k, v in response.headers.items()}
        final_url = (
            self._extract_header_case_insensitive(response_headers, "x-cf-bypasser-final-url").strip() or target_url
        )
        self._bind_response_effective_url(response, final_url)
        response.headers["x-mdcx-bypass-mode"] = "html"
        return response, ""

    async def _try_bypass_cloudflare(self, **kwargs) -> tuple[Response | None, str]:
        check_service_stop()
        batch = batch_diagnostics.get()
        outcomes: dict[str, bool] = {}
        try:
            result = await self._try_bypass_cloudflare_observed(**kwargs, service_outcomes=outcomes)
        except asyncio.CancelledError:
            if batch is not None:
                state = crawl_request_state.get()
                budget = getattr(state, "budget", None)
                wall_expired = budget is not None and asyncio.get_running_loop().time() >= budget.wall_deadline
                batch.record_bypass(kwargs["host"], "wall_timeout" if wall_expired else "cancelled")
                if wall_expired:
                    for category, success in outcomes.items():
                        batch.record_service(category, success)
                    check_service_stop()
            raise
        except (SiteCircuitOpen, SiteWallTimeout) as error:
            if batch is not None:
                batch.record_bypass(kwargs["host"], "skipped" if isinstance(error, SiteCircuitOpen) else "wall_timeout")
                if isinstance(error, SiteWallTimeout):
                    for category, success in outcomes.items():
                        batch.record_service(category, success)
                    check_service_stop()
            raise
        if batch is not None:
            batch.record_bypass(kwargs["host"], "success" if result[0] is not None else "failure")
            for category, success in outcomes.items():
                batch.record_service(category, success)
            check_service_stop()
        return result

    async def _try_bypass_cloudflare_observed(
        self,
        *,
        host: str,
        method: HttpMethod,
        target_url: str,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        data: dict[str, str] | list[tuple] | str | BytesIO | bytes | None,
        json_data: dict[str, Any] | None,
        timeout: float | httpx.Timeout | None,
        allow_redirects: bool,
        use_proxy: bool,
        service_outcomes: dict[str, bool] | None = None,
    ) -> tuple[Response | None, str]:
        if service_outcomes is None:
            service_outcomes = {}
        lock = await self._get_cf_host_lock(host)
        async with exempt_crawl_timeout():
            await lock.acquire()
        try:
            min_interval = self._get_cf_bypass_min_interval(host)
            while True:
                now = time.monotonic()
                last_attempt = self._cf_last_bypass_attempt_at.get(host, 0.0)
                if last_attempt <= 0:
                    break

                elapsed = now - last_attempt
                if elapsed >= min_interval:
                    break

                wait_seconds = min_interval - elapsed
                if wait_seconds >= 0.2:
                    self._log_cf(f"🕒 bypass 冷却中 {wait_seconds:.2f}s，等待后继续", host)
                async with exempt_crawl_timeout():
                    await asyncio.sleep(wait_seconds)

            self._cf_last_bypass_attempt_at[host] = time.monotonic()
            error = ""
            for i in range(self._cf_bypass_retries):
                if i == 0:
                    self._log_cf(f"🔐 尝试 mirror bypass: {target_url}", host)
                else:
                    self._log_cf(f"🔁 mirror bypass 重试 ({i + 1}/{self._cf_bypass_retries})", host)
                force_bypass_cache = i > 0

                can_retry = True
                html_bypass_cache = force_bypass_cache
                service_outcomes["mirror"] = False
                bypass_response, mirror_error = await self._call_bypass_mirror(
                    method=method,
                    target_url=target_url,
                    headers=headers,
                    cookies=cookies,
                    use_proxy=use_proxy,
                    bypass_cache=force_bypass_cache,
                    data=data,
                    json_data=json_data,
                    timeout=timeout,
                    allow_redirects=allow_redirects,
                )
                if bypass_response is not None:
                    service_outcomes["mirror"] = True
                    self._cf_host_challenge_hits[host] = 0
                    return bypass_response, ""

                if self._is_mirror_cf_challenge_error(mirror_error) and not force_bypass_cache:
                    refresh_lock = await self._get_cf_force_refresh_lock(host)
                    async with refresh_lock:
                        self._log_cf("♻️ mirror 命中挑战页，判定缓存可能失效，强制刷新后重试 mirror", host)
                        bypass_response, mirror_error = await self._call_bypass_mirror(
                            method=method,
                            target_url=target_url,
                            headers=headers,
                            cookies=cookies,
                            use_proxy=use_proxy,
                            bypass_cache=True,
                            data=data,
                            json_data=json_data,
                            timeout=timeout,
                            allow_redirects=allow_redirects,
                        )
                    if bypass_response is not None:
                        service_outcomes["mirror"] = True
                        self._cf_host_challenge_hits[host] = 0
                        return bypass_response, ""
                    html_bypass_cache = False

                mirror_status = self._extract_http_status_from_bypass_error(mirror_error, prefix="mirror HTTP")
                skip_html_fallback = mirror_status == 429 or (
                    mirror_status is not None and not self._is_retryable_status_code(mirror_status)
                )
                # A target's terminal 4xx is a site result, not service unavailability.
                if mirror_status is None or mirror_status >= 500:
                    service_outcomes["mirror"] = False
                else:
                    service_outcomes.pop("mirror", None)
                if skip_html_fallback:
                    if mirror_status == 429:
                        error = "mirror 返回终态 HTTP 429，跳过 /html 回退"
                    else:
                        error = f"mirror 返回终态 HTTP {mirror_status}，跳过 /html 回退"
                    can_retry = False
                    self._log_cf(f"⚠️ {error}", host)
                elif self._html_fallback_drops_javdb_cookie(
                    host=host,
                    headers=headers,
                    cookies=cookies,
                ):
                    error = (
                        f"mirror: {mirror_error}; javdb 携带用户 Cookie，"
                        "/html 回退不支持传 Cookie，已跳过以免发出无 Cookie 请求"
                    )
                    can_retry = False
                    self._log_cf("⚠️ javdb 携带用户 Cookie，/html 回退不能传 Cookie，跳过", host)
                elif str(method).upper() == "GET":
                    if html_bypass_cache:
                        self._log_cf(f"↩️ mirror 失败（强刷已启用），回退 /html: {mirror_error}", host)
                    else:
                        self._log_cf(f"↩️ mirror 失败，回退 /html: {mirror_error}", host)
                    service_outcomes["/html"] = False
                    bypass_response, html_error = await self._call_bypass_html(
                        target_url, use_proxy=use_proxy, bypass_cache=html_bypass_cache
                    )
                    html_status = self._extract_http_status_from_bypass_error(html_error, prefix="HTTP")
                    if bypass_response is not None or html_status is None or html_status >= 500:
                        service_outcomes["/html"] = bypass_response is not None
                    else:
                        service_outcomes.pop("/html", None)
                    if bypass_response is not None:
                        self._cf_host_challenge_hits[host] = 0
                        bypass_headers = {str(k): str(v) for k, v in bypass_response.headers.items()}
                        final_url = self._extract_header_case_insensitive(bypass_headers, "x-cf-bypasser-final-url")
                        if final_url and final_url.strip() and final_url.strip() != target_url:
                            self._log_cf(f"🌐 /html 最终地址: {final_url}", host)
                        return bypass_response, ""
                    error = f"mirror: {mirror_error}; html: {html_error}"
                    # One browser launch only: no retry or forced-cookie second round.
                    can_retry = False
                else:
                    error = f"mirror 失败且 {str(method).upper()} 不支持 /html 兜底: {mirror_error}"
                    if mirror_status is not None and not self._is_retryable_status_code(mirror_status):
                        can_retry = False

                if not can_retry:
                    break

                if i < self._cf_bypass_retries - 1:
                    sleep_seconds = self._calc_retry_sleep_seconds(i, after_cf_bypass=True)
                    self._log_cf(f"⚠️ bypass 获取失败，{sleep_seconds:.2f}s 后重试: {error}", host)
                    async with exempt_crawl_timeout():
                        await asyncio.sleep(sleep_seconds)

            return None, error or "bypass HTML 获取失败"
        finally:
            lock.release()

    @observe_http(cache=False)
    async def request(
        self,
        method: HttpMethod,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        use_proxy: bool = True,
        data: dict[str, str] | list[tuple] | str | BytesIO | bytes | None = None,
        json_data: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
        stream: bool = False,
        allow_redirects: bool = True,
        enable_cf_bypass: bool = True,
        retry_count: int | None = None,
    ) -> tuple[Response | None, str]:
        """
        执行请求的通用方法

        Args:
            url: 请求URL
            headers: 请求头
            cookies: cookies
            use_proxy: 是否使用代理
            data: 表单数据
            json_data: JSON数据
            timeout: 请求超时时间, 覆盖客户端默认值

        Returns:
            tuple[Optional[Response], str]: (响应对象, 错误信息)
        """
        try:
            check_service_stop()
            req_state = crawl_request_state.get()
            if req_state is not None and req_state.abandoned_error:
                return None, req_state.abandoned_error
            original_url = url
            url, sanitized = self._sanitize_url(url)
            if sanitized:
                self._log(f"⚠️ 检测到异常 URL，已清理: {original_url} -> {url}")

            u = httpx.URL(url)
            host = u.host or ""
            request_proxy = self.proxy if use_proxy else None
            purpose = infer_request_purpose(
                url,
                method=str(method),
                headers=dict(headers or {}),
                stream=stream,
                json_data=json_data,
            )
            apply_fingerprint = should_apply_fingerprint(
                url,
                cf_bypass_url=self.cf_bypass_url,
            )
            site_key = self._resolve_site_key(host)
            if self._is_site_circuit_open(site_key):
                raise SiteCircuitOpen(f"站点 {site_key} 已断路，本次运行不再请求")

            await self._wait_for_site_pause_if_needed(site_key)
            await self._wait_for_site_request_interval(site_key, host)

            request_start_time = self._get_time()

            retry_count = max(int(self.retry if retry_count is None else retry_count), 1)
            error_msg = ""
            bypass_round = 0
            bypass_failed = False
            proxy_transport_failure = False
            allow_lifetime_rotation = purpose != "download"
            ban_retried = False

            for attempt in range(max(retry_count, 2) + 1):
                check_service_stop()
                proxy_transport_failure = False
                # 增强的重试策略: 对网络错误和特定状态码都进行重试
                retry = False
                should_sleep_before_retry = True
                sleep_after_cf_bypass = False
                resp: Response | None = None
                fingerprint = (
                    self._get_fingerprint_for_request(
                        url,
                        request_proxy,
                        host,
                        purpose,
                        allow_lifetime_rotation=allow_lifetime_rotation,
                    )
                    if apply_fingerprint and host
                    else None
                )
                prepared_headers = self._prepare_headers(
                    url,
                    dict(headers or {}),
                    fingerprint=fingerprint,
                    purpose=purpose,
                    apply_fingerprint=apply_fingerprint,
                )
                pool_key = HostPoolManager.key_for_request(url, request_proxy, fingerprint)
                try:
                    _check_site_circuit()
                    if self._is_site_circuit_open(site_key):
                        raise SiteCircuitOpen(f"站点 {site_key} 已断路，本次运行不再请求")
                    limiter = self.limiters.get(host or "default")
                    async with exempt_crawl_timeout():
                        await limiter.acquire()
                    await self._wait_for_site_pause_if_needed(site_key)
                    req_headers = dict(prepared_headers)
                    req_cookies = self._merge_cookies(cookies)

                    direct_bypass = bool(
                        enable_cf_bypass
                        and self._cf_bypass_enabled
                        and host
                        and (host in self._cf_known_bypass_hosts or (ban_retried and bypass_round > 0))
                    )

                    if direct_bypass:
                        bypass_needed = True
                    else:
                        host_retry_semaphore = None
                        if host and self._cf_host_challenge_hits.get(host, 0) > 0:
                            host_retry_semaphore = await self._get_cf_host_retry_semaphore(host)
                        if host_retry_semaphore is not None:
                            async with host_retry_semaphore:
                                resp = await self._curl_request(
                                    method=method,
                                    url=url,
                                    proxy=request_proxy,
                                    fingerprint=fingerprint,
                                    headers=req_headers,
                                    cookies=req_cookies,
                                    params=params,
                                    data=data,
                                    json=json_data,
                                    timeout=timeout or not_set,
                                    stream=stream,
                                    allow_redirects=allow_redirects,
                                )
                        else:
                            resp = await self._curl_request(
                                method=method,
                                url=url,
                                proxy=request_proxy,
                                fingerprint=fingerprint,
                                headers=req_headers,
                                cookies=req_cookies,
                                params=params,
                                data=data,
                                json=json_data,
                                timeout=timeout or not_set,
                                stream=stream,
                                allow_redirects=allow_redirects,
                            )

                        self._log_dmm_response(resp, url, method=str(method), stream=stream)
                        # Any HTTP response proves this proxied connection worked,
                        # including target-site errors and Cloudflare challenges.
                        if request_proxy and (batch := batch_diagnostics.get()) is not None:
                            batch.record_service("代理", True)
                        bypass_needed = bool(
                            enable_cf_bypass
                            and self._cf_bypass_enabled
                            and host
                            and self._is_cf_challenge_response(resp)
                        )
                        if bypass_needed:
                            self._log_cf(f"🛑 检测到 Cloudflare 挑战页: {method} {url}", host)
                            self._cf_host_challenge_hits[host] = self._cf_host_challenge_hits.get(host, 0) + 1

                    if bypass_needed:
                        max_bypass_rounds = self._cf_request_bypass_rounds + (1 if ban_retried else 0)
                        if bypass_round >= max_bypass_rounds:
                            bypass_failed = True
                            error_msg = f"Cloudflare 挑战页持续存在，bypass 已达上限 ({max_bypass_rounds})"
                            retry = False
                            self._log_cf(f"🚫 {error_msg}", host)
                        else:
                            target_url = self._merge_url_params(url, params)
                            bypass_response, bypass_error = await self._try_bypass_cloudflare(
                                host=host,
                                method=method,
                                target_url=target_url,
                                headers=req_headers,
                                cookies=req_cookies,
                                data=data,
                                json_data=json_data,
                                timeout=self._cf_mirror_timeout,
                                allow_redirects=allow_redirects,
                                use_proxy=bool((self.cf_bypass_proxy or "").strip()),
                            )
                            bypass_round += 1

                            if bypass_response is not None:
                                self._log_dmm_response(bypass_response, url, method=str(method), stream=stream)
                                bypass_mode = self._extract_header_case_insensitive(
                                    {str(k): str(v) for k, v in bypass_response.headers.items()},
                                    "x-mdcx-bypass-mode",
                                )
                                is_1015 = self._is_cf_1015_response(bypass_response)
                                if bypass_response.status_code == 403 or is_1015:
                                    status_text = (
                                        f"{bypass_response.status_code} (1015)"
                                        if is_1015
                                        else f"{bypass_response.status_code}"
                                    )
                                    error_msg = f"HTTP {status_text}"
                                    should_retry = await self._handle_ban_action(
                                        host=host,
                                        site_key=site_key,
                                        status_code=bypass_response.status_code,
                                        is_1015=is_1015,
                                        ban_retried=ban_retried,
                                        request_start_time=request_start_time,
                                        responses_to_close=[bypass_response, resp],
                                        stream=stream,
                                    )
                                    if should_retry:
                                        ban_retried = True
                                        continue
                                    else:
                                        break
                                elif bypass_response.status_code >= 300 and not (
                                    bypass_response.status_code == 302
                                    and self._extract_header_case_insensitive(
                                        {str(k): str(v) for k, v in bypass_response.headers.items()}, "location"
                                    )
                                ):
                                    error_msg = (
                                        f"HTTP {bypass_response.status_code} (bypass:{bypass_mode or 'unknown'})"
                                    )
                                    retry = attempt < retry_count - 1 and self._is_retryable_status_code(
                                        bypass_response.status_code
                                    )
                                    self._log_cf(
                                        f"⚠️ bypass 返回非成功状态: {error_msg}，将{'重试' if retry else '停止重试'}",
                                        host,
                                    )
                                else:
                                    if host and host not in self._cf_known_bypass_hosts:
                                        self._cf_known_bypass_hosts.add(host)
                                        self._log(f"🛡️ [CF] {host} 已确认需要 bypass，本次运行后续请求直接走 bypass")
                                    self._log_cf(
                                        f"✅ bypass 成功（模式: {bypass_mode or 'unknown'}），直接使用 bypass 响应",
                                        host,
                                    )
                                    if stream:
                                        await self._close_response(resp)
                                    await self._record_site_success(site_key)
                                    return bypass_response, ""
                            else:
                                error_msg = f"Cloudflare 挑战页且 bypass 失败: {bypass_error}"
                                terminal_status = self._extract_terminal_bypass_status(bypass_error)
                                # Missing pages and ordinary rate limits retain their existing policy.
                                bypass_failed = terminal_status not in (403, 404, 429)
                                if terminal_status == 403:
                                    should_retry = await self._handle_ban_action(
                                        host=host,
                                        site_key=site_key,
                                        status_code=403,
                                        is_1015=False,
                                        ban_retried=ban_retried,
                                        request_start_time=request_start_time,
                                        responses_to_close=[resp],
                                        stream=stream,
                                    )
                                    if should_retry:
                                        ban_retried = True
                                        continue
                                    else:
                                        break
                                elif terminal_status is not None and (
                                    terminal_status == 429 or not self._is_retryable_status_code(terminal_status)
                                ):
                                    retry = False
                                    self._log_cf(f"🧱 bypass 命中终态 HTTP {terminal_status}，停止重试", host)
                                else:
                                    retry = attempt < retry_count - 1 and bypass_round < self._cf_request_bypass_rounds
                                    self._log_cf(f"⚠️ bypass 失败: {bypass_error}", host)

                    elif self._is_ban_response(resp):
                        is_1015 = self._is_cf_1015_response(resp)
                        status_text = f"{resp.status_code} (1015)" if is_1015 else f"{resp.status_code}"
                        error_msg = f"HTTP {status_text}"
                        should_retry = await self._handle_ban_action(
                            host=host,
                            site_key=site_key,
                            status_code=resp.status_code,
                            is_1015=is_1015,
                            ban_retried=ban_retried,
                            request_start_time=request_start_time,
                            responses_to_close=[resp],
                            stream=stream,
                        )
                        if should_retry:
                            ban_retried = True
                            continue
                        else:
                            break

                    # 检查响应状态
                    elif resp.status_code >= 300 and not (resp.status_code == 302 and resp.headers.get("Location")):
                        error_msg = f"HTTP {resp.status_code}"
                        retry = self._is_retryable_status_code(resp.status_code)
                        if retry and attempt < retry_count - 1:
                            await self._record_retryable_response_failure(error_msg, pool_key=pool_key)
                    else:
                        self._log(f"✅ {method} {url} 成功")
                        if host:
                            self._cf_host_challenge_hits[host] = 0
                        await self._record_transport_success(pool_key=pool_key)
                        await self._record_site_success(site_key)
                        return resp, ""
                except (SiteCircuitOpen, SiteWallTimeout):
                    raise
                except Timeout:
                    proxy_transport_failure = bool(request_proxy)
                    error_msg = "连接超时"
                    retry = True  # 超时错误进行重试
                    await self._record_transport_failure(error_msg, pool_key=pool_key)
                except ConnectionError as e:
                    proxy_transport_failure = bool(request_proxy)
                    error_msg = f"连接错误: {str(e)}"
                    retry = True  # 连接错误进行重试
                    await self._record_transport_failure(error_msg, pool_key=pool_key)
                except RequestException as e:
                    proxy_transport_failure = bool(request_proxy) and isinstance(e, ProxyError)
                    error_msg = f"请求异常: {str(e)} {getattr(e, 'code', '')}".strip()
                    retry = True  # 请求异常进行重试
                    await self._record_transport_failure(error_msg, pool_key=pool_key)
                except TimeoutError:
                    proxy_transport_failure = bool(request_proxy)
                    error_msg = "请求等待超时"
                    retry = True
                    await self._record_transport_failure(error_msg, pool_key=pool_key)
                except Exception as e:
                    error_msg = f"curl-cffi 异常: {str(e)}"
                    retry = True
                    await self._record_transport_failure(error_msg, pool_key=pool_key)
                if not retry:
                    if stream:
                        await self._close_response(resp)
                    break
                self._log(f"🔴 {method} {url} 失败: {error_msg} ({attempt + 1}/{retry_count})")
                if stream:
                    await self._close_response(resp)
                if attempt >= retry_count - 1 and not (self._is_ban_response(resp) and not ban_retried):
                    break
                # 重试前等待
                if should_sleep_before_retry and attempt < retry_count - 1:
                    if resp is not None and resp.status_code == 429:
                        max_429_retries = 2
                        if attempt >= max_429_retries:
                            retry = False
                            break
                        resp_headers = {str(k): str(v) for k, v in resp.headers.items()}
                        retry_after = self._parse_retry_after(resp_headers)
                        if retry_after is not None:
                            sleep_seconds = retry_after
                        else:
                            sleep_seconds = 5.0 if attempt == 0 else 10.0
                        sleep_seconds = min(sleep_seconds, getattr(self, "site_total_timeout", 120.0))
                        site_name = "javlibrary" if "javlibrary" in host.lower() else (host or "目标站点")
                        self._log(
                            f"⏳ [限流] {site_name} 返回 429，等待 {sleep_seconds:g}s 后重试 ({attempt + 1}/{max_429_retries})"
                        )
                    else:
                        sleep_seconds = self._calc_retry_sleep_seconds(attempt, after_cf_bypass=sleep_after_cf_bypass)
                        if sleep_after_cf_bypass and host:
                            self._log_cf(f"⏳ bypass 后退避 {sleep_seconds:.2f}s", host)
                    async with exempt_crawl_timeout():
                        await self._do_sleep(sleep_seconds)
            if proxy_transport_failure and (batch := batch_diagnostics.get()) is not None:
                batch.record_service("代理", False)
                check_service_stop()
            if bypass_failed and (req_state := crawl_request_state.get()) is not None:
                # Internal bypass attempts are already exhausted; do not add another retry.
                req_state.is_ban_failure = True
                req_state.abandoned_error = error_msg
                await self._record_site_ban_failure(site_key, status_code=None, request_start_time=request_start_time)
            self._record_crawl_failure(error_msg)
            return None, f"{method} {url} 失败: {error_msg}"
        except (SiteCircuitOpen, SiteWallTimeout):
            raise
        except Exception as e:
            error_msg = f"{method} {url} 未知错误:  {str(e)}"
            self._log(f"🔴 {error_msg}")
            self._record_crawl_failure(error_msg)
            return None, error_msg

    def _record_crawl_failure(self, error: str) -> None:
        state = crawl_request_state.get()
        # A missing number is a normal result, including mirror's terminal 404.
        if state is not None and not getattr(state, "is_ban_failure", False) and not re.search(r"\bHTTP 404\b", error):
            state.request_failed = True
            state.network_failure = error.startswith(_NETWORK_ERROR_PREFIXES)

    @observe_http(cache=True)
    async def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        encoding: str = "utf-8",
        use_proxy: bool = True,
        retry_count: int | None = None,
    ) -> tuple[str | None, str]:
        """请求文本内容"""
        resp, error = await self.request(
            "GET", url, headers=headers, cookies=cookies, use_proxy=use_proxy, retry_count=retry_count
        )
        if resp is None:
            return None, error
        try:
            resp.encoding = encoding
            return resp.text, error
        except Exception as e:
            return None, f"文本解析失败: {str(e)}"

    async def get_content(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        use_proxy: bool = True,
        retry_count: int | None = None,
    ) -> tuple[bytes | None, str]:
        """请求二进制内容"""
        resp, error = await self.request(
            "GET", url, headers=headers, cookies=cookies, use_proxy=use_proxy, retry_count=retry_count
        )
        if resp is None:
            return None, error

        return resp.content, ""

    @observe_http(cache=True)
    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        use_proxy: bool = True,
        retry_count: int | None = None,
    ) -> tuple[Any | None, str]:
        """请求JSON数据"""
        response, error = await self.request(
            "GET", url, headers=headers, cookies=cookies, use_proxy=use_proxy, retry_count=retry_count
        )
        if response is None:
            return None, error
        try:
            return response.json(), ""
        except Exception as e:
            return None, f"JSON解析失败: {str(e)}"

    @observe_http(cache=True)
    async def post_text(
        self,
        url: str,
        *,
        data: dict[str, str] | list[tuple] | str | BytesIO | bytes | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        encoding: str = "utf-8",
        use_proxy: bool = True,
        retry_count: int | None = None,
    ) -> tuple[str | None, str]:
        """POST 请求, 返回响应文本内容"""
        response, error = await self.request(
            "POST",
            url,
            data=data,
            json_data=json_data,
            headers=headers,
            cookies=cookies,
            use_proxy=use_proxy,
            retry_count=retry_count,
        )
        if response is None:
            return None, error
        try:
            response.encoding = encoding
            return response.text, ""
        except Exception as e:
            return None, f"文本解析失败: {str(e)}"

    @observe_http(cache=True)
    async def post_json(
        self,
        url: str,
        *,
        data: dict[str, str] | list[tuple] | str | BytesIO | bytes | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        use_proxy: bool = True,
        retry_count: int | None = None,
    ) -> tuple[Any | None, str]:
        """POST 请求, 返回响应JSON数据"""
        response, error = await self.request(
            "POST",
            url,
            data=data,
            json_data=json_data,
            headers=headers,
            cookies=cookies,
            use_proxy=use_proxy,
            retry_count=retry_count,
        )
        if error or response is None:
            return None, error

        try:
            return response.json(), ""
        except Exception as e:
            return None, f"JSON解析失败: {str(e)}"

    async def post_content(
        self,
        url: str,
        *,
        data: dict[str, str] | list[tuple] | str | BytesIO | bytes | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        use_proxy: bool = True,
        retry_count: int | None = None,
    ) -> tuple[bytes | None, str]:
        """POST请求, 返回二进制响应"""
        response, error = await self.request(
            "POST",
            url,
            data=data,
            json_data=json_data,
            headers=headers,
            cookies=cookies,
            use_proxy=use_proxy,
            retry_count=retry_count,
        )
        if error or response is None:
            return None, error

        return response.content, ""

    async def get_filesize(self, url: str, *, use_proxy: bool = True) -> int | None:
        """获取文件大小"""
        response, error = await self.request("HEAD", url, use_proxy=use_proxy)
        if response is None:
            self._log(f"🔴 获取文件大小失败: {url} {error}")
            return None
        if response.status_code < 400:
            content_length = self._extract_header_case_insensitive(
                {str(k): str(v) for k, v in response.headers.items()}, "content-length"
            )
            if not content_length:
                return None
            try:
                return int(content_length)
            except ValueError:
                self._log(f"🔴 获取文件大小失败: {url} Content-Length 解析错误")
                return None
        self._log(f"🔴 获取文件大小失败: {url} HTTP {response.status_code}")
        return None

    async def download(self, url: str, file_path: Path, *, use_proxy: bool = True) -> bool:
        """
        下载文件. 当文件较大时分块下载

        Args:
            url: 下载链接
            file_path: 保存路径
            use_proxy: 是否使用代理

        Returns:
            bool: 下载是否成功
        """
        # 获取文件大小
        file_size = None if self._is_dmm_image_url(url) else await self.get_filesize(url, use_proxy=use_proxy)
        # 判断是不是webp文件
        webp = False
        if file_path.suffix == "jpg" and ".webp" in url:
            webp = True

        MB = 1024**2
        # 2 MB 以上使用分块下载, 不清楚为什么 webp 不分块, 可能是因为要转换成 jpg
        if file_size and file_size > 2 * MB and not webp:
            return await self._download_chunks(url, file_path, file_size, use_proxy)

        content, error = await self.get_content(url, use_proxy=use_proxy)
        if not content:
            self._log(f"🔴 下载失败: {url} {error}")
            return False
        if not webp:
            return await self._write_file_content(url, file_path, content)
        try:
            byte_stream = BytesIO(content)
            img: Image.Image = Image.open(byte_stream)
            if img.mode == "RGBA":
                img = img.convert("RGB")
            img.save(file_path, quality=95, subsampling=0)
            img.close()
            return True
        except Exception as e:
            self._log(f"🔴 WebP转换失败: {url} {file_path} {str(e)}")
            return False

    async def _write_file_content(self, url: str, file_path: Path, content: bytes) -> bool:
        try:
            async with aiofiles.open(file_path, "wb") as f:
                await f.write(content)
            return True
        except Exception as e:
            self._log(f"🔴 文件写入失败: {url} {file_path} {str(e)}")
            return False

    async def _download_whole_file(
        self,
        url: str,
        file_path: Path,
        *,
        use_proxy: bool,
        expected_size: int | None = None,
    ) -> bool:
        content, error = await self.get_content(url, use_proxy=use_proxy)
        if not content:
            self._log(f"🔴 下载失败: {url} {error}")
            return False
        if expected_size is not None and len(content) != expected_size:
            self._log(f"🔴 下载大小不匹配: {url} {len(content)}/{expected_size}")
            return False
        return await self._write_file_content(url, file_path, content)

    async def _download_chunks(self, url: str, file_path: Path, file_size: int, use_proxy: bool = True) -> bool:
        """分块下载大文件"""
        MB = 1024**2
        # Range 的 end 为闭区间，最后一块最大只能到 file_size - 1。
        each_size = min(4 * MB, file_size)
        parts = [(s, min(s + each_size - 1, file_size - 1)) for s in range(0, file_size, each_size)]
        part_file_path = file_path.with_name(f"{file_path.name}.part")

        self._log(f"📦 分块下载: {url} {len(parts)} 个分块, 总大小: {file_size} bytes")

        # 先写入临时分块文件，全部成功后再替换目标文件，避免留下不可播放的成品文件。
        try:
            async with aiofiles.open(part_file_path, "wb") as f:
                await f.truncate(file_size)
        except Exception as e:
            self._log(f"🔴 文件创建失败: {url} {str(e)}")
            return False

        try:
            # 创建下载任务
            semaphore = asyncio.Semaphore(6)  # 限制并发数
            first_start, first_end = parts[0]
            first_error = await self._download_chunk(
                semaphore, url, part_file_path, first_start, first_end, 0, use_proxy
            )
            if first_error:
                if self._is_range_unsupported_error(first_error):
                    self._log(f"🟡 服务器不支持分块下载，回退普通下载: {url}")
                    with contextlib.suppress(Exception):
                        await aiofiles.os.remove(part_file_path)
                    return await self._download_whole_file(url, file_path, use_proxy=use_proxy, expected_size=file_size)
                self._log(f"🔴 分块 0 下载失败: {url} {first_error}")
                return False

            tasks = []

            for i, (start, end) in enumerate(parts[1:], start=1):
                task = self._download_chunk(semaphore, url, part_file_path, start, end, i, use_proxy)
                tasks.append(task)

            # 并发执行所有下载任务
            errors = await asyncio.gather(*tasks, return_exceptions=True)
            # 检查所有任务是否成功
            for i, err in enumerate(errors, start=1):
                if isinstance(err, Exception):
                    self._log(f"🔴 分块 {i} 下载失败: {url} {str(err)}")
                    return False
                elif err:
                    self._log(f"🔴 分块 {i} 下载失败: {url} {err}")
                    return False
            await asyncio.to_thread(os.replace, part_file_path, file_path)
            self._log(f"✅ 多分块下载完成: {url} {file_path}")
            return True
        except Exception as e:
            self._log(f"🔴 并发下载异常: {url} {str(e)}")
            return False
        finally:
            if await aiofiles.os.path.exists(part_file_path):
                with contextlib.suppress(Exception):
                    await aiofiles.os.remove(part_file_path)

    def _is_range_unsupported_error(self, error: str) -> bool:
        return "分块响应状态异常: HTTP 200" in str(error or "")

    async def _download_chunk(
        self,
        semaphore: asyncio.Semaphore,
        url: str,
        file_path: Path,
        start: int,
        end: int,
        chunk_id: int,
        use_proxy: bool = True,
    ) -> str | None:
        """下载单个分块"""
        retry_count = max(int(self.retry), 1)
        last_error = ""
        for attempt in range(retry_count):
            async with semaphore:
                success, last_error = await self._download_chunk_once(url, file_path, start, end, use_proxy)
                if success:
                    return ""

            if attempt < retry_count - 1:
                await asyncio.sleep(self._calc_retry_sleep_seconds(attempt))

        return last_error

    async def _download_chunk_once(
        self,
        url: str,
        file_path: Path,
        start: int,
        end: int,
        use_proxy: bool,
    ) -> tuple[bool, str]:
        expected_size = end - start + 1
        res, error = await self.request(
            "GET",
            url,
            headers={"Range": f"bytes={start}-{end}"},
            use_proxy=use_proxy,
            stream=True,
            retry_count=1,
        )
        if res is None:
            return False, error
        try:
            if res.status_code != 206:
                return False, f"分块响应状态异常: HTTP {res.status_code}"
            content = await asyncio.wait_for(res.acontent(), timeout=self._request_timeout_seconds(None))
            if len(content) != expected_size:
                return False, f"分块大小不匹配: {len(content)}/{expected_size}"
            async with aiofiles.open(file_path, "rb+") as fp:
                await fp.seek(start)
                await fp.write(content)
            return True, ""
        except Exception as exc:
            error = f"读取分块响应失败: {exc}"
            pool_key = HostPoolManager.key_for_url(url, self.proxy if use_proxy else None)
            await self._record_transport_failure(error, pool_key=pool_key)
            return False, error
        finally:
            await self._close_response(res)
