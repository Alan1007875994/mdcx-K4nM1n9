"""Per-call observations and batch service health, independent of site circuit rules."""

import asyncio
import copy
import re
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps


class CrawlStatus(StrEnum):
    NOT_FOUND = "正常未收录"
    MISSING = "正常字段缺失"
    REQUEST_ERROR = "请求错误"
    PARSE_ERROR = "解析异常"
    SKIPPED = "停站跳过"


ERROR_STATUSES = {CrawlStatus.REQUEST_ERROR, CrawlStatus.PARSE_ERROR, CrawlStatus.SKIPPED}
QUALITY_FIELDS = ("score", "outline", "actors", "thumb")


@dataclass(frozen=True)
class CrawlIssue:
    status: CrawlStatus
    reason: str


@dataclass
class SiteReport:
    site: str
    issues: list[CrawlIssue] = field(default_factory=list)
    missing: dict[str, CrawlStatus] = field(default_factory=dict)

    @property
    def has_error(self) -> bool:
        return any(issue.status in ERROR_STATUSES for issue in self.issues)


observations: ContextVar[list[CrawlIssue] | None] = ContextVar("crawl_observations", default=None)
request_cache: ContextVar[dict | None] = ContextVar("crawl_request_cache", default=None)


def record_status(status: CrawlStatus, reason: object) -> None:
    if (items := observations.get()) is not None:
        issue = CrawlIssue(status, str(reason))
        if issue not in items:
            items.append(issue)


def record_request_error(error: object) -> None:
    reason = str(error)
    if any(i.status == CrawlStatus.SKIPPED and i.reason in reason for i in observations.get() or []):
        return
    status = (
        CrawlStatus.SKIPPED
        if getattr(error, "status", None) == CrawlStatus.SKIPPED
        else CrawlStatus.NOT_FOUND
        if re.search(r"\bHTTP 404\b", reason)
        else CrawlStatus.REQUEST_ERROR
    )
    record_status(status, reason)


def record_parse_error(error: object) -> None:
    record_status(CrawlStatus.PARSE_ERROR, error)


def observe_http(*, cache: bool = False):
    """Observe only the final return; reuse successful decoded metadata within one movie."""

    def decorate(method):
        @wraps(method)
        async def observed(self, *args, **kwargs):
            saved = request_cache.get() if cache else None
            key = (method.__name__, repr(args), repr(sorted(kwargs.items())))
            if saved is not None and key in saved:
                return copy.deepcopy(saved[key])
            try:
                result, error = await method(self, *args, **kwargs)
            except Exception as error:
                if getattr(error, "status", None) == CrawlStatus.SKIPPED:
                    record_status(CrawlStatus.SKIPPED, error)
                raise
            if result is None and error:
                if args and args[0] == "HEAD" and re.search(r"\bHTTP 405\b", str(error)):
                    record_status(CrawlStatus.NOT_FOUND, "HEAD 405，沿用已有 GET 回退")
                elif "解析失败" in str(error):
                    record_parse_error(error)
                else:
                    record_request_error(error)
            elif saved is not None and result is not None and not error:
                saved[key] = copy.deepcopy((result, error))
            return result, error

        return observed

    return decorate


# Batch-only observations. Context propagation includes all concurrent crawl tasks.
LINK_MIN_CALLS = 10
LINK_MIN_FAILURES = 3
LINK_FAILURE_RATE = 0.25
LINK_MIN_SITES = 2
SITE_MIN_CALLS = 3
SITE_FAILURE_RATE = 0.5
OTHER_SITE_MIN_SUCCESSES = 3


