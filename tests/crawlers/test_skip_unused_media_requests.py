import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree

import mdcx.base.web as base_web
import mdcx.core.web as core_web
import mdcx.crawlers.avbase_new as avbase_module
import mdcx.crawlers.dmm_new as dmm_module
from mdcx.config.enums import DownloadableFile, FixedScrapingType, Language, NfoInclude
from mdcx.config.manager import manager
from mdcx.core.scrape_quality import missing_required
from mdcx.crawlers import fc2, fc2hub, mgstage
from mdcx.crawlers.avbase_new import AvbaseCrawler
from mdcx.crawlers.base.types import CrawlerData
from mdcx.crawlers.dmm_new import Category, DMMContext, DmmCrawler
from mdcx.crawlers.dmm_new.parsers import MediaVariant, MonoParser
from mdcx.models.types import CrawlerInput, CrawlersResult
from mdcx.web_async import AsyncWebClient


def _disable_all_media_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager.config, "download_files", [DownloadableFile.THEME_VIDEOS])
    monkeypatch.setattr(manager.config, "keep_files", [])
    monkeypatch.setattr(manager.config, "nfo_include_new", [])


@pytest.mark.asyncio
async def test_a_dmm_trailer_conversion_validation_skipped_when_unused_and_sent_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    head_calls: list[tuple[str, str]] = []

    class DummyClient:
        async def request(self, method: str, url: str, **kwargs):
            head_calls.append((method, url))
            return SimpleNamespace(status_code=200, url=url, headers={}), ""

    @contextlib.asynccontextmanager
    async def fake_acquire():
        yield SimpleNamespace(async_client=DummyClient())

    monkeypatch.setattr(manager, "acquire_computed", fake_acquire)
    raw_url = "https://cc3001.dmm.co.jp/pv/tempkey/ssis00090_sm_w.mp4"
    expected_converted = "https://cc3001.dmm.co.jp/litevideo/freepv/s/ssi/ssis00090/ssis00090_sm_w.mp4"

    # 1. 下载项与 NFO 均未启用 trailer：不发转换校验请求
    _disable_all_media_outputs(monkeypatch)
    res_unused = await base_web.get_dmm_trailer(raw_url)
    assert res_unused == raw_url
    assert head_calls == []

    result_obj = CrawlersResult.empty()
    result_obj.trailer = raw_url
    await core_web.trailer_download(result_obj, tmp_path, tmp_path, "TEST-001")
    assert result_obj.trailer == raw_url
    assert head_calls == []

    # 2. 勾选下载 trailer：按原逻辑发送转换校验 HEAD 请求
    monkeypatch.setattr(manager.config, "download_files", [DownloadableFile.TRAILER])
    res_enabled = await base_web.get_dmm_trailer(raw_url)
    assert res_enabled == expected_converted
    assert head_calls == [("HEAD", expected_converted)]

    # 3. 未勾选下载 trailer，但启用 NFO 且包含 trailer 字段：同样发送转换校验请求
    head_calls.clear()
    monkeypatch.setattr(manager.config, "download_files", [DownloadableFile.NFO])
    monkeypatch.setattr(manager.config, "nfo_include_new", [NfoInclude.TRAILER])
    res_nfo = await base_web.get_dmm_trailer(raw_url)
    assert res_nfo == expected_converted
    assert head_calls == [("HEAD", expected_converted)]


