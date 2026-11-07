"""Issue #112: 网络类失败 (连接/TLS/HTTP2/超时、重试耗尽、墙钟耗尽) 只算「这部片该站失败一次」, 连续 3 部才停站."""

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import RequestException

from mdcx.web_async import AsyncWebClient, SiteCircuitOpen
from tests.crawlers.test_anti_ban_step2 import DummyJavCrawler, _crawl_input
from tests.crawlers.test_javlibrary_429_backoff import FakeResponse

SITE = "javlibrary"


@pytest.fixture
def probe():
    class Probe:
        def __init__(self):
            self.now = 1000.0
            self.mode = "net"
            self.calls = 0
            self.sleeps = []
            self.logs = []
            self.client = AsyncWebClient(timeout=10, cf_bypass_url="", retry=3, log_fn=self.logs.append)
            self.client._time_fn = lambda: self.now
            self.client._sleep = self.sleep
            self.client._curl_request = self.request
            self.client._calc_retry_sleep_seconds = lambda *args, **kwargs: 0

        async def sleep(self, delay):
            self.sleeps.append(delay)
            self.now += delay

        async def request(self, **kwargs):
            url = str(kwargs["url"])
            self.calls += 1
            if self.mode == "net":
                raise CurlConnectionError("TLS connect error")
            if self.mode == "h2":
                raise RequestException("HTTP/2 stream error")
            if self.mode == "ban":
                return FakeResponse(status_code=403, content=b"Forbidden", url=url)
            if self.mode == "5xx":
                return FakeResponse(status_code=500, content=b"fail", url=url)
            return FakeResponse(status_code=200, content=b"<html>ok</html>", url=url)

    return Probe()


async def _run(probe, crawler, crawl_input):
    probe.now += 1  # 下一部片在上一部失败之后才开始 (虚拟时钟不自动前进)
    return await crawler.run(crawl_input)


def _count(probe):
    state = probe.client._site_ban_states.get(SITE)
    return state.failure_count if state else 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["net", "h2"])
async def test_single_movie_network_exhaustion_does_not_stop_site(probe, mode):
    probe.mode = mode
    crawler = DummyJavCrawler(probe.client)
    try:
        result = await _run(probe, crawler, _crawl_input("MOVIE-1"))
        assert result.data is None
        assert probe.calls == 3  # 重试次数不变
        assert not crawler._site_failure
        assert _count(probe) == 1
        assert probe.client._site_ban_states[SITE].pause_until == probe.now + 60
        assert any("第 1/3 次网络错误，暂停该站 60s" in line for line in probe.logs)
        assert not any("允许的请求尝试已耗尽" in line for line in probe.logs)
    finally:
        await probe.client.close()


@pytest.mark.asyncio
async def test_three_consecutive_network_failures_stop_site_and_success_resets(probe):
    crawler = DummyJavCrawler(probe.client)
    try:
        # 失败 2 次 -> 成功 -> 计数清零
        for index in (1, 2):
            probe.mode = "net"
            assert (await _run(probe, crawler, _crawl_input(f"A-{index}"))).data is None
            assert _count(probe) == index
        probe.mode = "ok"
        assert (await _run(probe, crawler, _crawl_input("A-OK"))).data is not None
        assert _count(probe) == 0
        assert not crawler._site_failure

        # 连续 3 部才停站
        probe.mode = "net"
        for index in (1, 2, 3):
            assert (await _run(probe, crawler, _crawl_input(f"B-{index}"))).data is None
            assert _count(probe) == index
            assert bool(crawler._site_failure) == (index == 3)
        assert "网络请求失败" in crawler._site_failure
        assert any("🚫 [断路器] javlibrary 本次运行停用" in line for line in probe.logs)

        calls = probe.calls
        result = await _run(probe, crawler, _crawl_input("B-4"))
        assert isinstance(result.debug_info.error, SiteCircuitOpen)
        assert probe.calls == calls
    finally:
        await probe.client.close()


@pytest.mark.asyncio
async def test_network_and_403_share_counter(probe):
    crawler = DummyJavCrawler(probe.client)
    try:
        for index, mode in enumerate(["net", "ban", "h2"], start=1):
            probe.mode = mode
            assert (await _run(probe, crawler, _crawl_input(f"MIX-{index}"))).data is None
            assert _count(probe) == index
            assert bool(crawler._site_failure) == (index == 3)
        assert "连续 3 部片" in crawler._site_failure
    finally:
        await probe.client.close()


@pytest.mark.asyncio
async def test_403_rule_unchanged(probe):
    probe.mode = "ban"
    crawler = DummyJavCrawler(probe.client)
    try:
        for index in (1, 2, 3):
            assert (await _run(probe, crawler, _crawl_input(f"BAN-{index}"))).data is None
        assert crawler._site_failure == "连续 3 部片被拒绝 (403)，可能已被限制访问"
    finally:
        await probe.client.close()


@pytest.mark.asyncio
async def test_http_status_exhaustion_keeps_immediate_stop(probe):
    """5xx 等 HTTP 状态耗尽: 现有行为不变, 仍立即停站."""
    probe.mode = "5xx"
    crawler = DummyJavCrawler(probe.client)
    try:
        assert (await _run(probe, crawler, _crawl_input("S-1"))).data is None
        assert crawler._site_failure == "允许的请求尝试已耗尽，未取得页面"
        assert _count(probe) == 0
    finally:
        await probe.client.close()


@pytest.mark.asyncio
async def test_wall_timeout_after_network_error_counts_once_without_stopping(probe):
    probe.client.site_wall_timeout = 0.2
    crawler = DummyJavCrawler(probe.client)

    async def run_after_network_error(ctx):
        import asyncio

        assert (await probe.client.get_text(crawler.base_url + "/x", retry_count=1))[0] is None
        await asyncio.sleep(1)

    crawler._run = run_after_network_error
    try:
        result = await _run(probe, crawler, _crawl_input("W-1"))
        assert isinstance(result.debug_info.error, TimeoutError)
        assert not crawler._site_failure
        assert _count(probe) == 1
        assert probe.client._site_ban_states[SITE].pause_until == probe.now + 60
    finally:
        await probe.client.close()


@pytest.mark.asyncio
async def test_wall_timeout_without_request_failure_is_not_counted(probe):
    probe.client.site_wall_timeout = 0.05
    crawler = DummyJavCrawler(probe.client)

    async def slow_run(ctx):
        import asyncio

        await asyncio.sleep(1)

    crawler._run = slow_run
    try:
        result = await _run(probe, crawler, _crawl_input("W-2"))
        assert isinstance(result.debug_info.error, TimeoutError)
        assert _count(probe) == 0
    finally:
        await probe.client.close()