@dataclass
class BatchDiagnostics:
    bypass: dict[str, Counter] = field(default_factory=dict)
    parse_errors: Counter = field(default_factory=Counter)
    abnormal_movies: set[str] = field(default_factory=set)
    missing_movies: set[str] = field(default_factory=set)
    service_failures: Counter = field(default_factory=Counter)
    service_streaks: Counter = field(default_factory=Counter)
    stop_reason: str = ""
    on_service_stop: Callable[[str], None] | None = None

    def record_service(self, category: str, success: bool) -> None:
        if self.stop_reason:
            if not success:
                self.service_failures[category] += 1
            return
        if success:
            self.service_streaks[category] = 0
            return
        self.service_failures[category] += 1
        self.service_streaks[category] += 1
        if self.service_streaks[category] >= 3:
            self.stop_reason = f"{category} 服务连续失败 3 次，已停止本批刮削；请检查服务后继续刮削剩余任务。"
            if self.on_service_stop is not None:
                self.on_service_stop(self.stop_reason)

    def record_bypass(self, site: str, outcome: str) -> None:
        self.bypass.setdefault(site, Counter())[outcome] += 1

    def record_report(self, report: SiteReport) -> None:
        # Generic crawler exceptions and invalid field types already map to PARSE_ERROR.
        self.parse_errors[report.site] += sum(i.status == CrawlStatus.PARSE_ERROR for i in report.issues)

    def record_movie(self, key: str, missing: bool, reports: list[SiteReport]) -> None:
        if any(report.has_error for report in reports):
            self.abnormal_movies.add(key)
        if missing and reports:
            self.missing_movies.add(key)
        else:
            self.missing_movies.discard(key)

    def lines(self, *, incomplete: bool = False) -> list[str]:
        lines = []
        if self.stop_reason:
            lines.append(f"诊断：{self.stop_reason}")
        for category, count in self.service_failures.items():
            lines.append(f"诊断：本批 {category} 服务最终失败 {count} 次。")
        successes = sum(counts["success"] for counts in self.bypass.values())
        failures = sum(counts["failure"] for counts in self.bypass.values())
        total = successes + failures
        failed_sites = sum(counts["failure"] > 0 for counts in self.bypass.values())
        if (
            total >= LINK_MIN_CALLS
            and failures >= LINK_MIN_FAILURES
            and failures / total >= LINK_FAILURE_RATE
            and failed_sites >= LINK_MIN_SITES
        ):
            lines.append(
                f"诊断：本批 bypass 验证失败偏多（{failures}/{total} 次，涉及 {failed_sites} 个网站），"
                "多半是 bypass 容器或它用的节点不稳。建议：先换一个节点重刮出错的几部；"
                "换了还不行，再重启 bypass 容器后重刮。"
            )
        for site, counts in sorted(self.bypass.items()):
            n = counts["success"] + counts["failure"]
            if (
                n >= SITE_MIN_CALLS
                and counts["failure"] / n >= SITE_FAILURE_RATE
                and successes - counts["success"] >= OTHER_SITE_MIN_SUCCESSES
            ):
                lines.append(
                    f"诊断：{site} 本批访问多数失败（{counts['failure']}/{n} 次），其他网站正常，"
                    "可能是当前节点被该站限制，或该站验证没通过。建议：稍后再刮，或换节点重刮这几部对照。"
                )
        for site, count in sorted(self.parse_errors.items()):
            if count:
                lines.append(
                    f"诊断：{site} 有 {count} 次页面没有被程序正常识别"
                    "（可能是网站改版、返回了异常页面，也可能是程序问题），请把这次日志交给指挥排查。"
                )
        missing = len(self.missing_movies - self.abnormal_movies)
        if missing:
            lines.append(
                f"诊断：有 {missing} 部片的部分字段沿来源链找完仍没有，而且这几部的请求和解析都没出错，"
                "多半是网站本来没有，不用反复重刮。"
            )
        if not lines:
            lines.append("诊断：本批未发现 bypass、节点或解析异常。")
        elif not any(self.parse_errors.values()):
            lines.append("诊断：本批没有记录到解析异常或程序内部异常，上面的问题出在网络或网站一侧。")
        if incomplete:
            lines.insert(0, "本批未跑完，以下只统计已完成部分")
        return lines


batch_diagnostics: ContextVar[BatchDiagnostics | None] = ContextVar("batch_diagnostics", default=None)


class ServiceStopped(asyncio.CancelledError):
    """Abort unfinished work without being swallowed by crawler Exception handlers."""


def check_service_stop() -> None:
    if (batch := batch_diagnostics.get()) is not None and batch.stop_reason:
        raise ServiceStopped(batch.stop_reason)
