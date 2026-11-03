from pathlib import Path
from types import SimpleNamespace

import pytest

from mdcx.config.enums import FixedScrapingType, Language, Website
from mdcx.config.models import Config, FieldConfig, FieldPriorityConfig
from mdcx.core.file_crawler import FileScraper
from mdcx.core.scrape_quality import QualityDeferred, QualityHeld, missing_required
from mdcx.crawlers.base import BaseCrawler, CrawlerData
from mdcx.crawlers.dmm_new import DmmCrawler
from mdcx.crawlers.javbus import JavbusCrawler
from mdcx.gen.field_enums import CrawlerResultFields
from mdcx.models.crawl_status import CrawlStatus, observe_http, record_parse_error, record_request_error
from mdcx.models.enums import FileMode
from mdcx.models.flags import Flags
from mdcx.models.types import CrawlerResult, CrawlTask, FileInfo


class Client:
    site_total_timeout = 5

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    @observe_http(cache=True)
    async def get_text(self, url, **kwargs):
        self.calls.append(url)
        return next(self.responses)

    @observe_http(cache=True)
    async def post_json(self, url, **kwargs):
        self.calls.append(url)
        return next(self.responses)

    def _log(self, _message):
        pass


def task():
    value = CrawlTask.empty()
    value.number = "TEST-001"
    value.mosaic = "有码"
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["HTTP 403", "HTTP 429", "HTTP 503", "connection timeout", "mirror /html failed"])
async def test_javbus_fallback_keeps_actual_request_error(error):
    client = Client(
        [(None, "HTTP 404"), ('<a class="movie-box" href="https://example.test/TEST-001_1"/>', ""), (None, error)]
    )
    result = await JavbusCrawler(client).run(task())
    assert result.report.has_error
    assert any(i.status == CrawlStatus.REQUEST_ERROR and error in i.reason for i in result.report.issues)


@pytest.mark.asyncio
async def test_javbus_normal_no_result_and_missing_fields():
    absent = await JavbusCrawler(Client([(None, "HTTP 404"), ("<html>empty search</html>", "")])).run(task())
    assert not absent.report.has_error
    assert set(absent.report.missing.values()) == {CrawlStatus.NOT_FOUND}
    detail = await JavbusCrawler(Client([("<h3>TEST-001 sample</h3>", "")])).run(task())
    assert detail.data.title
    assert not detail.report.has_error
    assert detail.report.missing["actors"] == CrawlStatus.MISSING
    assert detail.report.missing["thumb"] == CrawlStatus.MISSING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,error,expected",
    [
        (None, "HTTP 503", CrawlStatus.REQUEST_ERROR),
        ({"data": {"ppvContent": None}}, "", CrawlStatus.NOT_FOUND),
        ({"data": []}, "", CrawlStatus.PARSE_ERROR),
    ],
)
async def test_dmm_request_empty_and_parse_are_distinct(payload, error, expected):
    class DigitalOnly(DmmCrawler):
        async def _run(self, ctx):
            data = await self.fetch_digital(ctx, "https://video.dmm.co.jp/av/content/?id=test00001")
            return data.to_result()

        async def _http_request_with_retry(self, *args, **kwargs):
            return payload, error

    result = await DigitalOnly(Client([])).run(task())
    assert any(i.status == expected for i in result.report.issues)
    assert result.report.has_error == (expected != CrawlStatus.NOT_FOUND)


class QualityCrawler(BaseCrawler):
    def __init__(self, site, actions, records):
        super().__init__(Client([]))
        self._site = site
        self.actions = iter(actions)
        self.records = records

    def site(self):
        return self._site

    @classmethod
    def base_url_(cls):
        return "https://example.test"

    async def _run(self, ctx):
        self.records.append((self._site, ctx.input.number))
        action = next(self.actions)
        if action == "error":
            record_request_error("HTTP 503")
            return None
        if action == "parse":
            record_parse_error("invalid JSON")
            return None
        data = CrawlerResult.empty()
        data.title = "sample"
        data.source = self.site().value
        if action == "full":
            data.score, data.score_max = "4.5", "5"
            data.outline, data.actors, data.thumb = "sample outline", ["sample actor"], "https://example.test/cover.jpg"
        return data

    async def _generate_search_url(self, ctx): ...
    async def _parse_search_page(self, *args): ...
    async def _parse_detail_page(self, *args): ...


