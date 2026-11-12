import sys
import threading
from types import SimpleNamespace

import pytest

from mdcx.config.enums import Website
from mdcx.config.models import Config
from mdcx.core.network_check import (
    NetworkCheckSpec,
    NetworkCheckStatus,
    build_network_check_specs,
    format_result_line,
    run_network_check,
    run_network_check_item,
)
from mdcx.web_async import CF_MIRROR_TIMEOUT, AsyncWebClient, is_cf_challenge_response


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "ok", url: str = "https://example.test"):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = {}
        self.encoding = "utf-8"


class FakeClient:
    def __init__(self, *, fail_url_part: str = ""):
        self.fail_url_part = fail_url_part
        self.calls: list[dict] = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.fail_url_part and self.fail_url_part in url:
            raise RuntimeError("boom")
        return FakeResponse(url=url), ""


class FakeBypassClient:
    def __init__(self, *, bypass_ok: bool = True):
        self.bypass_ok = bypass_ok
        self.calls: list[dict] = []
        self.bypass_calls: list[dict] = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(
            text="<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script>Cloudflare</html>",
            url=url,
        ), ""

    async def _try_bypass_cloudflare(self, **kwargs):
        self.bypass_calls.append(kwargs)
        if not self.bypass_ok:
            return None, "bypass failed"
        response = FakeResponse(text="<html>ok</html>", url=kwargs["target_url"])
        response.headers["x-mdcx-bypass-mode"] = "mirror"
        return response, ""


class FakeConfig:
    use_proxy = False
    proxy = ""
    cf_bypass_url = ""
    cf_bypass_proxy = ""
    timeout = 5
    cf_bypass_timeout = 90
    javdb = ""
    javbus = ""
    theporndb_api_token = ""

    def get_site_url(self, site, default=""):
        return default


class FakeManager:
    config = FakeConfig()
    computed = None


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def fake_manager(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("mdcx.core.network_check._manager", lambda: FakeManager())


@pytest.mark.anyio
async def test_build_network_check_specs_uses_registered_sites_without_key_error(monkeypatch: pytest.MonkeyPatch):
    class DynamicCrawler:
        @classmethod
        def base_url_(cls):
            return ""

    class CustomConfig(FakeConfig):
        def get_site_url(self, site, default=""):
            return "https://custom.example"

    class CustomManager:
        config = CustomConfig()
        computed = None

    fake_crawlers = SimpleNamespace(
        get_registered_crawler_sites=lambda include_hidden=False: [Website.OFFICIAL],
        get_crawler=lambda site: DynamicCrawler,
    )
    monkeypatch.setitem(sys.modules, "mdcx.crawlers", fake_crawlers)
    monkeypatch.setattr("mdcx.core.network_check._manager", lambda: CustomManager())

    specs = await build_network_check_specs()

    assert any(spec.site == Website.OFFICIAL and spec.url == "https://custom.example" for spec in specs)


@pytest.mark.anyio
async def test_kin8_network_check_uses_site_homepage(monkeypatch: pytest.MonkeyPatch):
    class Kin8Crawler:
        @classmethod
        def base_url_(cls):
            return "https://www.kin8tengoku.com"

    fake_crawlers = SimpleNamespace(
        get_registered_crawler_sites=lambda include_hidden=False: [Website.KIN8],
        get_crawler=lambda site: Kin8Crawler,
    )
    monkeypatch.setitem(sys.modules, "mdcx.crawlers", fake_crawlers)

    specs = await build_network_check_specs()

    kin8 = next(spec for spec in specs if spec.site == Website.KIN8)
    assert kin8.url == "https://www.kin8tengoku.com/"
    assert "/moviepages/3681/index.html" not in kin8.url


@pytest.mark.anyio
async def test_run_network_check_item_catches_single_item_exception():
    spec = NetworkCheckSpec(name="bad", group="刮削站点", url="https://bad.example")

    result = await run_network_check_item(spec, client=FakeClient(fail_url_part="bad"))

    assert result.status == NetworkCheckStatus.FAILED
    assert result.message == "检测异常"
    assert result.error == "boom"


@pytest.mark.anyio
@pytest.mark.parametrize("response_shape", ["response", "error"])
@pytest.mark.parametrize("recovers", [True, False])
async def test_network_check_retries_502_once(response_shape, recovers):
    class GatewayClient(FakeClient):
        async def request(self, method, url, **kwargs):
            self.calls.append(kwargs)
            if recovers and len(self.calls) == 2:
                return FakeResponse(), ""
            if response_shape == "error":
                return None, f"GET {url} 失败: HTTP 502"
            return FakeResponse(status_code=502), ""

    client = GatewayClient()
    result = await run_network_check_item(
        NetworkCheckSpec(name="javdb", group="刮削站点", site=Website.JAVDB, url="https://example.test"),
        client=client,
    )
    assert len(client.calls) == 2
    assert all(call["retry_count"] == 1 for call in client.calls)
    assert result.status == (NetworkCheckStatus.OK if recovers else NetworkCheckStatus.FAILED)


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [200, 400, 401, 403, 429, 500, 523])
async def test_network_check_does_not_add_retries_for_other_statuses(status_code):
    class StatusClient(FakeClient):
        async def request(self, method, url, **kwargs):
            self.calls.append(kwargs)
            return FakeResponse(status_code=status_code), ""

    client = StatusClient()
    await run_network_check_item(
        NetworkCheckSpec(name="site", group="刮削站点", url="https://example.test"), client=client
    )
    assert len(client.calls) == 1


