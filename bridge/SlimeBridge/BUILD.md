# SlimeBridge 编译

把 `bridge/SlimeBridge` 编译成 `SlimeBridge.dll`，放到游戏的 `Mods\`。加载器是 UnityCoreMod。不要打成 `.umfmod`，也不要安装 UMF。

## 前置

- Windows 10/11
- 《史莱姆牧场》一代（Steam）
- 系统自带的 `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`

## 编译并安装

在仓库根目录：

```bat
py -3 install.py
```

脚本用游戏 `SlimeRancher_Data\Managed` 里的 dll 做引用，编译 `GameState.cs`、`Plugin.cs`、`HttpServer.cs`，再复制到 `<游戏目录>\Mods\SlimeBridge.dll`。

只编译、不复制插件时，也可以在 `bridge/SlimeBridge` 里执行 `csc @build.rsp`，然后自己把 dll 拷进 `Mods\`。

## 部署前

1. 完全退出游戏。游戏开着时覆盖 DLL，下次启动可能挂起。
2. 把 `Mods\SRMP.dll` 改名为 `SRMP.dll.disabled`。它的 OnDestroy 补丁在单机会打断存档。UnityCoreMod 本身留着。
3. 进存档后打开 `http://127.0.0.1:8124/api/health`，看到 `ok` 和 `features` 才算接上。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 找不到 `csc.exe` | 使用上面的 .NET Framework 64 位编译器路径 |
| 缺少 `Assembly-CSharp.dll` 等 | 引用必须来自这台机器的游戏 `Managed` 目录 |
| 健康检查没有新动作 | 游戏还在用旧 dll。退出游戏后重新复制 |
| 键鼠没反应，瞬移正常 | Steam 或游戏用管理员运行了，关掉管理员身份 |
| 存档写不进去 | 确认 `SRMP.dll` 已改名停用 |