def provider(primary_actions, secondary_actions):
    records = []
    crawlers = {
        Website.DMM: QualityCrawler(Website.DMM, primary_actions, records),
        Website.JAVDB: QualityCrawler(Website.JAVDB, secondary_actions, records),
    }

    async def get(site):
        return crawlers[site]

    return SimpleNamespace(get=get, crawlers=crawlers), records


def configure(monkeypatch):
    config = Config()
    config.fixed_scraping_type = FixedScrapingType.YOUMA
    config.website_youma = [Website.DMM, Website.JAVDB]
    monkeypatch.setattr(
        Config,
        "get_field_config",
        lambda self, field: FieldConfig(language=Language.JP, site_prority=[Website.DMM, Website.JAVDB]),
    )
    monkeypatch.setattr(
        Config, "get_type_field_config", lambda *args: FieldPriorityConfig(site_prority=[Website.DMM, Website.JAVDB])
    )
    return config


@pytest.mark.asyncio
@pytest.mark.parametrize("second,expected", [("full", "dmm"), ("error", "javdb")])
async def test_retry_uses_primary_recovery_or_cached_secondary(monkeypatch, second, expected):
    config = configure(monkeypatch)
    source, records = provider(["error", second], ["full"])
    scraper = FileScraper(config, source)
    assert (await scraper.run(task(), FileMode.Default)).field_sources[CrawlerResultFields.SCORE] == "javdb"
    final = await scraper.run(task(), FileMode.Default)
    assert final.field_sources[CrawlerResultFields.SCORE] == expected
    assert not missing_required(scraper.raw_result, FixedScrapingType.YOUMA)
    assert records.count((Website.DMM, "TEST-001")) == 2
    assert records.count((Website.JAVDB, "TEST-001")) == 1


@pytest.mark.asyncio
async def test_successful_decoded_requests_reused_but_errors_not_cached():
    client = Client([("ok", ""), (None, "HTTP 503"), ("recovered", "")])

    class Partial(QualityCrawler):
        async def _run(self, ctx):
            await self.async_client.get_text("https://example.test/good")
            await self.async_client.get_text("https://example.test/flaky")
            return CrawlerData(title="sample").to_result()

    crawler = Partial(Website.DMM, [], [])
    crawler.async_client = client
    value = task()
    value.request_cache = {}
    assert (await crawler.run(value)).report.has_error
    assert not (await crawler.run(value)).report.has_error
    assert client.calls == ["https://example.test/good", "https://example.test/flaky", "https://example.test/flaky"]