@pytest.mark.anyio
async def test_network_check_cancel_after_502_skips_retry():
    cancel_event = threading.Event()

    class CancelClient(FakeClient):
        async def request(self, method, url, **kwargs):
            self.calls.append(kwargs)
            cancel_event.set()
            return FakeResponse(status_code=502), ""

    client = CancelClient()
    result = await run_network_check_item(
        NetworkCheckSpec(name="site", group="刮削站点", url="https://example.test"),
        client=client,
        cancel_event=cancel_event,
    )
    assert len(client.calls) == 1
    assert result.status == NetworkCheckStatus.CANCELLED


@pytest.mark.anyio
@pytest.mark.parametrize("text,expected", [("<html>ok</html>", "无法确认 JavDB 登录状态"), ("/logout", "Cookie 有效")])
async def test_javdb_login_message_does_not_infer_cookie_expiry(monkeypatch, text, expected):
    config = FakeConfig()
    config.javdb = "test-cookie"
    monkeypatch.setattr("mdcx.core.network_check._manager", lambda: SimpleNamespace(config=config))

    class LoginClient(FakeClient):
        async def request(self, method, url, **kwargs):
            return FakeResponse(text=text), ""

    result = await run_network_check_item(
        NetworkCheckSpec(name="javdb", group="刮削站点", site=Website.JAVDB, url="https://example.test"),
        client=LoginClient(),
    )
    assert expected in result.message
    assert "过期" not in result.message
    assert "失效" not in result.message


@pytest.mark.anyio
async def test_javdb_network_check_carries_configured_cookie_into_direct_and_bypass_requests(
    monkeypatch: pytest.MonkeyPatch,
):
    class CookieConfig(FakeConfig):
        cf_bypass_url = "http://0.0.0.0:8000"
        javdb = "sid=secret"

    class CookieManager:
        config = CookieConfig()
        computed = None

    monkeypatch.setattr("mdcx.core.network_check._manager", lambda: CookieManager())
    client = FakeBypassClient()
    spec = NetworkCheckSpec(
        name="javdb",
        group="刮削站点",
        site=Website.JAVDB,
        url="https://javdb.com/",
        headers={"cookie": "sid=secret"},
        enable_cf_bypass=True,
    )

    result = await run_network_check_item(spec, client=client)

    assert client.calls[0]["headers"] == {"cookie": "sid=secret"}
    assert client.bypass_calls[0]["headers"] == {"cookie": "sid=secret"}
    assert "sid=secret" not in result.message