@pytest.mark.asyncio
async def test_b_dmm_and_avbase_cover_validation_skipped_when_unused_preserving_order_and_required(
    monkeypatch: pytest.MonkeyPatch,
):
    dmm_calls: list[str] = []
    avbase_calls: list[str] = []

    async def fake_dmm_check_url(url: str, length: bool = False, real_url: bool = False):
        dmm_calls.append(url)
        return url

    async def fake_avbase_check_url(url: str, length: bool = False, real_url: bool = False):
        avbase_calls.append(url)
        return url

    monkeypatch.setattr(dmm_module, "check_url", fake_dmm_check_url)
    monkeypatch.setattr(avbase_module, "check_url", fake_avbase_check_url)

    # 1. 下载项与 NFO 均未启用 poster/thumb：不发校验请求，保持原有候选排序且不改变 missing_required
    _disable_all_media_outputs(monkeypatch)
    dmm_crawler = DmmCrawler(client=None)
    ctx = DMMContext(input=CrawlerInput.empty())
    ctx.number_00 = "pred00816"
    ctx.number_no_00 = "pred816"

    dvd_url = "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=pred816/?i3_ref=search&i3_ord=2"
    bluray_url = "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=9pred816/?i3_ref=search&i3_ord=1"
    dvd_key = DmmCrawler._canonicalize_detail_url(dvd_url)
    bluray_key = DmmCrawler._canonicalize_detail_url(bluray_url)
    ctx.detail_media_variants = {
        dvd_key: MediaVariant.DVD,
        bluray_key: MediaVariant.BLURAY,
    }

    bluray_item = await dmm_crawler._sanitize_candidate_images(
        ctx,
        Category.MONO,
        bluray_url,
        CrawlerData(
            title="SAMPLE TITLE",
            score="4.5",
            score_max="5",
            outline="sample outline",
            actors=["Actor A"],
            thumb="https://pics.dmm.co.jp/mono/movie/adult/9pred816/9pred816pl.jpg",
            external_id=bluray_url,
        ),
    )
    dvd_item = await dmm_crawler._sanitize_candidate_images(
        ctx,
        Category.MONO,
        dvd_url,
        CrawlerData(
            title="SAMPLE TITLE",
            score="4.5",
            score_max="5",
            outline="sample outline",
            actors=["Actor A"],
            thumb="https://pics.dmm.co.jp/mono/movie/adult/pred816/pred816pl.jpg",
            external_id=dvd_url,
        ),
    )
    preferred = DmmCrawler._pick_preferred_image_candidate(
        ctx,
        [
            (Category.MONO, bluray_url, bluray_item),
            (Category.MONO, dvd_url, dvd_item),
        ],
        {bluray_key: 0, dvd_key: 1},
    )
    assert preferred is not None
    _, preferred_variant, preferred_data = preferred
    assert preferred_variant == MediaVariant.DVD
    assert preferred_data.thumb == "https://pics.dmm.co.jp/mono/movie/adult/pred816/pred816pl.jpg"

    finalized = await dmm_crawler._finalize_result_images(
        ctx,
        preferred_data,
        label="最终图片",
        validate_thumb=True,
    )
    assert finalized.thumb == "https://pics.dmm.co.jp/mono/movie/adult/pred816/pred816pl.jpg"
    assert finalized.poster == "https://pics.dmm.co.jp/mono/movie/adult/pred816/pred816ps.jpg"
    assert dmm_calls == []
    assert missing_required(finalized.to_result(), FixedScrapingType.YOUMA) == []

    avbase_crawler = AvbaseCrawler(client=None)
    avbase_ctx = avbase_crawler.new_context(CrawlerInput.empty())
    avbase_res = CrawlerData(
        title="SAMPLE TITLE",
        score="4.5",
        score_max="5",
        outline="sample outline",
        actors=["Actor A"],
        thumb="https://pics.dmm.co.jp/mono/movie/adult/pred816/pred816pl.jpg",
    ).to_result()
    avbase_processed = await avbase_crawler.post_process(avbase_ctx, avbase_res)
    assert avbase_processed.thumb == "https://pics.dmm.co.jp/mono/movie/adult/pred816/pred816pl.jpg"
    assert avbase_processed.poster == "https://pics.dmm.co.jp/mono/movie/adult/pred816/pred816ps.jpg"
    assert avbase_calls == []
    assert missing_required(avbase_processed, FixedScrapingType.YOUMA) == []

    # 2. 勾选下载 poster 与 thumb：仍按原逻辑发送候选校验请求
    monkeypatch.setattr(manager.config, "download_files", [DownloadableFile.POSTER, DownloadableFile.THUMB])
    await dmm_crawler._sanitize_candidate_images(
        ctx,
        Category.MONO,
        dvd_url,
        CrawlerData(
            thumb="https://pics.dmm.co.jp/mono/movie/adult/pred816/pred816pl.jpg",
            external_id=dvd_url,
        ),
    )
    await dmm_crawler._finalize_result_images(
        ctx,
        CrawlerData(thumb="https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/pred816/pred816pl.jpg"),
        label="最终图片",
        validate_thumb=False,
    )
    assert dmm_calls == [
        "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/pred816/pred816pl.jpg",
        "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/pred816/pred816ps.jpg",
    ]

    avbase_res_enabled = CrawlerData(
        title="SAMPLE TITLE",
        thumb="https://pics.dmm.co.jp/mono/movie/adult/pred816/pred816pl.jpg",
    ).to_result()
    await avbase_crawler.post_process(avbase_ctx, avbase_res_enabled)
    assert avbase_calls == [
        "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/pred816/pred816pl.jpg",
        "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/pred816/pred816ps.jpg",
    ]