@pytest.mark.asyncio
async def test_quality_gate_precedes_every_output_and_leaves_file(monkeypatch, tmp_path):
    from mdcx.core import scraper as module

    configure(monkeypatch)
    source, records = provider(["error", "error"], ["empty"])
    Flags.reset()
    path = tmp_path / "TEST-001.mp4"
    path.write_bytes(b"untouched")
    info = FileInfo.empty()
    info.number, info.mosaic = "TEST-001", "有码"
    info.file_path, info.folder_path = path, tmp_path
    monkeypatch.setattr(module.manager.config, "main_mode", 1)
    monkeypatch.setattr(module.manager.config, "fixed_scraping_type", FixedScrapingType.YOUMA)
    monkeypatch.setattr(module.manager.config, "website_youma", [Website.DMM, Website.JAVDB])

    async def check(*args):
        return True

    monkeypatch.setattr(module, "check_file", check)
    monkeypatch.setattr(
        module,
        "get_movie_path_setting",
        lambda *args: SimpleNamespace(success_folder=tmp_path / "output", movie_path=tmp_path),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("quality gate allowed an output side effect")

    for name in [
        "show_result",
        "translate_title_outline",
        "get_output_name",
        "creat_folder",
        "write_nfo",
        "poster_download",
        "thumb_download",
        "fanart_download",
        "move_movie",
    ]:
        monkeypatch.setattr(module, name, forbidden)
    scraper = module.Scraper(source)
    with pytest.raises(QualityDeferred):
        await scraper._process_one_file(info, FileMode.Default)
    scraper._quality_retrying = True
    with pytest.raises(QualityHeld):
        await scraper._process_one_file(info, FileMode.Default)
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == b"untouched"
    assert len(records) == 3
    assert "留原地" in scraper._quality_summaries[0]
    assert "dmm: 请求错误" in scraper._quality_summaries[0]


@pytest.mark.parametrize(
    "kind,required",
    [
        (FixedScrapingType.YOUMA, 4),
        (FixedScrapingType.WUMA, 2),
        (FixedScrapingType.SUREN, 1),
        (FixedScrapingType.FC2, 1),
    ],
)
def test_required_fields_use_raw_real_actor_names(kind, required):
    assert len(missing_required(CrawlerResult.empty(), kind)) == required
    result = CrawlerResult.empty()
    result.actors = [" "]
    assert "actors" in missing_required(result, FixedScrapingType.WUMA)


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    from mdcx.core import scraper as module

    configure(monkeypatch)
    Flags.reset()
    module.signal.stop = False
    monkeypatch.setattr(module.manager.config, "main_mode", 1)
    monkeypatch.setattr(module.manager.config, "fixed_scraping_type", FixedScrapingType.YOUMA)
    monkeypatch.setattr(module.manager.config, "website_youma", [Website.DMM, Website.JAVDB])

    async def check(*args):
        return True

    monkeypatch.setattr(module, "check_file", check)
    monkeypatch.setattr(
        module,
        "get_movie_path_setting",
        lambda *args: SimpleNamespace(success_folder=tmp_path / "output", movie_path=tmp_path),
    )
    info = FileInfo.empty()
    info.number, info.mosaic = "TEST-001", "有码"
    info.file_path, info.folder_path = tmp_path / "TEST-001.mp4", tmp_path
    return module, info


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary,secondary,stop,expected",
    [
        (["error", "full"], ["full"], False, "dmm"),
        (["error", "error"], ["full"], False, "javdb"),
        (["empty"], ["empty"], False, ""),
        (["error"], ["full"], True, "javdb"),
    ],
)
async def test_gate_accepts_recovery_fallback_normal_absence_and_stopped_site(
    gate_env, monkeypatch, primary, secondary, stop, expected
):
    module, info = gate_env
    source, records = provider(primary, secondary)
    scraper = module.Scraper(source)
    accepted = []

    class Accepted(Exception):
        pass

    def accept(result, *args):
        accepted.append(result)
        raise Accepted

    monkeypatch.setattr(module, "show_result", accept)
    if primary[0] == "error":
        with pytest.raises(QualityDeferred):
            await scraper._process_one_file(info, FileMode.Default)
        if stop:
            source.crawlers[Website.DMM]._site_failure = "three failures"
        scraper._quality_retrying = True
    with pytest.raises(Accepted):
        await scraper._process_one_file(info, FileMode.Default)
    assert accepted[0].field_sources[CrawlerResultFields.SCORE] == expected
    if stop:
        assert len(records) == 2
        assert "不重刮" in scraper._quality_summaries[0]
    if primary[0] == "empty":
        assert not scraper._quality_pending
    for line in scraper._quality_summaries:
        print(line)


@pytest.mark.asyncio
async def test_already_stopped_site_is_not_retried_and_missing_stays(gate_env):
    module, info = gate_env
    source, records = provider([], ["empty"])
    source.crawlers[Website.DMM]._site_failure = "three failures"
    scraper = module.Scraper(source)
    with pytest.raises(QualityHeld):
        await scraper._process_one_file(info, FileMode.Default)
    assert records == [(Website.JAVDB, "TEST-001")]
    assert not scraper._quality_pending


