"""桥接层（ws backend）：通过 HTTP REST 连接游戏内 UMF mod(SlimeBridge)。

SlimeBridge 在游戏里开 http://127.0.0.1:<port>/ 提供：
  GET  /api/status   -> {money, facilities, plorts, area, game_loaded, ...}
  POST /api/action   -> {"action": "...", "kind": "..."} -> {ok, gains, caught, ...}
  GET  /api/health   -> 存活探针

本类实现 IOController 全部方法，因此可无缝接入大脑状态机（不依赖键鼠/OCR，读到的
资金与设施来自游戏内部 mod，比 live 的截图方案准确得多）。

只依赖标准库 urllib，无需额外 pip 包。
"""
from __future__ import annotations

import abc
import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from typing import Dict, Any, List

_BRIDGE_ACTIONS = {
    "build_corral": ("build", "corral"),
    "build_coop": ("build", "coop"),
    "build_garden": ("build", "garden"),
    "upgrade_corral": ("upgrade", "corral"),
    "upgrade_coop": ("upgrade", "coop"),
    "upgrade_garden": ("upgrade", "garden"),
    "feed": ("feed", ""),
    "harvest": ("harvest", ""),
    "sell": ("sell", ""),
    "explore": ("explore", ""),
    "go_home": ("go_home", ""),
}


class BridgeClient:
    """极简 HTTP 客户端（标准库）。"""

    def __init__(self, host: str, port: int, timeout: float = 3.0):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout

    def get_json(self, path: str) -> Dict[str, Any] | None:
        try:
            with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    def post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None