@pytest.mark.asyncio
async def test_c_mgstage_fc2_fc2hub_sample_requests_skipped_when_unused_and_sent_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    mgstage_calls: list[str] = []
    fc2_calls: list[str] = []
    fc2hub_calls: list[str] = []

    class FakeMgstageClient:
        async def get_json(self, url: str, **kwargs):
            mgstage_calls.append(url)
            return {"url": "https://sample.mgstage.test/video/abc.ism/request?key=1"}, ""

    class FakeFc2SampleClient:
        def __init__(self, sink: list[str]):
            self._sink = sink

        async def get_text(self, url: str, **kwargs):
            self._sink.append(url)
            return '{"path":"https://sample.fc2.test/trailer.mp4?mid=1"}', ""

    mgstage_html = etree.fromstring(
        '<html><body><a class="review-btn" href="https://www.mgstage.com/mypage/review.php?pid=123"></a></body></html>',
        etree.HTMLParser(),
    )
    fc2hub_html = etree.fromstring(
        '<html><body><div class="player-api" data-id="4866909"></div></body></html>',
        etree.HTMLParser(),
    )

    # 1. 下载项与 NFO 均未启用 trailer：三个爬虫的样本接口请求次数均为 0
    _disable_all_media_outputs(monkeypatch)
    assert await mgstage.get_trailer(FakeMgstageClient(), mgstage_html) == ""
    assert await fc2.getTrailer(FakeFc2SampleClient(fc2_calls), "1723984") == ""
    assert await fc2hub.getTrailer(FakeFc2SampleClient(fc2hub_calls), fc2hub_html, "1723984") == ""
    assert mgstage_calls == []
    assert fc2_calls == []
    assert fc2hub_calls == []

    # 2. 勾选下载 trailer：三个爬虫均按原逻辑请求样本接口
    monkeypatch.setattr(manager.config, "download_files", [DownloadableFile.TRAILER])
    assert await mgstage.get_trailer(FakeMgstageClient(), mgstage_html) == "https://sample.mgstage.test/video/abc.mp4"
    assert (
        await fc2.getTrailer(FakeFc2SampleClient(fc2_calls), "1723984") == "https://sample.fc2.test/trailer.mp4?mid=1"
    )
    assert (
        await fc2hub.getTrailer(FakeFc2SampleClient(fc2hub_calls), fc2hub_html, "1723984")
        == "https://sample.fc2.test/trailer.mp4?mid=1"
    )
    assert mgstage_calls == ["https://www.mgstage.com/sampleplayer/sampleRespons.php?pid=123"]
    assert fc2_calls == ["https://adult.contents.fc2.com/api/v2/videos/1723984/sample"]
    assert fc2hub_calls == ["https://adult.contents.fc2.com/api/v2/videos/4866909/sample"]


