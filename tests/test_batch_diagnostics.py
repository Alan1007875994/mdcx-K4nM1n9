"""Synthetic observations only; no external requests or captured responses."""

import asyncio
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from mdcx.models.crawl_status import BatchDiagnostics, CrawlIssue, CrawlStatus, SiteReport, batch_diagnostics
from mdcx.web_async import AsyncWebClient, CrawlRequestState, SiteCircuitOpen, SiteWallTimeout, crawl_request_state


def counts(**sites):
    batch = BatchDiagnostics()
    for site, (success, failure) in sites.items():
        batch.bypass[site] = Counter(success=success, failure=failure)
    return batch


def test_synthetic_high_failure_batch():
    batch = counts(javlibrary=(0, 5), javten=(12, 4), missav=(14, 2))
    lines = batch.lines()
    assert "11/37" in lines[0] and "3 个网站" in lines[0]
    assert "javlibrary" in lines[1] and "5/5" in lines[1]
    assert len(lines) == 3 and "程序内部异常" in lines[-1]


def test_synthetic_low_failure_batch():
    batch = counts(javlibrary=(22, 0), javten=(12, 0), missav=(14, 2))
    assert batch.lines() == ["诊断：本批未发现 bypass、节点或解析异常。"]


@pytest.mark.parametrize(
    "a,b,triggered",
    [
        ((4, 2), (5, 1), True),  # exactly 25%, N=12
        ((3, 2), (3, 1), False),  # N=9
        ((4, 1), (4, 1), False),  # F=2
        ((9, 3), (0, 0), False),  # only one failing site
        ((5, 2), (5, 1), False),
    ],  # below 25%
)
def test_link_boundaries(a, b, triggered):
    assert ("本批 bypass" in counts(alpha=a, beta=b).lines()[0]) is triggered


@pytest.mark.parametrize(
    "a,b,triggered", [((0, 3), (3, 0), True), ((0, 2), (3, 0), False), ((2, 2), (3, 0), True), ((0, 3), (2, 0), False)]
)
def test_site_boundaries(a, b, triggered):
    assert any("alpha 本批访问多数失败" in line for line in counts(alpha=a, beta=b).lines()) is triggered


