"""Hard round deadlines with real local HTTP; no external sites are contacted."""

import asyncio
from types import SimpleNamespace

import pytest

import mdcx.web_async as web
from mdcx.config.enums import Language, Website
from mdcx.config.models import Config
from mdcx.core.file_crawler import FileScraper
from mdcx.models.crawl_status import CrawlStatus
from mdcx.models.types import CrawlTask
from tests.cf_fake_server import FakeBypassServer, ProbeCrawler
from tests.crawlers.test_anti_ban_step1 import _crawl_input


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["lock", "pause", "html_queue", "mirror_html", "interval", "backoff"])
async def test_deadline_includes_waits_and_releases_resources(stage):
    async with FakeBypassServer(log=lambda _: None) as server:
        client = server.client(site_wall_timeout=0.25, site_total_timeout=120)
        client._cf_bypass_min_interval = 0
        client._cf_site_min_intervals = {}
        crawler = ProbeCrawler(client, server.url("cold0.test"))
        scenario = server.scenarios["cold0.test"]
        lock = None
        queue_scope = None
        pause_until = None
        if stage == "lock":
            lock = await client._get_cf_host_lock("cold0.test")
            await lock.acquire()
        elif stage == "pause":
            state = client._site_ban_states.setdefault("jav321", web._SiteBanState())
            state.pause_until = client._get_time() + 60
            pause_until = state.pause_until
        elif stage == "html_queue":
            queue_scope = web._html_slot()
            await queue_scope.__aenter__()
        elif stage == "mirror_html":
            scenario.mirror_delay = 0.12
            scenario.html_delay = 1
        elif stage == "interval":
            client._site_request_intervals["jav321"] = 60
            client._site_last_request_at["jav321"] = client._get_time()
        else:
            scenario.direct_status = 403
            scenario.direct_body = b"Forbidden"
        try:
            started = asyncio.get_running_loop().time()
            result = await crawler.run(_crawl_input("FIRST"))
            elapsed = asyncio.get_running_loop().time() - started
            assert result.data is None
            assert isinstance(result.debug_info.error, TimeoutError)
            assert "墙钟超限" in str(result.debug_info.error)
            assert any(i.status == CrawlStatus.REQUEST_ERROR for i in result.report.issues)
            assert 0.20 <= elapsed < 1
            assert not crawler.is_stopped()
            counts = [server.count(kind, "cold0.test") for kind in ["direct", "mirror", "html"]]
            expected = {
                "lock": [1, 0, 0],
                "pause": [0, 0, 0],
                "html_queue": [1, 1, 0],
                "mirror_html": [1, 1, 1],
                "interval": [0, 0, 0],
                "backoff": [1, 0, 0],
            }
            assert counts == expected[stage]
            # Same number cannot append requests, including another language and idle time.
            await asyncio.sleep(0.02)
            task_input = _crawl_input("FIRST")
            task_input.language = Language.ZH_CN
            assert (await crawler.run(task_input)).data is None
            assert counts == [server.count(kind, "cold0.test") for kind in ["direct", "mirror", "html"]]
            if stage == "pause":
                assert client._site_ban_states["jav321"].pause_until == pause_until
            if stage == "backoff":
                assert not client._site_ban_states.get("jav321", web._SiteBanState()).failure_count
            if lock is not None:
                assert lock.locked()  # The waiting task must not release somebody else's lock.
                lock.release()
                lock = None
            else:
                assert not (await client._get_cf_host_lock("cold0.test")).locked()
            if queue_scope is not None:
                assert len(web._html_queue) == 1  # Only our blocker remains.
                await queue_scope.__aexit__(None, None, None)
                queue_scope = None
            assert not web._html_queue
            # A different number can still run after the safety wait is actually over.
            client._site_ban_states.clear()
            client._site_request_intervals.clear()
            server.warmed.add("cold0.test")
            scenario.direct_status, scenario.direct_body = 200, b"<html>ok</html>"
            assert (await crawler.run(_crawl_input("NEXT"))).data is not None
        finally:
            if lock is not None:
                lock.release()
            if queue_scope is not None:
                await queue_scope.__aexit__(None, None, None)
            await client.close()


@pytest.mark.asyncio
async def test_only_formal_retry_resets_wall_deadline_and_preserves_active_budget():
    async with FakeBypassServer(log=lambda _: None) as server:
        scenario = server.scenarios["plain.test"]
        scenario.direct_status, scenario.direct_body = 200, b"<html>ok</html>"
        scenario.direct_delay = 0.04
        client = server.client(site_wall_timeout=0.15, site_total_timeout=0.12)
        crawler = ProbeCrawler(client, server.url("plain.test"))
        try:
            assert (await crawler.run(_crawl_input("SAME"))).data is not None
            budget = crawler._number_budgets["SAME"]
            remaining = budget.remaining
            await asyncio.sleep(0.16)
            assert (await crawler.run(_crawl_input("SAME"))).data is None
            assert server.count("direct", "plain.test") == 1
            crawler.reset_wall_timeout("SAME")
            assert budget.remaining <= remaining
            assert (await crawler.run(_crawl_input("SAME"))).data is not None
            assert server.count("direct", "plain.test") == 2
            # Repeated formal resets cannot replenish the original active budget.
            crawler.reset_wall_timeout("SAME")
            scenario.direct_delay = 0.10
            result = await crawler.run(_crawl_input("SAME"))
            assert result.data is None
            assert "总等待超限" in str(result.debug_info.error)
            assert budget.remaining == 0
            assert not crawler.is_stopped()
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_retry_reset_targets_failed_sites_and_uses_the_same_number_mapping():
    calls = []

    class Provider:
        async def get(self, site):
            return SimpleNamespace(reset_wall_timeout=lambda number: calls.append((site, number)))

    scraper = object.__new__(FileScraper)
    scraper.crawler_provider = Provider()
    task = CrawlTask.empty()
    task.number, task.short_number = "300TEST-001", "TEST-001"
    await scraper.reset_wall_timeouts(task, ["javdb", "mgstage", "avsex", "javdb"])
    assert set(calls) == {(Website.JAVDB, "TEST-001"), (Website.MGSTAGE, "300TEST-001"), (Website.AVSEX, "300TEST-001")}
    assert len(calls) == 3
    assert task.number == "300TEST-001"


@pytest.mark.asyncio
async def test_config_default_validation_and_client_wiring():
    from pydantic import ValidationError

    from mdcx.config.computed import Computed

    config = Config()
    assert config.site_total_timeout == 120
    assert config.site_wall_timeout == 180
    config.site_wall_timeout = 210
    computed = Computed(config)
    try:
        assert computed.async_client.site_wall_timeout == 210
        with pytest.raises(ValidationError):
            Config(site_wall_timeout=0)
    finally:
        await computed.close()
