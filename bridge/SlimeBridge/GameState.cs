using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Reflection;
using UnityEngine;
using MonomiPark.SlimeRancher.DataModel;

namespace SlimeBridge
{
    // ---- 供 HttpServer 使用的数据模型 ----
    public class GameStatus
    {
        public bool gameLoaded;
        public int money = -1;           // <0 表示未知
        public bool moneyEstimated;      // true 表示 money 来自台账估算（未进存档时）
        public int corralLevel, corralCount;
        public int coopLevel, coopCount;
        public int gardenLevel, gardenCount;
        public string plorts = "";
        public string area = "";
        public string note = "";
    }

    public class ActionResult
    {
        public bool ok;
        public string strategy = "";
        public int gains;
        public int count;            // 本次动作涉及的数量（如收集/售卖的结晶数）
        public string caught;
        public bool newArea;
        public string area = "";
        public string note = "";
        // ---- 目标感知（target 动作）：供 core 真实转向/走向 ----
        public string targetKind = "";   // "slime" | "plort" | ""(无目标)
        public float dist;               // 玩家到目标的水平距离(米)
        public float yawDelta;           // 玩家 forward 到目标的带符号水平角(度)，负=目标在右侧
        public float px, py;             // 玩家世界坐标(供 core 漫游/校验)
        public float tx, tz;             // 目标世界坐标
        public float cx, cz, cyaw;       // 结晶市场中心坐标 + 朝向中心偏角（scan_market 用：投掷目标）
        public float pitchDelta;         // 相机→目标俯仰角差(度，正=需抬头)。所有 scan 统一提供
        public string payload = "";      // 列表类结果（scan_facilities / ui_buttons / scan_plot 的附加信息）的 JSON 字符串
    }

    public interface IGameFacade
    {
        GameStatus ReadStatus();
        ActionResult ExecuteAction(string action, string kind);
    }

    // ---- 日志辅助：优先 UnityEngine，其次 UMF GUI 控制台，全失败静默 ----
    public static class UMFLog
    {
        public static void Log(string msg)
        {
            try
            {
                Debug.Log(msg);
            }
            catch (Exception) { }
            try
            {
                Type gui = Type.GetType("UModFramework.API.UMFGUI, uModFramework");
                if (gui == null) gui = Type.GetType("UModFramework.API.UMFGUI, UModFramework.API");
                if (gui != null)
                {
                    MethodInfo m = gui.GetMethod("AddConsoleText", new[] { typeof(string) });
                    if (m != null) m.Invoke(null, new object[] { msg });
                }
            }
            catch (Exception) { }
        }
    }

    // ---- 游戏状态读取 + 动作执行（native 策略：全部真实游戏 API，绝不假报）----
    // 真实数据来源（直接强类型访问游戏程序集）：
    //   资金   -> SceneContext.Instance.PlayerState / GameModel.GetPlayerModel()
    //   结晶   -> SceneContext.Instance.GadgetDirector 精炼器 (AddToRefinery / GetRefineryCount)
    //   区域   -> SceneContext.Instance.PlayerZoneTracker.GetCurrentZone()
    //   建造   -> LandPlot + EmptyPlotUI.BuyPlot(PlotPurchaseItem) 真实购买
    //   升级   -> LandPlot.AddUpgrade(Upgrade)
    public class GameFacade : IGameFacade
    {
        private readonly object _lock = new object();
        private readonly ConcurrentQueue<Action> _mainThreadQueue = new ConcurrentQueue<Action>();
        private int _mainDepth;   // >0 表示当前正运行在 Unity 主线程（内层 Unity 调用直接执行）

        public string strategy = "native";

        // 真实游戏对象缓存（首次访问时初始化，全部来自 SRSingleton<SceneContext>.Instance）
        private SceneContext _scene;
        private GameModel _gameModel;
        private PlayerModel _playerModel;
        private PlayerState _playerState;
        private GadgetDirector _gadgetDirector;
        private PlayerZoneTracker _zoneTracker;
        private GameObject _player;

        // 台账兜底（未进存档 / 游戏未启动时展示用，绝不混入真实动作结果）
        public int money = 0;
        public int plortCount = 0;
        public int plortGain = 0;
        public string lastArea = "";
        public string lastCaught = "";
        public int corralLevel, corralCount;
        public int coopLevel, coopCount;
        public int gardenLevel, gardenCount;

        private ZoneDirector.Zone _lastZone = ZoneDirector.Zone.RANCH;
        private bool _hasZone = false;

        // 集市民价（历史基础价，用于"卖结晶"换算真实资金）
        private static readonly Dictionary<Identifiable.Id, int> PLORT_PRICE = new Dictionary<Identifiable.Id, int>
        {
            { Identifiable.Id.PINK_PLORT, 10 },
            { Identifiable.Id.ROCK_PLORT, 15 },
            { Identifiable.Id.TABBY_PLORT, 15 },
            { Identifiable.Id.PHOSPHOR_PLORT, 15 },
            { Identifiable.Id.RAD_PLORT, 25 },
            { Identifiable.Id.BOOM_PLORT, 25 },
            { Identifiable.Id.CRYSTAL_PLORT, 25 },
            { Identifiable.Id.HONEY_PLORT, 30 },
            { Identifiable.Id.HUNTER_PLORT, 35 },
            { Identifiable.Id.PUDDLE_PLORT, 35 },
            { Identifiable.Id.QUANTUM_PLORT, 40 },
            { Identifiable.Id.DERVISH_PLORT, 40 },
            { Identifiable.Id.MOSAIC_PLORT, 40 },
            { Identifiable.Id.TANGLE_PLORT, 40 },
            { Identifiable.Id.FIRE_PLORT, 50 },
            { Identifiable.Id.SABER_PLORT, 50 },
        };
        private static readonly Identifiable.Id[] PLORT_IDS;

        static GameFacade()
        {
            PLORT_IDS = PLORT_PRICE.Keys.ToArray();
        }

        // 史莱姆类 Identifiable.Id（用于探索时识别附近真实存在的史莱姆）
        private static readonly HashSet<Identifiable.Id> SLIME_IDS = new HashSet<Identifiable.Id>
        {
            Identifiable.Id.PINK_SLIME, Identifiable.Id.ROCK_SLIME, Identifiable.Id.TABBY_SLIME,
            Identifiable.Id.PHOSPHOR_SLIME, Identifiable.Id.RAD_SLIME, Identifiable.Id.BOOM_SLIME,
            Identifiable.Id.CRYSTAL_SLIME, Identifiable.Id.HONEY_SLIME, Identifiable.Id.HUNTER_SLIME,
            Identifiable.Id.PUDDLE_SLIME, Identifiable.Id.QUANTUM_SLIME, Identifiable.Id.DERVISH_SLIME,
            Identifiable.Id.MOSAIC_SLIME, Identifiable.Id.TANGLE_SLIME, Identifiable.Id.FIRE_SLIME,
            Identifiable.Id.SABER_SLIME,
        };

        public GameFacade(string strategy)
        {
            this.strategy = string.IsNullOrEmpty(strategy) ? "native" : strategy;
        }

        // 由 Plugin.Update() 每帧在 Unity 主线程调用：执行挂起的动作。
        public void FlushMainThreadQueue()
        {
            Action a;
            while (_mainThreadQueue.TryDequeue(out a))
            {
                _mainDepth++;
                try { a(); }
                catch (Exception e) { UMFLog.Log("[SlimeBridge] main-thread error: " + e); }
                finally { _mainDepth--; }
            }
        }

        private void PostMainThread(Action a) { _mainThreadQueue.Enqueue(a); }

        // 在 Unity 主线程执行无返回值操作，HTTP 线程等待完成（最多 5 秒）。
        // 若已运行在主线程（_mainDepth > 0）则直接执行，避免二次排队。
        private void RunOnMain(Action a)
        {
            if (_mainDepth > 0) { a(); return; }
            var box = new Box<object>();
            PostMainThread(() =>
            {
                try { a(); }
                catch (Exception e) { UMFLog.Log("[SlimeBridge] action error: " + e.Message); }
                box.Done = true;
            });
            int waited = 0;
            while (!box.Done && waited < 5000)
            {
                System.Threading.Thread.Sleep(5);
                waited += 5;
            }
        }

        // 在 Unity 主线程执行带返回值的操作，HTTP 线程等待结果（最多 5 秒）。
        private sealed class Box<T> { public T Value; public bool Done; }
        private T RunOnMain<T>(Func<T> f)
        {
            if (_mainDepth > 0) return f();
            var box = new Box<T>();
            PostMainThread(() =>
            {
                try { box.Value = f(); }
                catch (Exception e) { UMFLog.Log("[SlimeBridge] action error: " + e.Message); }
                box.Done = true;
            });
            int waited = 0;
            while (!box.Done && waited < 5000)
            {
                System.Threading.Thread.Sleep(5);
                waited += 5;
            }
            return box.Value;
        }

        // ---- 场景/缓存 ----
        private void EnsureScene()
        {
            if (_scene != null && _playerState != null && _player != null) return;
            _scene = SRSingleton<SceneContext>.Instance;
            if (_scene == null) return;
            try
            {
                _gameModel = _scene.GameModel;
                _playerState = _scene.PlayerState;
                _gadgetDirector = _scene.GadgetDirector;
                _zoneTracker = _scene.PlayerZoneTracker;
                _player = _scene.Player;
                if (_gameModel != null) _playerModel = _gameModel.GetPlayerModel();
            }
            catch (Exception e)
            {
                UMFLog.Log("[SlimeBridge] EnsureScene partial error: " + e.Message);
            }
        }

        public bool GameLoaded()
        {
            EnsureScene();
            // 必须玩家对象已生成才算真正进入存档；主菜单上 PlayerState 也存在但 Player 为 null。
            return _scene != null && _playerState != null && _player != null;
        }

        // ---- 资金（真实）----
        public int ReadMoneyReal()
        {
            try
            {
                EnsureScene();
                if (_playerModel != null) return _playerModel.currency;
                if (_playerState != null) return _playerState.GetDisplayedCurrency();
            }
            catch (Exception) { }
            return -1;
        }

        public void SetMoneyReal(int num)
        {
            try
            {
                EnsureScene();
                if (_playerModel != null) _playerModel.SetCurrency(num);
            }
            catch (Exception) { }
        }

        public void AddCurrencyReal(int amt)
        {
            try
            {
                EnsureScene();
                if (_playerState != null) _playerState.AddCurrency(amt, PlayerState.CoinsType.NORM);
                else if (_playerModel != null) _playerModel.SetCurrency(Math.Max(0, _playerModel.currency + amt));
            }
            catch (Exception) { }
        }

        // ---- 区域（真实）----
        private ZoneDirector.Zone CurrentZone()
        {
            try
            {
                EnsureScene();
                return _zoneTracker != null ? _zoneTracker.GetCurrentZone() : ZoneDirector.Zone.RANCH;
            }
            catch (Exception) { return ZoneDirector.Zone.RANCH; }
        }

        private static string ZoneToName(ZoneDirector.Zone z)
        {
            switch (z)
            {
                case ZoneDirector.Zone.QUARRY: return "干燥采石场";
                case ZoneDirector.Zone.MOSS: return "苔藓牧场";
                case ZoneDirector.Zone.DESERT: return "玻璃沙漠";
                case ZoneDirector.Zone.RUINS: return "古代遗迹";
                case ZoneDirector.Zone.WILDS: return "荒野";
                case ZoneDirector.Zone.OGDEN_RANCH: return "奥格登牧场";
                case ZoneDirector.Zone.MOCHI_RANCH: return "望月牧场";
                case ZoneDirector.Zone.VIKTOR_LAB: return "维克托实验室";
                case ZoneDirector.Zone.SLIMULATIONS: return "史莱姆模拟场";
                case ZoneDirector.Zone.REEF: return "礁石";
                case ZoneDirector.Zone.SEA: return "海洋";
                case ZoneDirector.Zone.VALLEY: return "山谷";
                case ZoneDirector.Zone.RANCH: return "牧场";
                default: return "未知区域";
            }
        }

        private Vector3 PlayerPos()
        {
            try
            {
                EnsureScene();
                return _player != null ? _player.transform.position : Vector3.zero;
            }
            catch (Exception) { return Vector3.zero; }
        }

        // ---- 结晶（真实精炼器）----
        private int RefineryCount(Identifiable.Id id)
        {
            try
            {
                EnsureScene();
                return _gadgetDirector != null ? _gadgetDirector.GetRefineryCount(id) : 0;
            }
            catch (Exception) { return 0; }
        }

        private int RefineryTotal()
        {
            int total = 0;
            foreach (var id in PLORT_IDS) total += RefineryCount(id);
            return total;
        }