@pytest.mark.anyio
async def test_run_network_check_does_not_stop_on_single_item_exception(monkeypatch: pytest.MonkeyPatch):
    async def fake_specs():
        return [
            NetworkCheckSpec(name="good", group="基础连通性", url="https://good.example"),
            NetworkCheckSpec(name="bad", group="基础连通性", url="https://bad.example"),
        ]

    monkeypatch.setattr("mdcx.core.network_check.build_network_check_specs", fake_specs)
    lines: list[str] = []

    results = await run_network_check(
        progress=lines.append, client=FakeClient(fail_url_part="bad"), concurrency=2, emit_header=False
    )

    assert len(results) == 2
    assert {result.spec.name: result.status for result in results} == {
        "good": NetworkCheckStatus.OK,
        "bad": NetworkCheckStatus.FAILED,
    }
    assert any("网络检测已完成" in line for line in lines)


@pytest.mark.anyio
async def test_run_network_check_can_cancel_between_groups(monkeypatch: pytest.MonkeyPatch):
    async def fake_specs():
        return [
            NetworkCheckSpec(name="first", group="基础连通性", url="https://first.example"),
            NetworkCheckSpec(name="second", group="刮削站点", url="https://second.example"),
        ]

    monkeypatch.setattr("mdcx.core.network_check.build_network_check_specs", fake_specs)
    cancel_event = threading.Event()
    lines: list[str] = []

    def progress(line: str):
        lines.append(line)
        if "first" in line:
            cancel_event.set()

    results = await run_network_check(
        progress=progress, cancel_event=cancel_event, client=FakeClient(), concurrency=1, emit_header=False
    )

    assert [result.spec.name for result in results] == ["first"]
    assert any("网络检测已取消" in line for line in lines)


@pytest.mark.anyio
async def test_javdbapi_spec_uses_real_query_url(monkeypatch: pytest.MonkeyPatch):
    class ApiCrawler:
        @classmethod
        def base_url_(cls):
            return "https://api.thejavdb.net/v1"

    fake_crawlers = SimpleNamespace(
        get_registered_crawler_sites=lambda include_hidden=False: [Website.JAVDBAPI],
        get_crawler=lambda site: ApiCrawler,
    )
    monkeypatch.setitem(sys.modules, "mdcx.crawlers", fake_crawlers)

    specs = await build_network_check_specs()

    javdbapi = next(spec for spec in specs if spec.site == Website.JAVDBAPI)
    assert javdbapi.url == "https://api.thejavdb.net/v1/movies?q=ssni-200"
    assert javdbapi.validator == "javdbapi"


def test_format_result_line_does_not_duplicate_error():
    spec = NetworkCheckSpec(name="site", group="刮削站点", url="https://example.test")
    from mdcx.core.network_check import NetworkCheckResult

    result = NetworkCheckResult(
        spec=spec,
        status=NetworkCheckStatus.FAILED,
        message="GET https://example.test 失败: HTTP 403",
        error="GET https://example.test 失败: HTTP 403",
    )

    line = format_result_line(result)

    assert line.count("GET https://example.test 失败: HTTP 403") == 1


@pytest.mark.anyio
async def test_run_network_check_item_actively_uses_cf_bypass_on_challenge(monkeypatch: pytest.MonkeyPatch):
    class BypassConfig(FakeConfig):
        cf_bypass_url = "http://0.0.0.0:8000"

    class BypassManager:
        config = BypassConfig()
        computed = None

    monkeypatch.setattr("mdcx.core.network_check._manager", lambda: BypassManager())
    client = FakeBypassClient()
    spec = NetworkCheckSpec(
        name="cf-site",
        group="刮削站点",
        url="https://cf.example",
        enable_cf_bypass=True,
        headers={"cookie": "a=b"},
    )

    result = await run_network_check_item(spec, client=client)

    assert result.status == NetworkCheckStatus.OK
    assert result.message == "连接正常，已通过 CF Bypass（mirror）"
    assert client.bypass_calls[0]["target_url"] == "https://cf.example"
    assert client.bypass_calls[0]["headers"] == {"cookie": "a=b"}
    assert client.bypass_calls[0]["timeout"] == CF_MIRROR_TIMEOUT


