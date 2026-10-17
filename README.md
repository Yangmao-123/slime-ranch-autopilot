# 史莱姆牧场自动游玩 (NEKO 插件)

让 NEKO 连接《史莱姆牧场》一代。默认是观望：不操作游戏，屏幕分享由玩家自己开。按 **V 键** 进入自主经营。每个操作先瞬移到对应地点，再收食物、按围栏里的史莱姆食性投喂、收结晶卖掉。资金达到下一档费用的两倍，才建造或升级围栏、鸡圈、菜园。每种建筑只建一次，再加一个升级件。牧场上已有无人机且程序写成功时，日常搬运交给无人机。

安装、玩法和常见问题见 `docs/guide.md`。协作者环境、接口和已知坑见 `docs/COLLABORATION.md`。

## 功能
| 需求 | 实现位置 |
| --- | --- |
| 观望 / 自主经营，V 键切换 | `core/voice.py` + `core/main_controller.py` |
| 瞬移到市场、地块、食物或结晶旁 | `GameState.cs` 的 `tp_market` / `tp_plot` / `tp_item` |
| 原生吸取、按食性发射、市场结算 | `vac_grab` / `vac_launch` / `feed_show` |
| 给已有无人机写搬运程序 | `drone_setup`（`DroneGadget.SetPrograms`） |
| 建造/升级，资金达到费用两倍才动手 | `core/ranch.py` + `core/main_controller.py` |
| 新建设施时汇报 | `core/comms.py` `report_discovery` |

## 结构
```
slime_ranch_autopilot/
├── plugin.toml            # NEKO 插件清单
├── __init__.py            # NEKO 插件入口
├── install.py             # 复制插件、建虚拟环境、编译 mod
├── core/                  # 自动化核心（可独立进程）
├── ui/panel.tsx           # 控制台
├── bridge/SlimeBridge/    # 游戏内 mod，编译后放到 Mods\SlimeBridge.dll
├── docs/                  # 用户指南、开发文档、协作指南
├── tools/                 # 诊断脚本
└── tests/
```

## 在本机验证（无需游戏）
```bat
py -3 run_standalone.py --ticks 20 --interval 0 --backend sim
py -3 plugin_check.py
py -3 -m unittest discover -s tests
```

## 游戏内 mod

NEKO 通过 `127.0.0.1:8124` 连接游戏里的 SlimeBridge。加载器是 UnityCoreMod：把 `SlimeBridge.dll` 放到 `<游戏目录>\Mods\`。不要打包成 `.umfmod`。

```bat
py -3 install.py
```

覆盖 DLL 前先完全退出游戏。把 `Mods\SRMP.dll` 改名为 `SRMP.dll.disabled`。编译细节见 `bridge/SlimeBridge/BUILD.md`。

`plugin.toml`：

```toml
[slime_ranch]
backend = "hybrid"
bridge_host = "127.0.0.1"
bridge_port = 8124
auto_launch_game = false
```

操作前请备份存档。本项目不改动 NEKO 平台代码。
