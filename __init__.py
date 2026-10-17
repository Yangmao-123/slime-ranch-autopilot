"""NEKO 插件入口 —— 史莱姆牧场自动游玩。

NEKO 插件进程是本插件的"薄壳"：负责启动/停止独立自动化核心子进程、同步配置、
把核心产生的消息推送到 NEKO 聊天/HUD、以及提供 Hosted UI 控制台。
重逻辑（游戏画面识别、键鼠控制、语音识别、自主状态机）在 core/ 独立进程里运行。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional

try:
    from plugin.sdk.plugin import (
        NekoPluginBase,
        Ok,
        Err,
        SdkError,
        lifecycle,
        neko_plugin,
        plugin_entry,
        tr,
        ui,
    )
except Exception:  # pragma: no cover - 仅在本机无 SDK 时跳过，真实 NEKO 环境会注入
    # 给出占位，便于静态扫描 / 冒烟测试在无 SDK 环境下不崩溃。
    def _nop_logger(*_a, **_k):  # type: ignore
        return None

    class _NekoPluginBasePlaceholder:  # type: ignore
        """无 SDK 环境的占位基类：与真实 NekoPluginBase 一样接收 ctx，并提供极简 logger。"""

        def __init__(self, ctx=None):  # type: ignore
            self.ctx = ctx
            self.config = {}
            self.logger = type("_Logger", (), {"info": _nop_logger, "warning": _nop_logger,
                                               "error": _nop_logger, "exception": _nop_logger})

    NekoPluginBase = _NekoPluginBasePlaceholder  # type: ignore

    def neko_plugin(cls):  # type: ignore
        return cls

    def plugin_entry(*a, **k):  # type: ignore
        def deco(fn):
            return fn

        return deco

    def lifecycle(*a, **k):  # type: ignore
        def deco(fn):
            return fn

        return deco

    def message(*a, **k):  # type: ignore
        def deco(fn):
            return fn

        return deco

    def ui():  # type: ignore
        class _UI:
            def context(self, *a, **k):
                return lambda fn: fn

            def action(self, *a, **k):
                return lambda fn: fn

        return _UI()

    ui = ui()  # type: ignore

    def Ok(v):  # type: ignore
        return v

    def Err(e):  # type: ignore
        return e

    def SdkError(message="", code=None, **kw):  # type: ignore
        return message

    def tr(key, default="", **p):  # type: ignore
        return default or key


PLUGIN_ID = "slime_ranch_autopilot"


def _get_cfg(self, key: str, default: Any) -> Any:
    """读取 [slime_ranch] 节配置。配置在 startup 时经 self.config 异步 dump 缓存到 self._cfg。"""
    section = self._cfg.get("slime_ranch", {}) or {}
    if not isinstance(section, dict):
        section = {}
    value = section.get(key, default) if isinstance(section, dict) else default
    return default if value in (None, "") else value


@neko_plugin
class SlimeRanchAutopilotPlugin(NekoPluginBase):
    def __init__(self, ctx=None):
        # ctx 由 NEKO 宿主传入，必须透传给基类（否则 "takes 1 positional argument
        # but 2 were given" 会在这里抛 TypeError 导致进程崩溃）。
        super().__init__(ctx)
        _base_logger = getattr(self, "logger", None)  # 先读基类可能提供的 logger
        _enable_logging = getattr(self, "enable_file_logging", None)
        try:
            self.file_logger = _enable_logging(log_level="INFO") if callable(_enable_logging) else None
        except Exception:
            self.file_logger = None
        self.logger = self.file_logger if self.file_logger is not None else (_base_logger or self)
        self._proc: Optional[subprocess.Popen] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._cfg: dict = {}
        # 运行目录：与自动化子进程共享的 JSON 交换目录。SDK 未提供可写 data_path
        # 的稳定约定，改放到用户临时目录下（写权限有保证、跨进程可访问）。
        _base = os.path.join(tempfile.gettempdir(), "neko-slime-ranch-autopilot")
        self._runtime_dir: str = os.path.join(_base, "runtime")
        try:
            os.makedirs(self._runtime_dir, exist_ok=True)
        except Exception:
            pass
        self._seen_msg: int = 0

    # ------------------------------------------------------------------ 生命周期
    @lifecycle(id="startup", name="启动", description="启动时同步配置（不自动拉起自动化核心，需手动点「启动自动游玩」）")
    async def startup(self, **_):
        # 配置为异步代理，用 dump 一次性读入并缓存到 self._cfg；不能同步 .get。
        try:
            cfg = await self.config.dump(timeout=5.0)
            self._cfg = cfg if isinstance(cfg, dict) else {}
        except Exception:
            self._cfg = {}
        try:
            os.makedirs(self._runtime_dir, exist_ok=True)
        except Exception:
            pass
        self._sync_config_to_disk()
        self._archive_outbox()
        self._poll_task = asyncio.create_task(self._poll_outbox_loop())
        # 插件被激活/随总开关启动时，不再自动拉起自动化核心：
        # 核心只在用户在面板点「启动自动游玩」(start_autopilot) 时才 spawn，
        # 避免"开总开关 -> 静默拉起核心 -> 核心一启动就尝试唤起游戏窗口"这条链路。
        self.logger.info("slime_ranch_autopilot 已启动(runtime=%s)", self._runtime_dir)
        return Ok({"started": True})

    @lifecycle(id="shutdown", name="关闭", description="关闭时停止自动化核心")
    async def shutdown(self, **_):
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        self._stop_core()
        return Ok({"stopped": True})

    # ------------------------------------------------------------------ 配置同步
    def _sync_config_to_disk(self) -> None:
        cfg = {
            "timeout_seconds": int(_get_cfg(self, "timeout_seconds", 10)),
            "default_mode": _get_cfg(self, "default_mode", "watch"),
            "game_exe": _get_cfg(self, "game_exe", ""),
            "auto_launch_game": bool(_get_cfg(self, "auto_launch_game", False)),
            "engine_python": _get_cfg(self, "engine_python", ""),
            "backend": _get_cfg(self, "backend", "ws"),
            "bridge_host": _get_cfg(self, "bridge_host", "127.0.0.1"),
            "bridge_port": int(_get_cfg(self, "bridge_port", 8124)),
            "calibration": _get_cfg(self, "calibration", {}) or {},
            "live_fallback_funds": int(_get_cfg(self, "live_fallback_funds", 1000)),
            "cycle_seconds": max(5, int(_get_cfg(self, "cycle_seconds", 30))),
        }
        path = os.path.join(self._runtime_dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ 核心进程
    def _spawn_core(self) -> bool:
        self._stop_core()
        # _stop_core 会写入一条 stop 指令；新核心启动后第一条 read_command()
        # 就会读到它并立即退出，因此必须在拉起新核心前清空 command.json。
        self._clear_command()

        base = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(base, "core", "main_controller.py")
        if not os.path.exists(script):
            self.logger.warning("找不到自动化核心脚本 %s", script)
            return False

        py = _get_cfg(self, "engine_python", "") or sys.executable
        cmd = [py, script, "--data-dir", self._runtime_dir, "--config",
               os.path.join(self._runtime_dir, "config.json")]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=base,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.logger.info("自动化核心已启动 pid=%s", self._proc.pid)
            return True
        except Exception as e:  # pragma: no cover
            self.logger.error("启动自动化核心失败: %s", e)
            self._proc = None
            return False

    def _stop_core(self) -> None:
        self._write_command({"type": "stop"})
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    def _write_command(self, data: dict) -> None:
        if not self._runtime_dir:
            return
        path = os.path.join(self._runtime_dir, "command.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def _clear_command(self) -> None:
        """清空待处理指令，避免新核心启动时读到上一条 stop。"""
        if not self._runtime_dir:
            return
        path = os.path.join(self._runtime_dir, "command.json")
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _archive_outbox(self) -> None:
        """启动时把上一轮的 outbox 挪走，避免历史消息被重新念一遍。"""
        self._seen_msg = 0
        if not self._runtime_dir:
            return
        path = os.path.join(self._runtime_dir, "outbox.jsonl")
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return
        dest = os.path.join(self._runtime_dir, "outbox.prev.jsonl")
        try:
            if os.path.exists(dest):
                os.remove(dest)
            os.replace(path, dest)
        except Exception:
            pass

    # ------------------------------------------------------------------ 出站消息
    def _poll_outbox(self) -> list[dict]:
        """读取核心写出的 outbox.jsonl 中尚未推送的消息。"""
        out = []
        if not self._runtime_dir:
            return out
        path = os.path.join(self._runtime_dir, "outbox.jsonl")
        if not os.path.exists(path):
            return out
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for i in range(self._seen_msg, len(lines)):
                line = lines[i].strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
            self._seen_msg = len(lines)
        except Exception:
            pass
        return out

    async def _poll_outbox_loop(self) -> None:
        while True:
            try:
                for item in self._poll_outbox():
                    self._relay(item)
            except Exception:
                pass
            await asyncio.sleep(1.0)

    def _relay(self, item: dict) -> None:
        kind = item.get("kind", "info")
        text = item.get("text", "")
        if not text:
            return
        try:
            self.push_message(
                source="slime_ranch_autopilot",
                visibility=[],
                ai_behavior="respond",
                parts=[{"type": "text", "text": text}],
                priority=5,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ Hosted UI
    @ui.context(id="dashboard")
    async def _dashboard(self):
        state = self._read_state()
        return {
            "running": self._proc is not None and self._proc.poll() is None
            if self._proc else False,
            "mode": state.get("mode", "unknown"),
            "phase": state.get("phase", "idle"),
            "funds": state.get("funds", 0),
            "discovery": state.get("last_discovery", ""),
            "reports": state.get("reports", []),
            "timeout_seconds": int(_get_cfg(self, "timeout_seconds", 10)),
            "cycle_seconds": int(_get_cfg(self, "cycle_seconds", 30)),
            "pid": self._proc.pid if self._proc else None,
        }

    def _read_state(self) -> dict:
        if not self._runtime_dir:
            return {}
        path = os.path.join(self._runtime_dir, "state.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    # ------------------------------------------------------------------ 动作入口
    @ui.action(label=tr("actions.start", default="启动自动游玩"), tone="primary", refresh_context=True)
    @plugin_entry(
        id="start_autopilot",
        name=tr("entries.start.name", default="启动自动游玩"),
        description=tr("entries.start.desc", default="启动自动化核心并接管史莱姆牧场"),
    )
    async def start_autopilot(self, **_):
        self._sync_config_to_disk()
        if self._proc and self._proc.poll() is None:
            return Ok({"started": True, "alive": True})
        ok = self._spawn_core()
        return Ok({"started": ok, "alive": ok}) if ok else Err(SdkError("启动核心失败，请检查引擎与数据目录"))

    @ui.action(label=tr("actions.stop", default="停止自动游玩"), tone="danger", refresh_context=True)
    @plugin_entry(
        id="stop_autopilot",
        name=tr("entries.stop.name", default="停止自动游玩"),
        description=tr("entries.stop.desc", default="停止自动化核心"),
    )
    async def stop_autopilot(self, **_):
        self._stop_core()
        return Ok({"stopped": True})

    @ui.action(label=tr("actions.setMode", default="切换模式"), refresh_context=True)
    @plugin_entry(
        id="set_mode",
        name=tr("entries.setmode.name", default="设置模式"),
        description=tr("entries.setmode.desc", default="watch 观望 / ranch 自主经营"),
        input_schema={
            "type": "object",
            "properties": {"mode": {"type": "string", "description": tr("fields.mode", default="watch 或 ranch")}},
            "required": ["mode"],
        },
    )
    async def set_mode(self, mode: str = "watch", **_):
        mode = mode.strip().lower()
        if mode == "chat":
            mode = "watch"
        elif mode == "voice":
            mode = "ranch"
        if mode not in ("watch", "ranch"):
            return Err(SdkError("mode 只支持 watch 或 ranch"))
        self._write_command({"type": "set_mode", "mode": mode})
        return Ok({"mode": mode})

    @ui.action(label=tr("actions.setTimeout", default="保存秒数"), refresh_context=True)
    @plugin_entry(
        id="set_timeout",
        name=tr("entries.settimeout.name", default="设置接管秒数"),
        description=tr("entries.settimeout.desc", default="语音模式下超过该秒数未说话则自主接管"),
        input_schema={
            "type": "object",
            "properties": {"seconds": {"type": "integer", "description": tr("fields.seconds", default="秒数")}},
            "required": ["seconds"],
        },
    )
    async def set_timeout(self, seconds: int = 10, **_):
        seconds = max(1, int(seconds))
        section = dict(self._cfg.get("slime_ranch", {}) or {})
        section["timeout_seconds"] = seconds
        self._cfg["slime_ranch"] = section
        self._sync_config_to_disk()
        self._write_command({"type": "set_timeout", "seconds": seconds})
        return Ok({"timeout_seconds": seconds})

    @ui.action(label=tr("actions.setCycle", default="保存循环时间"), refresh_context=True)
    @plugin_entry(
        id="set_cycle",
        name=tr("entries.setcycle.name", default="设置循环时间"),
        description=tr("entries.setcycle.desc", default="自主经营一轮完整循环的间隔秒数（默认30s）"),
        input_schema={
            "type": "object",
            "properties": {"seconds": {"type": "integer", "description": tr("fields.cycleSeconds", default="循环秒数")}},
            "required": ["seconds"],
        },
    )
    async def set_cycle(self, seconds: int = 30, **_):
        seconds = max(5, int(seconds))
        section = dict(self._cfg.get("slime_ranch", {}) or {})
        section["cycle_seconds"] = seconds
        self._cfg["slime_ranch"] = section
        self._sync_config_to_disk()
        self._write_command({"type": "set_cycle", "seconds": seconds})
        return Ok({"cycle_seconds": seconds})

    @ui.action(label=tr("actions.pushCmd", default="执行指令"), refresh_context=True)
    @plugin_entry(
        id="push_command",
        name=tr("entries.pushcmd.name", default="执行指令"),
        description=tr("entries.pushcmd.desc", default="向核心下达一条文字指令(用于测试或远程指挥)"),
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "description": tr("fields.text", default="指令内容")}},
            "required": ["text"],
        },
    )
    async def push_command(self, text: str = "", **_):
        if not text.strip():
            return Err(SdkError("指令为空"))
        self._write_command({"type": "command", "text": text})
        return Ok({"text": text})

    @ui.action(label=tr("actions.capture", default="校准截图"), refresh_context=True)
    @plugin_entry(
        id="capture_calibration",
        name=tr("entries.capture.name", default="校准截图"),
        description=tr("entries.capture.desc", default="截取当前游戏画面存为 PNG，用于校准资金区域与按钮坐标"),
    )
    async def capture_calibration(self, **_):
        self._write_command({"type": "command", "text": "校准截图"})
        return Ok({"queued": True, "note": "已触发校准截图，稍后查看核心输出的保存路径"})

    @plugin_entry(
        id="chat_recv",
        name=tr("entries.chat_recv.name", default="聊天消息"),
        description=tr("entries.chat_recv.desc", default="接收聊天消息"),
    )
    async def _on_chat(self, _id: Optional[str] = None, source: Optional[str] = None, payload: Optional[Any] = None, **_):
        # 聊天模式：玩家与 NEKO 的对话不作为游戏指令。这里仅记录，交由主 LLM 对话。
        return Ok({"handled": False, "note": "chat mode 下语音/聊天仅用于对话，不当作游戏指令"})