@pytest.mark.anyio
async def test_run_network_check_item_reports_cf_bypass_failure(monkeypatch: pytest.MonkeyPatch):
    class BypassConfig(FakeConfig):
        cf_bypass_url = "http://0.0.0.0:8000"

    class BypassManager:
        config = BypassConfig()
        computed = None

    monkeypatch.setattr("mdcx.core.network_check._manager", lambda: BypassManager())
    spec = NetworkCheckSpec(
        name="cf-site",
        group="刮削站点",
        url="https://cf.example",
        enable_cf_bypass=True,
    )

    result = await run_network_check_item(spec, client=FakeBypassClient(bypass_ok=False))

    assert result.status == NetworkCheckStatus.FAILED
    assert result.message == "Cloudflare Bypass 失败"
    assert result.error == "bypass failed"


@pytest.mark.anyio
async def test_cf_bypass_health_check_waits_for_cookie_generation():
    spec = NetworkCheckSpec(
        name="CF Bypass",
        group="辅助服务",
        url="http://bypass.example/cookies?url=http://example.com",
        use_proxy=False,
    )
    client = FakeClient()

    result = await run_network_check_item(spec, client=client)

    assert result.status == NetworkCheckStatus.OK
    assert result.message == "Cookie 检测通过"
    assert client.calls[0]["timeout"] == 90


@pytest.mark.anyio
async def test_cf_bypass_health_check_keeps_longer_diagnostic_timeout(monkeypatch: pytest.MonkeyPatch):
    class ShortBypassConfig(FakeConfig):
        cf_bypass_timeout = 1

    class ShortBypassManager:
        config = ShortBypassConfig()
        computed = None

    monkeypatch.setattr("mdcx.core.network_check._manager", lambda: ShortBypassManager())
    spec = NetworkCheckSpec(
        name="CF Bypass",
        group="辅助服务",
        url="http://bypass.example/cookies?url=http://example.com",
        use_proxy=False,
    )
    client = FakeClient()

    await run_network_check_item(spec, client=client)

    assert client.calls[0]["timeout"] == 5.0


@pytest.mark.anyio
async def test_site_check_keeps_diagnostic_timeout():
    spec = NetworkCheckSpec(name="site", group="刮削站点", url="https://example.test")
    client = FakeClient()

    await run_network_check_item(spec, client=client)

    assert client.calls[0]["timeout"] == 5.0


