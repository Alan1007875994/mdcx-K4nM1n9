import asyncio

import pytest
from pydantic import ValidationError

from mdcx.config.enums import Language, Website
from mdcx.config.models import Config
from mdcx.crawler import CrawlerProvider
from mdcx.models.types import CrawlerInput
from mdcx.web_async import crawl_request_state
from tests.cf_fake_server import FakeBypassServer, ProbeCrawler, Scenario


def crawl_input(number="ONE"):
    return CrawlerInput(
        appoint_number="",
        appoint_url="",
        file_path=None,
        number=number,
        mosaic="",
        short_number="",
        language=Language.UNDEFINED,
        org_language=Language.UNDEFINED,
    )


@pytest.mark.asyncio
async def test_failed_site_skips_later_numbers_and_new_provider_resets(monkeypatch):
    async with FakeBypassServer(log=lambda _: None) as server:
        server.scenarios["plain.test"] = Scenario(direct_status=500, direct_body=b"failure")
        logs = []
        client = server.client(retry=2, cf_bypass_url="", log_fn=logs.append)
        client._calc_retry_sleep_seconds = lambda *args, **kwargs: 0
        monkeypatch.setattr("mdcx.crawler.get_crawler", lambda _: ProbeCrawler)
        config = Config()
        monkeypatch.setattr(Config, "get_site_url", lambda self, site: server.url("plain.test").rstrip("/"))
        provider = CrawlerProvider(config, client)
        crawler = await provider.get(Website.JAV321)
        assert (await crawler.run(crawl_input())).data is None
        assert server.count("direct", "plain.test") == 2
        for number in ["TWO", "THREE"]:
            assert (await crawler.run(crawl_input(number))).data is None
        assert server.count("direct", "plain.test") == 2
        assert sum("跳过后续番号" in line for line in logs) == 1
        assert crawl_request_state.get() is None
        await provider.close()
        provider2 = CrawlerProvider(config, client)
        crawler2 = await provider2.get(Website.JAV321)
        server.scenarios["plain.test"].direct_status = 200
        assert (await crawler2.run(crawl_input("NEW"))).data is not None
        assert server.count("direct", "plain.test") == 3
        await provider2.close()
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mirror", [False, True])
async def test_404_does_not_open_circuit(mirror):
    async with FakeBypassServer(log=lambda _: None) as server:
        server.scenarios["missing.test"] = Scenario(
            direct_status=503 if mirror else 404,
            direct_body=(b"<html>just a moment cf-chl</html>" if mirror else b"missing"),
            mirror_status=404,
        )
        client = server.client(retry=5, cf_bypass_url=server.base if mirror else "")
        crawler = ProbeCrawler(client, server.url("missing.test").rstrip("/"))
        await crawler.run(crawl_input())
        await crawler.run(crawl_input("TWO"))
        assert not crawler._site_failure
        assert server.count("direct", "missing.test") == 2
        assert server.count("html", "missing.test") == 0
        await client.close()


@pytest.mark.asyncio
async def test_empty_search_and_parser_failure_do_not_open_circuit():
    async with FakeBypassServer(log=lambda _: None) as server:
        server.scenarios["plain.test"] = Scenario(direct_status=200, direct_body=b"no result")
        client = server.client(cf_bypass_url="")
        crawler = ProbeCrawler(client, server.url("plain.test").rstrip("/"))
        await crawler.run(crawl_input())
        await crawler.run(crawl_input("TWO"))
        assert not crawler._site_failure
        assert server.count("direct", "plain.test") == 2
        await client.close()


@pytest.mark.asyncio
async def test_total_limit_cancels_http_without_opening_circuit():
    async with FakeBypassServer(log=lambda _: None) as server:
        server.scenarios["slow.test"] = Scenario(direct_status=200, direct_delay=0.5)
        client = server.client(cf_bypass_url="", site_total_timeout=0.1)
        crawler = ProbeCrawler(client, server.url("slow.test").rstrip("/"))
        start = asyncio.get_running_loop().time()
        result = await crawler.run(crawl_input())
        assert 0.08 <= asyncio.get_running_loop().time() - start < 0.4
        assert isinstance(result.debug_info.error, TimeoutError)
        assert not crawler._site_failure
        server.scenarios["slow.test"].direct_delay = 0.0
        res_two = await crawler.run(crawl_input("TWO"))
        assert res_two.data is not None
        assert server.count("direct", "slow.test") == 2
        await client.close()


@pytest.mark.asyncio
async def test_budget_is_shared_by_languages_for_same_number():
    async with FakeBypassServer(log=lambda _: None) as server:
        server.scenarios["plain.test"] = Scenario(direct_status=200, direct_body=b"ok", direct_delay=0.08)
        client = server.client(cf_bypass_url="", site_total_timeout=0.12)
        crawler = ProbeCrawler(client, server.url("plain.test").rstrip("/"))
        assert (await crawler.run(crawl_input())).data is not None
        alternate = crawl_input()
        alternate.language = Language.ZH_CN
        alt_res = await crawler.run(alternate)
        assert alt_res.data is None
        assert isinstance(alt_res.debug_info.error, TimeoutError)
        assert not crawler._site_failure
        await client.close()