class BridgeIOController:
    """连接 SlimeBridge 的 IOController 实现（ws backend）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8124,
                 fallback_funds: int = -1, game_exe: str = "",
                 auto_launch: bool = False):
        self.client = BridgeClient(host, port)
        self.fallback_funds = fallback_funds  # 桥接读不到资金时的保底值
        self.game_exe = game_exe
        self.auto_launch = bool(auto_launch and game_exe)
        self._launched = False  # 只自动拉起一次，避免重复开游戏
        self._last_enter_note = ""
        self._last_status: Dict[str, Any] = {}
        self._mod_features: List[str] | None = None  # /api/health 能力握手缓存

    # ---------------- mod 能力握手 ----------------
    def mod_info(self) -> Dict[str, Any]:
        """读 mod 的 /api/health：版本 + 能力清单（NEKO 接入契约的一部分）。"""
        h = self.client.get_json("/api/health") or {}
        feats = h.get("features")
        return {
            "ok": bool(h.get("ok")),
            "version": str(h.get("version") or ""),
            "features": [str(f) for f in feats] if isinstance(feats, list) else [],
        }

    def mod_features(self) -> List[str]:
        if self._mod_features is None:
            self._mod_features = self.mod_info().get("features", [])
        return self._mod_features

    def supports(self, feature: str) -> bool:
        return feature in self.mod_features()

    # ---------------- IOController 接口 ----------------
    def enter_game(self) -> Dict[str, Any]:
        if not self.is_alive():
            if self.auto_launch and not self._launched \
                    and self.game_exe and os.path.exists(self.game_exe):
                self._launched = True
                try:
                    subprocess.Popen([self.game_exe], cwd=os.path.dirname(self.game_exe))
                except Exception as e:
                    return {"ok": False, "note": "启动游戏失败: %s" % e}
                for _ in range(60):  # 最多等 60s，等游戏内 mod 起 HTTP 服务
                    time.sleep(1.0)
                    if self.is_alive():
                        break
                if not self.is_alive():
                    return {"ok": False, "note": "游戏已启动，但 SlimeBridge 服务尚未就绪（检查游戏是否加载 Mods/SlimeBridge.dll）"}
            else:
                return {"ok": False, "note": "未检测到 SlimeBridge 服务。请先启动游戏并加载 Mods/SlimeBridge.dll，"
                                             "确保 http://127.0.0.1:8124 可访问"}
        # 已连上桥：若停在主菜单则自动读最近存档（游戏启动早期 MainMenuUI 尚未生成，
        # 需要重试 continue 直到真正进入存档）。
        st = self._get_status()
        if not bool(st.get("game_loaded")):
            last_note = ""
            for _ in range(20):  # 最多约 100s：每 5s 尝试一次自动读档
                res = self._post_action("continue")
                last_note = res.get("note", "")
                for _ in range(5):  # 尝试后轮询 5s
                    time.sleep(1.0)
                    st = self._get_status()
                    if bool(st.get("game_loaded")):
                        break
                if bool(st.get("game_loaded")):
                    break
            if not bool(st.get("game_loaded")):
                self._last_enter_note = last_note
        if bool(st.get("game_loaded")):
            time.sleep(2.0)  # 进档后稍等场景稳定
            st = self._get_status()
            return {"ok": True, "note": "已连接 SlimeBridge，进入存档成功（资金 $%s）" % st.get("money")}
        return {"ok": False, "note": "SlimeBridge 已连接，但尚未进入存档（%s；可尝试手动在主菜单点继续）"
                                    % (self._last_enter_note or "自动读档未生效")}

    def is_alive(self) -> bool:
        h = self.client.get_json("/api/health")
        return bool(h and h.get("ok") is True)

    def read_money(self) -> int:
        st = self._get_status()
        money = st.get("money")
        if money is None or money < 0:
            return self.fallback_funds
        return int(money)

    def get_facilities(self) -> Dict[str, int]:
        """返回 {kind: 已有地块数}，用于初始化状态机的建设进度（已建=有 1 级）。"""
        st = self._get_status()
        fac = st.get("facilities") or {}
        out: Dict[str, int] = {}
        for kind in ("corral", "coop", "garden"):
            d = fac.get(kind) or {}
            count = int(d.get("count", 0) or 0)
            if count > 0:
                out[kind] = 1  # 已有该设施 = 视为已建到 Lv1，之后走升级
        return out

    def _get_status(self) -> Dict[str, Any]:
        st = self.client.get_json("/api/status") or {}
        self._last_status = st
        return st

    def _post_action(self, action: str, kind: str = "") -> Dict[str, Any]:
        res = self.client.post_json("/api/action", {"action": action, "kind": kind}) or {}
        return res

    def scan_target(self, radius: float = 25.0) -> Dict[str, Any]:
        """混合模式：向 mod 查询附近史莱姆/结晶的精确感知数据（纯只读，无副作用）。

        返回：
          {"ok": True, "kind": "slime|plort", "dist": 米, "yaw": 度,
           "px","py": 玩家坐标, "tx","tz": 目标坐标}
          或 {"ok": False, ...}（SEARCH 半径内无目标 / 未进存档 / 服务不可达）。
        约定 yaw = mod 的 yaw_delta：目标相对玩家偏角，负=目标在右侧。
        """
        res = self._post_action("target")
        if not res.get("ok"):
            return {"ok": False, "dist": 1e9, "yaw": 0.0,
                    "note": res.get("note", "附近无可抓捕目标")}
        def f(key, default):
            try:
                v = res.get(key, default)
                return float(v) if v is not None and v != "" else default
            except Exception:
                return default
        return {
            "ok": True,
            "kind": res.get("target_kind", ""),
            "dist": f("dist", 1e9),
            "yaw": f("yaw_delta", 0.0),
            "px": f("px", 0.0), "py": f("py", 0.0),
            "tx": f("tx", 0.0), "tz": f("tz", 0.0),
            "pitch": f("pitch", 0.0),                     # 相机→目标俯仰差(度, 正=需抬头)
            "note": res.get("note", ""),
        }

    def scan_corral(self) -> Dict[str, Any]:
        """混合模式：向 mod 查询最近围栏(CORRAL)的方向/距离，供真实投放史莱姆。"""
        res = self._post_action("scan_corral")
        if not res.get("ok"):
            return {"ok": False, "dist": 1e9, "yaw": 0.0,
                    "note": res.get("note", "场景中没有围栏")}
        def f(key, default):
            try:
                v = res.get(key, default)
                return float(v) if v is not None and v != "" else default
            except Exception:
                return default
        return {
            "ok": True,
            "kind": res.get("target_kind", "corral"),
            "dist": f("dist", 1e9),
            "yaw": f("yaw_delta", 0.0),
            "px": f("px", 0.0), "py": f("py", 0.0),
            "tx": f("tx", 0.0), "tz": f("tz", 0.0),
            "pitch": f("pitch", 0.0),                     # 相机→目标俯仰差(度, 正=需抬头)
            "note": res.get("note", ""),
        }

    def scan_market(self) -> Dict[str, Any]:
        """混合模式：向 mod 查询结晶市场的位置/方向，供真实走到市场投出结晶换钱。"""
        res = self._post_action("scan_market")
        if not res.get("ok"):
            return {"ok": False, "dist": 1e9, "yaw": 0.0,
                    "note": res.get("note", "场景中找不到结晶市场")}
        def f(key, default):
            try:
                v = res.get(key, default)
                return float(v) if v is not None and v != "" else default
            except Exception:
                return default
        try:
            pitch = f("pitch", 0.0)
        except Exception:
            pitch = 0.0
        return {
            "ok": True,
            "kind": res.get("target_kind", "market"),
            "dist": f("dist", 1e9),
            "yaw": f("yaw_delta", 0.0),
            "px": f("px", 0.0), "py": f("py", 0.0),
            "tx": f("tx", 0.0), "tz": f("tz", 0.0),       # 走向目标 = 市场投入口正面站位点
            "cx": f("cx", 0.0), "cz": f("cz", 0.0),       # 市场中心（投掷对准目标）
            "cyaw": f("cyaw", 0.0),
            "pitch": pitch,                              # 相机→投入口俯仰角差(度, 正=需抬头)
            "area": res.get("area", ""),
            "note": res.get("note", ""),
        }

    def _scan_aim(self, action: str, empty_note: str) -> Dict[str, Any]:
        res = self._post_action(action)
        if not res.get("ok"):
            return {"ok": False, "dist": 1e9, "yaw": 0.0, "note": res.get("note", empty_note)}
        def f(key, default):
            try:
                v = res.get(key, default)
                return float(v) if v is not None and v != "" else default
            except Exception:
                return default
        return {
            "ok": True,
            "kind": res.get("target_kind", ""),
            "dist": f("dist", 1e9),
            "yaw": f("yaw_delta", 0.0),
            "px": f("px", 0.0), "py": f("py", 0.0),
            "tx": f("tx", 0.0), "tz": f("tz", 0.0),
            "pitch": f("pitch", 0.0),                     # 相机→目标俯仰差(度, 正=需抬头)
            "note": res.get("note", ""),
        }

    def scan_food(self) -> Dict[str, Any]:
        return self._scan_aim("scan_food", "附近没有食物")

    # ---------------- 原生吸取/投掷 ----------------
    def vac_grab(self, kind: str) -> Dict[str, Any]:
        """真空枪原生路径吸取 9m 内最近的食物/结晶（Vacuumable.TryConsume）。"""
        res = self._post_action("vac_grab", kind)
        return {"ok": bool(res.get("ok")), "note": res.get("note", ""),
                "id": res.get("caught", "")}

    def vac_launch(self, target: str) -> Dict[str, Any]:
        """从枪口把台账背包里的物品以真实物理投向 target(market|corral)。"""
        res = self._post_action("vac_launch", target)
        return {"ok": bool(res.get("ok")), "note": res.get("note", ""),
                "id": res.get("caught", "")}

    def scan_plort(self) -> Dict[str, Any]:
        return self._scan_aim("scan_plort", "附近没有结晶")

    # ---------------- 真实建造/升级所需的感知 ----------------
    @staticmethod
    def _payload_dict(res: Dict[str, Any]) -> Dict[str, Any]:
        raw = res.get("payload")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _payload_list(res: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw = res.get("payload")
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except Exception:
            return []
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def ui_hover(self) -> Dict[str, Any]:
        """当前 OS 光标处的 uGUI 射线命中：{"mx","my","n","chain":[命中点沿父链的控件名]}。

        core 用它做点击自校准：先把真实鼠标移到候选点，确认父链里出现目标按钮名再按下，
        任何 DPI 缩放/画布模式/原点差异都被这个闭环吸收。
        """
        res = self._post_action("ui_hover", "")
        out: Dict[str, Any] = {"ok": bool(res.get("ok")), "n": 0, "chain": [], "mx": 0.0, "my": 0.0}
        payload = self._payload_dict(res)
        chain = payload.get("chain")
        out["chain"] = [str(c) for c in chain] if isinstance(chain, list) else []
        try:
            out["n"] = int(payload.get("n") or 0)
            out["mx"] = float(payload.get("mx") or 0.0)
            out["my"] = float(payload.get("my") or 0.0)
        except Exception:
            pass
        return out

    def scan_plot(self, kind: str) -> Dict[str, Any]:
        """混合模式：定位最近的指定类型地块（corral/coop/garden/empty）。

        返回与 scan_target 同构的 yaw/dist（供 real.WalkCaptureIO 做转向/走位反馈），
        payload 里的 isEmpty/upgrades 供决策与购买验证。
        """
        res = self._post_action("scan_plot", kind)
        if not res.get("ok"):
            return {"ok": False, "dist": 1e9, "yaw": 0.0,
                    "note": res.get("note", "没有找到目标地块")}
        out = self._scan_aim_payload(res, kind)
        payload = self._payload_dict(res)
        out["is_empty"] = bool(payload.get("isEmpty"))
        ups = payload.get("upgrades")
        out["upgrades"] = [str(u) for u in ups] if isinstance(ups, list) else []
        return out

    def _scan_aim_payload(self, res: Dict[str, Any], kind: str) -> Dict[str, Any]:
        def f(key: str, default: float) -> float:
            try:
                v = res.get(key, default)
                return float(v) if v is not None and v != "" else default
            except Exception:
                return default
        return {
            "ok": True,
            "kind": kind,
            "dist": f("dist", 1e9),
            "yaw": f("yaw_delta", 0.0),
            "px": f("px", 0.0), "py": f("py", 0.0),
            "tx": f("tx", 0.0), "tz": f("tz", 0.0),
            "pitch": f("pitch", 0.0),                     # 相机→目标俯仰差(度, 正=需抬头)
            "note": res.get("note", ""),
        }

    def scan_facilities(self) -> List[Dict[str, Any]]:
        """全地块清单：[{kind, isEmpty, x, z, upgrades:[...]}]，真实购买验证与进度同步用。"""
        res = self._post_action("scan_facilities", "")
        if not res.get("ok"):
            return []
        return self._payload_list(res)

    def ui_buttons(self) -> Dict[str, Any]:
        """当前可见可点的 uGUI 按钮：{"screen_w", "screen_h", "buttons": [{label, sx, sy}]}。"""
        out: Dict[str, Any] = {"screen_w": 0, "screen_h": 0, "buttons": [], "menu_texts": []}
        res = self._post_action("ui_buttons", "")
        if not res.get("ok"):
            return out
        payload = self._payload_dict(res)
        try:
            out["screen_w"] = int(payload.get("screen_w") or 0)
            out["screen_h"] = int(payload.get("screen_h") or 0)
        except Exception:
            out["screen_w"] = out["screen_h"] = 0
        buttons = payload.get("buttons")
        out["buttons"] = [b for b in buttons if isinstance(b, dict)] if isinstance(buttons, list) else []
        texts = payload.get("menu_texts")
        out["menu_texts"] = [t for t in texts if isinstance(t, dict)] if isinstance(texts, list) else []
        return out

    def tp_market(self) -> Dict[str, Any]:
        res = self._post_action("tp_market")
        return {"ok": res.get("ok", False), "note": res.get("note", "传送到市场失败")}

    def tp_plot(self, kind: str) -> Dict[str, Any]:
        res = self._post_action("tp_plot", kind or "corral")
        return {"ok": res.get("ok", False), "note": res.get("note", "无法出现在地块前")}

    def tp_item(self, kind: str) -> Dict[str, Any]:
        res = self._post_action("tp_item", kind or "food")
        return {"ok": res.get("ok", False), "note": res.get("note", "附近没有可瞬移的目标")}

    def feed_show(self, kind: str = "corral@0") -> Dict[str, Any]:
        res = self._post_action("feed_show", kind or "corral@0")
        return {"ok": res.get("ok", False), "note": res.get("note", "投喂展示失败")}

    def save_now(self) -> Dict[str, Any]:
        res = self._post_action("save_now")
        return {"ok": res.get("ok", False), "note": res.get("note", "保存失败")}

    def drone_setup(self) -> Dict[str, Any]:
        res = self._post_action("drone_setup")
        return {"ok": res.get("ok", False), "note": res.get("note", "无人机没有布置上")}

    def build(self, kind: str) -> Dict[str, Any]:
        st = self._get_status()
        lvl = (st.get("facilities") or {}).get(kind, {}).get("level", 0)
        current_cost = {  # kind -> (建造费, 单级升级费)
            "corral": (225, 350), "coop": (400, 650), "garden": (300, 500),
        }.get(kind, (200, 300))
        if lvl == 0:
            cost = current_cost[0]
        else:
            cost = current_cost[1] * lvl
        res = self._post_action("build", kind)
        resp = {"ok": res.get("ok", False), "facility": kind, "cost": cost}
        resp["note"] = res.get("note", "") or f"桥接请求了建造 {kind}"
        return resp

    def upgrade(self, kind: str) -> Dict[str, Any]:
        st = self._get_status()
        lvl = (st.get("facilities") or {}).get(kind, {}).get("level", 0)
        base = {"corral": 350, "coop": 650, "garden": 500}.get(kind, 300)
        cost = base * max(1, lvl)
        res = self._post_action("upgrade", kind)
        resp = {"ok": res.get("ok", False), "facility": kind, "cost": cost}
        resp["note"] = res.get("note", "") or f"桥接请求了升级 {kind}"
        return resp

    def feed(self) -> Dict[str, Any]:
        res = self._post_action("feed")
        return {"ok": res.get("ok", False), "note": res.get("note", "已桥接投喂")}

    def harvest(self) -> Dict[str, Any]:
        res = self._post_action("harvest")
        # 桥接返回真实收集数量（GameState.HarvestReal 的 count），供状态机决定是否售卖
        return {"ok": res.get("ok", False), "count": int(res.get("count", 0) or 0),
                "note": res.get("note", "已桥接收集")}

    def get_plorts(self) -> List[str]:
        st = self._get_status()
        plorts = st.get("plorts") or ""
        return [p for p in plorts.replace("count=", "").split(",") if p]

    def sell(self) -> Dict[str, Any]:
        res = self._post_action("sell")
        return {"ok": res.get("ok", False), "gains": int(res.get("gains", 0) or 0),
                "plorts": [], "note": res.get("note", "已桥接售卖")}

    def explore(self) -> Dict[str, Any]:
        res = self._post_action("explore")
        return {"ok": res.get("ok", False), "caught": res.get("caught"),
                "area": res.get("area", ""), "new_area": bool(res.get("new_area")),
                "note": res.get("note", "已桥接探索")}

    def go_home(self) -> Dict[str, Any]:
        res = self._post_action("go_home")
        return {"ok": res.get("ok", False), "note": res.get("note", "已桥接返回牧场")}

    def set_overlay(self, text: str) -> None:
        """把状态文字推给游戏内 mod，渲染到游戏画面左上角叠加层。"""
        try:
            self.client.post_json("/api/action", {"action": "set_overlay", "kind": text})
        except Exception:
            pass

    def describe(self) -> Dict[str, Any]:
        alive = self.is_alive()
        st = self._last_status or {}
        return {"mode": "ws-bridge", "connected": alive,
                "game_loaded": st.get("game_loaded"),
                "money_estimated": st.get("money_estimated"),
                "note": "通过游戏内 SlimeBridge 桥接"}

    def capture(self) -> Dict[str, Any]:
        return {"ok": False, "note": "桥接模式不依赖截图，无需校准"}