"""大脑：观望 / 自主经营。

默认观望，不操作游戏。按 V 进入自主经营：每个操作先瞬移到对应地点再执行，
收结晶（围栏有收集器取储存槽、没有收集器捡地上的）、按当日市价在市场清仓，
资金达到费用两倍才建造或升级。不跑图、不抓野生史莱姆、不收食物不投喂。

运行：python core/main_controller.py --data-dir <dir> --config <cfg.json>
可以 --ticks N --interval 0 --backend sim 快速验收。
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.settings import Settings  # noqa: E402
from core.comms import Comms  # noqa: E402
from core.game import IOController, LiveIOController, run_manual_command  # noqa: E402
from core.sim import SimController  # noqa: E402
from core.bridge import BridgeIOController  # noqa: E402
from core.real import WalkCaptureIO  # noqa: E402
from core.ranch import RanchState, decide_build_action, FACILITY_NAMES, record_built  # noqa: E402
from core.voice import HotkeyListener  # noqa: E402

# 真实购买菜单的按钮关键词（中英双语：SR1 界面文字跟游戏语言走）。
# 建造菜单(空地块购买页)三个按钮文字大致为 Corral/Coop/Garden 或 围栏/鸡舍/菜园。
BUILD_KEYWORDS = {
    "corral": ["Corral", "围栏", "牧场"],
    "coop": ["Coop", "鸡舍", "鸡圈"],
    "garden": ["Garden", "菜园", "花园"],
}
# 升级菜单按钮文字是升级件名（围网/水槽/气网/结晶收集器/喂食器/喷灌…），
# 但 SR1 的升级页只有一个"购买"按钮(PurchaseButton)买当前展示项，故关键词以它为主。
UPGRADE_KEYWORDS = [
    "购买", "Purchase",
    "Netting", "Water Tank", "Air Net", "Plort Collector",
    "Feeder", "Water Trough", "Sprinkler", "Upgrade",
    "升级", "围网", "水槽", "水缸", "气网", "结晶收集", "喂食", "喷灌", "喷洒",
]

# ---- 观望模式：周边感知与"新发现" ----
SLIME_NAMES = {
    "PINK_SLIME": "粉色史莱姆", "ROCK_SLIME": "岩石史莱姆", "TABBY_SLIME": "虎斑史莱姆",
    "PHOSPHOR_SLIME": "荧光史莱姆", "RAD_SLIME": "辐射史莱姆", "BOOM_SLIME": "爆炸史莱姆",
    "CRYSTAL_SLIME": "结晶史莱姆", "HONEY_SLIME": "蜂蜜史莱姆", "HUNTER_SLIME": "猎猫史莱姆",
    "PUDDLE_SLIME": "水洼史莱姆", "QUANTUM_SLIME": "量子史莱姆", "DERVISH_SLIME": "旋风史莱姆",
    "MOSAIC_SLIME": "马赛克史莱姆", "TANGLE_SLIME": "藤蔓史莱姆", "FIRE_SLIME": "火焰史莱姆",
    "SABER_SLIME": "剑齿史莱姆",
    "GOLD_SLIME": "金史莱姆", "LUCKY_SLIME": "幸运史莱姆", "QUICKSILVER_SLIME": "水银史莱姆",
}
RARE_SLIMES = {"GOLD_SLIME", "LUCKY_SLIME", "QUICKSILVER_SLIME"}
WATCH_SCAN_EVERY = 4   # 每 4 个观望循环扫一次周边（sim_interval 2s → 约 8 秒一次）


class Brain:
    def __init__(self, settings: Settings, comms: Comms):
        self.s = settings
        self.c = comms
        self.mode = settings.default_mode if settings.default_mode in ("watch", "ranch") else "watch"
        self.ranch = RanchState()
        self.ticks = 0
        self._market_ready = False
        self._reset_fails = 0
        self._said: dict[str, tuple[str, int]] = {}
        self._overlay_last = ""
        self._sell_pending = False   # 背包里还有没卖掉的结晶（上轮售卖失败）
        self._drone_checked = False
        self._drones_on = False
        self._corral_i = 0
        self._hotkey = HotkeyListener(on_toggle=self._on_hotkey)
        # 后端选择
        if settings.backend == "live" and os.path.exists(settings.game_exe):
            self.io: IOController = LiveIOController(
                settings.game_exe,
                calibration=getattr(settings, "calibration", None),
                fallback_funds=getattr(settings, "live_fallback_funds", -1),
                auto_launch=getattr(settings, "auto_launch_game", False),
            )
            self.io.set_logger(self.c.logger)
            self.walk = None
            self._hybrid = False
        elif settings.backend in ("ws", "hybrid"):
            # 游戏内 SlimeBridge 桥接：读资金/设施来自游戏 mod，更准
            bridge = BridgeIOController(
                host=getattr(settings, "bridge_host", "127.0.0.1"),
                port=getattr(settings, "bridge_port", 8124),
                fallback_funds=getattr(settings, "live_fallback_funds", -1),
                game_exe=getattr(settings, "game_exe", ""),
                auto_launch=getattr(settings, "auto_launch_game", False),
            )
            self.io = bridge
            # hybrid：仅在执行层叠加真实键鼠（转向/走路/吸附），感知仍走桥接
            self.walk = WalkCaptureIO(bridge) if settings.backend == "hybrid" else None
            self._hybrid = settings.backend == "hybrid"
        else:
            self.io = SimController(comms, start_funds=settings.sim_start_funds)
            self.walk = None
            self._hybrid = False

    # ------------------------------------------------------------ 模式
    def _on_hotkey(self) -> None:
        """V 键：观望 <-> 自主经营。"""
        self._set_mode("watch" if self.mode == "ranch" else "ranch")

    def _say(self, key: str, text: str, every: int = 1) -> None:
        prev = self._said.get(key)
        if prev and prev[0] == text and self.ticks - prev[1] < every:
            return
        self._said[key] = (text, self.ticks)
        self.c.say(text)

    def _push_overlay(self) -> None:
        """把当前模式写到游戏画面左上角（仅在文字变化时推送）。"""
        if not getattr(self, "_hybrid", False) or not hasattr(self.io, "set_overlay"):
            return
        if self.mode != "ranch":
            text = "NEKO 观望中（按 V 自主经营）"
        elif not self._market_ready:
            text = "NEKO 正在前往结晶市场"
        else:
            text = "NEKO 自主经营中（按 V 回到观望）"
        if text == self._overlay_last:
            return
        self._overlay_last = text
        try:
            self.io.set_overlay(text)
        except Exception:
            pass

    def _set_mode(self, mode: str) -> None:
        if mode == "voice":
            mode = "ranch"
        elif mode not in ("watch", "ranch"):
            mode = "watch"
        self.mode = mode
        self._market_ready = False
        self._reset_fails = 0
        self._drone_checked = False
        self._drones_on = False
        self._said.clear()
        if self.walk:
            if mode == "ranch":
                self.walk.clear_abort()
            else:
                self.walk.abort()
        label = "自主经营" if mode == "ranch" else "观望"
        phase = "resetting" if mode == "ranch" else "watching"
        self.c.set_state(mode=mode, phase=phase)
        self.c.say(f"模式已设为：{label}")
        self._push_overlay()

    def _finish_reset(self, ok: bool, key: str, text: str) -> None:
        if ok:
            self._market_ready = True
            self._reset_fails = 0
        self._say(key, text, every=3)

    def _teleport_market(self, why: str) -> None:
        tp = self.io.tp_market() if hasattr(self.io, "tp_market") else {"ok": False, "note": "无法传送到市场"}
        self._finish_reset(bool(tp.get("ok")), "reset-tp", f"【自主】{why}{tp.get('note', '')}")

    def _reset_to_market(self) -> None:
        """进入经营时先瞬移到结晶市场，不走路。"""
        self.c.set_state(phase="resetting")
        if not self.walk:
            self._market_ready = True
            self._say("reset-sim", "【自主】模拟模式从牧场经营开始", every=99)
            return
        self._teleport_market("")

    def _maybe_dismiss_blocking_menu(self) -> None:
        """输入吞噬面板看门狗。

        SR1 失焦会自动暂停并驻留暂停菜单（NEKO 桌面窗抢焦点就会触发），暂停时吞掉
        全部移动/视角输入，抢回焦点也不会自动恢复。特征：有 Resume/Close/Back/Cancel
        类按钮且**没有** PurchaseButton（有购买按钮的是正常地块菜单，绝不能关）。
        命中则 hover 自校准点它恢复游戏。
        """
        if not self.walk:
            return
        try:
            ui = self.io.ui_buttons()
        except Exception:
            return
        names = [str(b.get("name", "")) for b in ui.get("buttons", [])]
        if not names or any("Purchase" in n for n in names):
            return
        target = next((n for n in names
                       if n in ("ResumeButton", "CloseButton", "BackButton", "CancelButton")), "")
        if not target:
            return
        if self.walk.click_button_named(target):
            self.c.say(f"【自主】检测到吞输入的面板(暂停/设置)，已点 {target} 恢复")

    def _ranch_tick(self) -> None:
        """每轮只做一件事：先瞬移到对应地点，再执行。"""
        self.ticks += 1
        if self.mode != "ranch":
            return
        self._maybe_dismiss_blocking_menu()
        self._maybe_autosave()
        if not self._market_ready:
            self._reset_to_market()
            return
        if not self.walk:
            fed = self.io.feed()
            if not fed.get("ok"):
                self._say("feed-skip", f"【自主】投喂未完成：{fed.get('note', '')}", every=4)
            self._gather_and_sell()
            self._infra_step()
            return
        if not self._drone_checked:
            self._drone_checked = True
            # 无人机布置默认禁用：TrySpawnOneDrone 生成的无人机缺初始化(电池/区域注册)，
            # 每帧 NRE 且炸掉 RegionRegistry 序列化——存档写不出(2026-10-18 实机)。
            # 待 drone_setup 完整复刻游戏创建流程后再默认启用。
            self._drones_on = (bool(getattr(self.s, "enable_drones", False))
                               and hasattr(self.io, "drone_setup")
                               and bool(self.io.drone_setup().get("ok")))
            if self._drones_on:
                self._say("drone", "【自主】无人机搬运已启用（实验）", every=1)
        if self._drones_on:
            if self.ticks % 5 == 4:
                self._infra_step()
            return
        # 一轮 = 完整经营循环（mod 端编排：市场出发→挨个围栏收取→满包立刻回市场
        # 按当日市价清仓再继续→直到地上/收集器没结晶→回市场门口等待）。
        # 收食物与投喂已按需求取消；循环结束后按资金闸推进建造/升级。
        cyc = self.walk.ranch_cycle()
        if cyc.get("ok"):
            self._sell_pending = False
            self._say("cycle", f"【自主】{cyc.get('note', '')}", every=2)
        else:
            self._say("no-cycle", f"【自主】{cyc.get('note', '')}", every=4)
        self._infra_step()

    # ------------------------------------------------------------ 观望：周边感知
    def _watch_tick(self) -> None:
        """观望模式：定期扫玩家周边，主动汇报新发现。

        新物种(跨会话持久 discoveries.json)/稀有史莱姆(金/幸运/水银,每次都喊,
        限频)/首次进入的区域。NEKO 只在"有新东西"时开口，不闲聊。
        """
        if not hasattr(self.io, "scan_near"):
            return
        self._watch_scans = getattr(self, "_watch_scans", 0) + 1
        if self._watch_scans % WATCH_SCAN_EVERY != 1:   # 第一次立即扫，之后每 4 轮
            return
        try:
            near = self.io.scan_near(30.0)
        except Exception:
            return
        if not near.get("ok"):
            return
        disc = self._load_discoveries()
        # 1) 稀有史莱姆：出现就喊（限频，同种 60 秒内不重复）
        for s in near.get("slimes", []):
            sid = s.get("id", "")
            if sid in RARE_SLIMES:
                name = SLIME_NAMES.get(sid, sid)
                self._say("rare-" + sid,
                          f"【稀有】{name}出现了！！就在附近，快看快看！", every=30)
        # 2) 新物种：第一次见到才记（持久化，下次启动不会再报）
        new_species = []
        for s in near.get("slimes", []):
            sid = s.get("id", "")
            if sid and sid not in RARE_SLIMES and sid not in disc["seen_slimes"]:
                disc["seen_slimes"].append(sid)
                new_species.append((sid, s.get("n", 1)))
        # 3) 首次进入的区域
        zone = near.get("zone", "")
        new_zone = bool(zone) and zone not in disc["zones"]
        if new_zone:
            disc["zones"].append(zone)
        if new_species or new_zone:
            self._save_discoveries()
        # 4) 开口汇报
        if new_species:
            parts = "、".join(f"「{SLIME_NAMES.get(sid, sid)}」×{n}" for sid, n in new_species)
            self.c.say(f"【新发现】第一次见到{parts}！NEKO 记下来啦～")
            self.c.report_discovery(
                "species-" + "-".join(sid for sid, _ in new_species) + f"-{self.ticks}",
                "新物种：" + parts)
        elif new_zone:
            summary = self._near_summary(near)
            self.c.say(f"【探索】来到了「{zone}」{summary}")
            self.c.report_discovery(f"zone-{zone}", f"探索新区域：{zone}")
        # 面板上的史莱姆列表同步为已发现的物种
        self.ranch.known_slimes = [SLIME_NAMES.get(s, s) for s in disc["seen_slimes"]]

    def _near_summary(self, near) -> str:
        bits = []
        slimes = [s for s in near.get("slimes", []) if s.get("id")]
        if slimes:
            top = sorted(slimes, key=lambda s: -s.get("n", 0))[:3]
            bits.append("、".join(f"{SLIME_NAMES.get(s['id'], s['id'])}×{s['n']}" for s in top))
        pn = sum(s.get("n", 0) for s in near.get("plorts", []))
        fn = sum(s.get("n", 0) for s in near.get("foods", []))
        if pn:
            bits.append(f"结晶×{pn}")
        if fn:
            bits.append(f"食物×{fn}")
        return ("（附近有 " + "，".join(bits) + "）") if bits else ""

    def _load_discoveries(self) -> dict:
        if getattr(self, "_disc", None) is None:
            try:
                with open(os.path.join(self.s.data_dir, "discoveries.json"), encoding="utf-8") as f:
                    self._disc = json.load(f)
            except Exception:
                self._disc = {}
            self._disc.setdefault("seen_slimes", [])
            self._disc.setdefault("zones", [])
        return self._disc

    def _save_discoveries(self) -> None:
        try:
            with open(os.path.join(self.s.data_dir, "discoveries.json"), "w", encoding="utf-8") as f:
                json.dump(self._disc, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    # ------------------------------------------------------------ 需求1 建造/升级（真实 UI 购买）
    def _facilities_signature(self):
        """设施快照（供真实购买前后对比验证）：(kind, 是否空地块, 已拥有升级) 的排序元组。"""
        try:
            fac = self.io.scan_facilities()
        except Exception:
            fac = []
        return tuple(sorted(
            (str(f.get("kind", "")), bool(f.get("isEmpty")),
             tuple(str(u) for u in (f.get("upgrades") or ())))
            for f in fac if isinstance(f, dict)
        ))

    def _sync_ranch_from_facilities(self) -> None:
        """把建设进度与游戏真实设施对齐（含玩家手动建/升的部分）。"""
        try:
            fac = self.io.scan_facilities()
        except Exception:
            return
        for f in fac:
            if not isinstance(f, dict):
                continue
            kind = f.get("kind")
            if kind not in FACILITY_NAMES or f.get("isEmpty"):
                continue
            ups = [str(u) for u in (f.get("upgrades") or [])]
            self.ranch.level[kind] = 2 if ups else 1
            self.ranch.upgrades[kind] = ups

    def _infra_step(self) -> None:
        """建造/升级：先瞬移到对应地块前，再直接完成购买，让变化出现在镜头里。"""
        if not self.walk:
            return
        funds = self.io.read_money()
        if hasattr(self.io, "scan_facilities"):
            self._sync_ranch_from_facilities()
        action = decide_build_action(self.ranch, funds)
        if not action:
            return
        kind = action["kind"]
        mode = action["mode"]
        spot = "empty" if mode == "build" else kind
        tp = self.io.tp_plot(spot) if hasattr(self.io, "tp_plot") else {"ok": False, "note": "无法瞬移到地块"}
        if not tp.get("ok"):
            self._say(f"build-{kind}", f"【自主】{tp.get('note', '找不到地块')}", every=4)
            return
        time.sleep(0.6)
        keywords = BUILD_KEYWORDS.get(kind, []) if mode == "build" else UPGRADE_KEYWORDS
        res = self.walk.real_purchase(kind, mode, keywords, self._facilities_signature)
        if res.get("ok"):
            if hasattr(self.io, "scan_facilities"):
                self._sync_ranch_from_facilities()
            new_level = self.ranch.level.get(kind, 0)
            record_built(self.ranch, kind)
            name = FACILITY_NAMES.get(kind, kind)
            verb = "建造了" if mode == "build" else "升级了"
            self.c.report_discovery(
                f"{kind}-lv{new_level}-{self.ticks}",
                f"已在{name}前{verb}，{res.get('note', '')}",
            )
        else:
            act = "建造" if mode == "build" else "升级"
            self._say(f"build-{kind}", f"【自主】已在地块前，{act}未成功：{res.get('note', '')}", every=4)

    def _maybe_autosave(self) -> None:
        """每 8 轮主动存一次，避免退出时存档被打断。"""
        if self.ticks % 8 != 0 or not hasattr(self.io, "save_now"):
            return
        saved = self.io.save_now()
        if saved.get("ok"):
            self._say("save", f"【自主】{saved.get('note', '已主动存档')}", every=8)

    def _gather_and_sell(self) -> None:
        # 混合模式：钱必须来自真实动作。mod 的 harvest/sell 是"凭空注入结晶/兑换资金"
        # 的魔法层，若每 tick 调用会让资金无视真实行为持续上涨，与"画面真实运作"矛盾，
        # 故 hybrid 下彻底禁用；真实售卖待集市 UI 点击接入后再启用。
        if getattr(self, "_hybrid", False):
            return
        res = self.io.harvest()
        if res.get("ok") and res.get("count", 0) == 0:
            return
        sr = self.io.sell()
        if sr.get("ok"):
            gains = sr.get("gains", 0)
            self.c.say(f"【自主】已运回结晶并在集市售卖，收入 ${gains}，当前资金 ${self.io.read_money()}")

    # ------------------------------------------------------------ 主循环
    def run(self, stop_condition) -> None:
        phase = "watching" if self.mode != "ranch" else "resetting"
        self.c.set_state(running=True, mode=self.mode, phase=phase, funds=self.io.read_money())
        gr = self.io.enter_game()
        if not gr.get("ok"):
            self.c.say(f"【启动】{gr.get('note','')}")
        if hasattr(self.io, "scan_facilities"):
            self._sync_ranch_from_facilities()
        hotkey_ok = self._hotkey.start()
        if not hotkey_ok:
            self.c.say("【提示】V 键热键没有挂上；可在插件控制台切换观望或自主经营")
        else:
            self.c.say(f"【提示】V 键热键已挂上（{getattr(self._hotkey, 'backend', '')}），按 V 切换观望/自主经营")
        self.c.say("NEKO 已连接史莱姆牧场。当前是观望，按 V 键进入自主经营。" if self.mode != "ranch"
                   else "NEKO 已连接史莱姆牧场。当前是自主经营，按 V 键回到观望。")
        self._push_overlay()

        while True:
            cmd = self.c.read_command()
            if cmd:
                self._handle_command(cmd)
            if stop_condition():
                break

            if self.mode != "ranch":
                self.ticks += 1
                self._watch_tick()
                self.c.set_state(mode="watch", phase="watching",
                                 funds=self.io.read_money(),
                                 infra=dict(self.ranch.level),
                                 slimes=list(self.ranch.known_slimes))
                self._push_overlay()
                time.sleep(self._interval())
                continue

            t0 = time.time()
            self._ranch_tick()
            self.c.set_state(mode="ranch",
                             phase="running" if self._market_ready else "resetting",
                             funds=self.io.read_money(),
                             infra=dict(self.ranch.level),
                             slimes=list(self.ranch.known_slimes))
            self._push_overlay()
            if self.walk:
                if not self._market_ready:
                    time.sleep(1.0)   # 重置阶段（瞬移到市场）不算循环间隔，立刻进第一轮
                else:
                    # 循环时间（面板可改，默认 30s）：从本轮开始计（含循环执行耗时）
                    rest = float(getattr(self.s, "cycle_seconds", 30)) - (time.time() - t0)
                    time.sleep(max(1.0, rest))
            else:
                time.sleep(self._interval())

        self.c.set_state(running=False, phase="stopped")
        self._hotkey.stop()

    def _interval(self) -> float:
        return self.s.sim_interval

    def _handle_command(self, cmd) -> None:
        t = cmd.get("type")
        if t == "stop":
            self.c.say("收到停止指令，停止游玩。")
            raise SystemExit("stop_command")
        if t == "set_mode":
            self._set_mode(cmd.get("mode", "watch"))
        elif t == "set_timeout":
            self.s.timeout_seconds = max(1, int(cmd.get("seconds", self.s.timeout_seconds)))
            self.c.say(f"接管秒数已更新为 {self.s.timeout_seconds}s")
        elif t == "set_cycle":
            self.s.cycle_seconds = max(5, int(cmd.get("seconds", getattr(self.s, "cycle_seconds", 30))))
            self.c.say(f"循环时间已更新为 {self.s.cycle_seconds}s")
        elif t == "command":
            text = cmd.get("text", "")
            res = run_manual_command(self.io, text)
            self.c.say(f"[指令] 「{text}」 -> {res.get('note','')}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="史莱姆牧场自动游玩核心")
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--config", required=False, default="")
    ap.add_argument("--ticks", type=int, default=0, help="跑 N 轮自主 tick 后退出(0=一直运行)")
    ap.add_argument("--interval", type=float, default=2.0, help="每轮间隔秒数")
    ap.add_argument("--backend", choices=["sim", "live", "ws", "hybrid"], default="")
    args = ap.parse_args(argv)

    s = Settings.load(args.data_dir, args.config)
    if args.backend:
        s.backend = args.backend
    if args.ticks:
        s.default_mode = "ranch"
    if args.interval >= 0:
        s.sim_interval = args.interval
    s.sim_interval = float(getattr(s, "sim_interval", args.interval))

    comms = Comms(args.data_dir)
    brain = Brain(s, comms)
    ticks = {"n": 0}

    def stop_condition() -> bool:
        if args.ticks and ticks["n"] >= args.ticks:
            return True
        return False

    orig = brain._ranch_tick

    def counted_tick():
        if brain.mode == "ranch" and brain._market_ready:
            ticks["n"] += 1
        orig()
    brain._interval = lambda: float(s.sim_interval)
    brain._ranch_tick = counted_tick

    try:
        brain.run(stop_condition)
    except SystemExit:
        pass
    finally:
        comms.set_state(running=False, phase="stopped")


if __name__ == "__main__":
    main()