@pytest.mark.asyncio
async def test_batch_finishes_first_pass_before_single_retry(monkeypatch):
    from mdcx.config.extend import MoviePathSetting
    from mdcx.core import scraper as module

    files = [Path("TEST-001.mp4"), Path("TEST-002.mp4")]
    events = []

    async def process(self, item):
        path, _, _ = item
        events.append((path, self._quality_retrying))
        if path == files[0] and not self._quality_retrying:
            self._quality_pending[path] = object()
        else:
            Flags.scrape_done += 1

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setattr(module.Scraper, "process_one_file", process)
    monkeypatch.setattr(module, "save_success_list", noop)
    monkeypatch.setattr(module, "_clean_empty_fodlers", noop)
    monkeypatch.setattr(
        module,
        "get_movie_path_setting",
        lambda *args, **kwargs: MoviePathSetting(
            movie_path=Path("."),
            movie_paths=[Path(".")],
            success_folder=Path("."),
            failed_folder=Path("."),
            ignore_dirs=[],
            extrafanart_folder=Path("."),
            softlink_path=Path("."),
        ),
    )
    for field, value in {
        "thread_number": 1,
        "thread_time": 0,
        "main_mode": 1,
        "switch_on": [],
        "scrape_softlink_path": False,
        "emby_on": [],
        "actor_photo_kodi_auto": False,
        "garbage_enabled": False,
    }.items():
        monkeypatch.setattr(module.manager.config, field, value)
    module.signal.stop = False
    await module.Scraper(object())._run(FileMode.Default, files)
    assert events == [(files[0], False), (files[1], False), (files[0], True)]
    assert Flags.scrape_done == 2


@pytest.mark.asyncio
async def test_quality_retry_waits_for_existing_pause_and_respects_stop(monkeypatch):
    from mdcx.core.scrape_quality import retryable_sites
    from mdcx.web_async import AsyncWebClient
    from tests.crawlers.test_anti_ban_step2 import DummyJavCrawler
    from tests.crawlers.test_javlibrary_429_backoff import FakeResponse

    config = configure(monkeypatch)
    config.website_youma = [Website.JAVLIBRARY]
    monkeypatch.setattr(
        Config, "get_field_config", lambda *args: FieldConfig(language=Language.JP, site_prority=[Website.JAVLIBRARY])
    )
    monkeypatch.setattr(
        Config, "get_type_field_config", lambda *args: FieldPriorityConfig(site_prority=[Website.JAVLIBRARY])
    )
    client = AsyncWebClient(
        timeout=10, cf_bypass_url="", site_request_intervals={"javlibrary": 5}, log_fn=lambda _: None
    )
    now = 1000.0
    times = []
    client._time_fn = lambda: now

    async def sleep(delay):
        nonlocal now
        now += delay

    async def transport(**kwargs):
        times.append(now)
        return FakeResponse(
            status_code=403 if len(times) <= 2 else 200,
            content=b"Forbidden" if len(times) <= 2 else b"ok",
            url=str(kwargs["url"]),
        )

    client._sleep = sleep
    client._curl_request = transport
    crawler = DummyJavCrawler(client)

    async def get(_site):
        return crawler

    scraper = FileScraper(config, SimpleNamespace(get=get))
    try:
        await scraper.run(task(), FileMode.Default)
        assert scraper.reports[0].has_error
        pause_end = client._site_ban_states["javlibrary"].pause_until
        assert await retryable_sites(scraper) == ["javlibrary"]
        result = await scraper.run(task(), FileMode.Default)
        assert result.title
        assert times[-1] >= pause_end
        assert all(b - a >= 5 for a, b in zip(times, times[1:], strict=False))
        crawler._site_failure = "stopped by existing rules"
        # Preserve the first error report to simulate another movie stopping the site before batch-end.
        from mdcx.models.crawl_status import CrawlIssue, SiteReport

        scraper.reports = [SiteReport("javlibrary", [CrawlIssue(CrawlStatus.REQUEST_ERROR, "HTTP 403")])]
        assert await retryable_sites(scraper) == []
        assert len(times) == 3
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_parse_error_invalidates_successful_download_cache(monkeypatch):
    config = configure(monkeypatch)
    client = Client([("bad", ""), ("good", "")])

    class Parsing(QualityCrawler):
        async def _run(self, ctx):
            body, _ = await self.async_client.get_text("https://example.test/detail")
            if body == "bad":
                record_parse_error("malformed detail")
            result = CrawlerResult.empty()
            result.title = body
            return result

    crawler = Parsing(Website.DMM, [], [])
    crawler.async_client = client
    secondary = QualityCrawler(Website.JAVDB, ["empty"], [])

    async def get(site):
        return crawler if site == Website.DMM else secondary

    scraper = FileScraper(config, SimpleNamespace(get=get))
    await scraper.run(task(), FileMode.Default)
    assert any(r.has_error for r in scraper.reports)
    await scraper.run(task(), FileMode.Default)
    assert len(client.calls) == 2
    assert not any(r.has_error for r in scraper.reports)