@pytest.mark.asyncio
async def test_d_dmm_trailer_supplement_pages_skipped_when_unused_and_sent_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    fanza_http_calls: list[tuple[str, str]] = []
    mono_detail_calls: list[str] = []

    crawler = DmmCrawler(client=AsyncWebClient(timeout=1))
    ctx = DMMContext(
        input=CrawlerInput(
            appoint_number="",
            appoint_url="",
            file_path=None,
            mosaic="",
            number="SSIS-001",
            short_number="SSIS-001",
            language=Language.UNDEFINED,
            org_language=Language.UNDEFINED,
        )
    )

    async def fake_http_request_with_retry(method: str, url: str, **kwargs):
        fanza_http_calls.append((method, url))
        if url == "https://api.tv.dmm.co.jp/graphql":
            return (
                {
                    "data": {
                        "fanzaTvPlus": {
                            "content": {
                                "title": "FANZA TV TITLE",
                                "description": "desc",
                                "startDeliveryAt": "2026-01-01T00:00:00Z",
                                "genres": [],
                                "playInfo": {"duration": 3600},
                                "actresses": [],
                                "packageImage": "https://pics.dmm.co.jp/digital/video/ssis00001/ssis00001ps.jpg",
                                "packageLargeImage": "https://pics.dmm.co.jp/digital/video/ssis00001/ssis00001pl.jpg",
                                "samplePictures": [],
                                "sampleMovie": {
                                    "url": "https://cc3001.dmm.co.jp/hlsvideo/freepv/s/ssi/ssis00001/playlist.m3u8",
                                    "thumbnail": "",
                                },
                                "reviewSummary": {"averagePoint": 4.0, "reviewerCount": 10},
                                "series": None,
                                "directors": [],
                                "maker": None,
                                "label": None,
                            }
                        }
                    }
                },
                "",
            )
        if url == "https://www.dmm.co.jp/litevideo/-/detail/=/cid=ssis00001/":
            return (
                '<iframe src="https://www.dmm.co.jp/service/digitalapi/-/html5_player/=/cid=ssis00001/"></iframe>',
                "",
            )
        if url == "https://www.dmm.co.jp/service/digitalapi/-/html5_player/=/cid=ssis00001/":
            return '{"src":"\\/\\/cc3001.dmm.co.jp\\/pv\\/TOKEN\\/ssis00001hhb.mp4"}', ""
        return None, f"unexpected url: {url}"

    mono_detail_url = "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=ssis001/"
    mono_ajax_url = "https://www.dmm.co.jp/mono/dvd/-/detail/ajax-movie/=/cid=ssis001/"
    mono_player_url = "https://www.dmm.co.jp/service/digitalapi/-/html5_player/=/cid=ssis001/"

    async def fake_super_fetch_detail(self_obj, inner_ctx, url: str, use_browser=None):
        mono_detail_calls.append(url)
        if url == mono_detail_url:
            return (
                '<html><body><h1 id="title">MONO TITLE</h1>'
                '<a data-video-url="/mono/dvd/-/detail/ajax-movie/=/cid=ssis001/">sample</a>'
                "</body></html>",
                "",
            )
        if url == mono_ajax_url:
            return f'<iframe src="{mono_player_url}"></iframe>', ""
        if url == mono_player_url:
            return 'const args = {"src": "//cc3001.dmm.co.jp/litevideo/freepv/s/ssi/ssis001/ssis001_mhb_w.mp4"};', ""
        return None, f"unexpected url: {url}"

    monkeypatch.setattr(crawler, "_http_request_with_retry", fake_http_request_with_retry)
    monkeypatch.setattr(dmm_module.GenericBaseCrawler, "_fetch_detail", fake_super_fetch_detail)

    # 1. 下载项与 NFO 均未启用 trailer：
    # - fanza_tv 仅保留同响应顺手解析出的地址，不请求 litevideo 详情页与播放器页
    # - mono 不请求 ajax-movie 与播放器页；若同一页面含 gaEventVideoStart 则仍保留同页解析结果
    _disable_all_media_outputs(monkeypatch)
    fanza_res_unused = await crawler.fetch_fanza_tv(ctx, "https://tv.dmm.co.jp/list/?content=ssis00001")
    assert fanza_res_unused.trailer == "https://cc3001.dmm.co.jp/litevideo/freepv/s/ssi/ssis00001/ssis00001_sm_w.mp4"
    assert fanza_http_calls == [("POST", "https://api.tv.dmm.co.jp/graphql")]

    mono_res_unused = await crawler.fetch_and_parse(ctx, mono_detail_url, MonoParser())
    assert mono_res_unused.trailer == ""
    assert mono_detail_calls == [mono_detail_url]

    same_page_html = """
    <html><body>
    <script>gaEventVideoStart('{"video_url":"\\/\\/cc3001.dmm.co.jp\\/litevideo\\/freepv\\/s\\/ssi\\/ssis001\\/ssis001_sm_w.mp4"}')</script>
    </body></html>
    """
    assert (
        await crawler._fetch_mono_trailer(ctx, mono_detail_url, same_page_html)
        == "https://cc3001.dmm.co.jp/litevideo/freepv/s/ssi/ssis001/ssis001_sm_w.mp4"
    )
    assert mono_detail_calls == [mono_detail_url]

    # 2. 勾选下载 trailer：fanza_tv 与 mono 均按原逻辑发送补充页面请求（详情页和播放器页）
    fanza_http_calls.clear()
    mono_detail_calls.clear()
    monkeypatch.setattr(manager.config, "download_files", [DownloadableFile.TRAILER])

    fanza_res_enabled = await crawler.fetch_fanza_tv(ctx, "https://tv.dmm.co.jp/list/?content=ssis00001")
    assert fanza_res_enabled.trailer == "https://cc3001.dmm.co.jp/pv/TOKEN/ssis00001hhb.mp4"
    assert fanza_http_calls == [
        ("POST", "https://api.tv.dmm.co.jp/graphql"),
        ("GET", "https://www.dmm.co.jp/litevideo/-/detail/=/cid=ssis00001/"),
        ("GET", "https://www.dmm.co.jp/service/digitalapi/-/html5_player/=/cid=ssis00001/"),
    ]

    mono_res_enabled = await crawler.fetch_and_parse(ctx, mono_detail_url, MonoParser())
    assert mono_res_enabled.trailer == "https://cc3001.dmm.co.jp/litevideo/freepv/s/ssi/ssis001/ssis001_mhb_w.mp4"
    assert mono_detail_calls == [mono_detail_url, mono_ajax_url, mono_player_url]
