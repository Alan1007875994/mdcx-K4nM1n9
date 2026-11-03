from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from curl_cffi.requests import Response
from curl_cffi.requests.exceptions import RequestException
from parsel import Selector
from PyQt6.QtWidgets import QApplication, QMainWindow

from mdcx.config.models import Config
from mdcx.controllers.main_window import main_window as module
from mdcx.controllers.main_window.main_window import MyMAinWindow
from mdcx.crawlers.base.types import CrawlerData
from mdcx.crawlers.dmm_new import DMMContext, DmmCrawler
from mdcx.models.flags import Flags
from mdcx.models.types import CrawlerInput
from mdcx.signals import signal_qt
from mdcx.views.MDCx import Ui_MDCx
from mdcx.web_async import AsyncWebClient, movie_request_scope


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    # Use the real controller methods and real widgets without startup/network/config loading.
    monkeypatch.setattr(MyMAinWindow, "__init__", lambda self: QMainWindow.__init__(self))
    window = MyMAinWindow()
    window.window_radius = 0
    window.Ui = Ui_MDCx()
    window.Ui.setupUi(window)
    window.main_log_queue = deque()
    window.main_log_batch_size = 80
    window.main_log_max_count = 10000
    window.logs_counts = window.req_logs_counts = 0
    window._request_log_file = None
    window._taskbar_progress_active = False
    window._taskbar_progress = Mock()
    config = Config(save_log=True, show_web_log=True)
    monkeypatch.setattr(module.manager, "config", config)
    monkeypatch.setattr(module.manager, "data_folder", tmp_path)
    monkeypatch.setattr(Flags, "log_txt", None)
    monkeypatch.setattr(signal_qt, "stop", False)
    signal_qt.get_log()
    yield window, config, tmp_path
    for handle in (Flags.log_txt, window._request_log_file):
        if handle is not None:
            handle.close()
    signal_qt.get_log()
    window.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("request_first", [False, True])
def test_request_panel_paired_files_are_written_without_close(window, request_first):
    win, _, root = window
    displayed = []
    win.main_req_logs_show.connect(displayed.append)
    text = "思考内容：有（123 字符）\n翻译失败诊断：合成异常\n被拒片段：合成文本\n人名对照获取失败\n<test> & 原样"
    if not request_first:
        win._write_main_logs_to_file(["主日志"])
    signal_qt.add_log(text)
    win.show_detail_log()
    if request_first:
        win._write_main_logs_to_file(["主日志"])
    main = next(p for p in (root / "Log").glob("*.txt") if not p.stem.endswith("-请求"))
    request = main.with_stem(main.stem + "-请求")
    # Read while unbuffered handles are still open, as after an interrupted process.
    assert "主日志" in main.read_text(encoding="utf-8")
    assert text in request.read_text(encoding="utf-8")
    assert "思考内容" in displayed[0]
    assert "&lt;test&gt;" in displayed[0]
    before = request.read_text(encoding="utf-8")
    win._request_log_file.close()
    win._write_request_log_to_file("下一行")
    assert request.read_text(encoding="utf-8") == before + "下一行\n"


def test_save_disabled_writes_neither_file_but_still_displays(window):
    win, config, root = window
    config.save_log = False
    displayed = []
    win.main_req_logs_show.connect(displayed.append)
    win.show_log_text("主日志")
    win._flush_main_log_queue()
    signal_qt.add_log("思考内容：无")
    win.show_detail_log()
    assert displayed
    assert not (root / "Log").exists()


def test_save_toggle_does_not_append_while_disabled_and_hidden_panel_still_saved(window):
    win, config, root = window
    win._write_request_log_to_file("before")
    config.save_log = False
    win._write_request_log_to_file("disabled")
    win._write_main_logs_to_file(["disabled"])
    config.save_log = True
    config.show_web_log = False
    signal_qt.add_log("hidden")
    win.show_detail_log()
    request = next((root / "Log").glob("*-请求.txt"))
    assert "before" in request.read_text(encoding="utf-8") and "hidden" in request.read_text(encoding="utf-8")
    assert all("disabled" not in p.read_text(encoding="utf-8") for p in (root / "Log").glob("*.txt"))


def test_request_credentials_redacted_without_losing_other_diagnostics(window):
    win, _, root = window
    win._write_request_log_to_file(
        "GET https://user:pass@example.test/path?token=secret-value&cookie=synthetic-query-cookie&id=123\n"
        "Authorization: Bearer synthetic-key\nCookie: session=synthetic-cookie\n"
        "api_key=synthetic-api-key\nKey: synthetic-plain-key\ntoken=synthetic-token\nsecret='synthetic quoted secret'\n思考内容：有（8 字符）"
    )
    text = next((root / "Log").glob("*-请求.txt")).read_text(encoding="utf-8")
    for secret in (
        "user:pass",
        "secret-value",
        "synthetic-key",
        "synthetic-cookie",
        "synthetic-api-key",
        "synthetic-plain-key",
        "synthetic-token",
        "synthetic-query-cookie",
        "synthetic quoted secret",
    ):
        assert secret not in text
    assert "example.test/path?token=[已隐藏]&cookie=[已隐藏]&id=123" in text
    assert "思考内容：有（8 字符）" in text