@pytest.mark.anyio
async def test_network_check_only_checks_enabled_scraping_sites_and_reports_skipped_count(
    monkeypatch: pytest.MonkeyPatch,
):
    site_urls = {
        Website.MISSAV: "https://missav.example",
        Website.MMTV: "https://7mmtv.example",
        Website.CABLEAV: "https://cableav.example",
        Website.FC2CLUB: "https://fc2club.example",
        Website.FC2PPVDB: "https://fc2ppvdb.example",
        Website.JAVDBAPI: "https://api.javdb.example/v1",
        Website.THEPORNDB: "https://theporndb.example",
    }

    def get_crawler(site: Website):
        url = site_urls.get(site)
        if url is None:
            return None
        return SimpleNamespace(base_url_=lambda u=url: u)

    fake_crawlers = SimpleNamespace(
        get_registered_crawler_sites=lambda include_hidden=False: list(site_urls.keys()),
        get_crawler=get_crawler,
    )
    monkeypatch.setitem(sys.modules, "mdcx.crawlers", fake_crawlers)

    config = Config(
        website_youma=[Website.MISSAV],
        website_wuma=[Website.MMTV],
        website_suren=[],
        website_fc2=[],
        website_oumei=[Website.THEPORNDB],
        website_guochan=[],
        cf_bypass_url="http://127.0.0.1:8000",
        theporndb_api_token="token-123",
    )
    monkeypatch.setattr("mdcx.core.network_check._manager", lambda: SimpleNamespace(config=config, computed=None))

    specs = await build_network_check_specs()
    scraping_sites = [spec.site for spec in specs if spec.group == "刮削站点"]
    assert scraping_sites == [Website.MISSAV, Website.MMTV]
    assert {spec.name for spec in specs if spec.group == "基础连通性"} == {"GitHub Raw", "通用 HTTPS"}
    assert {spec.name for spec in specs if spec.group == "账号/API"} == {"ThePornDB Token", "javdbapi"}
    assert {spec.name for spec in specs if spec.group == "辅助服务"} == {"CF Bypass"}

    class RecordingClient(FakeClient):
        async def request(self, method, url, **kwargs):
            self.calls.append({"method": method, "url": url, **kwargs})
            if "api.theporndb.net" in url:
                return FakeResponse(status_code=200, text='{"data": {}}', url=url), ""
            if "api.javdb.example" in url:
                return FakeResponse(status_code=200, text='{"universal_id": "SSNI-200"}', url=url), ""
            return FakeResponse(status_code=200, text="<html>ok</html>", url=url), ""

    client = RecordingClient()
    lines: list[str] = []
    results = await run_network_check(progress=lines.append, client=client, concurrency=2, emit_header=False)

    requested_urls = [call["url"] for call in client.calls]
    assert not any("cableav.example" in url for url in requested_urls)
    assert not any("fc2club.example" in url for url in requested_urls)
    assert not any("fc2ppvdb.example" in url for url in requested_urls)
    assert {r.spec.site for r in results if r.spec.group == "刮削站点"} == {Website.MISSAV, Website.MMTV}
    assert "  ℹ️ 未启用、已跳过：3 个" in lines