def test_parse_missing_order_and_all_round_error_exclusion():
    batch = counts(alpha=(0, 5), beta=(26, 6))
    error = SiteReport("alpha", [CrawlIssue(CrawlStatus.PARSE_ERROR, "synthetic")])
    batch.record_report(error)
    normal = SiteReport("beta")
    batch.record_movie("synthetic-a", True, [normal])
    batch.record_movie("synthetic-a", True, [normal])  # same movie counted once
    batch.record_movie("synthetic-b", True, [error])
    batch.record_movie("synthetic-b", True, [normal])  # recovery cannot erase first-round error
    batch.record_movie("synthetic-c", True, [normal])
    batch.record_movie("synthetic-c", False, [normal])  # final source supplied the field
    for status in (CrawlStatus.REQUEST_ERROR, CrawlStatus.SKIPPED):
        batch.record_movie(status, True, [SiteReport("beta", [CrawlIssue(status, "synthetic")])])
    lines = batch.lines(incomplete=True)
    assert "本批未跑完" in lines[0]
    assert "本批 bypass" in lines[1]
    assert "alpha 本批访问多数失败" in lines[2]
    assert "alpha 有 1 次页面" in lines[3]
    assert "有 1 部片" in lines[4]
    assert len(lines) == 5
    assert not any("本批没有记录到" in line for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["mirror", "refresh", "html", "failure", "terminal", "cancel", "circuit", "wall", "wall_cancel"]
)
async def test_logical_bypass_counts_only_final_outcome(monkeypatch, mode):
    batch = BatchDiagnostics()
    token = batch_diagnostics.set(batch)
    client = AsyncWebClient(timeout=1, cf_bypass_url="http://bypass.test")
    client._cf_bypass_min_interval = 0
    calls = []
    response = SimpleNamespace(headers={}, status_code=200)

    async def mirror(**kwargs):
        calls.append("refresh" if kwargs["bypass_cache"] else "mirror")
        if mode in {"cancel", "wall_cancel"}:
            raise asyncio.CancelledError
        if mode == "circuit":
            raise SiteCircuitOpen("synthetic")
        if mode == "wall":
            raise SiteWallTimeout("synthetic")
        if mode == "mirror" or (mode == "refresh" and kwargs["bypass_cache"]):
            return response, ""
        return (
            None,
            "mirror HTTP 400"
            if mode == "terminal"
            else "mirror 返回 Cloudflare 挑战页"
            if mode == "refresh"
            else "synthetic timeout",
        )

    async def html(*args, **kwargs):
        calls.append("html")
        return (response, "") if mode == "html" else (None, "synthetic timeout")

    monkeypatch.setattr(client, "_call_bypass_mirror", mirror)
    monkeypatch.setattr(client, "_call_bypass_html", html)
    state_token = (
        crawl_request_state.set(CrawlRequestState(is_blocked=lambda: False, budget=SimpleNamespace(wall_deadline=0)))
        if mode == "wall_cancel"
        else None
    )
    kwargs = {
        "host": "alpha.test",
        "method": "GET",
        "target_url": "https://alpha.test/",
        "headers": {},
        "cookies": None,
        "data": None,
        "json_data": None,
        "timeout": None,
        "allow_redirects": True,
        "use_proxy": False,
    }
    try:
        if mode in {"cancel", "wall_cancel", "circuit", "wall"}:
            exception = (
                asyncio.CancelledError
                if "cancel" in mode
                else SiteCircuitOpen
                if mode == "circuit"
                else SiteWallTimeout
            )
            with pytest.raises(exception):
                await client._try_bypass_cloudflare(**kwargs)
            expected = "cancelled" if mode == "cancel" else "skipped" if mode == "circuit" else "wall_timeout"
        else:
            await client._try_bypass_cloudflare(**kwargs)
            expected = "success" if mode in {"mirror", "refresh", "html"} else "failure"
        assert batch.bypass == {"alpha.test": Counter({expected: 1})}
        if mode == "html":
            assert calls == ["mirror", "html"]
        if mode == "refresh":
            assert calls == ["mirror", "refresh"]
        if mode == "terminal":
            assert calls == ["mirror"]
    finally:
        if state_token is not None:
            crawl_request_state.reset(state_token)
        batch_diagnostics.reset(token)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_scraper_combines_rounds_and_resets_batch_context(monkeypatch, stop):
    from mdcx.config.extend import MoviePathSetting
    from mdcx.core import scraper as module
    from mdcx.models.enums import FileMode
    from mdcx.models.flags import Flags

    emitted = []
    batches = []

    async def run_tasks(self, movies, total, threads):
        batch = batch_diagnostics.get()
        if not self._quality_retrying:
            batches.append(batch)
            assert not batch.bypass
            batch.record_bypass("alpha", "success")
            self._quality_pending[Path("synthetic.mp4")] = object()
            module.signal.stop = stop
        else:
            batch.record_bypass("alpha", "failure")
        Flags.scrape_done = total

    async def noop(*args):
        pass

    def paths(*args, **kwargs):
        p = Path(".")
        return MoviePathSetting(
            movie_path=p,
            movie_paths=[p],
            success_folder=p,
            failed_folder=p,
            ignore_dirs=[],
            extrafanart_folder=p,
            softlink_path=p,
        )

    monkeypatch.setattr(module.Scraper, "_run_tasks_with_limit", run_tasks)
    monkeypatch.setattr(module, "save_success_list", noop)
    monkeypatch.setattr(module, "_clean_empty_fodlers", noop)
    monkeypatch.setattr(module, "get_movie_path_setting", paths)
    monkeypatch.setattr(module.signal, "show_log_text", emitted.append)
    monkeypatch.setattr(module.signal, "stop", False)
    for name, value in {
        "thread_number": 1,
        "thread_time": 0,
        "main_mode": 1,
        "switch_on": [],
        "scrape_softlink_path": False,
        "emby_on": [],
        "actor_photo_kodi_auto": False,
        "garbage_enabled": False,
    }.items():
        monkeypatch.setattr(module.manager.config, name, value)
    scraper = module.Scraper(object())
    for _ in range(2):
        module.signal.stop = False
        await scraper._run(FileMode.Default, [Path("synthetic.mp4")])
        assert batch_diagnostics.get() is None
    assert batches[0] is not batches[1]
    for batch in batches:
        assert batch.bypass["alpha"] == Counter(success=1, failure=0 if stop else 1)
    diagnostics = [line for line in emitted if "诊断：" in line]
    assert len(diagnostics) == 2
    assert all(("本批未跑完" in line) == stop for line in diagnostics)