def _fake_session_factory(request):
    class FakeSession:
        closed = False

        def __init__(self, _fingerprint=None):
            pass

        async def close(self):
            self.closed = True

        async def request(self, **kwargs):
            return request(**kwargs)

    return FakeSession


@pytest.mark.asyncio
@pytest.mark.parametrize("matched", [False, True])
async def test_dmm_response_and_search_diagnostics_saved_without_another_request(window, matched):
    win, _, root = window
    response = Response()
    response.status_code = 200
    response.url = "https://account:password@www.dmm.co.jp/service/foreign/?cookie=secret&account=private&token=key"
    html = (
        '<a href="https://video.dmm.co.jp/av/content/?id=demo00123&i3_ref=search">合成结果</a>'
        if matched
        else "地区页面"
    )
    response.content = html.encode("utf-8")
    request = Mock(return_value=response)
    client = AsyncWebClient(timeout=1, log_fn=signal_qt.add_log)
    client._pool_manager._session_factory = _fake_session_factory(request)
    ctx = DMMContext(input=CrawlerInput.empty())
    ctx.input.number = "DEMO-123"
    crawler = DmmCrawler(client=client)
    try:
        with movie_request_scope("dmm"):
            text, error = await client.get_text("https://www.dmm.co.jp/search/=/searchstr=demo123/", retry_count=1)
            results = await crawler._parse_search_page(ctx, Selector(text), response.url)
        assert text == html and not error
        assert bool(results) == matched
        assert request.call_count == 1
        win.show_detail_log()
        saved = next((root / "Log").glob("*-请求.txt")).read_text(encoding="utf-8")
        assert "[DMM响应诊断] 阶段=搜索" in saved
        assert "最终URL=https://www.dmm.co.jp/service/foreign/" in saved
        assert f"HTTP状态=200 响应字节数={len(response.content)}" in saved
        assert f"匹配诊断: 番号=DEMO-123 阶段=搜索 命中={'是' if matched else '否'}" in saved
        for secret in ("account:password", "secret", "private", "token=key"):
            assert secret not in saved
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "use_proxy"), [(35, False), (35, True), (16, False), (16, True)])
async def test_transport_diagnostics_saved_with_time_host_and_actual_route(window, code, use_proxy):
    from datetime import datetime

    win, _, root = window
    failure = RequestException("synthetic failure", code=code)
    request = Mock(side_effect=failure)
    client = AsyncWebClient(
        timeout=1, proxy="http://proxy-user:proxy-password@proxy.test:7890", log_fn=signal_qt.add_log
    )
    client._pool_manager._session_factory = _fake_session_factory(request)
    try:
        text, error = await client.get_text("https://transport.test/detail", use_proxy=use_proxy, retry_count=1)
        assert text is None and "synthetic failure" in error
        assert request.call_count == 1
        assert request.call_args.kwargs["proxy"] == (client.proxy if use_proxy else None)
        win.show_detail_log()
        saved = next((root / "Log").glob("*-请求.txt")).read_text(encoding="utf-8")
        diagnostic = next(line for line in saved.splitlines() if "[传输诊断]" in line)
        timestamp = diagnostic.split("时间=", 1)[1].split()[0]
        assert datetime.fromisoformat(timestamp).tzinfo is not None
        assert f"主机=transport.test 路由={'代理' if use_proxy else '直连'} 错误码={code}" in diagnostic
        for secret in ("proxy.test", "proxy-user", "proxy-password", "7890"):
            assert secret not in diagnostic
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["", "合成标题"])
async def test_dmm_detail_match_diagnostic_saved(window, monkeypatch, title):
    win, _, root = window
    crawler = DmmCrawler(client=None)
    ctx = DMMContext(input=CrawlerInput.empty())
    ctx.input.number = "DEMO-123"
    calls = []

    async def fetch(*args):
        calls.append(args)
        return CrawlerData(title=title)

    async def sanitize(ctx, category, url, item):
        return item

    async def finalize(ctx, item, **kwargs):
        return item

    monkeypatch.setattr(crawler, "fetch_and_parse", fetch)
    monkeypatch.setattr(crawler, "_sanitize_candidate_images", sanitize)
    monkeypatch.setattr(crawler, "_finalize_result_images", finalize)
    await crawler._detail(ctx, ["https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=demo00123/"])
    assert len(calls) == 1
    win.show_detail_log()
    saved = next((root / "Log").glob("*-请求.txt")).read_text(encoding="utf-8")
    assert f"匹配诊断: 番号=DEMO-123 阶段=详情 命中={'是' if title else '否'}" in saved


