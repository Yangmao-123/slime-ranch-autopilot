"""V 键热键：在观望和自主经营之间切换。

优先用 pynput。没有 pynput 时，在 Windows 上用系统低级键盘钩子，同样不要求游戏窗口在前台。
"""
from __future__ import annotations
import sys
import threading
import time
from typing import Callable, Optional

try:
    import pynput.keyboard  # type: ignore
    _HAS_PYNPUT = True
except Exception:  # pragma: no cover
    _HAS_PYNPUT = False

try:
    import sounddevice as sd  # type: ignore
    import numpy as np  # type: ignore
    _HAS_AUDIO = True
except Exception:  # pragma: no cover
    _HAS_AUDIO = False

_whisper_model = None


class VoiceInput:
    """负责"采集一段语音 -> 转写为文字"。"""

    def __init__(self, lang: str = "zh"):
        self.lang = lang
        self.enabled = _HAS_AUDIO

    def _load_model(self):
        global _whisper_model
        if _whisper_model is None:
            from faster_whisper import WhisperModel  # type: ignore
            _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
        return _whisper_model

    def listen_once(self, seconds: float = 3.0) -> Optional[str]:
        """录 seconds 秒并转写。返回文字；失败/静音返回 None。"""
        if not self.enabled:
            return None
        try:
            import sounddevice as sd
            import numpy as np
            fs = 16000
            audio = sd.rec(int(fs * seconds), samplerate=fs, channels=1, dtype="float32")
            sd.wait()
            audio = np.squeeze(audio)
            if audio is None or not np.any(audio):
                return None
            model = self._load_model()
            segments, _ = model.transcribe(audio, language=self.lang)
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return text or None
        except Exception:
            return None


class HotkeyListener:
    """全局低延迟热键。回调必须短小，重逻辑放线程/队列。

    V 键在观望和自主经营之间切换。
    """

    def __init__(self, on_toggle: Callable[[], None]):
        self.on_toggle = on_toggle
        self._last_toggle_ts = 0.0          # 防抖：按住 V 时系统自动重复触发，只响应首次
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._hook_ready = threading.Event()
        self._hook_ok = False
        self.backend = ""                    # 实际生效的热键后端(win32-hook / pynput)

    def _fire(self) -> None:
        now = time.time()
        if now - self._last_toggle_ts <= 0.35:
            return
        self._last_toggle_ts = now
        self.toggle()

    def _listen(self) -> None:
        def on_press(key):
            if getattr(key, "char", None) == "v":
                self._fire()

        def on_release(key):
            return True

        try:
            with pynput.keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
                self._stop.wait()
                listener.stop()
        except Exception:
            pass

    def _listen_win32(self) -> None:
        import ctypes
        from ctypes import wintypes

        WH_KEYBOARD_LL = 13
        WM_KEYDOWN = 0x0100
        PM_REMOVE = 1
        VK_V = 0x56
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        def handler(nCode, wParam, lParam):
            if nCode >= 0 and wParam == WM_KEYDOWN:
                info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if info.vkCode == VK_V and not (info.flags & 0x10):
                    self._fire()
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._hook_proc = HOOKPROC(handler)
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc, kernel32.GetModuleHandleW(None), 0)
        self._hook_ok = bool(hook)
        self._hook_ready.set()
        if not hook:
            return
        msg = wintypes.MSG()
        try:
            while not self._stop.is_set():
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.01)
        finally:
            user32.UnhookWindowsHookEx(hook)

    def start(self) -> bool:
        """挂上全局热键，返回是否成功。

        Windows 上优先用低级键盘钩子（按 vkCode 判定，**免疫中文输入法**——pynput 的
        key.char=='v' 在 IME 开着时收不到裸 'v'，这是 V 键失灵的主因），失败再退 pynput
        （pynput 挂掉时线程会静默退出，故用 is_alive 校验）。
        """
        if sys.platform == "win32":
            self._hook_ready.clear()
            self._thread = threading.Thread(target=self._listen_win32, daemon=True)
            self._thread.start()
            self._hook_ready.wait(1.0)
            if self._hook_ok:
                self.backend = "win32-hook"
                return True
        if _HAS_PYNPUT:
            t = threading.Thread(target=self._listen, daemon=True)
            t.start()
            time.sleep(0.4)
            if t.is_alive():
                self._thread = t
                self.backend = "pynput"
                return True
        return False

    def stop(self) -> None:
        self._stop.set()

    def toggle(self):
        if self.on_toggle:
            try:
                self.on_toggle()
            except Exception:
                pass