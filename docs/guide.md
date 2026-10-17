# 史莱姆牧场自动游玩 —— 用户指南

## 一、这是什么
NEKO 连接插件后，默认**观望**，不操作游戏。按键如下：

- **V 键**：在观望和自主经营之间切换。Windows 上优先用系统键盘钩子，装了 `pynput` 时作兜底。中文输入法下也能收到 V。
  - **观望（默认）**：不按键、不走路、不主动说话。要猫娘看见画面，请自己打开 NEKO 的屏幕分享。
  - **自主经营**：每个操作先瞬移到对应地点再执行。一轮只做一件事。

自主经营一轮做这些事：

1. 进入经营时瞬移到结晶市场投入口前约 3.5 米。
2. 逐个瞬移到围栏前收结晶（2026-10-18 起不再收食物、不再投喂）：
   - 围栏装了**结晶收集器**：从收集器储存槽逐个取出——物品栏 +1、收集器 -1。
   - 围栏**没有**收集器：收集围栏范围内的地上结晶——物品栏 +1、地上对应消失。
3. 瞬移到结晶市场，按**当日市价**批量清仓：物品栏每减 1 个结晶，资金加对应单价，直到结晶清零。资金读数会一格一格往上涨。
4. 资金达到下一档费用的两倍，才建造或升级围栏、鸡圈、菜园。每种只建一次，再加一个升级件。先尝试点游戏里的购买菜单，点不到再由 mod 完成购买。人会先出现在对应地块前。

不跑图、不抓野生史莱姆。大约每 8 轮主动存档一次。

## 二、快速验收（不用打开游戏也能验证逻辑）
核心自带 **SIM 模拟模式**，用同一套决策代码跑一遍。

```bat
setup.bat
py -3 run_standalone.py --ticks 20 --interval 0 --backend sim
py -3 -m unittest discover -s tests
```

日志在 `%TEMP%\neko-slime-ranch-autopilot\runtime\outbox.jsonl`。插件重新启动时，上一轮日志会挪到 `outbox.prev.jsonl`，不会再念一遍。

## 三、安装到 NEKO
在仓库根目录双击 `install.bat`，或执行：

```bat
py -3 install.py
```

脚本会：

1. 把仓库复制到旁边的 `N.E.K.O\plugin\plugins\slime_ranch_autopilot`（目录名必须是下划线）。
2. 在那份副本里建 `.venv-core` 并安装 Pillow。
3. 把 `engine_python` 和找到的 `SlimeRancher.exe` 写入副本的 `plugin.toml`。
4. 用系统 `csc.exe` 编译 `SlimeBridge.dll`，覆盖到游戏的 `Mods\SlimeBridge.dll`。

然后：

1. 把 `Mods\SRMP.dll` 改名为 `SRMP.dll.disabled`。这个多人 mod 在单机会打断存档。加载器仍然是 UnityCoreMod，只停用 SRMP 本体。
2. 完全退出游戏再进存档。游戏开着时覆盖 DLL，下次启动可能挂起。
3. 浏览器打开 `http://127.0.0.1:8124/api/health`，看到 `ok` 才算接上。
4. 重启 NEKO，在面板上点「启动自动游玩」，再按 V。

Steam 和游戏不要用管理员身份运行。那样键鼠会被系统丢掉，瞬移却仍然有效。

打开 NEKO「用户插件」总开关不会自动拉起核心，也不会自动启动游戏。核心只在点「启动自动游玩」时启动。要顺带打开游戏，把 `plugin.toml` 里的 `auto_launch_game` 改成 `true`。

## 四、真实游玩（hybrid 模式）
推荐 `backend = "hybrid"`。mod 负责读状态、瞬移、原生吸取和发射、市场结算；转向和点菜单用真实键鼠。光标锁定时，游戏不读注入的鼠标按键，所以吸取和发射不走模拟点击。

改过 `bridge/SlimeBridge/*.cs` 后必须完全退出游戏，再跑 `install.bat` 或自行编译覆盖 `Mods\SlimeBridge.dll`。

协作者环境、接口和已知坑见 `docs/COLLABORATION.md`。
