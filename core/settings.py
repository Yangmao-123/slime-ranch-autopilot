"""配置加载/保存。核心从 config.json 读取参数，并把运行时配置写回。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from typing import Dict, Any


DEFAULT_CONFIG: Dict[str, Any] = {
    "timeout_seconds": 10,
    "default_mode": "watch",
    "game_exe": "",
    "auto_launch_game": False,
    "enable_drones": False,   # 无人机搬运（实验）：当前生成的无人机缺初始化会破坏存档，保持 false
    "cycle_seconds": 30,      # 经营循环时间（秒）：市场→围栏收取→满包回卖→直到清空，一轮隔这么久再跑
    "engine_python": "",
    "backend": "hybrid",  # hybrid=混合(桥接感知+真实键鼠执行,推荐)；ws=桥接；sim=模拟；live=键鼠校准
    # live 校准：money_region={left,top,width,height}(0..1 归一化)、
    # 以及按钮点位 btn_confirm/btn_sell_all/btn_home=(fx,fy)。缺省用通用键盘操作。
    "bridge_host": "127.0.0.1",   # ws 后端：SlimeBridge 服务主机(仅本机)
    "bridge_port": 8124,          # ws 后端：SlimeBridge 服务端口
    "calibration": {},
}


def _resolve_game_exe(explicit: str) -> str:
    """配置路径不存在时，到常见 Steam 目录里找一代游戏。找不到就留空。"""
    if explicit and os.path.exists(explicit):
        return explicit
    roots = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Steam", "steamapps", "common", "Slime Rancher", "SlimeRancher.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Steam", "steamapps", "common", "Slime Rancher", "SlimeRancher.exe"),
    ]
    for drive in ("C", "D", "E", "F", "G"):
        roots.append(rf"{drive}:\SteamLibrary\steamapps\common\Slime Rancher\SlimeRancher.exe")
        roots.append(rf"{drive}:\Steam\steamapps\common\Slime Rancher\SlimeRancher.exe")
    for path in roots:
        if path and os.path.exists(path):
            return path
    return ""


def _normalize_mode(value: Any) -> str:
    """watch=观望，ranch=自主经营。旧配置 chat/voice 映射过来。"""
    mode = str(value or "watch").strip().lower()
    if mode == "voice":
        return "ranch"
    if mode == "ranch":
        return "ranch"
    return "watch"


@dataclass
class Settings:
    data_dir: str = ""
    timeout_seconds: int = 10
    default_mode: str = "watch"      # watch 观望 / ranch 自主经营
    game_exe: str = ""
    auto_launch_game: bool = False
    backend: str = "sim"             # sim | live | ws
    bridge_host: str = "127.0.0.1"   # ws 后端主机
    bridge_port: int = 8124          # ws 后端端口
    sim_interval: float = 2.0        # 自主每轮间隔秒数
    cycle_seconds: int = 30          # 经营循环时间（秒），面板可改
    # 模拟层内部状态初始值
    sim_start_funds: int = 800
    sim_inventory: Dict[str, int] = field(default_factory=dict)
    resolution: str = "1280x720"
    calibration: Dict[str, Any] = field(default_factory=dict)  # live 校准配置
    # 当 OCR 不可用(未装 tesseract) / 读取失败时，读取资金退回该固定值：
    #   值含义：-1 = 未知(不主动建造)；>=0 表示按该保底资金决策。
    #   先用 >=0 的可见值让自主游玩在无 OCR 环境也能跑起来。
    live_fallback_funds: int = 1000

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def load(data_dir: str, config_path: str = "") -> "Settings":
        cfg: Dict[str, Any] = dict(DEFAULT_CONFIG)
        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                cfg.update({k: v for k, v in loaded.items() if v not in (None, "")})
            except Exception:
                pass
        settings = Settings(data_dir=data_dir)
        settings.timeout_seconds = max(1, int(cfg.get("timeout_seconds", 10)))
        settings.default_mode = _normalize_mode(cfg.get("default_mode", "watch"))
        settings.game_exe = _resolve_game_exe(str(cfg.get("game_exe", "") or ""))
        settings.auto_launch_game = bool(cfg.get("auto_launch_game", False))
        settings.backend = cfg.get("backend", "sim")
        settings.bridge_host = cfg.get("bridge_host", "127.0.0.1")
        try:
            settings.bridge_port = int(cfg.get("bridge_port", 8124))
        except (TypeError, ValueError):
            settings.bridge_port = 8124
        if isinstance(cfg.get("sim_start_funds"), (int, float)):
            settings.sim_start_funds = int(cfg["sim_start_funds"])
        try:
            settings.cycle_seconds = max(5, int(cfg.get("cycle_seconds", 30)))
        except (TypeError, ValueError):
            settings.cycle_seconds = 30
        if isinstance(cfg.get("sim_inventory"), dict):
            settings.sim_inventory = cfg["sim_inventory"]
        if isinstance(cfg.get("calibration"), dict):
            settings.calibration = cfg["calibration"]
        if "live_fallback_funds" in cfg:
            try:
                settings.live_fallback_funds = int(cfg["live_fallback_funds"])
            except (TypeError, ValueError):
                pass
        return settings


def ensure_config(data_dir: str, overrides: Dict[str, Any] | None = None) -> str:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "config.json")
    cfg: Dict[str, Any] = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v not in (None, "")})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return path