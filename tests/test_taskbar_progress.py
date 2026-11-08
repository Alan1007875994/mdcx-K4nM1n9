import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mdcx.controllers.main_window import windows_taskbar as taskbar


@pytest.fixture
def progress(monkeypatch):
    monkeypatch.setattr(taskbar, "IS_WINDOWS", True)
    register = Mock(return_value=12345)
    monkeypatch.setattr(
        ctypes, "WinDLL", Mock(return_value=SimpleNamespace(RegisterWindowMessageW=register)), raising=False
    )
    api = Mock()
    monkeypatch.setattr(taskbar, "_TaskbarAPI", Mock(return_value=api))
    log = Mock()
    progress = taskbar.WindowsTaskbarProgress(log)
    progress.ready = True
    return progress, api, log


def test_progress_waits_for_taskbar_button_and_preserves_counts(progress):
    bar, api, _ = progress
    bar.ready = False
    bar.update(123, 2, 23)
    api.set_progress.assert_not_called()
    from ctypes.wintypes import MSG

    message = MSG()
    message.message = bar.message_id
    bar.native_message(ctypes.addressof(message))
    api.set_progress.assert_called_once_with(123, 2, 23)
    bar.update(123, 2, 23)
    assert api.set_progress.call_count == 1
    bar.update(123, 23, 23)
    bar.clear(123)
    api.clear.assert_called_once_with(123)
    # Explorer recreation restores the pending cleared state.
    bar.native_message(ctypes.addressof(message))
    assert api.clear.call_count == 2


def test_windows_failure_logs_once_and_does_not_interrupt(progress):
    bar, api, log = progress
    api.set_progress.side_effect = OSError("synthetic COM failure")
    bar.update(123, 1, 23)
    bar.update(123, 2, 23)
    bar.clear(123)
    assert bar.disabled
    log.assert_called_once()
    api.close.assert_called_once()


def test_non_windows_does_not_load_or_call_windows_api(monkeypatch):
    monkeypatch.setattr(taskbar, "IS_WINDOWS", False)
    load = Mock(side_effect=AssertionError("Windows API called on another OS"))
    monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)
    bar = taskbar.WindowsTaskbarProgress(Mock())
    bar.native_message(0)
    bar.update(123, 1, 23)
    bar.clear(123)
    bar.close()
    load.assert_not_called()


def test_com_vtable_uses_64_bit_counts_and_releases(monkeypatch):
    # Exercise the actual ctypes adapter, using synthetic COM vtable functions.
    calls = []
    prototype = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", prototype, raising=False)
    callbacks = [
        prototype(ctypes.c_uint32, ctypes.c_void_p)(lambda this: calls.append(("release",)) or 0),
        prototype(ctypes.c_int32, ctypes.c_void_p)(lambda this: calls.append(("init",)) or 0),
        prototype(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64)(
            lambda this, hwnd, done, total: calls.append(("progress", hwnd, done, total)) or 0
        ),
        prototype(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)(
            lambda this, hwnd, state: calls.append(("clear", hwnd, state)) or 0
        ),
    ]
    table = (ctypes.c_void_p * 11)()
    for index, callback in zip((2, 3, 9, 10), callbacks, strict=True):
        table[index] = ctypes.cast(callback, ctypes.c_void_p)
    instance = ctypes.pointer(ctypes.cast(table, ctypes.POINTER(ctypes.c_void_p)))

    def create(clsid, outer, context, iid, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(instance, ctypes.c_void_p)
        return 0

    ole = SimpleNamespace(
        CoInitializeEx=Mock(return_value=1), CoCreateInstance=Mock(side_effect=create), CoUninitialize=Mock()
    )
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=ole), raising=False)
    api = taskbar._TaskbarAPI()
    api.set_progress(2**40, 2**33, 2**34)
    api.clear(2**40)
    api.set_error(2**40)
    api.close()
    assert calls == [
        ("init",),
        ("progress", 2**40, 2**33, 2**34),
        ("clear", 2**40, 0),
        ("clear", 2**40, 4),
        ("release",),
    ]
    ole.CoUninitialize.assert_called_once()


def test_window_taskbar_uses_first_pass_display_count(monkeypatch):
    from mdcx.controllers.main_window import main_window as module
    from mdcx.models.flags import Flags

    monkeypatch.setattr(module, "IS_WINDOWS", True)
    monkeypatch.setattr(module.signal_qt, "stop", False)
    monkeypatch.setattr(Flags, "stop_requested", False)
    monkeypatch.setattr(Flags, "scrape_done", 0)
    monkeypatch.setattr(Flags, "scrape_processed", 1)
    monkeypatch.setattr(Flags, "total_count", 200)
    window = SimpleNamespace(
        Ui=SimpleNamespace(progressBar_scrape=Mock()),
        _taskbar_progress_active=False,
        _taskbar_progress=Mock(),
        winId=lambda: 123,
    )
    window._update_taskbar_progress = lambda: module.MyMAinWindow._update_taskbar_progress(window)
    window._clear_taskbar_progress = lambda: module.MyMAinWindow._clear_taskbar_progress(window)
    # Less than one percent still updates Windows with the exact same count.
    module.MyMAinWindow.set_processbar(window, 0)
    window.Ui.progressBar_scrape.setProperty.assert_called_once_with("value", 0)
    window._taskbar_progress.update.assert_called_once_with(123, 1, 200)
    monkeypatch.setattr(Flags, "scrape_processed", 200)
    module.MyMAinWindow.set_processbar(window, 100)
    window._taskbar_progress.update.assert_called_with(123, 200, 200)
    module.MyMAinWindow.set_processbar(window, 0)
    window._taskbar_progress.clear.assert_called_once_with(123)


def test_service_error_stays_red_through_explorer_recreation_and_clears_for_next_batch(progress):
    bar, api, _ = progress
    bar.show_error(123, 0, 3)
    api.set_progress.assert_called_once_with(123, 1, 3)
    api.set_error.assert_called_once_with(123)
    from ctypes.wintypes import MSG

    message = MSG()
    message.message = bar.message_id
    bar.native_message(ctypes.addressof(message))
    assert api.set_error.call_count == 2
    bar.clear(123)
    assert not bar.error
    bar.update(123, 1, 3)
    assert api.set_error.call_count == 2