@pytest.mark.asyncio
async def test_batch_rest_is_consumed_before_deferred_retry(gate_env, monkeypatch):
    from mdcx.config.enums import Switch
    from mdcx.models.types import CrawlersResult, OtherInfo

    module, info = gate_env
    now = 1000.0
    calls = []

    async def sleep(seconds):
        nonlocal now
        now += seconds

    async def get_info(*args):
        return info

    async def process(self, *args):
        calls.append((self._quality_retrying, now))
        if not self._quality_retrying:
            self._quality_pending[info.file_path] = object()
            raise QualityDeferred
        data = CrawlersResult.empty()
        data.number = info.number
        return data, OtherInfo.empty()

    monkeypatch.setattr(module, "get_file_info_v2", get_info)
    monkeypatch.setattr(module.Scraper, "_process_one_file", process)
    monkeypatch.setattr(module.time, "time", lambda: now)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    for field, value in {
        "garbage_enabled": False,
        "thread_time": 0,
        "switch_on": [Switch.REST_SCRAPE],
        "rest_count": 1,
    }.items():
        monkeypatch.setattr(module.manager.config, field, value)
    monkeypatch.setattr(Flags, "rest_time_convert", 2)
    Flags.total_count = 1
    Flags.remain_list = [info.file_path]
    scraper = module.Scraper(object())
    await scraper.process_one_file((info.file_path, 1, 1))
    scraper._quality_retrying = True
    await scraper.process_one_file((info.file_path, 1, 1))
    assert calls == [(False, 1000.0), (True, 1002.0)]
    assert Flags.scrape_done == 1
    assert Flags.succ_count == 1
    assert Flags.fail_count == 0
    assert Flags.remain_list == []


@pytest.mark.asyncio
async def test_display_progress_counts_first_pass_outcomes_without_counting_retry(gate_env, monkeypatch):
    from unittest.mock import Mock

    from mdcx.config.enums import Switch
    from mdcx.models.types import CrawlersResult, OtherInfo

    module, info = gate_env
    progress, labels, logs = Mock(), Mock(), Mock()
    monkeypatch.setattr(module.signal, "exec_set_processbar", SimpleNamespace(emit=progress))
    monkeypatch.setattr(module.signal, "set_label_file_path", SimpleNamespace(emit=labels))
    monkeypatch.setattr(module.signal, "show_log_text", logs)
    monkeypatch.setattr(module, "get_movie_path_setting", lambda *args: SimpleNamespace(failed_folder=info.folder_path))

    async def get_info(*args):
        return info

    outcomes = iter(["success", "failed", "deferred", "success"])
    seen_at_start = []

    async def process(self, *args):
        seen_at_start.append((Flags.scrape_processed, progress.call_args.args[0]))
        outcome = next(outcomes)
        if outcome == "deferred":
            self._quality_pending[info.file_path] = object()
            raise QualityDeferred
        if outcome == "failed":
            return None, None
        data = CrawlersResult.empty()
        data.number = info.number
        return data, OtherInfo.empty()

    async def stay(_folder, path, _old):
        return path

    monkeypatch.setattr(module, "get_file_info_v2", get_info)
    monkeypatch.setattr(module, "move_file_to_failed_folder", stay)
    monkeypatch.setattr(module.Scraper, "_process_one_file", process)
    for field, value in {
        "garbage_enabled": False,
        "thread_time": 0,
        "switch_on": [Switch.REST_SCRAPE],
        "rest_count": 100,
    }.items():
        monkeypatch.setattr(module.manager.config, field, value)
    Flags.total_count = 3
    Flags.remain_list = [info.file_path] * 3
    scraper = module.Scraper(object())
    for index in range(3):
        await scraper.process_one_file((info.file_path, index + 1, 3))
    assert Flags.scrape_processed == 3
    assert Flags.scrape_done == 2
    scraper._quality_retrying = True
    await scraper.process_one_file((info.file_path, 1, 3))
    assert seen_at_start == [(0, 0), (1, 33), (2, 66), (3, 100)]
    assert [c.args[0] for c in progress.call_args_list] == [0, 33, 33, 66, 66, 100, 100, 100]
    assert Flags.scrape_processed == Flags.scrape_done == 3
    assert Flags.succ_count == 2 and Flags.fail_count == 1
    assert "重刮 1/1" in labels.call_args.args[0]
    assert any("重刮 0/1" in c.args[0] for c in labels.call_args_list)
    cumulative = [c.args[0] for c in logs.call_args_list if "已累计刮削" in c.args[0]]
    assert len(cumulative) == 4
    assert all("/3，" in line or "/3 · 重刮" in line for line in cumulative)
    assert Flags.remain_list == []
    Flags.reset()
    assert Flags.scrape_processed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "not_found", "error", "parse"])
