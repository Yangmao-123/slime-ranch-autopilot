# 协作开发指南 —— 可分发内容清单

> 面向加入本项目的协作者：什么可以拿走、怎么搭建环境、改哪里、怎么验证。
> 仓库地址：https://github.com/Yangmao-123/slime-ranch-autopilot

---

## TODO（规划中 / 待认领）

按优先级排序，认领后在条目后加 `@你的名字`。

### 事务性

- [x] **推送积压提交**：清单随本次提交进入 `main`。作者机器上若还留着未推送的 `8b63b71`，先 `git pull` 再推，避免和这次提交分叉
- [x] **清理远程旧分支**：`feat/real-ui-ranch-loop`（PR #2 已合并）

### P0 —— 下一个大特性

- [x] **无人机搬运网络**（代码已接上，未实机验证）
  - `drone_setup`：已有无人机时调用 `SetPrograms`（围栏结晶→市场，第二槽菜园蔬菜→喂食器），并给基站补水。没有无人机才尝试放置
  - 自主经营：布置成功后日常搬运交给无人机，人只在建造/升级时出场；布置失败就继续自己瞬移收菜
  - 还差一次活体确认：7Zee 未解锁时定义是否为 null、程序槽字段是否写进了正在飞的那台

### P1 —— 稳定性打磨

- [x] **自动保存常态化**：每 8 轮经营调用 `save_now`
- [x] **outbox 重放修复**：插件启动时把旧 `outbox.jsonl` 挪成 `outbox.prev.jsonl`
- [x] **视觉层修复**：`capture_window` 改为抓客户区，和点击用同一套矩形
- [x] **建造菜单设施切换**：`menu_texts` 带上 sx/sy，购买前按文字点对应设施/升级件。点击链路未实机走查

### P2 —— 体验改进

- [x] **升级页多升级件选择**：升级前先点结晶收集器 / 喂食器 / 喷灌对应的文字项，再点购买。未实机验证
- [x] **多围栏/多史莱姆种类支持**：投喂按 `corral@N` 轮流换围栏，食物取该栏史莱姆食性的交集（最爱优先，否则蔬菜/水果/肉的常备食物）。未实机验证
- [x] **`feed_show` 的 Feeder 反射写入**：note 里回报实际调用的 `MaybeAddToSlot` 参数类型；调用异常会记失败
- [x] **诊断脚本清理**：`diag_*.py`、`_probe_*.py` 移入 `tools/`
- [x] **英文文档**：`docs/COLLABORATION.en.md`、`docs/guide.en.md`
- [x] **CI**：`.github/workflows/tests.yml` 跑 `unittest discover`

**建议顺序**：先清事务性项 → P1 小改动大收益 → P0 无人机（下一个大块）。


### 已知未修（记录在案暂不处理）

- `GetSystemMetrics` 多显示器场景未验证（当前单屏 1280×800 实测通过）
- `_walk_to_point` 的走路路径仍保留（瞬移策略为主），真实走路仅在 `approach_market` 等处兜底

---

## 一、仓库内容总览（全部可分发）

```
slime-ranch-autopilot/
├── plugin.toml              # NEKO 插件清单（id/version/配置节/UI 面板声明）
├── __init__.py              # NEKO 插件入口（薄壳：拉核心子进程、同步配置、消息转发、Hosted UI）
├── core/                    # 自动化核心（独立 Python 子进程，也可脱离 NEKO 单跑）
│   ├── main_controller.py   # 大脑：观望/自主经营状态机、牧场经营循环
│   ├── real.py              # 执行层：瞄准闭环、原生吸取/发射、购买流程、暂停看门狗
│   ├── bridge.py            # 感知层：SlimeBridge HTTP 客户端（scan_*/vac_*/tp_*）
│   ├── ranch.py             # 决策：建造/升级优先级与 2 倍资金闸
│   ├── game.py              # live 后端：键鼠动作、指令执行器
│   ├── sim.py               # SIM 后端（纯模拟，无需游戏即可验收逻辑）
│   ├── winio.py             # Windows 键鼠注入（SendInput）+ 窗口管理
│   ├── vision.py            # 截图/帧差（视觉验证，DPI 环境下有已知局限）
│   ├── voice.py             # V 键全局热键（win32 钩子优先，pynput 兜底）
│   └── settings.py          # 配置加载（config.json 默认值）
├── bridge/SlimeBridge/      # 游戏内 C# mod（UnityCoreMod 加载）
│   ├── GameState.cs         # 感知/动作实现（scan/vac/tp/econ/debug 全套）
│   ├── HttpServer.cs        # 127.0.0.1:8124 HTTP 服务 + JSON 序列化
│   ├── Plugin.cs            # mod 入口 + 中文 overlay 渲染
│   ├── build.rsp            # csc 编译参数（引用游戏 Managed 下的 dll）
│   └── BUILD.md             # mod 编译踩坑清单
├── ui/panel.tsx             # NEKO 面板前端（Hosted UI）
├── i18n/zh.json             # 中文文案
├── docs/
│   ├── guide.md             # 用户指南（安装/玩法/验收）
│   └── 开发文档.md           # 开发文档（架构与决策）
├── tests/                   # 单元测试（无需游戏）
│   ├── test_smoke.py        # 决策/清单/SIM 冒烟（13 项）
│   ├── test_bridge_protocol.py  # mod HTTP 协议一致性（内置假服务器）
│   └── fake_bridge_server.py
├── install.py / install.bat # 一键安装（复制+venv+编译 mod）
├── setup.bat                # 依赖安装（.venv-core + Pillow）
├── run_standalone.py        # 脱离 NEKO 独立跑核心（SIM/live）
├── plugin_check.py          # plugin.toml 契约自检
├── DESIGN.md / README.md    # 设计文档 / 项目说明
└── diag_*.py, _probe_*.py   # 诊断脚本，已移到 tools/
```

