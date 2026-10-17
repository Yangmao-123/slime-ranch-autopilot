"""混合模式执行器（hybrid backend）：真实键鼠操作 + 画面验证。

感知仍由 SlimeBridge mod 提供（资金/设施/坐标/目标 yaw 偏差都很准），本模块只负责把
mod 的"目标感知"（scan_target 返回的 yaw_delta / dist）翻译成**真实游戏输入**：
  - 用鼠标移动真实转动视角，直到对准目标（mod 的 yaw 实时反馈收敛）；
  - 按 W 真实向前走，直到距离进入真空枪射程；
  - 按住鼠标左键真实吸附目标。

这样人物的移动、转向、动作都是真实在画面上发生的（不再是瞬移魔法），物品栏/真空包
计数会随真实抓捕变化，汇报与画面一致。

winio 依赖惰性加载，避免进 sim 模式时拖垮环境。
"""
from __future__ import annotations

import math
import time
from typing import Dict, Any

# 转向收敛阈值：目标已基本居中
YAW_TOLERANCE = 5.0
# 真空枪实用吸附射程（米）：距离小于它就开始按左键吸
VACUUM_RANGE = 5.0
# 结晶市场站位：贴到投入口(橙色投掷管)正前方这么近，正对投掷口（用户截图的期望站位）
MARKET_STAND_DIST = 3.5


