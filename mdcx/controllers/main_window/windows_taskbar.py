"""Windows taskbar progress via the system ITaskbarList3 COM interface."""

import ctypes
from uuid import UUID

from mdcx.consts import IS_WINDOWS


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


class _TaskbarAPI:
    def __init__(self):
        self.ole32 = ctypes.WinDLL("ole32")
        self.ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.ole32.CoInitializeEx.restype = ctypes.c_int32
        self.ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.ole32.CoCreateInstance.restype = ctypes.c_int32
        self.ole32.CoUninitialize.argtypes = []
        self.ole32.CoUninitialize.restype = None
        self.pointer = ctypes.c_void_p()
        hr = self.ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        self.initialized = hr >= 0
        if hr != -2147417850:  # RPC_E_CHANGED_MODE: Qt already initialized COM differently
            self._check(hr)
        try:
            clsid = _GUID.from_buffer_copy(UUID("56FDF344-FD6D-11D0-958A-006097C9A090").bytes_le)
            iid = _GUID.from_buffer_copy(UUID("EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF").bytes_le)
            self._check(
                self.ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(self.pointer))
            )
            self._call(3)  # HrInit
        except Exception:
            self.close()
            raise

    @staticmethod
    def _check(hr):
        if hr < 0:
            raise OSError(f"HRESULT 0x{hr & 0xFFFFFFFF:08X}")

    def _call(self, index, *args, types=(), result_type=ctypes.c_int32):
        table = ctypes.cast(self.pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        method = ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *types)(table[index])
        result = method(self.pointer, *args)
        if result_type == ctypes.c_int32:
            self._check(result)

    def set_progress(self, hwnd, completed, total):
        self._call(9, hwnd, completed, total, types=(ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64))

    def clear(self, hwnd):
        self._call(10, hwnd, 0, types=(ctypes.c_void_p, ctypes.c_uint32))  # TBPF_NOPROGRESS

    def set_error(self, hwnd):
        self._call(10, hwnd, 4, types=(ctypes.c_void_p, ctypes.c_uint32))  # TBPF_ERROR

    def close(self):
        if self.pointer:
            self._call(2, result_type=ctypes.c_uint32)  # Release
            self.pointer = ctypes.c_void_p()
        if self.initialized:
            self.ole32.CoUninitialize()
            self.initialized = False


class WindowsTaskbarProgress:
    """Called only on the Qt UI thread; failures disable this optional feature once."""

    def __init__(self, log):
        self.log = log
        self.api = None
        self.message_id = 0
        self.ready = False
        self.disabled = not IS_WINDOWS
        self.pending = None
        self.last = None
        self.error = False
        if IS_WINDOWS:
            try:
                user32 = ctypes.WinDLL("user32")
                user32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
                user32.RegisterWindowMessageW.restype = ctypes.c_uint32
                self.message_id = user32.RegisterWindowMessageW("TaskbarButtonCreated")
                if not self.message_id:
                    raise OSError("RegisterWindowMessageW failed")
            except Exception:
                self._disable()

    def _disable(self):
        if not self.disabled:
            self.disabled = True
            self.log("⚠️ Windows 任务栏进度不可用，本次运行不再尝试；刮削继续。")
        self.close()

    def native_message(self, pointer):
        if self.disabled:
            return
        from ctypes.wintypes import MSG

        message = MSG.from_address(pointer)
        if message.message == self.message_id:
            self.ready = True
            self.last = None  # Also restore progress after Explorer restarts.
            self._apply()

    def update(self, hwnd, completed, total):
        if self.disabled:
            return
        total = max(0, int(total))
        completed = min(max(0, int(completed)), total)
        self.pending = (hwnd, completed, total)
        self._apply()

    def show_error(self, hwnd, completed, total):
        if not self.error:
            self.last = None
        self.error = True
        self.update(hwnd, max(1, completed), max(1, total))

    def clear(self, hwnd):
        self.error = False
        self.update(hwnd, 0, 0)

    def _apply(self):
        if self.disabled or not self.ready or self.pending is None or self.pending == self.last:
            return
        try:
            if self.api is None:
                self.api = _TaskbarAPI()
            hwnd, completed, total = self.pending
            if total:
                self.api.set_progress(hwnd, completed, total)
                if self.error:
                    self.api.set_error(hwnd)
            else:
                self.api.clear(hwnd)
            self.last = self.pending
        except Exception:
            self._disable()

    def close(self):
        if self.api is not None:
            try:
                self.api.close()
            except Exception:
                pass
            finally:
                self.api = None
