# Collaboration guide

Repository: https://github.com/Yangmao-123/slime-ranch-autopilot

This is the English copy of `docs/COLLABORATION.md`. The Chinese file stays the source of truth for the TODO list.

## What you can share

The git repository (source, docs, tests), a `.neko-plugin` package, a source-tree install into the NEKO data directory, or the docs alone.

Do not commit `plugin.meta.json`, a `plugin.toml` with a personal `game_exe` / `engine_python`, the NEKO data directory, or one-off screenshots.

## Environment

Windows 10/11, Slime Rancher 1 (Steam), UnityCoreMod, the .NET Framework `csc.exe` that ships with Windows, Python 3.11+ (3.13 is fine). Pillow is optional. NEKO is only required for the full panel loop; `run_standalone.py` runs the core without it.

Disable `Mods\SRMP.dll` (rename to `SRMP.dll.disabled`). Its OnDestroy patch throws in single player and breaks saves when this plugin destroys items.

## Contract

Mod and core talk on `127.0.0.1:8124`.

- `GET /api/health` returns `version` and `features`. Missing features must be reported, not ignored.
- `GET /api/status` returns money, facilities, plorts, area, player position.
- `POST /api/action` takes `{"action","kind"}`. Lists travel in `payload`.

Perception is read-only and camera-relative (`yaw` + `pitch`). Movement actions are `tp_market`, `tp_plot` (`corral@N` picks the Nth plot), `tp_item`, `slimes_home`. Native actions are `vac_grab`, `vac_launch`, `feed_show`. Market sales use `EconomyDirector`. `drone_setup` programs ranch drones. `save_now` calls `AutoSaveDirector.SaveAllNow`.

Change both `GameState.cs` and `core/bridge.py`, and register the new name in `features`.

## Runtime rules

Teleport sets the body facing only. The camera must be turned with the mouse using `AimForward()`. Walking, looking, and menu clicks use real input. Vacuum and throw do not: the locked cursor ignores injected mouse buttons, so those go through the game API. Dismiss the pause menu before acting, and do not announce that dismissal. The first upgrade archives old `outbox.jsonl` as `outbox.prev.jsonl`; subsequent starts restore SDK-accepted positions from `outbox.cursor.json` and retry pending messages. Local acceptance is not playback confirmation.

## Deploy

Quit the game before replacing `Mods\SlimeBridge.dll`. Copy the Python tree into the NEKO plugin directory from `storage_policy.json`, then regenerate `plugin.meta.json` with `derive_plugin_metadata()`. Do not run Steam or the game as administrator, or `SendInput` is dropped while teleports still work.

## Checks before a PR

`python -m unittest discover -s tests` and `python -m py_compile`. Say whether a C# change was tried in-game. PRs should say what changed, why, and how to verify. Merge with squash.