def test_dmm_html_diagnostic_requires_final_url_evidence_and_hides_service_address(window):
    win, _, root = window
    client = AsyncWebClient(timeout=1, log_fn=signal_qt.add_log)
    response = Response()
    response.status_code = 200
    response.content = b"offline"
    response.url = "https://video.dmm.co.jp/av/content/?id=demo00123"
    response.headers["x-mdcx-bypass-mode"] = "html"
    with movie_request_scope("dmm"):
        client._log_dmm_response(response, "http://private-bypass.test:8000/html", method="GET", stream=False)
        client._log_dmm_response(response, response.url, method="GET", stream=False)
        response.headers["x-cf-bypasser-final-url"] = "https://www.dmm.co.jp/service/foreign/"
        client._log_dmm_response(response, response.url, method="GET", stream=False)
        client._log_dmm_response(response, response.url, method="GET", stream=True)
        client._log_dmm_response(response, response.url, method="POST", stream=False)
    win.show_detail_log()
    saved = next((root / "Log").glob("*-请求.txt")).read_text(encoding="utf-8")
    assert saved.count("[DMM响应诊断]") == 2
    assert "阶段=详情" in saved
    assert "最终URL=未记录 HTTP状态=200 响应字节数=7 来源=html" in saved
    assert "最终URL=https://www.dmm.co.jp/service/foreign/" in saved
    assert "private-bypass" not in saved


def test_taskbar_counts_first_pass_including_deferred_and_clears_on_end(window, monkeypatch):
    win, _, _ = window
    monkeypatch.setattr(module, "IS_WINDOWS", True)
    monkeypatch.setattr(Flags, "stop_requested", False)
    monkeypatch.setattr(Flags, "total_count", 23)
    monkeypatch.setattr(Flags, "scrape_done", 5)
    monkeypatch.setattr(Flags, "scrape_processed", 7)
    monkeypatch.setattr(Flags, "succ_count", 3)
    monkeypatch.setattr(Flags, "fail_count", 2)
    win.set_processbar(30)  # Success, failure and deferred first-pass outcomes share one count.
    assert win.Ui.progressBar_scrape.value() == 30
    win._taskbar_progress.update.assert_called_with(int(win.winId()), 7, 23)
    monkeypatch.setattr(Flags, "scrape_processed", 23)
    win.set_processbar(100)
    win._taskbar_progress.update.assert_called_with(int(win.winId()), 23, 23)
    win.set_processbar(0)
    win._taskbar_progress.clear.assert_called_with(int(win.winId()))
    assert not win._taskbar_progress_active


def test_native_event_observes_message_and_returns_initialized_result(window, monkeypatch):
    import ctypes
    from ctypes.wintypes import MSG

    from PyQt6 import sip

    win, _, _ = window
    monkeypatch.setattr(module, "IS_WINDOWS", True)
    message = MSG()
    pointer = ctypes.addressof(message)
    result = win.nativeEvent(b"windows_generic_MSG", sip.voidptr(pointer))
    assert result == (False, 0)
    win._taskbar_progress.native_message.assert_called_once_with(pointer)


def test_confirmed_stop_clears_immediately_and_ignores_late_progress(window, monkeypatch):
    win, config, _ = window
    config.switch_on = []
    win.Ui.pushButton_start_cap.setText("■ 停止")
    win._taskbar_progress_active = True
    win.threads_list = []
    win.show_stop_info_thread = Mock()
    monkeypatch.setattr(module, "IS_WINDOWS", True)
    monkeypatch.setattr(Flags, "stop_requested", False)
    monkeypatch.setattr(
        module, "executor", SimpleNamespace(run=Mock(side_effect=lambda coro: coro.close()), cancel_async=Mock())
    )
    win.pushButton_stop_scrape_clicked()
    assert Flags.stop_requested
    win._taskbar_progress.clear.assert_called_once()
    win.set_processbar(70)
    win._update_taskbar_progress()
    win._taskbar_progress.update.assert_not_called()


def test_switching_log_folder_keeps_existing_pair_and_non_windows_skips_progress(window, monkeypatch):
    win, _, root = window
    win._write_request_log_to_file("original")
    old_pair = {p.name: p.read_bytes() for p in (root / "Log").glob("*.txt")}
    monkeypatch.setattr(module.manager, "data_folder", root / "other")
    win._write_request_log_to_file("new folder")
    assert old_pair == {p.name: p.read_bytes() for p in (root / "Log").glob("*.txt")}
    assert "new folder" in next((root / "other" / "Log").glob("*-请求.txt")).read_text(encoding="utf-8")
    monkeypatch.setattr(module, "IS_WINDOWS", False)
    win.set_processbar(70)
    win._update_taskbar_progress()
    win._taskbar_progress.update.assert_not_called()