@pytest.mark.anyio
async def test_network_check_mirror_bypass_uses_scraping_mirror_timeout(monkeypatch: pytest.MonkeyPatch):
    class RawResponse:
        def __init__(self, status_code: int, content: bytes, url: str, headers: dict[str, str] | None = None):
            self.status_code = status_code
            self.content = content
            self.text = content.decode("utf-8", errors="ignore")
            self.url = url
            self.headers = dict(headers or {})
            self.encoding = "utf-8"

    challenge_html = (
        b"<html><head><title>Just a moment...</title></head>"
        b"<body><script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script></body></html>"
    )
    normal_html = b"<html><body>normal page</body></html>"

    client = AsyncWebClient(timeout=15, cf_bypass_url="http://127.0.0.1:8000", retry=1)
    curl_calls: list[dict] = []

    async def fake_curl_request(**kwargs):
        curl_calls.append(kwargs)
        url = kwargs["url"]
        if url.startswith("http://127.0.0.1:8000/"):
            return RawResponse(200, normal_html, url, headers={"content-type": "text/html"})
        return RawResponse(
            403,
            challenge_html,
            url,
            headers={"server": "cloudflare", "cf-ray": "89abcdef-NRT", "content-type": "text/html"},
        )

    monkeypatch.setattr(client, "_curl_request", fake_curl_request)
    config = FakeConfig()
    config.cf_bypass_url = "http://127.0.0.1:8000"
    config.timeout = 15
    monkeypatch.setattr("mdcx.core.network_check._manager", lambda: SimpleNamespace(config=config, computed=None))

    try:
        scrape_resp, scrape_err = await client.request("GET", "https://scrape-cf.example/video")
        assert scrape_err == ""
        assert scrape_resp is not None
        scrape_mirror_timeout = next(c["timeout"] for c in curl_calls if c["url"].startswith("http://127.0.0.1:8000/"))

        curl_calls.clear()
        spec = NetworkCheckSpec(
            name="missav",
            group="刮削站点",
            site=Website.MISSAV,
            url="https://check-cf.example/",
            enable_cf_bypass=True,
        )
        result = await run_network_check_item(spec, client=client)

        direct_call = next(c for c in curl_calls if not c["url"].startswith("http://127.0.0.1:8000/"))
        mirror_call = next(c for c in curl_calls if c["url"].startswith("http://127.0.0.1:8000/"))
        assert direct_call["timeout"] == 5.0
        assert mirror_call["timeout"] == scrape_mirror_timeout == CF_MIRROR_TIMEOUT
        assert result.status == NetworkCheckStatus.OK
        assert result.message == "连接正常，已通过 CF Bypass（mirror）"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_network_check_200_normal_page_with_residual_cf_markers_not_flagged_and_real_challenge_flagged(
    monkeypatch: pytest.MonkeyPatch,
):
    config = FakeConfig()
    config.cf_bypass_url = "http://127.0.0.1:8000"
    monkeypatch.setattr("mdcx.core.network_check._manager", lambda: SimpleNamespace(config=config, computed=None))

    residual_200_html = (
        "<html><head><script src='https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js'></script></head>"
        "<body><h1>7mmtv challenge video</h1><footer>Cloudflare Ray ID: 1234567890abcdef</footer></body></html>"
    )

    class NormalWithResidualClient(FakeBypassClient):
        async def request(self, method, url, **kwargs):
            self.calls.append({"method": method, "url": url, **kwargs})
            resp = FakeResponse(status_code=200, text=residual_200_html, url=url)
            resp.headers = {"server": "cloudflare", "cf-ray": "1234567890abcdef-HKG", "content-type": "text/html"}
            return resp, ""

    normal_client = NormalWithResidualClient()
    spec = NetworkCheckSpec(
        name="7mmtv",
        group="刮削站点",
        site=Website.MMTV,
        url="https://7mmtv.example/",
        enable_cf_bypass=True,
    )
    normal_result = await run_network_check_item(spec, client=normal_client)
    assert is_cf_challenge_response(FakeResponse(status_code=200, text=residual_200_html)) is False
    assert normal_result.status == NetworkCheckStatus.OK
    assert normal_result.message == "连接正常"
    assert normal_client.bypass_calls == []

    # Real challenge page (403 with cloudflare header + challenge marker, or challenge-platform marker)
    no_bypass_config = FakeConfig()
    no_bypass_config.cf_bypass_url = ""
    monkeypatch.setattr(
        "mdcx.core.network_check._manager", lambda: SimpleNamespace(config=no_bypass_config, computed=None)
    )

    class RealChallengeClient(FakeClient):
        def __init__(self, response: FakeResponse):
            super().__init__()
            self._response = response

        async def request(self, method, url, **kwargs):
            self.calls.append({"method": method, "url": url, **kwargs})
            return self._response, ""

    challenge_403 = FakeResponse(
        status_code=403,
        text="<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing</body></html>",
        url="https://cf.example/",
    )
    challenge_403.headers = {"server": "cloudflare", "cf-ray": "9999-NRT", "content-type": "text/html"}
    assert is_cf_challenge_response(challenge_403) is True
    res_403 = await run_network_check_item(spec, client=RealChallengeClient(challenge_403))
    assert res_403.status == NetworkCheckStatus.WARNING
    assert res_403.message == "被 Cloudflare 挑战页拦截"

    challenge_platform = FakeResponse(
        status_code=503,
        text="<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'></script></html>",
        url="https://cf.example/",
    )
    assert is_cf_challenge_response(challenge_platform) is True
    res_platform = await run_network_check_item(spec, client=RealChallengeClient(challenge_platform))
    assert res_platform.status == NetworkCheckStatus.WARNING
    assert res_platform.message == "被 Cloudflare 挑战页拦截"