async def test_field_log_normal_absence_is_plain_but_errors_remain_red(monkeypatch, failure):
    from mdcx.crawlers.base import CrawlerNotFound

    config = configure(monkeypatch)
    source, _ = provider([failure], ["full"])
    if failure == "empty":

        async def absent(ctx):
            return None

        source.crawlers[Website.DMM]._run = absent
    elif failure == "not_found":

        async def absent(ctx):
            raise CrawlerNotFound("不在官网番号前缀列表中")

        source.crawlers[Website.DMM]._run = absent
    result = await FileScraper(config, source).run(task(), FileMode.Default)
    lines = [line for line in result.field_log.splitlines() if "dmm" in line and "失败:" in line]
    assert lines
    assert all(("🔴" if failure in {"error", "parse"} else "⚪") in line for line in lines)
    skipped = [line for line in result.field_log.splitlines() if "已失败, 跳过" in line]
    assert skipped and all("⚪" in line and "🔴" not in line for line in skipped)
    assert all(
        "🔴" not in line for line in result.field_log.splitlines() if "未找到" in line or "所有来源均无数据" in line
    )


@pytest.mark.asyncio
async def test_batch_parse_observation_counts_actual_calls_not_cached_reports(monkeypatch):
    from mdcx.models.crawl_status import BatchDiagnostics, batch_diagnostics

    config = configure(monkeypatch)
    source, _ = provider(["parse", "full"], ["full"])
    scraper = FileScraper(config, source)
    batch = BatchDiagnostics()
    token = batch_diagnostics.set(batch)
    try:
        await scraper.run(task(), FileMode.Default)
        await scraper.run(task(), FileMode.Default)
        assert batch.parse_errors["dmm"] == 1
        assert batch.parse_errors["javdb"] == 0
    finally:
        batch_diagnostics.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary,secondary,expected", [("empty", "empty", 1), ("empty", "full", 0), ("parse", "full", 0)]
)
async def test_batch_normal_absence_uses_merged_required_fields(gate_env, monkeypatch, primary, secondary, expected):
    from mdcx.models.crawl_status import BatchDiagnostics, batch_diagnostics

    module, info = gate_env
    source, _ = provider([primary], [secondary])
    scraper = module.Scraper(source)
    batch = BatchDiagnostics()

    class Accepted(Exception):
        pass

    def accept(*args):
        raise Accepted

    monkeypatch.setattr(module, "show_result", accept)
    token = batch_diagnostics.set(batch)
    try:
        with pytest.raises((Accepted, QualityDeferred)):
            await scraper._process_one_file(info, FileMode.Default)
        assert len(batch.missing_movies - batch.abnormal_movies) == expected
    finally:
        batch_diagnostics.reset(token)