## 二、分发渠道与形态

| 形态 | 内容 | 适用对象 |
| --- | --- | --- |
| **Git 仓库** | 全部源码 + 文档 + 测试 | 所有协作者（主开发流） |
| **`.neko-plugin` 包** | `neko-plugin build` 产物（含 plugin.meta.json） | 测试用户（NEKO 插件管理器导入） |
| **源码目录直装** | 仓库复制到 NEKO 数据目录（见下文部署路径） | 本机联调 |
| **文档单独分发** | `docs/guide.md`、`docs/开发文档.md`、本文件 | 评估/上手 |

## 三、协作者环境要求

| 依赖 | 说明 |
| --- | --- |
| Windows 10/11 | mod 与键鼠注入仅支持 Windows |
| 《史莱姆牧场》一代（Steam） | 需本体；联调需进存档 |
| UnityCoreMod 加载器 | 游戏目录 `Mods\` 可被加载（SRMP 安装器提供，**注意：SRMP.dll 本身需禁用**，见第七节） |
| .NET Framework 4.x csc | `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`（系统自带） |
| Python 3.11+（含 3.13） | 核心 + 测试；`Pillow` 为可选依赖（视觉验证用） |
| NEKO 桌面应用 | 端到端联调需要；纯 mod/核心开发可不装（`run_standalone.py`） |

## 四、模块与接口契约（改动前必读）

### 4.1 mod ↔ 核心的 HTTP 契约（`bridge.py` ↔ `HttpServer.cs`）

- 端口固定 `127.0.0.1:8124`（与 `plugin.toml` `[slime_ranch] bridge_port` 一致）
- `GET /api/health` → `{"ok", "version", "features":[...]}` —— **能力握手**：核心据此判断 mod 版本（缺 `ui_buttons` 等能力时明确提示更新而非静默失败）
- `GET /api/status` → 资金/设施/结晶/区域/玩家坐标
- `POST /api/action {"action":..., "kind":...}` → 标量字段 + `payload`(JSON 字符串，承载列表)
- 动作分组：
  - **感知（只读）**：`target` `scan_corral` `scan_market` `scan_food` `scan_plort` `scan_plot` `scan_facilities` `ui_buttons` `ui_hover`（均带相机相对 yaw + `pitch` 字段）
  - **移动/放置**：`tp_market` `tp_plot` `tp_item` `slimes_home`
  - **原生交互**：`vac_grab`（TryConsume+真实物品栏）`vac_launch`（Expel/经济结算）`feed_show`
  - **经济**：市场结算走 `EconomyDirector.GetCurrValue + RegisterSold`（真实市价+饱和度）
  - **诊断**：`probe` `debug_market` `debug_scene` `debug_ui` `debug_types` `debug_cams` `debug_members` `ammo_probe` `save_now`
- **改协议必须同时改两端**（`GameState.cs` + `bridge.py`），并在 `/api/health` 的 `features` 里登记新能力名

### 4.2 核心执行层的关键设计（`core/real.py`）

- **瞄准闭环**：yaw/pitch 均为**相机相对**（`AimForward()` 动态选相机——`Camera.main` 在 SR1 不随视角旋转）；瞬移只设身体朝向，镜头必须靠鼠标闭环真实转
- **输入分工**：走路/转视角/开菜单点按钮走**真实键鼠**（已验证可用）；吸取/发射走**游戏原生路径**（SR1 光标锁定态对注入鼠标按键无响应——实测系统级按键已按下但游戏不读）
- **暂停看门狗**：SR1 失焦自动暂停且驻留菜单吞输入；NEKO 说话会抢焦点触发——动作前 `_dismiss_if_paused`，恢复**不发消息**（否则自激励死循环）
- **物品栏集成**：吸取 `MaybeAddToSlot(+1)`、发射 `DecrementSelectedAmmo(-1)`、售出 `Decrement`——HUD 真实增减；mod 内 `_bag` 台账仅兜底

### 4.3 插件壳（`__init__.py`）

- `startup()` **不自动拉核心**（用户点「启动自动游玩」才 spawn）
- 面板按钮依赖 `plugin.meta.json`——**改动仓库代码后必须重新生成**（见第六节），否则按钮 404

## 五、构建与部署路径

| 目标 | 产物 | 路径 |
| --- | --- | --- |
| 游戏内 mod | `bridge/SlimeBridge/bin/Release/SlimeBridge.dll` | `<游戏目录>\Mods\SlimeBridge.dll`（**游戏必须完全退出**才能覆盖） |
| NEKO 插件（源码直装） | `__init__.py` + `core/*.py` + `plugin.toml` 等 | `D:\N.E.K.O\.neko-plugin-installations\plugins\slime_ranch_autopilot\`（NEKO 数据根下，按 `storage_policy.json` 的 `selected_root`） |
| 插件元数据 | `plugin.meta.json`（按钮/入口注册表） | 上述插件目录内，**由 NEKO 仓库 venv 的 `derive_plugin_metadata()` 生成**，不手写 |
| SlimeBridge C# 桥 | `csc @build.rsp`（或 install.py 第4步） | 编译到游戏 `Mods\` |

## 六、标准开发循环

```bash
# 1) 拉仓库
git clone https://github.com/Yangmao-123/slime-ranch-autopilot
cd slime-ranch-autopilot

# 2) 装依赖（可选：SIM 验收只需标准库）
setup.bat

# 3) 改代码 → 跑测试（无需游戏）
python -m unittest discover -s tests     # 13 项全过
python -m py_compile core/*.py __init__.py

# 4) SIM 快速验收（无需游戏）
python run_standalone.py --ticks 20 --interval 0 --backend sim

# 5) 编译 mod（改了 C# 时；游戏须退出）
python install.py   # 或 csc @bridge/SlimeBridge/build.rsp 后手动复制

# 6) 部署 Python 侧到 NEKO 数据目录 + 重新生成 plugin.meta.json + 刷新注册表
#    （见 docs/guide.md 4.1 节；NEKO 侧用 derive_plugin_metadata() 生成元数据，
#     再 POST /plugin/slime_ranch_autopilot/refresh 或面板「重载」）

# 7) 实机验收：进存档 → 面板「启动自动游玩」→ V 切自主经营
#    监控：runtime/outbox.jsonl（行为汇报）+ http://127.0.0.1:8124/api/status
```

## 七、已知坑（新协作者最容易踩的）

1. **SRMP 冲突**：`Mods\SRMP.dll`（多人 mod）的 OnDestroy 补丁在单机抛 NRE，与本插件高频物品销毁共振成异常风暴 → **打断存档写入**。已禁用（改名 `.disabled`）。环境里若有 SRMP 需同样处理。
2. **管理员运行**：Steam/游戏以管理员运行时，插件的 SendInput 会被 UIPI 静默丢弃（走路/视角全失效，但瞬移照常——极具迷惑性）。排查：`GetTokenInformation(TokenElevation)`。
3. **DPI 虚拟化**：200% 缩放下窗口客户区 960×720 vs 渲染 1920×1440；`vision.py` 抓图错位（帧差判定不可靠，已改用 mod 权威复核）；UI 点击换算用客户区矩形（`winio.get_client_rect`）。
4. **运行中覆盖 DLL**：游戏开着时覆盖 `Mods\SlimeBridge.dll` 会导致下次启动挂起。**永远先退出游戏再复制**。
5. **`Camera.main` 不转**：SR1 里它不随视角旋转，瞄准一律用 `AimForward()`（玩家层级下启用的相机）。
6. **中文输入法**：pynput 热键收不到裸 'v'——V 键走 win32 低级钩子（vkCode 判定）。

## 八、贡献流程

1. 从 `main` 拉分支：`feat/<名字>` 或 `fix/<名字>`
2. 提交前：`python -m unittest discover -s tests` 全过 + `py_compile` 无错
3. **涉及 C# 的改动**：在本地编译通过并在说明里注明"已实机验证"或"未实机验证"
4. **涉及协议的改动**：两端（mod+核心）同步改、`features` 登记、`tests/test_bridge_protocol.py` 补用例
5. PR 描述必填：改了什么 / 为什么 / 怎么验证的（实机步骤）
6. 合并走 squash，保持 main 可一键部署

## 九、不可分发 / 不入库的内容

- `D:\N.E.K.O\` 下的一切（部署副本、用户数据、存档）
- `plugin.meta.json`（构建产物，各环境自动生成）
- 个人路径配置（`game_exe`/`engine_python` 填好的 plugin.toml 副本）
- `_patch_*.py` 等一次性补丁脚本、`_probe_shot.png` 等截图

---

*本文件随仓库演进，改动架构/协议时同步更新第三、四、七节。*
