"""核心 <-> NEKO 插件 之间的文件握手。

- state.json        核心 -> 插件 ：当前状态快照（供 Hosted UI / 状态面板）。
- outbox.jsonl      核心 -> 插件 ：逐行 JSON 出站消息（插件会 push_message 到聊天/HUD）。
- command.json      插件 -> 核心 ：控制指令（stop / set_mode / set_timeout / command）。
- 本模块同时负责本地状态维护与"新发现汇报"去重。
"""
from __future__ import annotations
import json
import os
import time
from typing import Dict, Any, List


class Comms:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        # 独立进程里的通用 logger，供 vision 等内部模块记录 non-fatal 信息
        import logging
        self.logger = logging.getLogger("slime_ranch_autopilot.core")
        self.state_path = os.path.join(data_dir, "state.json")
        self.outbox_path = os.path.join(data_dir, "outbox.jsonl")
        self.command_path = os.path.join(data_dir, "command.json")
        self._seen_discoveries: set[str] = set()
        self._state: Dict[str, Any] = {
            "running": True,
            "mode": "chat",
            "phase": "booting",
            "funds": 0,
            "last_discovery": "",
            "reports": [],
        }

    # ------------------------------------------------------------ 状态
    def set_state(self, **kwargs) -> None:
        self._state.update(kwargs)
        self._state["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get_state(self) -> Dict[str, Any]:
        return dict(self._state)

    # ------------------------------------------------------------ 出站
    def emit(self, kind: str, text: str) -> bool:
        line = json.dumps({"ts": time.strftime("%H:%M:%S"), "kind": kind, "text": text}, ensure_ascii=False)
        try:
            with open(self.outbox_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return True
        except Exception:
            self.logger.warning("核心消息写入失败，将在后续扫描重试")
            return False

    def report_discovery(self, what: str, text: str) -> bool:
        """有新发现时汇报（同类发现只报一次），并写入 state.reports。"""
        if what in self._seen_discoveries:
            return True
        if not self.emit("discovery", f"[新发现] {text}"):
            return False
        self._seen_discoveries.add(what)
        self._state["last_discovery"] = text
        self._state.setdefault("reports", []).append(text)
        self._state["reports"] = self._state["reports"][-12:]
        self.set_state()
        return True

    def say(self, text: str) -> None:
        self.emit("info", text)

    # ------------------------------------------------------------ 入站
    def read_command(self) -> Dict[str, Any] | None:
        """取走一条指令；每次只处理最新一条。"""
        if not os.path.exists(self.command_path):
            return None
        try:
            # utf-8-sig 兼容带 BOM 的写入（如某些脚本/编辑器生成的命令文件）
            with open(self.command_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            os.remove(self.command_path)
            return data
        except Exception:
            return None