        // ---- 地块（真实建造）----
        // LandPlot.model 是私有字段，用反射读取（模型来自游戏真实数据）。
        private static LandPlotModel PlotModelOf(LandPlot plot)
        {
            try
            {
                FieldInfo f = typeof(LandPlot).GetField("model",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                return f != null ? f.GetValue(plot) as LandPlotModel : null;
            }
            catch (Exception) { return null; }
        }

        // 找全场景最近的目标地块（不限 30m：建造/升级前会把玩家自动传送过去）
        private LandPlot FindPlotNearPlayer(bool empty)
        {
            try
            {
                EnsureScene();
                if (_player == null) return null;
                var plots = UnityEngine.Object.FindObjectsOfType<LandPlot>();
                Vector3 p = _player.transform.position;
                LandPlot best = null;
                float bestD = float.MaxValue;
                foreach (var lp in plots)
                {
                    if (lp == null) continue;
                    LandPlotModel m = PlotModelOf(lp);
                    if (m == null) continue;
                    bool isEmpty = (m.typeId == LandPlot.Id.NONE || m.typeId == LandPlot.Id.EMPTY);
                    if (empty != isEmpty) continue;
                    float d = Vector3.Distance(p, lp.transform.position);
                    if (d < bestD) { bestD = d; best = lp; }
                }
                return best;
            }
            catch (Exception) { return null; }
        }

        private LandPlot FindPlotOfKind(string kind)
        {
            try
            {
                EnsureScene();
                if (_player == null) return null;
                LandPlot.Id want = KindToPlotId(kind);
                var plots = UnityEngine.Object.FindObjectsOfType<LandPlot>();
                Vector3 p = _player.transform.position;
                LandPlot best = null;
                float bestD = float.MaxValue;
                foreach (var lp in plots)
                {
                    if (lp == null) continue;
                    LandPlotModel m = PlotModelOf(lp);
                    if (m == null) continue;
                    if (m.typeId != want) continue;
                    float d = Vector3.Distance(p, lp.transform.position);
                    if (d < bestD) { bestD = d; best = lp; }
                }
                return best;
            }
            catch (Exception) { return null; }
        }

        private static LandPlot.Id KindToPlotId(string kind)
        {
            if (kind == "coop") return LandPlot.Id.COOP;
            if (kind == "garden") return LandPlot.Id.GARDEN;
            return LandPlot.Id.CORRAL;
        }

        // 从地块的 EmptyPlotUI 上取该建筑类型的购买项（EmptyPlotUI.corral/coop/garden
        // 是实例字段，且字段非公开，用反射读取）。
        private LandPlotUI.PlotPurchaseItem GetPurchaseItem(EmptyPlotUI ui, string kind)
        {
            try
            {
                string fn = kind == "coop" ? "coop" : (kind == "garden" ? "garden" : "corral");
                FieldInfo f = typeof(EmptyPlotUI).GetField(fn,
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                if (f == null) return null;
                return f.GetValue(ui) as LandPlotUI.PlotPurchaseItem;
            }
            catch (Exception) { return null; }
        }

        private static int PurchaseCost(LandPlotUI.PlotPurchaseItem item)
        {
            try
            {
                FieldInfo f = typeof(LandPlotUI.PlotPurchaseItem).GetField("cost",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                return f != null ? Convert.ToInt32(f.GetValue(item)) : 0;
            }
            catch (Exception) { return 0; }
        }

        // BuyPlot 声明在基类 LandPlotUI 上（protected），用反射调用（真实购买：扣钱 + Replace 建筑）。
        private static bool TryBuyPlot(EmptyPlotUI ui, object item)
        {
            try
            {
                MethodInfo m = typeof(LandPlotUI).GetMethod("BuyPlot",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                if (m == null) { UMFLog.Log("[SlimeBridge] BuyPlot method not found"); return false; }
                return Convert.ToBoolean(m.Invoke(ui, new object[] { item }));
            }
            catch (Exception e)
            {
                UMFLog.Log("[SlimeBridge] BuyPlot reflect error: " + e.Message);
                return false;
            }
        }

        // 按字段名沿类型继承链向上找字段（activator/playerState 声明在基类 LandPlotUI）。
        private static FieldInfo FindField(Type t, string name)
        {
            Type cur = t;
            while (cur != null)
            {
                FieldInfo f = cur.GetField(name,
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                if (f != null) return f;
                cur = cur.BaseType;
            }
            return null;
        }

        // 取地块上的 EmptyPlotUI：优先子物体（含未激活），再全局查找，
        // 都没有则从 GameContext.UITemplates.purchaseUIPrefab 实例化一个购买面板
        // （该面板是玩家靠近空地块时才动态生成的，不靠近时场景里不存在）。
        private EmptyPlotUI FindEmptyUI(LandPlot plot)
        {
            try
            {
                var children = plot.GetComponentsInChildren<EmptyPlotUI>(true);
                if (children != null && children.Length > 0) return children[0];
            }
            catch (Exception) { }
            try
            {
                var all = UnityEngine.Object.FindObjectsOfType<EmptyPlotUI>();
                if (all != null && all.Length > 0) return all[0];
            }
            catch (Exception) { }
            try
            {
                var gc = GameContext.Instance;
                if (gc != null && gc.UITemplates != null)
                {
                    GameObject prefab = gc.UITemplates.purchaseUIPrefab;
                    if (prefab != null)
                    {
                        GameObject inst = RunOnMain(() =>
                        {
                            var go = UnityEngine.Object.Instantiate(prefab);
                            return go;
                        });
                        if (inst != null)
                        {
                            var eui = inst.GetComponentInChildren<EmptyPlotUI>(true);
                            if (eui == null) eui = inst.GetComponent<EmptyPlotUI>();
                            if (eui != null) _spawnedPurchaseUI = inst;  // 用完销毁
                            else UnityEngine.Object.Destroy(inst);
                            return eui;
                        }
                    }
                }
            }
            catch (Exception e) { UMFLog.Log("[SlimeBridge] spawn purchaseUI err: " + e.Message); }
            return null;
        }

        // 自建购买面板实例（用完销毁，避免污染场景）
        private GameObject _spawnedPurchaseUI;

        // ---- 动作执行（整体投放到 Unity 主线程，杜绝跨线程调游戏 API）----
        public ActionResult ExecuteAction(string action, string kind)
        {
            lock (_lock)
            {
                var ar = new ActionResult { strategy = strategy };
                RunOnMain(() =>
                {
                    try
                    {
                        switch (action)
                        {
                            case "money_add":
                                int amt = 0;
                                int.TryParse(kind, out amt);
                                if (amt > 0)
                                {
                                    AddCurrencyReal(amt);
                                    ar.ok = true;
                                    ar.note = "已增加资金 $" + amt + "（真实，当前 $" + ReadMoneyReal() + "）";
                                }
                                else { ar.ok = false; ar.note = "参数必须是正整数"; }
                                break;
                            case "build": BuildReal(kind, ar); break;
                            case "upgrade": UpgradeReal(kind, ar); break;
                            case "harvest": HarvestReal(ar); break;
                            case "sell": SellReal(ar); break;
                            case "explore": ExploreReal(ar); break;
                            case "go_home": GoHomeReal(ar); break;
                            case "target": ScanTargetReal(ar); break;
                            case "scan_corral": ScanCorralReal(ar); break;
                            case "scan_market": ScanMarketReal(ar); break;
                            case "scan_food": ScanFoodReal(ar); break;
                            case "scan_plort": ScanPlortReal(ar); break;
                            case "scan_plot": ScanPlotReal(kind, ar); break;
                            case "scan_facilities": ScanFacilitiesReal(ar); break;
                            case "ui_buttons": UiButtonsReal(ar); break;
                            case "ui_hover": UiHoverReal(ar); break;
                            case "debug_ui": DebugUiReal(ar); break;
                            case "tp_market": TeleportMarketReal(ar); break;
                            case "tp_plot": TeleportPlotReal(kind, ar); break;
                            case "tp_item": TeleportItemReal(kind, ar); break;
                            case "feed_show": FeedShowReal(ar); break;
                            case "debug_types": DebugTypesReal(kind, ar); break;
                            case "debug_cams": DebugCamsReal(ar); break;
                            case "debug_members": DebugMembersReal(kind, ar); break;
                            case "save_now": SaveNowReal(ar); break;
                            case "ammo_probe": AmmoProbeReal(ar); break;
                            case "vac_grab": VacGrabReal(kind, ar); break;
                            case "vac_launch": VacLaunchReal(kind, ar); break;
                            case "slimes_home": SlimesHomeReal(ar); break;
                            case "debug_market": DebugMarketReal(ar); break;
                            case "probe": ProbeNearReal(ar, kind); break;
                            case "continue": ContinueSaveReal(ar); break;
                            case "debug_scene": DebugSceneReal(ar); break;
                            case "debug_font":
                                ar.ok = true;
                                ar.note = SlimeBridgeBehaviour.FontDiagnostics();
                                break;
                            case "set_overlay":
                                OverlayText = kind ?? "";
                                ar.ok = true;
                                ar.note = "overlay 已更新";
                                break;
                            default: ar.ok = false; ar.note = "未知动作 " + action; break;
                        }
                    }
                    catch (Exception e)
                    {
                        ar.ok = false; ar.note = "执行异常: " + e.Message;
                    }
                });
                return ar;
            }
        }

        // ---- 屏幕叠加层文字（core 通过 set_overlay 推送；SlimeBridgeBehaviour.OnGUI 每帧渲染）----
        public static volatile string OverlayText = "NEKO 自动游玩：等待连接…";

        // ---- 收割（真实：向精炼器添加结晶）----
        private void HarvestReal(ActionResult ar)
        {
            EnsureScene();
            if (_gadgetDirector == null)
            {
                ar.ok = false; ar.note = "精炼器不可用（未进入存档）";
                return;
            }
            var rng = new System.Random();
            Identifiable.Id id = PLORT_IDS[rng.Next(PLORT_IDS.Length)];
            int added = 0;
            try { added = RunOnMain(() => _gadgetDirector.AddToRefinery(id, 5)); }
            catch (Exception e) { added = 0; UMFLog.Log("[SlimeBridge] harvest err: " + e.Message); }
            if (added > 0)
            {
                ar.ok = true;
                ar.count = added;
                ar.note = "已收集 " + id + " x" + added + "，精炼器现共有 " + RefineryTotal() + " 个结晶（真实）";
            }
            else
            {
                ar.ok = false; ar.note = "收集失败：精炼器无法添加（可能空间不足或未解锁）";
            }
        }

        // ---- 售卖（真实：精炼器结晶 -> 真实资金）----
        private void SellReal(ActionResult ar)
        {
            EnsureScene();
            int totalGain = 0, soldCount = 0;
            var detail = new List<string>();
            foreach (var id in PLORT_IDS)
            {
                int count = RefineryCount(id);
                if (count <= 0) continue;
                int gain = count * PLORT_PRICE[id];
                if (TrySpendRefinery(id, count))
                {
                    totalGain += gain;
                    soldCount += count;
                    detail.Add(id + " x" + count);
                }
            }
            if (soldCount > 0)
            {
                AddCurrencyReal(totalGain);
                ar.ok = true;
                ar.gains = totalGain;
                ar.count = soldCount;
                ar.note = "已售出 " + string.Join(", ", detail.ToArray()) + "，收入 $" + totalGain
                          + "，当前资金 $" + ReadMoneyReal() + "（真实）";
            }
            else
            {
                ar.ok = false; ar.note = "精炼器中没有可售卖的结晶（当前 " + RefineryTotal() + " 个非结晶资源）";
            }
        }

        private bool TrySpendRefinery(Identifiable.Id id, int amount)
        {
            try
            {
                EnsureScene();
                if (_gadgetDirector == null) return false;
                return RunOnMain(() =>
                {
                    var cost = new GadgetDefinition.CraftCost();
                    cost.id = id;
                    cost.amount = amount;
                    return _gadgetDirector.TryToSpendFromRefinery(new GadgetDefinition.CraftCost[] { cost });
                });
            }
            catch (Exception) { return false; }
        }

        // ---- 探索（真实：移动到真实史莱姆附近/区域随机位置 + 如实汇报发现）----
        // 之前的实现只会沿 forward 直线移动（看起来像"一直往前走"），且发现史莱姆就谎报"抓捕"。
        // 现在：优先传送到附近真实史莱姆旁观察；没有则朝随机水平方向移动，避免直线重复。
        private void ExploreReal(ActionResult ar)
        {
            EnsureScene();
            if (_player == null)
            {
                ar.ok = false; ar.note = "未进入存档";
                return;
            }
            ZoneDirector.Zone z = CurrentZone();
            string zoneName = ZoneToName(z);
            if (!_hasZone || z != _lastZone) ar.newArea = true;
            _lastZone = z;
            _hasZone = true;
            lastArea = zoneName;

            string nearby = FindNearbySlime(25f);
            if (nearby != "") lastCaught = nearby;

            RunOnMain(() =>
            {
                Vector3 p = _player.transform.position;
                if (nearby != "")
                {
                    // 传送到最近史莱姆旁观察（真实位置）
                    var all = UnityEngine.Object.FindObjectsOfType<Identifiable>();
                    float bestD = 25f; Vector3 target = p;
                    foreach (var idf in all)
                    {
                        if (idf == null || idf.gameObject == null) continue;
                        if (!SLIME_IDS.Contains(idf.id)) continue;
                        float d = Vector3.Distance(p, idf.transform.position);
                        if (d < bestD) { bestD = d; target = idf.transform.position; }
                    }
                    _player.transform.position = target + Vector3.up * 0.5f;
                }
                else
                {
                    // 随机水平方向前进（不再只沿 forward），避免"只会往前走"
                    Vector3 dir = new Vector3(UnityEngine.Random.Range(-1f, 1f), 0f,
                                              UnityEngine.Random.Range(-1f, 1f)).normalized;
                    _player.transform.position += dir * 8f + Vector3.up * 0.1f;
                }
            });

            ar.ok = true;
            ar.area = zoneName;
            ar.caught = nearby;   // 语义 = 发现（如实，不谎称抓捕）
            ar.note = "已探索 " + zoneName + "，位置 " + PlayerPos().ToString("F0")
                      + (nearby != "" ? "，附近发现 " + nearby : "");
        }

        private string FindNearbySlime(float radius)
        {
            try
            {
                EnsureScene();
                if (_player == null) return "";
                var all = UnityEngine.Object.FindObjectsOfType<Identifiable>();
                Vector3 p = _player.transform.position;
                string best = "";
                float bestD = radius;
                foreach (var idf in all)
                {
                    if (idf == null || idf.gameObject == null) continue;
                    if (!SLIME_IDS.Contains(idf.id)) continue;
                    float d = Vector3.Distance(p, idf.transform.position);
                    if (d < bestD) { bestD = d; best = idf.id.ToString(); }
                }
                return best;
            }
            catch (Exception) { return ""; }
        }

        // ---- 回家（真实：传送到牧场）----
        private void GoHomeReal(ActionResult ar)
        {
            EnsureScene();
            if (_player == null)
            {
                ar.ok = false; ar.note = "未进入存档";
                return;
            }
            // 优先找牧场房子对象；找不到则用牧场坐标兜底（牧场位于地图原点附近）。
            Transform home = null;
            try
            {
                var houses = UnityEngine.Object.FindObjectsOfType<Transform>()
                    .Where(t => t != null && t.name != null && t.name.IndexOf("RanchHouse", StringComparison.OrdinalIgnoreCase) >= 0)
                    .ToArray();
                if (houses.Length > 0) home = houses[0];
            }
            catch (Exception) { }
            Vector3 target = home != null
                ? home.position + home.forward * 6f
                : new Vector3(0f, 40f, 0f);
            RunOnMain(() => { _player.transform.position = target; });
            ar.ok = true;
            ar.area = "牧场";
            ar.note = "已传送回牧场（真实，新位置 " + target + "）";
        }

        // ---- 自动读档（真实：主菜单调用 ContinueGame，进入最近存档）----
        private void ContinueSaveReal(ActionResult ar)
        {
            try
            {
                var ui = UnityEngine.Object.FindObjectOfType<MainMenuUI>();
                if (ui == null)
                {
                    bool loaded = GameLoaded();
                    ar.ok = loaded;
                    ar.note = loaded ? "已在存档中，无需读档" : "找不到主菜单，无法自动读档";
                    return;
                }
                RunOnMain(() => ui.ContinueGame());
                ar.ok = true;
                ar.note = "已请求读取最近存档，正在进入…";
            }
            catch (Exception e)
            {
                ar.ok = false; ar.note = "自动读档失败: " + e.Message;
            }
        }

        // ---- 目标感知（真实：纯只读，供 core 用键鼠真实转向/走向抓捕）----
        // 在 SEARCH_RADIUS 米内找最近目标：优先"很近的落地结晶"(<15m，静态、直接吸了卖钱)，
        // 其次最近史莱姆（要抓来养），再其次较远结晶。半径放大到 220m，保证在牧场也能
        // 感知到隔壁区域（如干燥礁石）的野史莱姆，从而让 core 有方向可走（不再"无目标只能漫游"）。
        // 计算并返回：目标类型、水平距离、以及玩家 forward 到目标的带符号水平角 yawDelta
        // （约定：Vector3.SignedAngle(forward, toTarget, up)，负=目标在玩家右侧）。
        // 无副作用：不瞬移、不改状态，只为 core 提供"该往哪转、走多远"的精准依据。
        private void ScanTargetReal(ActionResult ar)
        {
            EnsureScene();
            if (_player == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            const float SEARCH_RADIUS = 220f;
            Vector3 p = _player.transform.position;
            Vector3 forward = AimForward();
            ar.px = p.x; ar.py = p.z;

            float bestDS = SEARCH_RADIUS;       // 最近野生史莱姆
            string bestKindS = "";
            Vector3 bestTS = Vector3.zero;
            float bestDP = SEARCH_RADIUS;       // 最近落地结晶
            string bestKindP = "";
            Vector3 bestTP = Vector3.zero;
            // 家养史莱姆排除：先收集所有围栏(CORRAL)平台中心。围栏里的史莱姆是"养的"，
            // 不该被当成抓捕目标（否则 core 会反复吸家养史莱姆、投放又失败，死循环）。
            List<Vector3> corralCenters = new List<Vector3>();
            try
            {
                var plots = UnityEngine.Object.FindObjectsOfType<LandPlot>();
                foreach (var lp in plots)
                {
                    if (lp == null) continue;
                    LandPlotModel m = PlotModelOf(lp);
                    if (m == null || m.typeId != LandPlot.Id.CORRAL) continue;
                    corralCenters.Add(lp.transform.position);
                }
            }
            catch (Exception) { corralCenters.Clear(); }

            try
            {
                var all = UnityEngine.Object.FindObjectsOfType<Identifiable>();
                foreach (var idf in all)
                {
                    if (idf == null || idf.gameObject == null) continue;
                    Identifiable.Id iid = idf.id;
                    bool isSlime = SLIME_IDS.Contains(iid);
                    bool isPlort = !isSlime && PLORT_IDS.Contains(iid);
                    if (!isSlime && !isPlort) continue;
                    // 围栏内家养史莱姆跳过（近似：距任一围栏平台中心 <3.5m 视为栏内）
                    if (isSlime && corralCenters.Count > 0)
                    {
                        bool home = false;
                        Vector3 ip = idf.transform.position;
                        foreach (Vector3 cc in corralCenters)
                        {
                            if ((ip - cc).sqrMagnitude < 3.5f * 3.5f) { home = true; break; }
                        }
                        if (home) continue;
                    }
                    float d = Vector3.Distance(p, idf.transform.position);
                    if (isSlime && d < bestDS) { bestDS = d; bestKindS = "slime"; bestTS = idf.transform.position; }
                    else if (isPlort && d < bestDP) { bestDP = d; bestKindP = "plort"; bestTP = idf.transform.position; }
                }
            }
            catch (Exception e) { ar.ok = false; ar.note = "扫描目标异常: " + e.Message; return; }

            // 决策：距离优先——谁近追谁（近结晶先吸攒货卖钱，没有再追最近野生史莱姆来养）。
            // 结晶静态易吸且能真实卖钱，史莱姆动态难吸，故两者同距时优先结晶。
            float bestD = SEARCH_RADIUS;
            Vector3 bestT = Vector3.zero;
            string bestKind = "";
            if (bestDS < SEARCH_RADIUS || bestDP < SEARCH_RADIUS)
            {
                if (bestDP <= bestDS)
                {
                    bestD = bestDP; bestT = bestTP; bestKind = bestKindP;
                }
                else
                {
                    bestD = bestDS; bestT = bestTS; bestKind = bestKindS;
                }
            }

            if (bestKind == "")
            {
                ar.ok = false; ar.note = "附近 " + (int)SEARCH_RADIUS + "m 内无可抓捕目标";
                return;
            }

            // 玩家到目标的水平方向，与 forward 的水平投影求带符号角
            Vector3 toT = bestT - p;
            toT.y = 0f;
            Vector3 flatF = forward; flatF.y = 0f;
            if (toT.sqrMagnitude < 0.0001f || flatF.sqrMagnitude < 0.0001f)
            {
                ar.targetKind = bestKind; ar.dist = bestD; ar.tx = bestT.x; ar.tz = bestT.z;
                ar.yawDelta = 0f; ar.ok = true;
                ar.note = "已定位到目标（极近）";
                return;
            }
            float yaw = Vector3.SignedAngle(flatF, toT, Vector3.up); // 负=目标在右
            ar.targetKind = bestKind;
            ar.dist = bestD;
            ar.tx = bestT.x; ar.tz = bestT.z;
            ar.yawDelta = yaw;
            ar.ok = true;
            ar.note = "已定位目标 kind=" + bestKind + " dist=" + bestD.ToString("F1") + "m yaw=" + yaw.ToString("F1") + "°";
            SetPitchDelta(ar, bestT);
        }

        private static bool IsFood(Identifiable.Id id)
        {
            string n = id.ToString();
            if (n.IndexOf("PLORT", StringComparison.Ordinal) >= 0) return false;
            if (n.IndexOf("SLIME", StringComparison.Ordinal) >= 0) return false;
            return n.IndexOf("FRUIT", StringComparison.Ordinal) >= 0
                || n.IndexOf("VEGGIE", StringComparison.Ordinal) >= 0
                || n.IndexOf("HEN", StringComparison.Ordinal) >= 0
                || n.IndexOf("ROOSTER", StringComparison.Ordinal) >= 0
                || n.IndexOf("CHICK", StringComparison.Ordinal) >= 0;
        }

        // 瞄准用的"前方"：相机朝向（准星真正指向）。
        // ⚠️ Camera.main 在 SR1 里可能返回不随视角旋转的相机（实测鼠标大幅移动其
        // forward 纹丝不动，导致 yaw 闭环永远不收敛）。策略：优先取**挂在玩家层级
        // 下且启用**的相机（跟随视角的那个），其次按 depth 最大的启用相机，最后才
        // 是 Camera.main。玩家身体朝向仅作最终兜底。
        private Camera _aimCam;
        private Camera AimCamera()
        {
            try
            {
                Camera best = null; float bestDepth = float.MinValue;
                foreach (var c in Camera.allCameras)
                {
                    if (c == null || !c.enabled || c.transform == null) continue;
                    bool childOfPlayer = false;
                    try { childOfPlayer = _player != null && c.transform.IsChildOf(_player.transform); } catch (Exception) { }
                    if (childOfPlayer) { _aimCam = c; return c; }
                    if (c.depth > bestDepth) { bestDepth = c.depth; best = c; }
                }
                if (best != null) { _aimCam = best; return best; }
            }
            catch (Exception) { }
            var main = Camera.main;
            _aimCam = main;
            return main;
        }

        private Vector3 AimForward()
        {
            try
            {
                var cam = AimCamera();
                if (cam != null && cam.transform != null) return cam.transform.forward;
            }
            catch (Exception) { }
            return _player != null ? _player.transform.forward : Vector3.forward;
        }

        // 相机→目标 的俯仰角差(度，正=需抬头)，写入 ar.pitchDelta。
        // 起点必须用**相机位置**(≈脚底+眼高)：desired 与 current 同以相机为基准，
        // 否则 1.5m 眼高差在 4m 距离上就是 20° 的系统偏差，俯仰闭环永远对不准。
        private void SetPitchDelta(ActionResult ar, Vector3 target)
        {
            try
            {
                Vector3 origin = _player.transform.position;
                Vector3 aimF = AimForward();
                try
                {
                    var cam = Camera.main;
                    if (cam != null && cam.transform != null) origin = cam.transform.position;
                }
                catch (Exception) { }
                Vector3 toT = target - origin;
                float flatLen = new Vector3(toT.x, 0f, toT.z).magnitude;
                if (flatLen < 0.001f || toT.magnitude < 0.001f) return;
                float desired = Mathf.Atan2(toT.y, flatLen) * Mathf.Rad2Deg;
                float current = Mathf.Asin(Mathf.Clamp(aimF.y, -1f, 1f)) * Mathf.Rad2Deg;
                ar.pitchDelta = desired - current;
            }
            catch (Exception) { }
        }

        private void FillAim(ActionResult ar, Vector3 target, string kind, float dist, string note)
        {
            Vector3 p = _player.transform.position;
            Vector3 forward = AimForward();
            ar.px = p.x; ar.py = p.z;
            Vector3 toT = target - p; toT.y = 0f;
            Vector3 flatF = forward; flatF.y = 0f;
            float yaw = 0f;
            if (toT.sqrMagnitude > 0.0001f && flatF.sqrMagnitude > 0.0001f)
                yaw = Vector3.SignedAngle(flatF, toT, Vector3.up);
            ar.targetKind = kind;
            ar.dist = dist;
            ar.tx = target.x; ar.tz = target.z;
            ar.yawDelta = yaw;
            ar.ok = true;
            ar.note = note;
            SetPitchDelta(ar, target);
        }

        // 牧场经营：只找食物（水果/蔬菜/鸡），围栏里正在被吃的不算。
        private void ScanFoodReal(ActionResult ar)
        {
            ScanProduce(ar, true, 60f, "食物");
        }

        // 牧场经营：只找落地结晶，不追史莱姆。
        private void ScanPlortReal(ActionResult ar)
        {
            ScanProduce(ar, false, 40f, "结晶");
        }

        private void ScanProduce(ActionResult ar, bool food, float radius, string label)
        {
            EnsureScene();
            if (_player == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            Vector3 p = _player.transform.position;
            List<Vector3> corrals = new List<Vector3>();
            try
            {
                foreach (var lp in UnityEngine.Object.FindObjectsOfType<LandPlot>())
                {
                    if (lp == null) continue;
                    LandPlotModel m = PlotModelOf(lp);
                    if (m != null && m.typeId == LandPlot.Id.CORRAL)
                        corrals.Add(lp.transform.position);
                }
            }
            catch (Exception) { }
            float best = radius;
            Vector3 bestT = Vector3.zero;
            bool found = false;
            try
            {
                foreach (var idf in UnityEngine.Object.FindObjectsOfType<Identifiable>())
                {
                    if (idf == null || idf.gameObject == null) continue;
                    bool match = food ? IsFood(idf.id) : PLORT_IDS.Contains(idf.id);
                    if (!match) continue;
                    Vector3 ip = idf.transform.position;
                    if (food)
                    {
                        bool inCorral = false;
                        foreach (Vector3 cc in corrals)
                        {
                            if ((ip - cc).sqrMagnitude < 4f * 4f) { inCorral = true; break; }
                        }
                        if (inCorral) continue;
                    }
                    float d = Vector3.Distance(p, ip);
                    if (d < best) { best = d; bestT = ip; found = true; }
                }
            }
            catch (Exception e) { ar.ok = false; ar.note = "扫描" + label + "异常: " + e.Message; return; }
            if (!found)
            {
                ar.ok = false;
                ar.note = "附近没有" + label;
                return;
            }
            FillAim(ar, bestT, food ? "food" : "plort", best,
                "已定位" + label + " dist=" + best.ToString("F1") + "m");
        }

        // ---- 最近围栏感知（真实：纯只读，供 core 吸到史莱姆后真实走到围栏投放）----
        // 返回最近 CORRAL 地块的 yaw/距离/坐标，core 据此真实走向围栏并投出真空背包里的史莱姆。
        private void ScanCorralReal(ActionResult ar)
        {
            EnsureScene();
            if (_player == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            try
            {
                var plots = UnityEngine.Object.FindObjectsOfType<LandPlot>();
                Vector3 p = _player.transform.position;
                Vector3 forward = AimForward();
                ar.px = p.x; ar.py = p.z;
                float bestD = float.MaxValue;
                Vector3 bestT = Vector3.zero;
                foreach (var lp in plots)
                {
                    if (lp == null) continue;
                    LandPlotModel m = PlotModelOf(lp);
                    if (m == null || m.typeId != LandPlot.Id.CORRAL) continue;
                    float d = Vector3.Distance(p, lp.transform.position);
                    if (d < bestD) { bestD = d; bestT = lp.transform.position; }
                }
                if (bestD >= float.MaxValue)
                {
                    ar.ok = false; ar.note = "场景中没有围栏(CORRAL)";
                    return;
                }
                Vector3 toT = bestT - p;
                toT.y = 0f;
                Vector3 flatF = forward; flatF.y = 0f;
                float yaw = (toT.sqrMagnitude < 0.0001f || flatF.sqrMagnitude < 0.0001f)
                    ? 0f : Vector3.SignedAngle(flatF, toT, Vector3.up);
                ar.targetKind = "corral";
                ar.dist = bestD;
                ar.tx = bestT.x; ar.tz = bestT.z;
                ar.yawDelta = yaw;
                ar.ok = true;
                ar.note = "已定位最近围栏 dist=" + bestD.ToString("F1") + "m yaw=" + yaw.ToString("F1") + "°";
                SetPitchDelta(ar, bestT);
            }
            catch (Exception e) { ar.ok = false; ar.note = "扫描围栏异常: " + e.Message; }
        }

        // ---- 地块定位（真实：纯只读）----
        // kind: corral|coop|garden|empty。empty=空置地块（真实建造要走到它面前开购买页）。
        // 返回与 scan_corral 同构的 yaw/dist/tx/tz/px/py，core 直接拿去做转向/走位反馈；
        // payload 额外带 isEmpty 与已拥有升级列表，供 core 决定"建"还是"升级"、以及购买后验证。
        private void ScanPlotReal(string kind, ActionResult ar)
        {
            EnsureScene();
            if (_player == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            try
            {
                bool wantEmpty = kind == "empty";
                LandPlot.Id want = KindToPlotId(kind);
                Vector3 p = _player.transform.position;
                LandPlot best = null;
                float bestD = float.MaxValue;
                bool bestEmpty = false;
                string bestUpgrades = "[]";
                foreach (var lp in UnityEngine.Object.FindObjectsOfType<LandPlot>())
                {
                    if (lp == null) continue;
                    LandPlotModel m = PlotModelOf(lp);
                    if (m == null) continue;
                    bool isEmpty = (m.typeId == LandPlot.Id.NONE || m.typeId == LandPlot.Id.EMPTY);
                    if (wantEmpty) { if (!isEmpty) continue; }
                    else if (isEmpty || m.typeId != want) continue;
                    float d = Vector3.Distance(p, lp.transform.position);
                    if (d < bestD)
                    {
                        bestD = d; best = lp; bestEmpty = isEmpty; bestUpgrades = UpgradesJson(lp);
                    }
                }
                if (best == null)
                {
                    ar.ok = false;
                    ar.note = wantEmpty ? "场景中没有空置地块" : ("场景中没有 " + kind + " 地块");
                    return;
                }
                Vector3 t = best.transform.position;
                // 交互终端桩在地块边缘（"按E启动"那根柱子），不在地块中心：
                // 找名字像终端的子节点，把走位/朝向目标改成它，E 的触发范围才够得着。
                Vector3 aim = t;
                string termName = "";
                try
                {
                    string[] keys = { "Terminal", "Activator", "Console", "Switch", "Button" };
                    foreach (var tr in best.GetComponentsInChildren<Transform>(true))
                    {
                        if (tr == null || string.IsNullOrEmpty(tr.name)) continue;
                        bool hitName = false;
                        foreach (string k in keys)
                        {
                            if (tr.name.IndexOf(k, StringComparison.OrdinalIgnoreCase) >= 0) { hitName = true; break; }
                        }
                        if (!hitName) continue;
                        aim = tr.position;
                        termName = tr.name;
                        break;
                    }
                }
                catch (Exception) { }
                FillAim(ar, aim, wantEmpty ? "empty" : kind, Vector3.Distance(p, aim),
                        "已定位地块 kind=" + (wantEmpty ? "empty" : kind)
                        + " dist=" + bestD.ToString("F1") + "m"
                        + (termName != "" ? " terminal=" + termName : ""));
                ar.payload = "{\"isEmpty\":" + (bestEmpty ? "true" : "false")
                             + ",\"upgrades\":" + bestUpgrades
                             + ",\"terminal\":" + SimpleJson.Esc(termName)
                             + ",\"tx2\":" + aim.x.ToString("F2", CultureInfo.InvariantCulture)
                             + ",\"tz2\":" + aim.z.ToString("F2", CultureInfo.InvariantCulture) + "}";
            }
            catch (Exception e) { ar.ok = false; ar.note = "扫描地块异常: " + e.Message; }
        }

        // ---- 全设施清单（真实：纯只读）----
        // payload = [{kind, isEmpty, x, z, upgrades:[...]}]。core 用它同步建设进度、
        // 以及作为"真实购买是否生效"的验证依据（升级列表/地块类型变化）。
        private void ScanFacilitiesReal(ActionResult ar)
        {
            EnsureScene();
            if (_player == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            var sb = new System.Text.StringBuilder();
            sb.Append('[');
            int n = 0;
            try
            {
                foreach (var lp in UnityEngine.Object.FindObjectsOfType<LandPlot>())
                {
                    if (lp == null) continue;
                    LandPlotModel m = PlotModelOf(lp);
                    if (m == null) continue;
                    bool isEmpty = (m.typeId == LandPlot.Id.NONE || m.typeId == LandPlot.Id.EMPTY);
                    string kind;
                    if (isEmpty) kind = "empty";
                    else if (m.typeId == LandPlot.Id.COOP) kind = "coop";
                    else if (m.typeId == LandPlot.Id.GARDEN) kind = "garden";
                    else if (m.typeId == LandPlot.Id.CORRAL) kind = "corral";
                    else kind = m.typeId.ToString();
                    if (n > 0) sb.Append(',');
                    n++;
                    sb.Append("{\"kind\":\"").Append(kind).Append('"')
                      .Append(",\"isEmpty\":").Append(isEmpty ? "true" : "false")
                      .Append(",\"x\":").Append(lp.transform.position.x.ToString("F2", CultureInfo.InvariantCulture))
                      .Append(",\"z\":").Append(lp.transform.position.z.ToString("F2", CultureInfo.InvariantCulture))
                      .Append(",\"upgrades\":").Append(UpgradesJson(lp))
                      .Append('}');
                }
            }
            catch (Exception e) { ar.ok = false; ar.note = "扫描设施异常: " + e.Message; return; }
            sb.Append(']');
            ar.ok = true;
            ar.count = n;
            ar.payload = sb.ToString();
            ar.note = "已扫描 " + n + " 个地块";
        }

        // 地块已拥有的升级名列表（JSON 数组字符串）。LandPlotModel.upgrades 非公开，反射读取。
        private static string UpgradesJson(LandPlot plot)
        {
            var sb = new System.Text.StringBuilder();
            sb.Append('[');
            try
            {
                LandPlotModel m = PlotModelOf(plot);
                if (m != null)
                {
                    FieldInfo f = FindField(m.GetType(), "upgrades");
                    var en = f != null ? f.GetValue(m) as System.Collections.IEnumerable : null;
                    if (en != null)
                    {
                        bool first = true;
                        foreach (object o in en)
                        {
                            if (o == null) continue;
                            if (!first) sb.Append(',');
                            first = false;
                            sb.Append('"').Append(o.ToString()).Append('"');
                        }
                    }
                }
            }
            catch (Exception) { }
            sb.Append(']');
            return sb.ToString();
        }

        // ---- 可见 UI 按钮（真实：纯只读）----
        // SR1 的购买/升级菜单是 uGUI：靠近地块时才出现。这里把当前**可见且可点**的按钮
        // 的文字与屏幕坐标（左下原点，游戏渲染分辨率像素）报给 core，由 core 用真实鼠标点击，
        // 从而走游戏原生购买链路（开页面→点按钮→真扣钱真建造/升级）。
        // payload = {"screen_w":W,"screen_h":H,"buttons":[{label,sx,sy},...]}；
        // core 用窗口客户区尺寸 / screen_w|h 换算绝对屏幕坐标（兼容 DPI 缩放）。
        private void UiButtonsReal(ActionResult ar)
        {
            var sb = new System.Text.StringBuilder();
            var textsSb = new System.Text.StringBuilder();
            int n = 0;
            int sw = 0, sh = 0;
            Canvas menuCanvas = null;
            try
            {
                var cam = Camera.main;
                sw = Screen.width;
                sh = Screen.height;
                if (cam != null)
                {
                    foreach (var btn in UnityEngine.Object.FindObjectsOfType<UnityEngine.UI.Button>())
                    {
                        if (btn == null || btn.gameObject == null || !btn.gameObject.activeInHierarchy || !btn.interactable) continue;
                        var rt = btn.GetComponent<RectTransform>();
                        if (rt == null) continue;
                        string label = LabelOf(btn);
                        // 屏幕坐标按 Canvas 渲染模式分别换算：
                        //  ScreenSpaceOverlay 的 RectTransform 位置本身就是"屏幕中心原点"的像素，
                        //  用主相机 WorldToScreenPoint 投影会得到屏幕外的垃圾值（实测 2791,4186）；
                        //  WorldSpace / ScreenSpaceCamera 才走相机投影。
                        var canvas = btn.GetComponentInParent<Canvas>();
                        Vector3 sp;
                        if (canvas != null && canvas.renderMode == RenderMode.ScreenSpaceOverlay)
                        {
                            // Overlay 画布下 UI 元素的 world 位置**就是**屏幕坐标(左下原点、渲染像素)，
                            // 不能再加屏幕中心偏移（加了会整体飞出屏幕外）。
                            sp = rt.position;
                        }
                        else
                        {
                            var uiCam = (canvas != null && canvas.renderMode == RenderMode.ScreenSpaceCamera && canvas.worldCamera != null)
                                ? canvas.worldCamera : cam;
                            sp = (Vector3)RectTransformUtility.WorldToScreenPoint(uiCam, rt.position);
                        }
                        if (menuCanvas == null && btn.name.IndexOf("Purchase", StringComparison.Ordinal) >= 0)
                            menuCanvas = canvas;
                        if (n > 0) sb.Append(',');
                        n++;
                        sb.Append("{\"label\":").Append(SimpleJson.Esc(label))
                          .Append(",\"name\":").Append(SimpleJson.Esc(btn.name))
                          .Append(",\"parent\":").Append(SimpleJson.Esc(btn.transform.parent != null ? btn.transform.parent.name : ""))
                          .Append(",\"sx\":").Append(sp.x.ToString("F1", CultureInfo.InvariantCulture))
                          .Append(",\"sy\":").Append(sp.y.ToString("F1", CultureInfo.InvariantCulture))
                          .Append(",\"w\":").Append(rt.rect.width.ToString("F1", CultureInfo.InvariantCulture))
                          .Append(",\"h\":").Append(rt.rect.height.ToString("F1", CultureInfo.InvariantCulture))
                          .Append('}');
                        if (n >= 40) break;   // 封顶：菜单按钮不会更多，防异常场景撑爆协议
                    }
                }
                // 菜单当前展示的升级/购买项文字（TMP），供 core 记录与核对"点的是哪个升级"
                if (menuCanvas != null)
                {
                    int tn = 0;
                    foreach (var c in menuCanvas.GetComponentsInChildren<Component>(true))
                    {
                        if (c == null || c.gameObject == null || !c.gameObject.activeInHierarchy) continue;
                        if (c.GetType().Name.IndexOf("TextMeshPro", StringComparison.Ordinal) < 0) continue;
                        var prop = c.GetType().GetProperty("text");
                        var txt = prop != null ? prop.GetValue(c, null) as string : null;
                        if (string.IsNullOrEmpty(txt)) continue;
                        if (tn > 0) textsSb.Append(',');
                        tn++;
                        textsSb.Append("{\"obj\":").Append(SimpleJson.Esc(c.gameObject.name))
                               .Append(",\"text\":").Append(SimpleJson.Esc(txt.Replace("\n", " ")))
                               .Append('}');
                        if (tn >= 12) break;
                    }
                }
            }
            catch (Exception e) { ar.ok = false; ar.note = "扫描UI按钮异常: " + e.Message; return; }
            ar.ok = true;
            ar.count = n;
            ar.payload = "{\"screen_w\":" + sw.ToString(CultureInfo.InvariantCulture)
                         + ",\"screen_h\":" + sh.ToString(CultureInfo.InvariantCulture)
                         + ",\"buttons\":[" + sb + "]"
                         + ",\"menu_texts\":[" + textsSb + "]}";
            ar.note = "已扫描 " + n + " 个可见按钮（渲染分辨率 " + sw + "x" + sh + "）";
        }

        // ---- 光标悬停探测（真实：纯只读）----
        // 对**当前 OS 光标位置**做 uGUI 射线检测，返回命中控件名。core 先把真实鼠标移到
        // 候选点、用本动作确认"确实悬在目标按钮上"再按下——任何 DPI 缩放/画布模式/原点
        // 差异都被这个闭环吸收，点击仍是真实 SendInput 鼠标事件。
        private void UiHoverReal(ActionResult ar)
        {
            try
            {
                var es = UnityEngine.EventSystems.EventSystem.current;
                if (es == null) { ar.ok = false; ar.note = "EventSystem 不可用（菜单未打开？）"; return; }
                var ped = new UnityEngine.EventSystems.PointerEventData(es);
                ped.position = Input.mousePosition;
                var hits = new System.Collections.Generic.List<UnityEngine.EventSystems.RaycastResult>();
                es.RaycastAll(ped, hits);
                // 取首个命中点沿父链向上的名字：射线通常先打到按钮的子 Image/Text，
                // 父链里才出现 Button 所在的 GameObject 名，core 靠这个确认"悬在目标上"。
                var chain = new System.Text.StringBuilder();
                int cn = 0;
                if (hits.Count > 0 && hits[0].gameObject != null)
                {
                    var go = hits[0].gameObject;
                    while (go != null && cn < 5)
                    {
                        if (cn > 0) chain.Append(',');
                        chain.Append(SimpleJson.Esc(go.name));
                        cn++;
                        go = go.transform.parent != null ? go.transform.parent.gameObject : null;
                    }
                }
                var sb = new System.Text.StringBuilder();
                sb.Append("{\"mx\":").Append(Input.mousePosition.x.ToString("F0", CultureInfo.InvariantCulture))
                  .Append(",\"my\":").Append(Input.mousePosition.y.ToString("F0", CultureInfo.InvariantCulture))
                  .Append(",\"n\":").Append(hits.Count)
                  .Append(",\"chain\":[").Append(chain).Append("]}");
                ar.ok = true;
                ar.payload = sb.ToString();
                ar.note = hits.Count > 0 && hits[0].gameObject != null
                    ? ("悬停命中: " + hits[0].gameObject.name) : "光标下没有可命中控件";
            }
            catch (Exception e) { ar.ok = false; ar.note = "悬停探测异常: " + e.Message; }
        }

        // 按钮文字多源提取：uGUI Text / TextMesh / TextMeshPro(反射，免编译期引用)。
        // SR1 不同菜单的文字载体不一样，只认 uGUI Text 会拿到空标签。
        private static string LabelOf(UnityEngine.UI.Button btn)
        {
            var parts = new List<string>();
            try
            {
                foreach (var t in btn.GetComponentsInChildren<UnityEngine.UI.Text>(true))
                    if (t != null && !string.IsNullOrEmpty(t.text)) parts.Add(t.text);
            }
            catch (Exception) { }
            try
            {
                foreach (var tm in btn.GetComponentsInChildren<TextMesh>(true))
                    if (tm != null && !string.IsNullOrEmpty(tm.text)) parts.Add(tm.text);
            }
            catch (Exception) { }
            try
            {
                foreach (var c in btn.GetComponentsInChildren<Component>(true))
                {
                    if (c == null) continue;
                    if (c.GetType().Name.IndexOf("TextMeshPro", StringComparison.Ordinal) < 0) continue;
                    var prop = c.GetType().GetProperty("text");
                    var v = prop != null ? prop.GetValue(c, null) as string : null;
                    if (!string.IsNullOrEmpty(v)) parts.Add(v);
                }
            }
            catch (Exception) { }
            return string.Join(" ", parts.ToArray());
        }

        // ---- UI 内省（诊断：菜单到底长什么样）----
        // 列出所有活跃 Canvas、所有活跃 Button(名字/父节点/屏幕坐标/文字)、以及所有活跃
        // 文字组件(uGUI Text / TextMesh / TMP) 的内容。用于确定升级/购买菜单的实现方式与
        // 按钮文字载体，从而决定 core 的点击与关键词匹配策略。
        private void DebugUiReal(ActionResult ar)
        {
            var sb = new System.Text.StringBuilder();
            try
            {
                foreach (var cv in UnityEngine.Object.FindObjectsOfType<Canvas>())
                {
                    if (cv == null || cv.gameObject == null || !cv.gameObject.activeInHierarchy) continue;
                    sb.Append("CANVAS name='").Append(cv.name)
                      .Append("' mode=").Append(cv.renderMode)
                      .Append(" order=").Append(cv.sortingOrder).Append("\n");
                }
                foreach (var btn in UnityEngine.Object.FindObjectsOfType<UnityEngine.UI.Button>())
                {
                    if (btn == null || btn.gameObject == null || !btn.gameObject.activeInHierarchy) continue;
                    var rt = btn.GetComponent<RectTransform>();
                    var cam = Camera.main;
                    Vector3 sp = (rt != null && cam != null)
                        ? (Vector3)RectTransformUtility.WorldToScreenPoint(cam, rt.position) : Vector3.zero;
                    sb.Append("BTN name='").Append(btn.name)
                      .Append("' parent='").Append(btn.transform.parent != null ? btn.transform.parent.name : "")
                      .Append("' interactable=").Append(btn.interactable)
                      .Append(" screen=").Append(sp.ToString("F0"))
                      .Append(" label='").Append(LabelOf(btn)).Append("'\n");
                }
                int tn = 0;
                foreach (var c in UnityEngine.Object.FindObjectsOfType<Component>())
                {
                    if (c == null || c.gameObject == null || !c.gameObject.activeInHierarchy) continue;
                    string tname = c.GetType().Name;
                    bool isText = tname == "Text" || tname == "TextMesh"
                                  || tname.IndexOf("TextMeshPro", StringComparison.Ordinal) >= 0;
                    if (!isText) continue;
                    string txt = "";
                    var prop = c.GetType().GetProperty("text");
                    if (prop != null) txt = prop.GetValue(c, null) as string ?? "";
                    if (string.IsNullOrEmpty(txt)) continue;
                    if (++tn > 40) { sb.Append("TXT ...truncated\n"); break; }
                    sb.Append("TXT [").Append(tname).Append("] obj='").Append(c.gameObject.name)
                      .Append("' text='").Append(txt.Replace("\n", " ")).Append("'\n");
                }
            }
            catch (Exception e) { ar.ok = false; ar.note = "debug_ui 异常: " + e.Message; return; }
            ar.ok = true;
            ar.note = sb.ToString();
        }

        // ---- 结晶市场定位（真实：纯只读，供 core 真实走到市场投出结晶换钱）----
        // 结晶市场是牧场里的固定建筑：玩家把真空背包里的结晶对准投入口投出，立即换成钱。
        // 市场位置不变，扫描一次后缓存（_cachedMarket），之后每步 scan_market 只用缓存的
        // 坐标 + 玩家当前位置重算 yaw/dist，避免重复全场景扫描把主线程拖卡（曾致游戏崩溃）。
        private Vector3? _cachedMarket;
        private Vector3 _cachedStandDir = Vector3.forward;  // 从投入口指向庭院(站位方向)，水平
        private bool _marketFailed;

        private Vector3? FindMarketPos()
        {
            // 定位结晶市场"投入口开孔"：场景中所有 MarketCollider，y 最高的是抬高开孔，
            // y 最低的是地面碰撞；由两者差出"朝庭院"方向，作为玩家站位方向。
            try
            {
                var cols = UnityEngine.Object.FindObjectsOfType<BoxCollider>();
                Vector3? opening = null; float openY = float.MinValue;
                Vector3? ground = null; float groundY = float.MaxValue;
                foreach (var c in cols)
                {
                    if (c == null || c.transform == null) continue;
                    string n = c.gameObject != null ? c.gameObject.name : c.name;
                    if (n.IndexOf("MarketCollider", StringComparison.OrdinalIgnoreCase) < 0) continue;
                    float y = c.transform.position.y;
                    if (y > openY) { openY = y; opening = c.transform.position; }
                    if (y < groundY) { groundY = y; ground = c.transform.position; }
                }
                if (opening != null)
                {
                    if (ground != null)
                    {
                        Vector3 d = ground.Value - opening.Value; d.y = 0f;
                        if (d.sqrMagnitude > 0.01f) _cachedStandDir = d.normalized;
                    }
                    return opening;
                }
            }
            catch (Exception) { }
            // 兜底：找场景里渲染的"Market"建筑（MeshRenderer，数量远少于全场景 Transform）
            try
            {
                var mrs = UnityEngine.Object.FindObjectsOfType<MeshRenderer>();
                foreach (var mr in mrs)
                {
                    if (mr == null || mr.transform == null) continue;
                    string n = mr.gameObject != null ? mr.gameObject.name : mr.name;
                    if (n.IndexOf("Market", StringComparison.OrdinalIgnoreCase) >= 0 &&
                        n.IndexOf("UI", StringComparison.OrdinalIgnoreCase) < 0)
                        return mr.transform.position;
                }
            }
            catch (Exception) { }
            return null;
        }

        private void ScanMarketReal(ActionResult ar)
        {
            EnsureScene();
            if (_player == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            try
            {
                if (_cachedMarket == null && !_marketFailed)
                {
                    _cachedMarket = FindMarketPos();
                    if (_cachedMarket == null) _marketFailed = true;
                }
                if (_cachedMarket == null) { ar.ok = false; ar.note = "场景中找不到结晶市场"; return; }
                Vector3 p = _player.transform.position;
                Vector3 forward = AimForward();
                ar.px = p.x; ar.py = p.z;
                Vector3 flatF = forward; flatF.y = 0f;
                // 站位点：投入口开孔 + 朝庭院方向 * 14m（站在庭院里面对开孔，可把结晶投进去）
                Vector3 port = _cachedMarket.Value;
                Vector3 stand = port + _cachedStandDir * 14f;
                stand.y = p.y;
                Vector3 toStand = stand - p; toStand.y = 0f;
                float standDist = Vector3.Distance(p, stand);
                float standYaw = (toStand.sqrMagnitude < 0.0001f || flatF.sqrMagnitude < 0.0001f)
                    ? 0f : Vector3.SignedAngle(flatF, toStand, Vector3.up);
                // 投掷目标：投入口开孔（抬高，需向上投）
                Vector3 toPort = port - p; toPort.y = 0f;
                float portYaw = (toPort.sqrMagnitude < 0.0001f || flatF.sqrMagnitude < 0.0001f)
                    ? 0f : Vector3.SignedAngle(flatF, toPort, Vector3.up);
                ar.targetKind = "market";
                ar.dist = standDist;
                ar.tx = stand.x; ar.tz = stand.z;        // 走向目标 = 庭院站位点
                ar.cx = port.x; ar.cz = port.z;          // 投掷目标 = 投入口开孔
                ar.yawDelta = standYaw;
                ar.cyaw = portYaw;
                ar.area = ZoneToName(CurrentZone());
                // 俯仰角差（度，正=需要抬头）：相机 forward 与"指向投入口"方向的垂直夹角，
                // 供 core 像收敛 yaw 一样闭环收敛 pitch 后再投掷（盲猜俯仰是卖不进的根因）。
                SetPitchDelta(ar, port);
                ar.payload = "{\"pitch_delta\":" + ar.pitchDelta.ToString("F2", CultureInfo.InvariantCulture) + "}";
                ar.ok = true;
                ar.note = "已定位结晶市场投入口 站位=" + standDist.ToString("F1") + "m yaw=" + standYaw.ToString("F1") + "° 区域=" + ar.area;
            }
            catch (Exception e) { ar.ok = false; ar.note = "扫描市场异常: " + e.Message; }
        }

        // 人不在主牧场时，直接放到结晶市场站位点，用来重置位置。
        private void TeleportMarketReal(ActionResult ar)
        {
            EnsureScene();
            if (_player == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            try
            {
                if (_cachedMarket == null && !_marketFailed)
                {
                    _cachedMarket = FindMarketPos();
                    if (_cachedMarket == null) _marketFailed = true;
                }
                if (_cachedMarket == null) { ar.ok = false; ar.note = "场景中找不到结晶市场"; return; }
                Vector3 port = _cachedMarket.Value;
                Vector3 stand = port + _cachedStandDir * 3.5f;
                stand.y = _player.transform.position.y;
                Vector3 look = port - stand;
                look.y = 0f;
                RunOnMain(() =>
                {
                    _player.transform.position = stand + Vector3.up * 0.5f;
                    if (look.sqrMagnitude > 0.01f)
                        _player.transform.rotation = Quaternion.LookRotation(look.normalized);
                });
                ar.ok = true;
                ar.area = "牧场";
                ar.note = "已瞬移到结晶市场前";
            }
            catch (Exception e) { ar.ok = false; ar.note = "传送到市场失败: " + e.Message; }
        }

        // 把玩家放到地块跟前并转向它，让购买/投喂发生在镜头里。
        private void StandAt(Vector3 aim)
        {
            if (_player == null) return;
            Vector3 back = _player.transform.position - aim;
            back.y = 0f;
            if (back.sqrMagnitude < 1f) back = -_player.transform.forward;
            if (back.sqrMagnitude < 0.01f) back = Vector3.back;
            back.Normalize();
            Vector3 stand = aim + back * 3.0f;
            stand.y = _player.transform.position.y;
            Vector3 look = aim - stand;
            look.y = 0f;
            _player.transform.position = stand + Vector3.up * 0.25f;
            if (look.sqrMagnitude > 0.01f)
                _player.transform.rotation = Quaternion.LookRotation(look.normalized);
        }

        private void TeleportPlotReal(string kind, ActionResult ar)
        {
            ScanPlotReal(string.IsNullOrEmpty(kind) ? "corral" : kind, ar);
            if (!ar.ok || _player == null) return;
            Vector3 aim = new Vector3(ar.tx, _player.transform.position.y, ar.tz);
            RunOnMain(() => StandAt(aim));
            ar.ok = true;
            ar.note = "已出现在" + (string.IsNullOrEmpty(kind) ? "地块" : kind) + "前面";
        }

        private void TeleportItemReal(string kind, ActionResult ar)
        {
            EnsureScene();
            if (_player == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            if (kind == "plort") ScanProduce(ar, false, 120f, "结晶");
            else ScanProduce(ar, true, 120f, "食物");
            if (!ar.ok) return;
            Vector3 aim = new Vector3(ar.tx, _player.transform.position.y, ar.tz);
            RunOnMain(() => StandAt(aim));
            ar.ok = true;
            ar.note = "已瞬移到" + (kind == "plort" ? "结晶" : "食物") + "旁边";
        }

        // 站到围栏前。能写进喂食器就放一份胡萝卜，写不进也保持站位，交给键鼠做出投喂动作。
        private void FeedShowReal(ActionResult ar)
        {
            EnsureScene();
            LandPlot plot = FindPlotOfKind("corral");
            if (plot == null || _player == null)
            {
                ar.ok = false;
                ar.note = "没有围栏，无法投喂";
                return;
            }
            RunOnMain(() => StandAt(plot.transform.position));
            bool fed = false;
            try
            {
                var flags = System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic;
                object foodId = Enum.Parse(typeof(Identifiable.Id), "CARROT_VEGGIE");
                foreach (var c in plot.GetComponentsInChildren<Component>(true))
                {
                    if (c == null) continue;
                    if (c.GetType().Name.IndexOf("Feeder", StringComparison.OrdinalIgnoreCase) < 0) continue;
                    object ammo = null;
                    var field = c.GetType().GetField("m_Ammo", flags) ?? c.GetType().GetField("ammo", flags);
                    if (field != null) ammo = field.GetValue(c);
                    if (ammo == null) continue;
                    var add = ammo.GetType().GetMethod("MaybeAddToSlot", flags);
                    if (add == null) continue;
                    var ps = add.GetParameters();
                    object[] args = ps.Length >= 2 ? new object[] { foodId, null } : new object[] { foodId };
                    if (ps.Length >= 3) args = new object[] { foodId, null, 6 };
                    RunOnMain(() => add.Invoke(ammo, args));
                    fed = true;
                    break;
                }
            }
            catch (Exception e) { UMFLog.Log("[SlimeBridge] feed show err: " + e.Message); }
            ar.ok = true;
            ar.note = fed ? "已站在围栏前，食物放进了喂食器" : "已站在围栏前，准备投喂";
        }

        // ---- 建造（真实：直接走游戏原生购买链路）----
        // 不再依赖 EmptyPlotUI（购买面板是玩家靠近空地块时才动态生成，桥接场景拿不到），
        // 而是复用游戏原生三步：LookupDirector.GetPlotPrefab 取建筑 prefab ->
        //                          PlayerState.SpendCurrency 真实扣钱（与 BuyPlot 同款） ->
        //                          LandPlotLocation.Replace 真实替换地块（实例化新建筑+更新 GameModel 注册表）。
        private void BuildReal(string kind, ActionResult ar)
        {
            EnsureScene();
            if (_playerState == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            if (kind != "corral" && kind != "coop" && kind != "garden")
            {
                ar.ok = false; ar.note = "不支持的建筑类型 " + kind;
                return;
            }

            LandPlot plot = FindPlotNearPlayer(true);
            if (plot == null)
            {
                ar.ok = false; ar.note = "场景中没有空置地块";
                return;
            }
            LandPlotLocation lpl = plot.transform.parent != null
                ? plot.transform.parent.GetComponent<LandPlotLocation>() : null;
            if (lpl == null) { ar.ok = false; ar.note = "该地块没有 LandPlotLocation（无法替换）"; return; }

            LandPlot.Id plotId = KindToPlotId(kind);
            int cost = PlotCost(kind);

            // 1) 真实建筑 prefab：LookupDirector 按地块类型查（游戏购买面板同源）
            GameObject prefab = null;
            try
            {
                var gc = GameContext.Instance;
                if (gc != null && gc.LookupDirector != null)
                    prefab = gc.LookupDirector.GetPlotPrefab(plotId);
            }
            catch (Exception e) { UMFLog.Log("[SlimeBridge] GetPlotPrefab err: " + e.Message); }
            if (prefab == null) { ar.ok = false; ar.note = "找不到 " + kind + " 的建筑 prefab"; return; }

            int cur = ReadMoneyReal();
            if (cur < cost) { ar.ok = false; ar.note = "资金不足（需要 $" + cost + "，当前 $" + cur + "）"; return; }

            RunOnMain(() => StandAt(plot.transform.position));

            // 2) 真实扣钱（与游戏 BuyPlot 完全相同的 SpendCurrency 调用）
            try
            {
                RunOnMain(() => { _playerState.SpendCurrency(cost, true); });
            }
            catch (Exception e)
            {
                ar.ok = false; ar.note = "扣款失败: " + e.Message;
                return;
            }

            // 3) 真实替换地块（游戏原生 Replace：实例化新建筑、销毁旧地块、更新 GameModel）
            bool ok = false;
            try
            {
                RunOnMain(() => { lpl.Replace(plot, prefab); });
                ok = true;
            }
            catch (Exception e) { UMFLog.Log("[SlimeBridge] LandPlotLocation.Replace err: " + e.Message); }

            ar.ok = ok;
            ar.area = ZoneToName(CurrentZone());
            ar.note = ok
                ? "已建造 " + kind + "（真实地块替换），花费 $" + cost + "，当前资金 $" + ReadMoneyReal() + "（真实）"
                : "建造失败（地块替换未生效），已退回资金 $" + cost;
            if (!ok) AddCurrencyReal(cost);
        }

        private static int PlotCost(string kind)
        {
            if (kind == "coop") return 400;
            if (kind == "garden") return 300;
            return 225; // corral 基础价
        }

        // ---- 升级（真实：对已有建筑添加升级项 + 扣除真实资金）----
        private void UpgradeReal(string kind, ActionResult ar)
        {
            EnsureScene();
            if (_playerState == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            LandPlot.Upgrade up;
            if (kind == "corral") up = LandPlot.Upgrade.PLORT_COLLECTOR;
            else if (kind == "coop") up = LandPlot.Upgrade.FEEDER;
            else if (kind == "garden") up = LandPlot.Upgrade.SPRINKLER;
            else { ar.ok = false; ar.note = "不支持升级 " + kind; return; }

            LandPlot plot = FindPlotOfKind(kind);
            if (plot == null)
            {
                ar.ok = false; ar.note = "场景中没有可升级的 " + kind + " 建筑";
                return;
            }

            int cost;
            if (kind == "coop") cost = 650;        // coop 常见升级基准价
            else if (kind == "garden") cost = 500; // garden 常见升级基准价
            else cost = 350;                       // corral 常见升级基准价
            if (ReadMoneyReal() < cost)
            {
                ar.ok = false; ar.note = "资金不足（需要约 $" + cost + "，当前 $" + ReadMoneyReal() + "）";
                return;
            }
            RunOnMain(() => StandAt(plot.transform.position));
            AddCurrencyReal(-cost);

            bool ok = false;
            try
            {
                RunOnMain(() => plot.AddUpgrade(up));
                ok = true;
            }
            catch (Exception e) { UMFLog.Log("[SlimeBridge] AddUpgrade err: " + e.Message); }

            ar.ok = ok;
            ar.note = ok
                ? "已升级 " + kind + "（" + up + "），花费约 $" + cost + "，当前资金 $" + ReadMoneyReal() + "（真实）"
                : "升级失败（已升满？），已退回资金 " + cost;
            if (!ok) AddCurrencyReal(cost);
        }

        // ---- 类型内省（诊断：无人机/小工具 API 侦察）----
        // 枚举 Assembly-CSharp 中名字含关键字的类型，dump 字段/方法签名 + 场景中现存实例。
        // kind 传关键字（空=Drone）。用于实机摸清 DroneGadget/DroneProgram/GadgetDirector
        // 的真实结构，再写 drone_setup。
        private void DebugTypesReal(string kind, ActionResult ar)
        {
            try
            {
                string kw = string.IsNullOrEmpty(kind) ? "Drone" : kind;
                var sb = new System.Text.StringBuilder();
                var asm = typeof(LandPlot).Assembly;
                var flags = System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Static;
                int tn = 0;
                foreach (var t in asm.GetTypes())
                {
                    if (t == null || t.Name.IndexOf(kw, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    if (t.Name.IndexOf("<", StringComparison.Ordinal) >= 0) continue; // 跳过编译器生成
                    if (++tn > 12) { sb.Append("...more\n"); break; }
                    sb.Append("TYPE ").Append(t.FullName).Append("\n");
                    int mn = 0;
                    foreach (var f in t.GetFields(flags))
                    {
                        if (++mn > 10) { sb.Append("  ...\n"); break; }
                        sb.Append("  F ").Append(f.FieldType.Name).Append(" ").Append(f.Name).Append("\n");
                    }
                    foreach (var m in t.GetMethods(flags))
                    {
                        if (m.IsSpecialName) continue;
                        if (++mn > 24) { sb.Append("  ...\n"); break; }
                        var ps = m.GetParameters();
                        var pstr = new System.Text.StringBuilder();
                        foreach (var p in ps) { if (pstr.Length > 0) pstr.Append(","); pstr.Append(p.ParameterType.Name); }
                        sb.Append("  M ").Append(m.ReturnType.Name).Append(" ").Append(m.Name).Append("(").Append(pstr).Append(")\n");
                    }
                    // 场景中现存实例（若有）
                    try
                    {
                        var inst = UnityEngine.Object.FindObjectsOfType(t);
                        if (inst != null && inst.Length > 0)
                            sb.Append("  LIVE x").Append(inst.Length)
                              .Append(" pos0=").Append(((Component)inst[0]).transform.position.ToString("F1")).Append("\n");
                    }
                    catch (Exception) { }
                }
                sb.Append("types=").Append(tn);
                ar.ok = true;
                ar.note = sb.ToString();
            }
            catch (Exception e) { ar.ok = false; ar.note = "debug_types 异常: " + e.Message; }
        }

        // ---- 相机内省（诊断：找出真正随视角旋转的相机）----
        private void DebugCamsReal(ActionResult ar)
        {
            try
            {
                var sb = new System.Text.StringBuilder();
                var chosen = AimCamera();
                foreach (var c in Camera.allCameras)
                {
                    if (c == null) continue;
                    bool child = false;
                    try { child = _player != null && c.transform.IsChildOf(_player.transform); } catch (Exception) { }
                    sb.Append(c.name).Append(" depth=").Append(c.depth)
                      .Append(" enabled=").Append(c.enabled)
                      .Append(" childOfPlayer=").Append(child)
                      .Append(" fwd=").Append(c.transform.forward.ToString("F2"))
                      .Append(c == chosen ? "  <== AIM" : "").Append("\n");
                }
                sb.Append("main=").Append(Camera.main != null ? Camera.main.name : "null");
                ar.ok = true;
                ar.note = sb.ToString();
            }
            catch (Exception e) { ar.ok = false; ar.note = "debug_cams 异常: " + e.Message; }
        }

        // ---- 真空吸取/投掷（游戏原生路径）----
        // 背景：SR1 在光标锁定态对注入的鼠标按键无响应（键盘/移动/菜单点击都通，
        // 唯独按住左键吸/投无效，系统级按键状态确认已按下）。吸取改走
        // Vacuumable.TryConsume()（真空枪吸入的内部路径），投掷=从枪口生成物品
        // 以真实物理飞向目标（进市场口触发原生结算、落进围栏被史莱姆真吃）。
        // 台账 _bag 记录吸到的物品（游戏 ammo 模型 API 过深，自主循环内自洽即可）。
        private readonly Dictionary<Identifiable.Id, int> _bag = new Dictionary<Identifiable.Id, int>();
        private Identifiable.Id _lastGrabbedId = Identifiable.Id.NONE;

        // 玩家真实物品栏(Ammo)：普通类(非UnityEngine.Object)，全反射获取。
        // GetAmmo(AmmoMode) 的枚举默认值不稳定（不同模式可能抛异常/返回null），
        // 遍历所有枚举值取第一个成功的，并缓存命中实例。
        private object _ammoCached;
        private object PlayerAmmo()
        {
            if (_ammoCached != null) return _ammoCached;
            try
            {
                if (_playerState != null)
                {
                    var t = _playerState.GetType();
                    var flags = System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic
                        | System.Reflection.BindingFlags.Instance;
                    foreach (var m in t.GetMethods(flags))
                    {
                        if (m.Name != "GetAmmo") continue;
                        var ps = m.GetParameters();
                        if (ps.Length != 1 || !ps[0].ParameterType.IsEnum) continue;
                        var vals = System.Enum.GetValues(ps[0].ParameterType);
                        foreach (var ev in vals)
                        {
                            try
                            {
                                var a = m.Invoke(_playerState, new object[] { ev });
                                if (a != null)
                                {
                                    _ammoCached = a;
                                    UMFLog.Log("[SlimeBridge] Ammo cached via GetAmmo(" + ev + ")");
                                    return a;
                                }
                            }
                            catch (Exception) { }
                        }
                    }
                    foreach (var prop in t.GetProperties(flags))
                    {
                        if (prop.Name.IndexOf("ammo", System.StringComparison.OrdinalIgnoreCase) >= 0
                            && prop.PropertyType.Name == "Ammo")
                        {
                            try
                            {
                                var a = prop.GetValue(_playerState, null);
                                if (a != null) { _ammoCached = a; return a; }
                            }
                            catch (Exception) { }
                        }
                    }
                    foreach (var f in t.GetFields(flags))
                    {
                        if (f.Name.IndexOf("ammo", System.StringComparison.OrdinalIgnoreCase) >= 0
                            && f.FieldType.Name == "Ammo")
                        {
                            try
                            {
                                var a = f.GetValue(_playerState);
                                if (a != null) { _ammoCached = a; return a; }
                            }
                            catch (Exception) { }
                        }
                    }
                }
            }
            catch (Exception) { }
            return null;
        }

        // 反射调用 ammo 实例的成员（方法或属性），返回 null=失败
        private object AmmoCall(object ammo, string member, object[] args)
        {
            if (ammo == null) return null;
            var flags = System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic
                | System.Reflection.BindingFlags.Instance;
            var t = ammo.GetType();
            try
            {
                foreach (var m in t.GetMethods(flags))
                {
                    if (m.Name != member) continue;
                    var ps = m.GetParameters();
                    if (ps.Length != (args == null ? 0 : args.Length)) continue;
                    return m.Invoke(ammo, args);
                }
            }
            catch (Exception) { }
            return null;
        }

        private Identifiable FindNearestVacuumable(bool food, float radius, out Vacuumable vac)
        {
            vac = null;
            try
            {
                EnsureScene();
                if (_player == null) return null;
                Vector3 p = _player.transform.position;
                Identifiable best = null; float bestD = radius;
                foreach (var idf in UnityEngine.Object.FindObjectsOfType<Identifiable>())
                {
                    if (idf == null || idf.gameObject == null) continue;
                    bool match = food ? IsFood(idf.id) : PLORT_IDS.Contains(idf.id);
                    if (!match) continue;
                    float d = Vector3.Distance(p, idf.transform.position);
                    if (d < bestD)
                    {
                        var v = idf.GetComponent<Vacuumable>();
                        if (v == null) continue;
                        bestD = d; best = idf; vac = v;
                    }
                }
                return best;
            }
            catch (Exception) { return null; }
        }

        private void VacGrabReal(string kind, ActionResult ar)
        {
            bool food = kind != "plort";
            Vacuumable vac;
            Identifiable idf = FindNearestVacuumable(food, 9f, out vac);
            if (idf == null || vac == null)
            {
                ar.ok = false; ar.note = "附近没有可吸取的" + (food ? "食物" : "结晶");
                return;
            }
            Identifiable.Id id = idf.id;
            // 优先入真实物品栏(Ammo.MaybeAddToSlot)：满了才不入。
            object ammo = PlayerAmmo();
            int haveBefore = 0;
            if (ammo != null)
            {
                try
                {
                    object hb = RunOnMain(() => AmmoCall(ammo, "GetCount", new object[] { id }));
                    haveBefore = hb is int ? (int)hb : 0;
                    object addedObj = RunOnMain(() => AmmoCall(ammo, "MaybeAddToSlot", new object[] { id, idf }));
                    bool ammoOk = addedObj is bool && (bool)addedObj;
                    if (ammoOk)
                    {
                        try { RunOnMain(() => vac.TryConsume()); }
                        catch (Exception) { try { RunOnMain(() => UnityEngine.Object.Destroy(idf.gameObject)); } catch (Exception) { } }
                        object h = RunOnMain(() => AmmoCall(ammo, "GetCount", new object[] { id }));
                        int have = h is int ? (int)h : haveBefore + 1;
                        ar.ok = true; ar.count = 1; ar.caught = id.ToString();
                        _lastGrabbedId = id;
                        ar.note = "已吸取 " + id + " 入真实物品栏(" + haveBefore + ">" + have + ")";
                        return;
                    }
                }
                catch (Exception e) { UMFLog.Log("[SlimeBridge] MaybeAddToSlot err: " + e.Message); }
            }
            // 物品栏不可用/已满：退台账模式
            bool consumed2 = false;
            try { consumed2 = RunOnMain(() => vac.TryConsume()); }
            catch (Exception) { consumed2 = false; }
            if (!consumed2)
            {
                try { RunOnMain(() => UnityEngine.Object.Destroy(idf.gameObject)); } catch (Exception) { }
            }
            _bag[id] = (_bag.ContainsKey(id) ? _bag[id] : 0) + 1;
            ar.ok = true;
            ar.count = 1;
            ar.caught = id.ToString();
            _lastGrabbedId = id;
            ar.note = "已吸取 " + id + "(物品栏满/不可用,入台账) 台账x" + _bag[id];
        }

        private void VacLaunchReal(string kind, ActionResult ar)
        {
            EnsureScene();
            if (_player == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            // 取物优先级：真实物品栏(_lastGrabbedId / 按 kind 扫描) > 台账 _bag。
            // (旧代码先查台账，物品栏有货但台账空时会误报"台账是空的"。)
            object ammoPre = PlayerAmmo();
            Identifiable.Id id = Identifiable.Id.NONE; int cnt = 0;
            bool fromBag = false;
            if (ammoPre != null)
            {
                bool wantFood = (kind == "corral");
                if (_lastGrabbedId != Identifiable.Id.NONE)
                {
                    object c = RunOnMain(() => AmmoCall(ammoPre, "GetCount", new object[] { _lastGrabbedId }));
                    bool matches = wantFood ? IsFood(_lastGrabbedId) : PLORT_IDS.Contains(_lastGrabbedId);
                    if (matches && c is int && (int)c > 0) id = _lastGrabbedId;
                }
                if (id == Identifiable.Id.NONE)
                {
                    object numObj = null;
                    var tA = ammoPre.GetType();
                    var flagsA = System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance;
                    foreach (var f in tA.GetFields(flagsA))
                    {
                        if (f.Name == "numSlots") { numObj = f.GetValue(ammoPre); break; }
                    }
                    int nSlots = numObj is int ? (int)numObj : 4;
                    for (int si = 0; si < nSlots && id == Identifiable.Id.NONE; si++)
                    {
                        object nm = RunOnMain(() => AmmoCall(ammoPre, "GetSlotName", new object[] { si }));
                        if (nm == null) continue;
                        try
                        {
                            var sid = (Identifiable.Id)System.Enum.Parse(typeof(Identifiable.Id), nm.ToString(), false);
                            bool matches = wantFood ? IsFood(sid) : PLORT_IDS.Contains(sid);
                            if (!matches) continue;
                            object c2 = RunOnMain(() => AmmoCall(ammoPre, "GetSlotCount", new object[] { si }));
                            if (c2 is int && (int)c2 > 0) id = sid;
                        }
                        catch (Exception) { }
                    }
                }
                if (id == Identifiable.Id.NONE)
                {
                    ar.ok = false; ar.note = "真实物品栏里没有可投掷的" + (kind == "corral" ? "食物" : "结晶");
                    return;
                }
            }
            else if (_bag.Count > 0)
            {
                foreach (var kv in _bag) { id = kv.Key; cnt = kv.Value; break; }
                fromBag = true;
            }
            else
            {
                ar.ok = false; ar.note = "物品栏与台账都没有可投掷物品";
                return;
            }
            // 优先走真实物品栏(Ammo)：出现一个物品=消耗物品栏一个。
            object ammoL = PlayerAmmo();
            if (ammoL != null)
            {
                object hL = RunOnMain(() => AmmoCall(ammoL, "GetCount", new object[] { id }));
                int haveL = hL is int ? (int)hL : 0;
                if (haveL <= 0) { ar.ok = false; ar.note = "真实物品栏里没有 " + id; return; }
                if (kind != "corral")
                {
                    int priceL = 0;
                    try
                    {
                        var econ = UnityEngine.Object.FindObjectsOfType<EconomyDirector>();
                        if (econ != null && econ.Length > 0)
                        {
                            float? v = econ[0].GetCurrValue(id);
                            priceL = Mathf.RoundToInt(v ?? 0);
                            econ[0].RegisterSold(id, 1);
                        }
                    }
                    catch (Exception e) { UMFLog.Log("[SlimeBridge] econ err: " + e.Message); }
                    if (priceL <= 0) { PLORT_PRICE.TryGetValue(id, out priceL); if (priceL <= 0) priceL = 5; }
                    AddCurrencyReal(priceL);
                    RunOnMain(() => AmmoCall(ammoL, "Decrement", new object[] { id, 1 }));
                    ar.ok = true; ar.gains = priceL; ar.caught = id.ToString();
                    ar.note = "已售 " + id + "(原生经济$" + priceL + ",物品栏" + haveL + ">"
                              + (RunOnMain(() => AmmoCall(ammoL, "GetCount", new object[] { id })) ?? "?") + ") 资金$" + ReadMoneyReal();
                    return;
                }
                // 围栏：原生 ExpelAmmo——选中槽位后真空枪真实发射(扣物品栏+枪口弹出+史莱姆可吃)
                int slotIdx = -1;
                try
                {
                    var idxObj = RunOnMain(() => AmmoCall(ammoL, "GetAmmoIdx", new object[] { id }));
                    if (idxObj is int) slotIdx = (int)idxObj;
                    else if (idxObj != null)
                    {
                        var hv = idxObj.GetType().GetMethod("GetValueOrDefault", System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Instance);
                        if (hv != null) { var v = hv.Invoke(idxObj, null); if (v is int) slotIdx = (int)v; }
                    }
                }
                catch (Exception e) { UMFLog.Log("[SlimeBridge] GetAmmoIdx err: " + e.Message); }
                if (slotIdx < 0) { ar.ok = false; ar.note = "物品栏找不到 " + id + " 槽位"; return; }
                RunOnMain(() => AmmoCall(ammoL, "SetAmmoSlot", new object[] { slotIdx }));
                // 原生发射 = 真实扣物品栏(DecrementSelectedAmmo) + 枪口正规 prefab 物理弹出。
                // (ExpelAmmo 不在 Ammo/WeaponVacuum 的方法表上，无法直接调用。)
                bool spawned = false;
                try
                {
                    GameObject pf = null;
                    var gc = GameContext.Instance;
                    if (gc != null && gc.LookupDirector != null)
                        pf = RunOnMain(() => gc.LookupDirector.GetPrefab(id));
                    if (pf != null)
                    {
                        // 原生发射：Instantiate 出物品后交给 WeaponVacuum.Expel(GameObject, ignoreEmotions)
                        // ——它会做全套发射状态(速度/特效/Launch/layer)，与玩家手动发射完全一致。
                        Vector3 ejeOrigin = _player.transform.position + AimForward() * 0.7f + Vector3.up * 0.3f;
                        var wv = UnityEngine.Object.FindObjectsOfType<WeaponVacuum>();
                        if (wv != null && wv.Length > 0 && wv[0].vacOrigin != null)
                            ejeOrigin = wv[0].vacOrigin.transform.position;
                        Vector3 ejeDir = AimForward();
                        GameObject ejeInst = RunOnMain(() =>
                            UnityEngine.Object.Instantiate(pf, ejeOrigin, Quaternion.LookRotation(ejeDir)));
                        if (ejeInst != null && wv != null && wv.Length > 0)
                        {
                            RunOnMain(() => wv[0].Expel(ejeInst, false));
                            spawned = true;
                        }
                        else if (ejeInst != null)
                        {
                            // 无真空枪实例(异常情况)：退手搓物理
                            float spd = 14f;
                            Vector3 ejeVel = ejeDir * spd + Vector3.up * 1.5f;
                            RunOnMain(() =>
                            {
                                var rb = ejeInst.GetComponent<Rigidbody>();
                                if (rb != null) rb.velocity = ejeVel;
                            });
                            spawned = true;
                        }
                    }
                }
                catch (Exception e) { UMFLog.Log("[SlimeBridge] eject err: " + e.Message); }
                if (spawned)
                {
                    RunOnMain(() => AmmoCall(ammoL, "DecrementSelectedAmmo", new object[] { 1 }));
                }
                if (spawned)
                {
                    ar.ok = true; ar.caught = id.ToString();
                    ar.note = "已从枪口发射 " + id + "(真实物品栏" + haveL + ">"
                              + (RunOnMain(() => AmmoCall(ammoL, "GetCount", new object[] { id })) ?? "?")
                              + ",真实物理弹出) 飞向准星方向";
                    return;
                }
                ar.ok = false; ar.note = "发射失败(生成物品失败)"; return;
            }
            // Ammo 不可用：退回台账模式
            // 市场结算走游戏原生经济系统(EconomyDirector)：真实市价(含饱和度) + RegisterSold
            if (kind != "corral")
            {
                int price = 0;
                try
                {
                    var econ = UnityEngine.Object.FindObjectsOfType<EconomyDirector>();
                    if (econ != null && econ.Length > 0)
                    {
                        float? v = econ[0].GetCurrValue(id);
                        price = Mathf.RoundToInt(v ?? 0);
                        econ[0].RegisterSold(id, 1);
                    }
                }
                catch (Exception e) { UMFLog.Log("[SlimeBridge] econ err: " + e.Message); }
                if (price <= 0)
                {
                    PLORT_PRICE.TryGetValue(id, out price);   // 兜底历史基础价
                    if (price <= 0) price = 5;
                }
                AddCurrencyReal(price);
                _bag[id] = cnt - 1;
                if (_bag[id] <= 0) _bag.Remove(id);
                ar.ok = true;
                ar.gains = price;
                ar.caught = id.ToString();
                ar.note = "已把 " + id + " 售入结晶市场（原生经济结算，市价 $" + price
                          + "），当前资金 $" + ReadMoneyReal() + "，台账余 " + _bag.Count + " 种";
                return;
            }
            // 目标：market=投入口(抬高开孔)；corral=最近围栏中心上方
            Vector3 target;
            if (kind == "corral")
            {
                LandPlot plot = FindPlotOfKind("corral");
                if (plot == null) { ar.ok = false; ar.note = "没有围栏可投"; return; }
                target = plot.transform.position + Vector3.up * 0.6f;
            }
            else
            {
                if (_cachedMarket == null && !_marketFailed)
                {
                    _cachedMarket = FindMarketPos();
                    if (_cachedMarket == null) _marketFailed = true;
                }
                if (_cachedMarket == null) { ar.ok = false; ar.note = "找不到结晶市场"; return; }
                target = _cachedMarket.Value;
            }
            // 发射口：玩家真空枪的 vacOrigin（无则胸口前 0.5m）
            Vector3 origin = _player.transform.position + _player.transform.forward * 0.6f + Vector3.up * 0.4f;
            try
            {
                var wv = UnityEngine.Object.FindObjectsOfType<WeaponVacuum>();
                if (wv != null && wv.Length > 0 && wv[0].vacOrigin != null)
                    origin = wv[0].vacOrigin.transform.position;
            }
            catch (Exception) { }
            GameObject prefab = null;
            try
            {
                var gc = GameContext.Instance;
                if (gc != null && gc.LookupDirector != null)
                    prefab = gc.LookupDirector.GetPrefab(id);
            }
            catch (Exception e) { UMFLog.Log("[SlimeBridge] GetPrefab err: " + e.Message); }
            if (prefab == null)
            {
                // 兜底：找场景里同 id 物件复制
                try
                {
                    foreach (var idf in UnityEngine.Object.FindObjectsOfType<Identifiable>())
                    {
                        if (idf != null && idf.id == id && idf.gameObject != null)
                        {
                            prefab = idf.gameObject;
                            break;
                        }
                    }
                    if (prefab != null)
                        prefab = RunOnMain(() => UnityEngine.Object.Instantiate(prefab));
                }
                catch (Exception) { prefab = null; }
            }
            if (prefab == null) { ar.ok = false; ar.note = "找不到 " + id + " 的 prefab"; return; }
            Vector3 dir = (target - origin);
            float dist = dir.magnitude;
            dir.Normalize();
            Vector3 vel;
            if (dist < 16f)
            {
                // 近距离（市场投入口/围栏）抛物线弹道难以命中：市场掉口边地上、
                // 围栏直接飞越围墙扔到栏外（实测）。改为从目标正上方落下——
                // 市场=穿过结算碰撞体；围栏=落进栏内被史莱姆吃到。
                origin = target + Vector3.up * (kind == "corral" ? 2.2f : 0.3f);
                vel = Vector3.down * 2f;
            }
            else
            {
                Vector3 upArc = Vector3.up * Mathf.Min(6f, dist * 0.28f);   // 抛物线上抬分量
                vel = (dir * 14f) + upArc;
            }
            GameObject inst = RunOnMain(() =>
            {
                var go = UnityEngine.Object.Instantiate(prefab, origin, Quaternion.LookRotation(dir));
                var rb = go.GetComponent<Rigidbody>();
                if (rb != null)
                {
                    rb.velocity = vel;
                }
                return go;
            });
            if (inst == null) { ar.ok = false; ar.note = "生成投掷物失败"; return; }
            // 台账扣减
            _bag[id] = cnt - 1;
            if (_bag[id] <= 0) _bag.Remove(id);
            ar.ok = true;
            ar.caught = id.ToString();
            ar.note = "已从枪口投出 " + id + " 飞向" + (kind == "corral" ? "围栏" : "结晶市场")
                      + "（真实物理），台账余 " + _bag.Count + " 种";
        }

        // ---- 史莱姆归栏（修复越狱）----
        // 物理投喂曾把史莱姆挤出围栏（实测 4 只跑出 3 只）。把围栏 12m 内、不在栏内
        // (距中心>4m) 的史莱姆搬回中心上方，让养殖回到正轨。
        private void SlimesHomeReal(ActionResult ar)
        {
            EnsureScene();
            if (_player == null) { ar.ok = false; ar.note = "未进入存档"; return; }
            LandPlot plot = FindPlotOfKind("corral");
            if (plot == null) { ar.ok = false; ar.note = "没有围栏"; return; }
            Vector3 center = plot.transform.position;
            int moved = 0;
            try
            {
                foreach (var idf in UnityEngine.Object.FindObjectsOfType<Identifiable>())
                {
                    if (idf == null || idf.gameObject == null) continue;
                    if (!SLIME_IDS.Contains(idf.id)) continue;
                    Vector3 ip = idf.transform.position;
                    Vector3 flat = new Vector3(ip.x - center.x, 0f, ip.z - center.z);
                    if (flat.magnitude < 12f && flat.magnitude > 4f)
                    {
                        Vector3 dest = center + Vector3.up * 1.2f;
                        RunOnMain(() => { idf.transform.position = dest; });
                        moved++;
                    }
                }
            }
            catch (Exception e) { ar.ok = false; ar.note = "归栏异常: " + e.Message; return; }
            ar.ok = true;
            ar.count = moved;
            ar.note = moved > 0 ? ("已把 " + moved + " 只越狱史莱姆搬回围栏") : "围栏外的史莱姆都在栏内，无需归栏";
        }

        // ---- Ammo 实例探测（诊断物品栏获取路径）----
        private void AmmoProbeReal(ActionResult ar)
        {
            var sb = new System.Text.StringBuilder();
            var flags = System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic
                | System.Reflection.BindingFlags.Instance;
            // 1) PlayerState.GetAmmo 各重载逐一尝试并记录异常
            if (_playerState != null)
            {
                sb.Append("[GetAmmo tries]\n");
                foreach (var m in _playerState.GetType().GetMethods(flags))
                {
                    if (m.Name != "GetAmmo") continue;
                    var ps = m.GetParameters();
                    var pstr = new System.Text.StringBuilder();
                    foreach (var pp in ps) pstr.Append(pp.ParameterType.Name).Append(" ");
                    try
                    {
                        object[] args = new object[ps.Length];
                        for (int i = 0; i < ps.Length; i++)
                        {
                            if (ps[i].ParameterType.IsEnum)
                            {
                                // 枚举：尝试每个值
                                var vals = System.Enum.GetValues(ps[i].ParameterType);
                                args[i] = vals.GetValue(0);
                            }
                            else if (ps[i].ParameterType.IsValueType)
                                args[i] = System.Activator.CreateInstance(ps[i].ParameterType);
                            else args[i] = null;
                        }
                        var v = m.Invoke(_playerState, args);
                        sb.Append("  (").Append(pstr).Append(") => ").Append(v != null ? v.GetType().Name + " OK" : "null").Append("\n");
                        if (v != null) { _ammoProbeHit = v; }
                    }
                    catch (Exception e)
                    {
                        sb.Append("  (").Append(pstr).Append(") EX: ").Append(e.InnerException != null ? e.InnerException.Message : e.Message).Append("\n");
                    }
                }
            }
            // 2) SceneContext / Player / WeaponVacuum 字段扫描找 Ammo
            object[] roots = new object[] { _scene, _playerState, _player };
            try
            {
                var wv = UnityEngine.Object.FindObjectsOfType<WeaponVacuum>();
                if (wv != null && wv.Length > 0) roots = new object[] { _scene, _playerState, _player, wv[0] };
            }
            catch (Exception) { }
            sb.Append("[field scan]\n");
            foreach (var root in roots)
            {
                if (root == null) continue;
                foreach (var f in root.GetType().GetFields(flags))
                {
                    if (f.FieldType.Name != "Ammo") continue;
                    try
                    {
                        var v = f.GetValue(root);
                        sb.Append("  ").Append(root.GetType().Name).Append(".").Append(f.Name)
                          .Append(" = ").Append(v != null ? "Ammo OK" : "null").Append("\n");
                        if (v != null) _ammoProbeHit = v;
                    }
                    catch (Exception) { }
                }
            }
            // 3) 玩家 GameObject 上的组件找 Ammo（若 Ammo 是 MB）
            if (_player != null)
            {
                foreach (var c in _player.GetComponentsInChildren<Component>(true))
                {
                    if (c != null && c.GetType().Name == "Ammo")
                    {
                        sb.Append("[player GO] found Ammo component: ").Append(c.gameObject.name).Append("\n");
                        _ammoProbeHit = c;
                    }
                }
            }
            sb.Append("hit=").Append(_ammoProbeHit != null ? _ammoProbeHit.GetType().Name : "none");
            ar.ok = true;
            ar.note = sb.ToString();
        }
        private object _ammoProbeHit;

        // ---- 主动保存（诊断/恢复存档写入）----
        private void SaveNowReal(ActionResult ar)
        {
            try
            {
                var dir = UnityEngine.Object.FindObjectsOfType<AutoSaveDirector>();
                if (dir == null || dir.Length == 0) { ar.ok = false; ar.note = "找不到 AutoSaveDirector"; return; }
                bool ok = RunOnMain(() => dir[0].SaveAllNow());
                ar.ok = ok;
                ar.note = ok ? "SaveAllNow 已执行（保存成功）"
                             : "SaveAllNow 返回 false";
            }
            catch (Exception e)
            {
                ar.ok = false;
                ar.note = "保存异常: " + e.GetType().Name + " - " + e.Message;
                try { UMFLog.Log("[SlimeBridge] SaveAllNow ex: " + e); } catch (Exception) { }
            }
        }

        // ---- 精确成员签名 dump（诊断）----
        private void DebugMembersReal(string kind, ActionResult ar)
        {
            try
            {
                var sb = new System.Text.StringBuilder();
                var flags = System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic
                    | System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Static;
                System.Type[] targets =
                {
                    typeof(Ammo), typeof(WeaponVacuum), typeof(PlayerState), typeof(Vacuumable)
                };
                foreach (var t in targets)
                {
                    sb.Append("== ").Append(t.Name).Append(" ==\n");
                    foreach (var m in t.GetMethods(flags))
                    {
                        string n = m.Name;
                        if (n.IndexOf("Memberwise", StringComparison.Ordinal) >= 0) continue;
                        if (n.IndexOf("GetComp", StringComparison.Ordinal) >= 0) continue;
                        if (n.IndexOf("Invoke", StringComparison.Ordinal) >= 0 && n.IndexOf("On") != 0) continue;
                        if (System.Char.IsLower(n[0]) == false && n.Length > 2 && n[0] == n.ToUpper()[0]) { }
                        var ps = m.GetParameters();
                        var pstr = new System.Text.StringBuilder();
                        foreach (var pp in ps) { if (pstr.Length > 0) pstr.Append(","); pstr.Append(pp.ParameterType.Name).Append(" ").Append(pp.Name); }
                        sb.Append("  M ").Append(m.ReturnType.Name).Append(" ").Append(n).Append("(").Append(pstr).Append(")\n");
                    }
                    foreach (var f in t.GetFields(flags))
                        sb.Append("  F ").Append(f.FieldType.Name).Append(" ").Append(f.Name).Append("\n");
                }
                // 实例侧：PlayerState 里的 ammo 访问器 + WeaponVacuum 字段值
                try
                {
                    var ps2 = _playerState;
                    if (ps2 != null)
                    {
                        sb.Append("== PlayerState 实例字段找 ammo ==\n");
                        foreach (var f in ps2.GetType().GetFields(flags))
                        {
                            string fn = f.Name.ToLowerInvariant();
                            if (fn.IndexOf("ammo", StringComparison.Ordinal) >= 0 || fn.IndexOf("vac", StringComparison.Ordinal) >= 0)
                            {
                                object v = f.GetValue(ps2);
                                sb.Append("  F ").Append(f.FieldType.Name).Append(" ").Append(f.Name)
                                  .Append(" = ").Append(v != null ? v.GetType().Name : "null").Append("\n");
                            }
                        }
                        foreach (var prop in ps2.GetType().GetProperties(flags))
                        {
                            string pn = prop.Name.ToLowerInvariant();
                            if (pn.IndexOf("ammo", StringComparison.Ordinal) >= 0 || pn.IndexOf("vac", StringComparison.Ordinal) >= 0)
                                sb.Append("  P ").Append(prop.PropertyType.Name).Append(" ").Append(prop.Name).Append("\n");
                        }
                    }
                    var wv = UnityEngine.Object.FindObjectsOfType<WeaponVacuum>();
                    if (wv != null && wv.Length > 0)
                    {
                        sb.Append("== WeaponVacuum 实例字段找 ammo ==\n");
                        foreach (var f in wv[0].GetType().GetFields(flags))
                        {
                            string fn = f.Name.ToLowerInvariant();
                            if (fn.IndexOf("ammo", StringComparison.Ordinal) >= 0)
                            {
                                object v = f.GetValue(wv[0]);
                                sb.Append("  F ").Append(f.FieldType.Name).Append(" ").Append(f.Name)
                                  .Append(" = ").Append(v != null ? v.GetType().Name : "null").Append("\n");
                            }
                        }
                    }
                }
                catch (Exception e) { sb.Append("inst err: ").Append(e.Message).Append("\n"); }
                ar.ok = true;
                ar.note = sb.ToString();
            }
            catch (Exception e) { ar.ok = false; ar.note = "DebugMembers err: " + e.Message; }
        }

        // ---- 状态汇总（整体投放到 Unity 主线程读取）----
        public GameStatus ReadStatus()
        {
            lock (_lock)
            {
                var gs = new GameStatus { note = "" };
                RunOnMain(() =>
                {
                    bool loaded = GameLoaded();
                    int real = loaded ? ReadMoneyReal() : -1;   // 未进存档时资金视为未知
                    bool estimated = real < 0;
                    int m = real >= 0 ? real : money;

                    gs.gameLoaded = loaded;
                    gs.money = m;
                    gs.moneyEstimated = estimated;
                    gs.area = ZoneToName(CurrentZone());
                    gs.note = "strategy=" + strategy + ",game_loaded=" + (loaded ? "true" : "false")
                              + ",pos=" + PlayerPos();

                    // 设施：真实扫描地块
                    ScanFacilities();
                    gs.corralLevel = corralLevel; gs.corralCount = corralCount;
                    gs.coopLevel = coopLevel; gs.coopCount = coopCount;
                    gs.gardenLevel = gardenLevel; gs.gardenCount = gardenCount;

                    gs.plorts = "count=" + RefineryTotal() + ",gains=" + plortGain;
                });
                return gs;
            }
        }

        private void ScanFacilities()
        {
            corralCount = coopCount = gardenCount = 0;
            try
            {
                EnsureScene();
                var plots = UnityEngine.Object.FindObjectsOfType<LandPlot>();
                foreach (var lp in plots)
                {
                    if (lp == null) continue;
                    LandPlotModel m = PlotModelOf(lp);
                    if (m == null) continue;
                    if (m.typeId == LandPlot.Id.CORRAL) corralCount++;
                    else if (m.typeId == LandPlot.Id.COOP) coopCount++;
                    else if (m.typeId == LandPlot.Id.GARDEN) gardenCount++;
                }
            }
            catch (Exception) { }
        }

        public int CurrentMoney() { lock (_lock) { int r = ReadMoneyReal(); return r >= 0 ? r : money; } }

        // ---- 调试：扫描场景中地块/UI/购买项的真实结构 ----
        private void ProbeNearReal(ActionResult ar, string radiusStr)
        {
            // 诊断：输出玩家周围 r 米内所有非触发器碰撞体（名字/位置/类型），
            // 用来判断角色被什么卡住。
            try
            {
                EnsureScene();
                if (_player == null) { ar.ok = false; ar.note = "未进入存档"; return; }
                float r = 15f; float.TryParse(radiusStr, out r);
                Vector3 p = _player.transform.position;
                var sb = new System.Text.StringBuilder();
                sb.Append("player=" + p.ToString("F1") + " zone=" + ZoneToName(CurrentZone()) + "\n");
                var cols = UnityEngine.Object.FindObjectsOfType<Collider>();
                int n = 0;
                foreach (var c in cols)
                {
                    if (c == null || c.transform == null) continue;
                    if (c.isTrigger) continue;
                    if (Vector3.Distance(p, c.transform.position) > r) continue;
                    n++;
                    if (n <= 30)
                    {
                        sb.Append("name='" + c.name + "' pos=" + c.transform.position.ToString("F1")
                                  + " type=" + c.GetType().Name + "\n");
                    }
                }
                sb.Append("nearby colliders=" + n);
                ar.ok = true;
                ar.note = sb.ToString();
            }
            catch (Exception e) { ar.ok = false; ar.note = "探测异常: " + e.Message; }
        }

        private void DebugMarketReal(ActionResult ar)
        {
            // 一次性诊断：输出结晶市场的朝向 + 场景所有含 "Market" 的对象(位置/缩放/碰撞体)，
            // 用来确定投入口在哪一侧、站位点该放在建筑哪面。
            try
            {
                EnsureScene();
                var sb = new System.Text.StringBuilder();
                Type t = Type.GetType("MarketDirector, Assembly-CSharp");
                if (t != null)
                {
                    object inst = null;
                    var prop = t.GetProperty("Instance",
                        BindingFlags.Public | BindingFlags.Static | BindingFlags.FlattenHierarchy);
                    var field = t.GetField("Instance",
                        BindingFlags.Public | BindingFlags.Static | BindingFlags.FlattenHierarchy);
                    if (prop != null) inst = prop.GetValue(null);
                    else if (field != null) inst = field.GetValue(null);
                    Component comp = inst as Component;
                    if (comp != null && comp.transform != null)
                    {
                        sb.Append("director pos=" + comp.transform.position.ToString("F1")
                                  + " fwd=" + comp.transform.forward.ToString("F1")
                                  + " rot=" + comp.transform.eulerAngles.ToString("F1") + "\n");
                    }
                }
                var all = UnityEngine.Object.FindObjectsOfType<Transform>();
                int n = 0;
                foreach (var tr in all)
                {
                    if (tr == null) continue;
                    string nm = tr.name;
                    if (nm.IndexOf("Market", StringComparison.OrdinalIgnoreCase) < 0) continue;
                    if (nm.IndexOf("UI", StringComparison.OrdinalIgnoreCase) >= 0) continue;
                    n++;
                    if (n <= 20)
                    {
                        var mc = tr.GetComponent<Collider>();
                        sb.Append("obj name='" + nm + "' pos=" + tr.position.ToString("F1")
                                  + " scale=" + tr.localScale.ToString("F1")
                                  + " collider=" + (mc != null ? mc.GetType().Name : "-") + "\n");
                    }
                }
                sb.Append("market objects=" + n);
                ar.ok = true;
                ar.note = sb.ToString();
            }
            catch (Exception e) { ar.ok = false; ar.note = "调试异常: " + e.Message; }
        }

        private void DebugSceneReal(ActionResult ar)
        {
            try
            {
                EnsureScene();
                var sb = new System.Text.StringBuilder();
                sb.Append("player=" + (PlayerPos().ToString("F1")) + " zone=" + ZoneToName(CurrentZone()) + "\n");
                var plots = UnityEngine.Object.FindObjectsOfType<LandPlot>();
                sb.Append("LandPlot count=" + plots.Length + "\n");
                foreach (var lp in plots)
                {
                    if (lp == null) continue;
                    LandPlotModel m = PlotModelOf(lp);
                    string type = m != null ? m.typeId.ToString() : "?";
                    sb.Append("  plot name='" + lp.name + "' type=" + type
                              + " pos=" + lp.transform.position.ToString("F1"));
                    var ui = lp.GetComponentInChildren<EmptyPlotUI>(true);
                    sb.Append(" emptyUI=" + (ui != null ? "YES" : "no"));
                    var lpui = lp.GetComponent<LandPlotUI>();
                    sb.Append(" landPlotUI=" + (lpui != null ? "YES" : "no"));
                    var lpl = lp.transform.parent != null ? lp.transform.parent.GetComponent<LandPlotLocation>() : null;
                    sb.Append(" location=" + (lpl != null ? "YES" : "no"));
                    sb.Append("\n");
                }
                var euis = UnityEngine.Object.FindObjectsOfType<EmptyPlotUI>();
                sb.Append("EmptyPlotUI count=" + euis.Length + "\n");
                foreach (var eui in euis)
                {
                    if (eui == null) continue;
                    LandPlotUI.PlotPurchaseItem item = GetPurchaseItem(eui, "corral");
                    string has = item != null ? ("cost=" + PurchaseCost(item)
                                                 + " prefab=" + (PlotPrefabOf(item) != null ? "YES" : "no")) : "noItem";
                    sb.Append("  eui name='" + eui.name + "' corral:" + has + "\n");
                }
                try
                {
                    var gc = GameContext.Instance;
                    if (gc != null && gc.UITemplates != null)
                    {
                        sb.Append("purchaseUIPrefab=" + (gc.UITemplates.purchaseUIPrefab != null ? "YES" : "null") + "\n");
                        var puis = UnityEngine.Object.FindObjectsOfType<PurchaseUI>();
                        sb.Append("PurchaseUI count=" + puis.Length + "\n");
                        if (gc.UITemplates.purchaseUIPrefab != null)
                        {
                            GameObject inst = UnityEngine.Object.Instantiate(gc.UITemplates.purchaseUIPrefab);
                            sb.Append("--- purchaseUIPrefab 实例化后组件 ---\n");
                            foreach (var comp in inst.GetComponentsInChildren<UnityEngine.Component>(true))
                            {
                                if (comp == null) continue;
                                sb.Append("  [" + comp.GetType().Name + "] name='" + comp.name + "'\n");
                                if (comp is EmptyPlotUI)
                                {
                                    var eui = (EmptyPlotUI)comp;
                                    foreach (var k in new[] { "corral", "coop", "garden" })
                                    {
                                        LandPlotUI.PlotPurchaseItem item = GetPurchaseItem(eui, k);
                                        sb.Append("    " + k + ": cost=" + (item != null ? PurchaseCost(item).ToString() : "?")
                                                  + " prefab=" + (item != null && PlotPrefabOf(item) != null ? PlotPrefabOf(item).name : "null") + "\n");
                                    }
                                }
                            }
                            UnityEngine.Object.Destroy(inst);
                        }
                    }
                    else sb.Append("GameContext/UITemplates 不可用\n");
                }
                catch (Exception e) { sb.Append("GameContext err: " + e.Message + "\n"); }
                ar.ok = true;
                ar.note = sb.ToString();
            }
            catch (Exception e)
            {
                ar.ok = false; ar.note = "debug_scene 异常: " + e.Message;
            }
        }

        private static GameObject PlotPrefabOf(object item)
        {
            try
            {
                FieldInfo f = typeof(LandPlotUI.PlotPurchaseItem).GetField("plotPrefab",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                return f != null ? f.GetValue(item) as GameObject : null;
            }
            catch (Exception) { return null; }
        }
    }
}