@pytest.mark.asyncio
async def test_running_sibling_stops_before_next_retry_after_circuit_opens():
    async with FakeBypassServer(log=lambda _: None) as server:
        server.scenarios["plain.test"] = Scenario(direct_status=500, direct_body=b"failure")
        client = server.client(cf_bypass_url="", retry=2)
        client._calc_retry_sleep_seconds = lambda *args, **kwargs: 0.1
        crawler = ProbeCrawler(client, server.url("plain.test").rstrip("/"))
        task = asyncio.create_task(crawler.run(crawl_input()))
        while server.count("direct", "plain.test") == 0:
            await asyncio.sleep(0.01)
        crawler._open_circuit("另一个番号已失败")
        await task
        assert server.count("direct", "plain.test") == 1
        await client.close()


@pytest.mark.parametrize("field,default", [("cf_bypass_timeout", 90), ("site_total_timeout", 120)])
def test_timeout_defaults_roundtrip_and_validation(field, default):
    assert getattr(Config(), field) == default
    assert getattr(Config.model_validate_json(Config(**{field: default + 10}).model_dump_json()), field) == default + 10
    for invalid in (0, -1):
        with pytest.raises(ValidationError):
            Config(**{field: invalid})


@pytest.mark.asyncio
async def test_failed_check_does_not_block_scrape_and_warm_cache_uses_mirror(monkeypatch):
    from types import SimpleNamespace

    from mdcx.core.network_check import NetworkCheckSpec, NetworkCheckStatus, run_network_check_item

    async with FakeBypassServer(log=lambda _: None) as server:
        server.scenarios["cold0.test"].html_status = 500
        client = server.client()
        fake_manager = SimpleNamespace(config=SimpleNamespace(cf_bypass_url=server.base, timeout=10))
        monkeypatch.setattr("mdcx.core.network_check._manager", lambda: fake_manager)
        spec = NetworkCheckSpec(name="cold", group="test", url=server.url("cold0.test"), enable_cf_bypass=True)
        assert (await run_network_check_item(spec, client=client)).status == NetworkCheckStatus.FAILED
        assert server.count("html", "cold0.test") == 1
        server.scenarios["cold0.test"].html_status = 200
        crawler = ProbeCrawler(client, server.url("cold0.test").rstrip("/"))
        assert (await crawler.run(crawl_input())).data is not None
        assert server.count("html", "cold0.test") == 2
        assert (await crawler.run(crawl_input("TWO"))).data is not None
        assert server.count("html", "cold0.test") == 2
        assert not crawler._site_failure
        await client.close()


@pytest.mark.asyncio
async def test_cancelling_html_while_queued_does_not_launch_browser():
    async with FakeBypassServer(log=lambda _: None) as server:
        server.scenarios["cold0.test"].html_delay = 0.4
        client = server.client(site_total_timeout=0.1)
        blocker = asyncio.create_task(client._call_bypass_html(server.url("cold0.test"), use_proxy=False))
        while server.count("html", "cold0.test") == 0:
            await asyncio.sleep(0.01)
        crawler = ProbeCrawler(client, server.url("cold1.test").rstrip("/"))
        result = await crawler.run(crawl_input())
        assert isinstance(result.debug_info.error, TimeoutError)
        assert server.count("html", "cold1.test") == 0
        await blocker
        assert server.count("html", "cold1.test") == 0
        # A later client can still acquire the process-wide slot.
        assert (await client._call_bypass_html(server.url("cold2.test"), use_proxy=False))[0] is not None
        await client.close()


@pytest.mark.asyncio
async def test_budget_does_not_charge_idle_time_spent_at_other_sites():
    async with FakeBypassServer(log=lambda _: None) as server:
        server.scenarios["plain.test"] = Scenario(direct_status=200, direct_body=b"ok", direct_delay=0.02)
        client = server.client(cf_bypass_url="", site_total_timeout=0.15)
        crawler = ProbeCrawler(client, server.url("plain.test").rstrip("/"))
        assert (await crawler.run(crawl_input())).data is not None
        await asyncio.sleep(0.2)
        assert (await crawler.run(crawl_input())).data is not None
        assert not crawler._site_failure
        await client.close()


@pytest.mark.asyncio
async def test_real_jav321_exhausts_normal_timeout_attempts_then_skips():
    from mdcx.crawlers.jav321 import Jav321Crawler

    async with FakeBypassServer(log=lambda _: None) as server:
        server.scenarios["plain.test"] = Scenario(direct_status=200, direct_body=b"ok", direct_delay=0.2)
        logs = []
        client = server.client(cf_bypass_url="", retry=5, timeout=0.03, log_fn=logs.append)
        client._calc_retry_sleep_seconds = lambda *args, **kwargs: 0
        crawler = Jav321Crawler(client, server.url("plain.test").rstrip("/"))
        assert (await crawler.run(crawl_input("SSNI-126"))).data is None
        assert server.count("direct", "plain.test") == 5
        assert any("(5/5)" in line for line in logs)
        await crawler.run(crawl_input("PDVIJ-001"))
        await crawler.run(crawl_input("PRED-110"))
        assert server.count("direct", "plain.test") == 15
        await crawler.run(crawl_input("IPX-001"))
        assert server.count("direct", "plain.test") == 15
        assert sum("跳过后续番号" in line for line in logs) == 1
        await client.close()