class WalkCaptureIO:
    def __init__(self, bridge, window_exe: str = "SlimeRancher.exe"):
        self.io = bridge          # BridgeIOController（负责感知：scan_target / read_money 等）
        self.window_exe = window_exe
        self._win = None
        self._vision = None
        self._hwnd = None
        self._abort = False

    def abort(self) -> None:
        """观望打断进行中的走路和吸附。"""
        self._abort = True

    def clear_abort(self) -> None:
        self._abort = False

    def _aborted(self) -> Dict[str, Any] | None:
        if not self._abort:
            return None
        return {"ok": False, "moved": False, "aborted": True, "gains": 0, "note": "已切回观望，停下操作"}

    # ---------------- 依赖与窗口 ----------------
    def _load(self):
        if self._win is None or self._win is False:
            try:
                from . import winio  # type: ignore
                self._win = winio
            except Exception:
                self._win = False
        return self._win

    def _vload(self):
        """惰性加载视觉层(vision.py)，提供帧对比/区域裁剪。"""
        if self._vision is None or self._vision is False:
            try:
                from . import vision  # type: ignore
                self._vision = vision
            except Exception:
                self._vision = False
        return self._vision

    def _capture_frame(self):
        """抓当前游戏画面，供 NEKO 用眼睛确认真实动作。失败返回 None。"""
        win = self._load()
        if win is None or win is False or not self._hwnd:
            return None
        try:
            return win.capture_window(self._hwnd)
        except Exception:
            return None

    def _ensure_ready(self) -> bool:
        win = self._load()
        if win is None or win is False:
            return False
        try:
            self._hwnd = win.find_game_window(self.window_exe)
        except Exception:
            self._hwnd = None
        if not self._hwnd:
            return False
        try:
            win.ensure_foreground(self._hwnd)
        except Exception:
            pass
        return True

    def _lock_mouse(self) -> None:
        """把光标放回窗口中心并单击一次，让第三人称视角进入"鼠标锁定/准星"状态，
        之后相对移动鼠标即可真实转动视角（光标被锁在中央不会跑丢）。"""
        win = self._win
        try:
            win.ensure_foreground(self._hwnd)          # 每次按键/鼠标前再次抢前台
        except Exception:
            pass
        c = self._center()
        if not c:
            return
        try:
            win.mouse_move_to(c[0], c[1])
            time.sleep(0.05)
            win.mouse_click(c[0], c[1])   # 单击锁定视角（游戏会把光标归中并进入准星态）
            time.sleep(0.1)
        except Exception:
            pass

    def _center(self):
        """返回窗口屏幕中心绝对坐标 (x, y)。取不到则 None。"""
        win = self._load()
        try:
            rect = win.get_window_rect(self._hwnd)
        except Exception:
            rect = None
        if rect is None:
            return None
        x, y, w, h = rect
        return int(x + w / 2), int(y + h / 2)

    def _reassert_foreground(self, win) -> None:
        """走位/转向循环里每步补抢焦点：SendInput 只进前台窗口，用户切走就会空走。"""
        try:
            if not win.is_foreground(self._hwnd):
                win.ensure_foreground(self._hwnd)
        except Exception:
            pass

    # ---------------- 真实操作 ----------------
    def _turn_toward(self, target: Dict[str, Any], max_steps: int = 18,
                     rescanner=None) -> bool:
        """真实转动视角对准目标。用两段式步长 + 短反馈周期，抑制史莱姆跳动导致过冲：
        - 大偏差粗调(大步)，小偏差/中偏差精调(小步)，避免一次转太多。
        - 相对移动只产生像素增量，光标始终锁在中央，可持续小幅修正。
        - rescanner 可选：每步重扫目标的反馈源。默认扫史莱姆/结晶(scan_target)；
          走向市场/围栏时必须传对应 rescanner，否则中途会改成去追史莱姆。
        """
        win = self._load()
        if win is None or win is False:
            return False
        re_scan = rescanner or self.io.scan_target
        for _ in range(max_steps):
            if self._aborted():
                return False
            self._reassert_foreground(win)
            if _ % 6 == 3:
                self._dismiss_if_paused()
            yaw = float(target.get("yaw", 0.0))
            if abs(yaw) < YAW_TOLERANCE:
                return True
            if abs(yaw) > 30:
                step = 300          # 粗调：大偏差大步
            elif abs(yaw) > 12:
                step = 120          # 中调
            else:
                step = 50           # 精调：小步趋近，避免过冲
            dx = int(step) if yaw > 0 else int(-step)   # yaw<0 时左移使 yaw 回升到 0
            try:
                win.mouse_move_rel(dx, 0)               # 相对移动真实转视角
            except Exception:
                return False
            time.sleep(0.28)                            # 短反馈，及时跟目标移动
            target = re_scan()
            if not target.get("ok"):
                return False
        return abs(float(target.get("yaw", 0.0))) < (YAW_TOLERANCE + 6.0)

    def _walk_to_point(self, target_dist: float, max_steps: int, rescanner,
                       stall_limit: int = 3, final_dist: float | None = None) -> Dict[str, Any]:
        """按 W 真实走向 rescanner() 报告的目标，直到进入 target_dist 米。

        距离自适应：>60m 按住 W+Shift 2.2s(≈20m)，15-60m 按住 W+Shift 1.4s(≈12m)，
        <15m 按住 W 0.8s(≈3-4m，慢速防过冲)。每步先用 yaw 反馈对准方向，再真实前进，
        并用**眼睛(帧对比)确认人物真的在动**：连走几步画面纹丝不动(失焦/被墙挡/输入
        没生效)就提前放弃，避免空口汇报"已走到"。stall_limit 控制连续"没动"几次才放弃
        （投放围栏这类近处小目标可调大，避免误判被墙挡）。

        两点改进：
        1) 距离感知优先：只要 rescanner 报的距离确实在缩短（说明在往前走），即使单步
           画面变化小也不算"卡住"——避免对着大建筑缓慢接近时误判失焦；
        2) final_dist 放宽到达：步数耗尽或判定被堵时，若实际距离已 <= final_dist 仍算
           "已到达"（市场/围栏这类大建筑：站在外圈就能投，不必贴到中心）。

        返回 {"ok", "moved"}：moved=True 表示画面检测到真实位移。
        """
        win = self._load()
        vision = self._vload()
        if win is None or win is False:
            return {"ok": False, "moved": False}
        accept_dist = final_dist if final_dist is not None else (target_dist + 1.5)
        stalls = 0
        detours = 0
        last_d = None
        for _ in range(max_steps):
            stopped = self._aborted()
            if stopped:
                return stopped
            self._reassert_foreground(win)
            if _ % 6 == 3:
                self._dismiss_if_paused()
            t = rescanner()
            if not t.get("ok"):
                return {"ok": False, "moved": False}
            d = float(t.get("dist", 1e9))
            if d <= target_dist:
                return {"ok": True, "moved": True}
            if not self._turn_toward(t, rescanner=rescanner):   # 先对准再走，避免白走
                return {"ok": False, "moved": False}
            improved = last_d is not None and d < last_d - 0.4   # 距离确在缩短 = 在往前走
            last_d = d
            pos_before = (float(t.get("px", 0.0)), float(t.get("py", 0.0)))
            before = self._capture_frame()
            if d > 60:
                hold, keys = 2.2, ["w", "shift"]        # 远距离：跑步，大步长
            elif d > 15:
                hold, keys = 1.4, ["w", "shift"]
            else:
                hold, keys = 0.8, ["w"]                 # 近距离：慢走，避免冲过头
            try:
                if len(keys) > 1:
                    win.keys_hold(keys, hold)
                else:
                    win.key_hold(keys[0], hold)
            except Exception:
                return {"ok": False, "moved": False}
            time.sleep(0.15)
            after_scan = rescanner()
            moved_m = 0.0
            if after_scan.get("ok"):
                moved_m = ((float(after_scan.get("px", 0.0)) - pos_before[0]) ** 2
                           + (float(after_scan.get("py", 0.0)) - pos_before[1]) ** 2) ** 0.5
            # 人几乎没挪动就是原地踏步：不要继续按住 W。侧移一次仍不动就停，交给传送到目标前。
            stepping = moved_m < 0.3 and not improved
            if not stepping and before is not None and vision not in (None, False):
                after = self._capture_frame()
                mv = vision.movement_fraction(before, after)
                stepping = mv is not None and mv < 0.012 and not improved
            if stepping:
                stalls += 1
                if stalls >= 2:
                    stalls = 0
                    detours += 1
                    if detours >= 2:
                        t2 = rescanner()
                        if t2.get("ok") and float(t2.get("dist", 1e9)) <= accept_dist:
                            return {"ok": True, "moved": True}
                        return {"ok": False, "moved": False, "stuck": True,
                                "note": "原地踏步，已停止空走"}
                    try:
                        win.key_hold("a" if detours % 2 else "d", 0.6)
                    except Exception:
                        return {"ok": False, "moved": False, "stuck": True, "note": "原地踏步，已停止空走"}
                else:
                    self._lock_mouse()
        t = rescanner()
        got = t.get("ok") and float(t.get("dist", 1e9)) <= accept_dist
        return {"ok": got, "moved": True}

    def _walk_toward(self, max_steps: int = 30) -> Dict[str, Any]:
        """真实走向最近抓捕目标(史莱姆/结晶)，直到进入真空枪射程。"""
        return self._walk_to_point(VACUUM_RANGE, max_steps, self.io.scan_target)

    def _vacuum(self, seconds: float = 1.1) -> Dict[str, Any]:
        """对准目标后，按住鼠标左键真实吸附。

        返回 {"ok", "visual"}：visual=True 表示画面检测到物品栏/画面发生变化，
        说明真的吸到了东西（这就是你要的"物品栏的变化"验证）。
        """
        win = self._load()
        vision = self._vload()
        if win is None or win is False:
            return {"ok": False, "visual": False}
        stopped = self._aborted()
        if stopped:
            return stopped
        c = self._center()
        if not c:
            return {"ok": False, "visual": False}
        before = self._capture_frame()
        try:
            win.mouse_hold(c[0], c[1], seconds)         # 对准中心按左键吸
        except Exception:
            return {"ok": False, "visual": False}
        time.sleep(0.25)
        visual = False
        if before is not None and vision is not None and vision is not False:
            after = self._capture_frame()
            if after is not None:
                hud_chg = vision.frame_diff(before, after, region=vision.HUD_REGION)
                whole_chg = vision.frame_diff(before, after)
                visual = hud_chg > 0.02 or whole_chg > 0.03
        return {"ok": True, "visual": visual}

    # ---------------- 对外动作 ----------------
    def capture_target(self, vacuum_seconds: float = 1.1) -> Dict[str, Any]:
        """混合模式抓捕一步：感知目标 -> 真实转向 -> 真实走近 -> 左键吸。

        返回 {"ok", "kind", "note", "remaining"}: remaining=吸完后目标是否还在射程
        （吸收未吐出的史莱姆降低 remaining，提示可继续）。纯真实输入，无可瞬移副作用。
        """
        if not self._ensure_ready():
            return {"ok": False, "kind": "", "note": "未找到游戏窗口，无法真实操作"}
        self._lock_mouse()                              # 锁定一次进入准星态，后续相对移动转视角
        t = self.io.scan_target()
        if not t.get("ok"):
            return {"ok": False, "kind": "", "note": t.get("note", "附近无可抓捕目标")}
        kind = t.get("kind", "")
        if not self._turn_toward(t):
            return {"ok": False, "kind": kind, "note": "转向对准目标失败（未对准）"}
        wres = self._walk_toward()
        if not wres.get("ok"):
            return {"ok": False, "kind": kind,
                    "note": "未能靠近目标至吸程内（画面检测疑似失焦/被堵，已尝试重锁）"}
        vres = self._vacuum(vacuum_seconds)
        # 吸入后看目标是否仍在（判定为"吸附到东西 / 击退目标"）
        after = self.io.scan_target()
        remaining = bool(after.get("ok"))
        label = "史莱姆" if kind == "slime" else "结晶"
        if vres.get("visual"):
            note = "已真实走向并吸附 %s（画面确认生效：物品栏/画面有变化）" % label
        else:
            note = "已走向%s并按住左键，但画面未见明显变化（可能被挡/已吸到但未刷新，下一轮会核实）" % label
        if remaining and kind == "slime":
            note += "（目标仍在附近，可继续抓捕）"
        return {"ok": True, "kind": kind, "note": note, "remaining": remaining,
                "dist": float(after.get("dist", t.get("dist", 0))),
                "visual": vres.get("visual", False)}

    def _scan_port_close(self) -> Dict[str, Any]:
        """重扫市场并把目标换算成"投入口正前方贴近站"。

        旧版用 mod 报的 14m"庭院站位点"，人离投入口太远、投掷不可靠；期望站位是
        紧贴橙色投掷管正前方（见用户截图）。距离由玩家坐标(px,py)与投入口(cx,cz)
        自算，朝向用 mod 的 cyaw（指向投入口开孔）。
        """
        m = self.io.scan_market()
        if not m.get("ok"):
            return m
        px = float(m.get("px", 0.0) or 0.0)
        py = float(m.get("py", 0.0) or 0.0)
        cx = float(m.get("cx", 0.0) or 0.0)
        cz = float(m.get("cz", 0.0) or 0.0)
        m["dist"] = math.hypot(cx - px, cz - py)
        m["yaw"] = float(m.get("cyaw", 0.0) or 0.0)
        m["tx"], m["tz"] = cx, cz
        return m

    def approach_market(self) -> Dict[str, Any]:
        """只走到结晶市场投入口正前方（截图站位），不投掷。人已在牧场时用来重置位置。"""
        if not self._ensure_ready():
            return {"ok": False, "note": "未找到游戏窗口，无法走到市场"}
        stopped = self._aborted()
        if stopped:
            return stopped
        self._lock_mouse()
        m = self._scan_port_close()
        if not m.get("ok"):
            return {"ok": False, "note": m.get("note", "场景中找不到结晶市场")}
        if m.get("area") != "牧场":
            return {"ok": False, "area": m.get("area", ""), "note": "当前不在牧场"}
        if float(m.get("dist", 1e9)) <= MARKET_STAND_DIST:
            return {"ok": True, "note": "已在结晶市场投入口前"}
        wres = self._walk_to_point(MARKET_STAND_DIST, 40, self._scan_port_close,
                                   stall_limit=6, final_dist=MARKET_STAND_DIST + 4.5)
        if wres.get("ok"):
            return {"ok": True, "note": "已走到结晶市场投入口前"}
        return {"ok": False, "note": "未能走到结晶市场投入口"}

    def _pitch_toward(self, rescanner=None, tol: float = 4.0, max_steps: int = 8) -> bool:
        """闭环收敛视线俯仰到目标：所有 scan 现在统一带 pitch(相机相对, 正=需抬头)。

        rescanner 返回带 pitch 的 scan 结果；缺省用市场投入口。瞬移后镜头没对准时
        这里的鼠标补偿会真实把镜头转到目标（修"瞬移后不动作"的另一半根因）。
        """
        scan = rescanner or self._scan_port_close
        win = self._load()
        if win is None or win is False:
            return False
        for _ in range(max_steps):
            if self._aborted():
                return False
            m = scan()
            if not m.get("ok"):
                return False
            pd = float(m.get("pitch", 0.0) or 0.0)
            if abs(pd) <= tol:
                return True
            step = int(max(-240, min(240, pd * 10)))   # 经验 px/度，闭环会自行修正
            try:
                win.mouse_move_rel(0, -step)           # 正 pd=抬头=鼠标上移(负 y)
            except Exception:
                return False
            time.sleep(0.25)
        m = scan()
        return abs(float(m.get("pitch", 0.0) or 0.0)) <= (tol + 3.0)

    def collect_scanned(self, scanner, label: str) -> Dict[str, Any]:
        """按指定扫描走向并吸附。scanner 需返回与 scan_target 相同的 yaw/dist。"""
        if not self._ensure_ready():
            return {"ok": False, "note": "未找到游戏窗口"}
        stopped = self._aborted()
        if stopped:
            return stopped
        self._lock_mouse()
        t = scanner()
        if not t.get("ok"):
            return {"ok": False, "note": t.get("note", "附近没有%s" % label)}
        if not self._turn_toward(t, rescanner=scanner):
            return {"ok": False, "note": "未能对准%s" % label}
        wres = self._walk_to_point(VACUUM_RANGE, 24, scanner, stall_limit=4)
        if not wres.get("ok"):
            return {"ok": False, "note": "未能靠近%s" % label}
        vres = self._vacuum()
        if vres.get("aborted"):
            return vres
        if not vres.get("visual"):
            return {"ok": False, "visual": False, "note": "对着%s吸附后画面没有变化，这次不算收进背包" % label}
        return {"ok": True, "note": "已吸到%s" % label, "visual": True}

    def deposit_to_corral(self, max_steps: int = 30) -> Dict[str, Any]:
        """把真空背包里的史莱姆真实走到最近围栏并投放（快速左键投出）。

        感知仍走 mod（scan_corral 给最近围栏的方向/距离），执行全真实：走到围栏 4m 内
        -> 对准 -> 轻按左键把背包当前物品投出（画面中史莱姆应落进围栏）。
        返回 {"ok", "note"}。
        """
        win = self._load()
        if win is None or win is False or not self._ensure_ready():
            return {"ok": False, "note": "未找到游戏窗口"}
        self._lock_mouse()
        c = self.io.scan_corral()
        if not c.get("ok"):
            return {"ok": False, "note": c.get("note", "场景中没有围栏，无法投放")}
        # 走到围栏 6m 内（围栏平台本身约 4m 宽，6m 已算"贴着围栏"；stall 容忍放大避免误判被堵）
        wres = self._walk_to_point(6.0, max_steps, self.io.scan_corral, stall_limit=5)
        if not wres.get("ok"):
            return {"ok": False, "note": "未能走到围栏旁（画面检测疑似失焦/被堵）"}
        if not self._turn_toward(self.io.scan_corral()):
            return {"ok": False, "note": "未能对准围栏"}
        try:
            win.mouse_move_rel(0, 25)                   # 略微向下看，让投出物落进围栏
            time.sleep(0.1)
            c2 = self._center()
            if not c2:
                return {"ok": False, "note": "取不到窗口中心"}
            win.mouse_click(c2[0], c2[1])               # 快速左键 = 投出真空背包当前物品
        except Exception:
            return {"ok": False, "note": "投放点击失败"}
        return {"ok": True, "note": "已走到围栏旁并投出真空背包物品（史莱姆应已进入围栏）"}

    def sell_plorts(self, throws: int = 8) -> Dict[str, Any]:
        """真实售卖结晶：真实走到结晶市场建筑前，对准投入口，连续左键投出背包结晶。

        Slime Rancher 的真实售卖机制：把真空背包里的结晶投进结晶市场投入口，结晶立刻
        换成钱（无需开面板）。收益验证走 mod 真实资金（卖前/卖后 read_money 对比），
        画面中人物真实走到市场、结晶真实飞进出（这才是"看到钱从画面里来"）。
        返回 {"ok", "gains", "note"}。
        """
        win = self._load()
        if win is None or win is False or not self._ensure_ready():
            return {"ok": False, "gains": 0, "note": "未找到游戏窗口"}
        self._lock_mouse()
        m = self._scan_port_close()
        if not m.get("ok"):
            return {"ok": False, "gains": 0, "note": m.get("note", "场景中找不到结晶市场")}
        # 市场在牧场，玩家若在野外直线走回会被牧场围墙挡住，故仅当已在牧场时才真实走去卖
        if m.get("area") != "牧场":
            return {"ok": False, "gains": 0, "note": f"玩家当前在{m.get('area','未知区域')}，暂不穿越围墙去市场，待回到牧场再卖"}
        # 走到投入口正前方贴近站（截图站位）；堵在路上会自动 A/D 侧移绕行。
        wres = self._walk_to_point(MARKET_STAND_DIST, 40, self._scan_port_close,
                                   stall_limit=6, final_dist=MARKET_STAND_DIST + 4.5)
        if not wres.get("ok"):
            # 兜底：当前起点可能被卡（贴围墙/建筑/地形缝隙），先传送回牧场房子
            # （开阔区，实测从房子到市场可正常直线走到）再走一次。
            try:
                self.io.go_home()
                time.sleep(1.5)
            except Exception:
                pass
            m2 = self._scan_port_close()
            if m2.get("ok") and m2.get("area") == "牧场":
                wres = self._walk_to_point(MARKET_STAND_DIST, 30, self._scan_port_close,
                                           stall_limit=6, final_dist=MARKET_STAND_DIST + 4.5)
        if not wres.get("ok"):
            t = self._scan_port_close()
            dnote = f"（距投入口 {float(t.get('dist', 0)):.0f}m）" if t.get("ok") else ""
            return {"ok": False, "gains": 0,
                    "note": f"未能走到结晶市场投入口前{dnote}，本轮暂不售卖"}

        if not self._turn_toward(self._scan_port_close(), rescanner=self._scan_port_close):
            return {"ok": False, "gains": 0, "note": "未能对准结晶市场投入口"}
        try:
            before = int(self.io.read_money())
        except Exception:
            before = -1
        c = self._center()
        if not c:
            return {"ok": False, "gains": 0, "note": "取不到窗口中心"}
        try:
            before = int(self.io.read_money())
        except Exception:
            before = -1
        try:
            # 先闭环收敛俯仰到投入口(mod 报 pitch_delta)，再投；每发之间只加很小
            # 的角度散布补偿抛物线误差。盲猜上抬像素是"投进不加钱"的根因。
            if not self._pitch_toward():
                return {"ok": False, "gains": 0, "note": "未能把视线俯仰收敛到投入口，本轮暂不售卖"}
            for i in range(throws):
                if not self._turn_toward(self._scan_port_close(), rescanner=self._scan_port_close):
                    break
                off = [0, 6, -4][i % 3]
                if off:
                    win.mouse_move_rel(0, -off)
                    time.sleep(0.08)
                win.mouse_hold(c[0], c[1], 0.45)    # 蓄力投出背包结晶
                if off:
                    win.mouse_move_rel(0, off)
                time.sleep(0.3)
            time.sleep(2.0)                     # 等结晶飞入市场入账
        except Exception:
            return {"ok": False, "gains": 0, "note": "投出点击失败"}
        try:
            after = int(self.io.read_money())
        except Exception:
            after = -1
        gains = max(0, after - before) if before >= 0 and after >= 0 else 0
        if gains > 0:
            return {"ok": True, "gains": gains,
                    "note": f"已在结晶市场卖出结晶，收入 ${gains}，当前资金 ${after}"}
        return {"ok": False, "gains": 0,
                "note": "投进市场后资金没有增加，这次不算卖出"}

    def grab_here(self, kind: str) -> Dict[str, Any]:
        """瞬移到物品旁、镜头真实对准（画面可见），吸取走游戏原生路径。

        SR1 光标锁定态对注入的鼠标按键无响应（键盘/移动/菜单点击均通，唯独按住
        左键吸/投无效，系统级按键状态已确认按下）——吸取改 Vacuumable.TryConsume。
        """
        self._dismiss_if_paused()
        label = "食物" if kind == "food" else "结晶"
        tp = self.io.tp_item(kind) if hasattr(self.io, "tp_item") else {"ok": False, "note": "无法瞬移"}
        if not tp.get("ok"):
            return tp
        time.sleep(0.45)
        scanner = self.io.scan_food if kind == "food" else self.io.scan_plort
        self._dismiss_if_paused()
        target = scanner()
        if target.get("ok"):
            self._turn_toward(target, rescanner=scanner, max_steps=12)
            self._pitch_toward(scanner)          # 镜头真实对准目标（画面可见）再吸
        if hasattr(self.io, "vac_grab"):
            res = self.io.vac_grab(kind)
            if res.get("ok"):
                return {"ok": True, "note": "%s，镜头对准后已吸取（原生路径）" % tp.get("note", "")}
            return {"ok": False, "note": "%s。%s" % (tp.get("note", ""), res.get("note", ""))}
        # 老版本 mod：退回鼠标吸附
        vac = self._vacuum(0.9)
        if vac.get("aborted"):
            return vac
        after = scanner()
        if (not after.get("ok")) or float(after.get("dist", 1e9)) > float(target.get("dist", 0)) + 8:
            return {"ok": True, "note": "已瞬移到%s旁边并吸起" % label}
        return {"ok": False, "note": "%s，但吸附后目标仍在原地" % tp.get("note", "")}

    def feed_here(self) -> Dict[str, Any]:
        """瞬移到围栏前、镜头真实对准（画面可见），食物以真实物理投进围栏。"""
        self._dismiss_if_paused()
        slot = "corral@%d" % getattr(self, "_corral_i", 0)
        tp = self.io.tp_plot(slot) if hasattr(self.io, "tp_plot") else {"ok": False, "note": "无法瞬移到围栏"}
        self._corral_i = getattr(self, "_corral_i", 0) + 1
        if not tp.get("ok"):
            return tp
        time.sleep(0.45)
        win = self._load()
        if win not in (None, False):
            self._dismiss_if_paused()
            target = self.io.scan_plot(slot) if hasattr(self.io, "scan_plot") else {"ok": False}
            if target.get("ok"):
                self._turn_toward(target, rescanner=lambda: self.io.scan_plot(slot), max_steps=12)
                self._pitch_toward(lambda: self.io.scan_plot(slot))
        thrown = {"ok": False, "note": ""}
        if hasattr(self.io, "vac_launch"):
            thrown = self.io.vac_launch(slot)
        magic = self.io.feed_show(slot) if hasattr(self.io, "feed_show") else {"ok": False, "note": ""}
        notes = tp.get("note", "已到围栏前")
        if thrown.get("ok"):
            notes += "。" + thrown.get("note", "")
        if magic.get("note"):
            notes += "。" + magic.get("note", "")
        return {"ok": bool(thrown.get("ok") or magic.get("ok")),
                "note": notes}

    def sell_here(self) -> Dict[str, Any]:
        """瞬移到结晶市场前，台账背包里的结晶以真实物理投进投入口（原生结算入账）。"""
        self._dismiss_if_paused()
        tp = self.io.tp_market() if hasattr(self.io, "tp_market") else {"ok": False, "note": "无法瞬移到市场"}
        if not tp.get("ok"):
            return {"ok": False, "gains": 0, "note": tp.get("note", "")}
        time.sleep(0.45)
        win = self._load()
        if win not in (None, False):
            self._dismiss_if_paused()
            if not self._turn_toward(self._scan_port_close(), rescanner=self._scan_port_close, max_steps=8):
                pass  # 对准失败也继续：投掷走物理计算，不依赖准星
        try:
            before = int(self.io.read_money())
        except Exception:
            before = -1
        thrown = 0
        last_note = ""
        if hasattr(self.io, "vac_launch"):
            for _ in range(8):
                res = self.io.vac_launch("market")
                if not res.get("ok"):
                    last_note = res.get("note", "")
                    break
                thrown += 1
                time.sleep(0.8)
            time.sleep(2.0)   # 等飞行物进市场口结算
        else:
            return self._throw_into_market()
        try:
            after = int(self.io.read_money())
        except Exception:
            after = -1
        gains = max(0, after - before) if before >= 0 and after >= 0 else 0
        if gains > 0:
            return {"ok": True, "gains": gains,
                    "note": f"已瞬移到市场投出 {thrown} 件结晶（真实物理），收入 ${gains}，当前 ${after}"}
        return {"ok": False, "gains": 0,
                "note": f"投出 {thrown} 件后资金没有增加{('（' + last_note + '）') if last_note else ''}"}

    def _throw_into_market(self, throws: int = 6) -> Dict[str, Any]:
        win = self._load()
        if win is None or win is False or not self._ensure_ready():
            return {"ok": False, "gains": 0, "note": "未找到游戏窗口"}
        self._lock_mouse()
        if not self._turn_toward(self._scan_port_close(), rescanner=self._scan_port_close, max_steps=8):
            return {"ok": False, "gains": 0, "note": "已在市场前，但没对准投入口"}
        try:
            before = int(self.io.read_money())
        except Exception:
            before = -1
        c = self._center()
        if not c:
            return {"ok": False, "gains": 0, "note": "取不到窗口中心"}
        try:
            if not self._pitch_toward():
                return {"ok": False, "gains": 0, "note": "已在市场前，但视线没对准投入口"}
            for _ in range(throws):
                win.mouse_hold(c[0], c[1], 0.45)
                time.sleep(0.3)
            time.sleep(1.2)
        except Exception:
            return {"ok": False, "gains": 0, "note": "投出点击失败"}
        try:
            after = int(self.io.read_money())
        except Exception:
            after = -1
        gains = max(0, after - before) if before >= 0 and after >= 0 else 0
        if gains > 0:
            return {"ok": True, "gains": gains,
                    "note": f"已瞬移到市场并卖出，收入 ${gains}，当前资金 ${after}"}
        return {"ok": False, "gains": 0, "note": "已瞬移到市场并投掷，但资金没有增加"}

    # ---------------- 真实 UI 购买（走到设备→开菜单→点按钮→验证） ----------------
    # 地块菜单的交互键：实现期探针实测 SR1 是靠近自动弹出还是按键弹出后固化在这里。
    INTERACT_KEYS = ("e",)
    UI_WAIT_SECONDS = 8.0
    # SR1 失焦自动暂停的菜单按钮名（驻留时吞掉全部输入）
    _BLOCKING_MENU_BUTTONS = ("ResumeButton", "CloseButton", "BackButton", "CancelButton")

    def _dismiss_if_paused(self) -> None:
        """暂停菜单/设置面板驻留时点 Resume/Close 恢复；有 PurchaseButton 的正常菜单不碰。"""
        try:
            ui = self.io.ui_buttons()
        except Exception:
            return
        names = [str(b.get("name", "")) for b in ui.get("buttons", [])]
        if not names or any("Purchase" in n for n in names):
            return
        target = next((n for n in names if n in self._BLOCKING_MENU_BUTTONS), "")
        if target:
            self.click_button_named(target)

    def approach_plot(self, kind: str, final_dist: float = 3.0) -> Dict[str, Any]:
        """真实走到指定类型地块（corral/coop/garden/empty）跟前，供开购买/升级菜单。"""
        if not self._ensure_ready():
            return {"ok": False, "note": "未找到游戏窗口，无法走到地块"}
        stopped = self._aborted()
        if stopped:
            return stopped
        self._lock_mouse()

        def scanner():
            return self.io.scan_plot(kind)

        t = scanner()
        if not t.get("ok"):
            return {"ok": False, "note": t.get("note", "没有找到目标地块")}
        wres = self._walk_to_point(final_dist, 60, scanner,
                                   stall_limit=8, final_dist=final_dist + 4.0)
        if not wres.get("ok"):
            return {"ok": False, "stuck": bool(wres.get("stuck")),
                    "note": wres.get("note") or f"未能走到{kind}地块跟前"}
        return {"ok": True, "note": f"已走到{kind}地块跟前"}

    def arrive_plot(self, kind: str, final_dist: float = 3.0) -> Dict[str, Any]:
        """先走路。原地踏步或走不到时，直接出现在地块前面，让玩家看见人在目标跟前。"""
        walked = self.approach_plot(kind, final_dist)
        if walked.get("aborted"):
            return walked
        if walked.get("ok"):
            return walked
        tp = self.io.tp_plot(kind) if hasattr(self.io, "tp_plot") else {"ok": False, "note": "无法出现在地块前"}
        if tp.get("ok"):
            time.sleep(0.8)
            return {"ok": True, "shown": True, "note": tp.get("note", "已出现在地块前")}
        return walked

    def _click_candidates(self, btn: Dict[str, Any], ui: Dict[str, Any]):
        """由按钮上报的渲染坐标生成绝对屏幕坐标候选（多种原点/翻转假设）。

        真实映射由 ui_hover 闭环裁决：候选点里哪个能让游戏 EventSystem 射线命中
        目标按钮，就用哪个点按下。DPI 缩放统一按客户区/渲染分辨率比例换算。
        """
        win = self._load()
        if win is None or win is False:
            return []
        sw = int(ui.get("screen_w") or 0)
        sh = int(ui.get("screen_h") or 0)
        try:
            rect = win.get_client_rect(self._hwnd)
        except Exception:
            rect = None
        if not rect or sw <= 0 or sh <= 0:
            return []
        x0, y0, cw, ch = rect
        try:
            sx = float(btn.get("sx", 0.0))
            sy = float(btn.get("sy", 0.0))
        except Exception:
            return []
        xs = [sx, sx - sw / 2.0]
        ys = [sy, sh - sy, sy - sh / 2.0, sh / 2.0 - sy, sh - sy - sh / 2.0]
        seen = set()
        out = []
        for rx in xs:
            for ry in ys:
                pt = (int(x0 + rx * cw / sw), int(y0 + (sh - ry) * ch / sh))
                if pt in seen:
                    continue
                seen.add(pt)
                out.append(pt)
        return out

    def _hover_click(self, b: Dict[str, Any], ui: Dict[str, Any], target_name: str) -> bool:
        """候选点里找一个让游戏 EventSystem 射线命中 target_name 的，命中即真实点击。"""
        win = self._load()
        if win is None or win is False:
            return False
        for pt in self._click_candidates(b, ui):
            try:
                win.ensure_foreground(self._hwnd)
                win.mouse_move_to(pt[0], pt[1])
            except Exception:
                continue
            time.sleep(0.12)
            hov = self.io.ui_hover()
            if target_name and target_name in hov.get("chain", []):
                try:
                    win.mouse_click(pt[0], pt[1])
                except Exception:
                    return False
                return True
        return False

    def click_button_named(self, name: str) -> bool:
        """按按钮名 hover 自校准点击（关菜单/点确定这类无文字按钮用）。"""
        ui = self.io.ui_buttons()
        b = next((x for x in ui.get("buttons", []) if str(x.get("name", "")) == name), None)
        if b is None:
            return False
        return self._hover_click(b, ui, name)

    def click_ui_button(self, keywords, timeout: float = UI_WAIT_SECONDS) -> Dict[str, Any]:
        """轮询可见 UI 按钮，真实鼠标点击第一个文字命中 keywords 的按钮。

        轮询期内若一个按钮都没有，按一次交互键（菜单若需按键才会打开）；再没有就报失败，
        **绝不**退化成按固定坐标瞎点（点错扣钱比不买危害大）。
        """
        win = self._load()
        if win is None or win is False or not self._ensure_ready():
            return {"ok": False, "note": "未找到游戏窗口，无法点击菜单"}
        deadline = time.time() + timeout
        pressed = False
        ui: Dict[str, Any] = {}
        while time.time() < deadline:
            stopped = self._aborted()
            if stopped:
                return stopped
            self._dismiss_if_paused()
            ui = self.io.ui_buttons()
            hit = None
            for b in ui.get("buttons", []):
                label = str(b.get("label", ""))
                bname = str(b.get("name", "")).lower()
                # 文字或按钮名命中都算（建造菜单按钮可能只有名字如 CorralButton 没文字）
                if any(k and (k in label or k.lower() in bname) for k in keywords):
                    hit = (b, label or str(b.get("name", "")))
                    break
            if hit is not None:
                b, label = hit
                target_name = str(b.get("name", ""))
                if not self._hover_click(b, ui, target_name):
                    # 自校准全候选都落不到目标按钮上：宁可不点，也不瞎点扣钱
                    return {"ok": False,
                            "note": f"悬停自校准失败：光标落不到 {target_name or label} 上，本轮跳过点击"}
                return {"ok": True, "label": label,
                        "note": f"已点击菜单按钮「{label}」(hover 自校准)"}
            if not pressed:
                pressed = True
                for k in self.INTERACT_KEYS:
                    try:
                        win.press_key(k)
                        time.sleep(0.4)
                    except Exception:
                        pass
            time.sleep(0.5)
        seen = ui.get("buttons", []) if isinstance(ui, dict) else []
        desc = "; ".join(f"{b.get('name','')}:{b.get('label','')}" for b in seen[:8])
        texts = "; ".join(str(t.get("text", ""))[:24] for t in (ui.get("menu_texts") or [])[:6]) if isinstance(ui, dict) else ""
        return {"ok": False,
                "note": f"菜单里没找到可购买按钮（关键词: {'/'.join(keywords)}）| 看到按钮: {desc or '无'} | 菜单文字: {texts or '无'}"}

    def real_purchase(self, kind: str, mode: str, keywords, verify) -> Dict[str, Any]:
        """真实购买一步：走到地块→等菜单→点按钮→用 verify() 前后对比验证生效。

        verify 返回可比较的设施快照（地块类型+升级列表）。钱掉了但快照没变=误点，
        判失败且本轮不重试，避免反复误扣钱。
        """
        spot = "empty" if mode == "build" else kind
        ap = self.arrive_plot(spot)
        if not ap.get("ok"):
            return ap
        if ap.get("shown"):
            time.sleep(0.4)
        # 购买页要"对着地块边的终端桩按交互键"才开（用户确认），终端桩在地块某一侧：
        # 贴近→对准→试 E→没菜单就沿边缘横移/再贴近，换方位重试，最多 6 轮。
        def scanner():
            return self.io.scan_plot("empty" if mode == "build" else kind)
        win0 = self._load()
        for step in range(6):
            if self._aborted():
                return {"ok": False, "aborted": True, "note": "已切回观望，停下操作"}
            if self.io.ui_buttons().get("buttons"):
                break
            t = scanner()
            if t.get("ok"):
                self._turn_toward(t, rescanner=scanner)
            try:
                win0.press_key("e")
            except Exception:
                pass
            time.sleep(1.0)
            if self.io.ui_buttons().get("buttons"):
                break
            try:
                if step % 2 == 0:
                    win0.key_hold("w", 0.5)          # 再贴近一点
                else:
                    win0.key_hold("d", 0.6)          # 沿地块边缘横移找终端桩
                    t2 = scanner()
                    if t2.get("ok"):
                        self._turn_toward(t2, rescanner=scanner)
            except Exception:
                pass
            time.sleep(0.3)
        try:
            before_money = int(self.io.read_money())
        except Exception:
            before_money = -1
        before = verify()
        picked = self._select_menu_item(kind, mode)
        if picked.get("ok"):
            time.sleep(0.4)
        cr = self.click_ui_button(keywords, timeout=4.0)
        if not cr.get("ok"):
            magic = self.io.build(kind) if mode == "build" else self.io.upgrade(kind)
            if magic.get("ok"):
                time.sleep(0.6)
                return {"ok": True, "shown": True,
                        "note": "人已经站在地块前，直接完成%s，请看画面。%s" % (
                            "建造" if mode == "build" else "升级", magic.get("note", ""))}
            return cr
        time.sleep(1.5)
        after = verify()
        try:
            after_money = int(self.io.read_money())
        except Exception:
            after_money = -1
        if after != before:
            return {"ok": True, "label": cr.get("label", ""),
                    "note": f"已真实购买（点击「{cr.get('label','')}」），资金 ${before_money}→${after_money}"}
        if 0 <= after_money < before_money:
            return {"ok": False, "note": "扣了钱但设施没变化，疑似误点，本轮不再重试"}
        return {"ok": False, "note": "点击后设施状态没有变化，本次购买未生效"}

    def _select_menu_item(self, kind: str, mode: str) -> Dict[str, Any]:
        """建造/升级页上的设施项是 TMP 文字。先按文字点一下，再点购买。"""
        if mode == "build":
            needles = {
                "corral": ["Corral", "围栏", "畜栏"],
                "coop": ["Coop", "鸡舍", "鸡圈"],
                "garden": ["Garden", "菜园", "花园"],
            }.get(kind, [])
        else:
            needles = {
                "corral": ["Plort Collector", "结晶收集", "Collector"],
                "coop": ["Feeder", "喂食", "食槽"],
                "garden": ["Sprinkler", "喷灌", "喷洒"],
            }.get(kind, [])
        ui = self.io.ui_buttons() if hasattr(self.io, "ui_buttons") else {}
        win = self._load()
        for item in ui.get("menu_texts") or []:
            text = str(item.get("text") or "")
            low = text.lower()
            if not any(n.lower() in low for n in needles):
                continue
            if item.get("sx") is None or item.get("sy") is None:
                continue
            fake = {"sx": item.get("sx"), "sy": item.get("sy"), "w": 80, "h": 28,
                    "name": str(item.get("obj") or "")}
            if self._hover_click(fake, ui, fake["name"]):
                return {"ok": True, "note": text}
            pts = list(self._click_candidates(fake, ui))
            if pts and win not in (None, False):
                try:
                    win.mouse_click(pts[0][0], pts[0][1])
                    return {"ok": True, "note": text}
                except Exception:
                    pass
        return {"ok": False, "note": "菜单里没有对应的设施项"}

    def feed_visible(self) -> Dict[str, Any]:
        """投喂要让玩家看见：走到或出现在围栏前，做出投掷，再尝试把食物放进喂食器。"""
        arrived = self.arrive_plot("corral", 4.0)
        if not arrived.get("ok"):
            return arrived
        win = self._load()
        if win is None or win is False:
            return {"ok": False, "note": "未找到游戏窗口"}
        target = self.io.scan_corral() if hasattr(self.io, "scan_corral") else {"ok": False}
        if target.get("ok"):
            self._turn_toward(target, rescanner=self.io.scan_corral, max_steps=12)
            self._pitch_toward(self.io.scan_corral)   # 镜头对准围栏(略微俯视)再投掷
        try:
            win.mouse_move_rel(0, 8)                   # 微调余量：投出物弧线略下压进围栏
            time.sleep(0.1)
            center = self._center()
            if center:
                win.mouse_click(center[0], center[1])
                time.sleep(0.25)
                win.mouse_click(center[0], center[1])
        except Exception:
            pass
        magic = self.io.feed_show() if hasattr(self.io, "feed_show") else {"ok": False, "note": ""}
        where = arrived.get("note", "已到围栏前")
        extra = magic.get("note", "")
        return {"ok": True, "note": "%s。已做出投喂动作。%s" % (where, extra)}

    def wander_step(self, seconds: float = 1.5) -> Dict[str, Any]:
        """无目标时随机真实走动几秒（漫游），让画面真实移动而非原地/瞬移。"""
        win = self._load()
        if win is None or win is False or not self._ensure_ready():
            return {"ok": False, "note": "未找到游戏窗口"}
        # 小概率随机转动视角，其余向前漫游，制造真实探索感
        try:
            c = self._center()
            if c and _maybe(0.4):
                win.mouse_move_to(c[0] + (280 if _maybe(0.5) else -280), c[1])
                time.sleep(0.2)
            win.key_hold("w", seconds)
        except Exception:
            return {"ok": False, "note": "漫游失败"}
        return {"ok": True, "note": "已真实漫游 %.1fs" % seconds}


def _maybe(p: float) -> bool:
    import random
    return random.random() < p