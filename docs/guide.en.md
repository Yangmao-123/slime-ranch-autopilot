# Slime Rancher autopilot — user guide

NEKO starts in watch mode and does not touch the game. Press **V** to enter ranch mode. Press V again to go back to watching.

With SlimeBridge connected, watch mode scans a 30-meter radius around the player immediately and then every 8 seconds. It reports the initial surroundings and changes to species, counts, food, plorts, or area; unchanged surroundings stay quiet. Gold, Lucky, and Quicksilver Slimes receive reminders at most once per species every 60 seconds. Screen sharing is not required for this scan.

Ranch mode, one action per cycle:

1. Teleport to the plort market.
2. If a placed drone accepts a program, it hauls plorts to the market, and vegetables to the feeder when it has a second slot. The player then only shows up to build or upgrade.
3. Otherwise teleport beside food and vacuum it, feed each corral the food its slimes eat, then collect plorts and sell them at the market.
4. Build or upgrade a corral, coop, or garden only when funds are at least twice the next cost. Each building is built once and upgraded once.

The game must be fully quit before `SlimeBridge.dll` is replaced. Rename `Mods\SRMP.dll` to `SRMP.dll.disabled` or saves can fail. Do not run the game as administrator.

Install from the repo root:

```bat
py -3 install.py
```

Then start the game, load a save, and open `http://127.0.0.1:8124/api/health`. Restart NEKO and press "start autopilot" on the panel.

Without the game:

```bat
py -3 run_standalone.py --ticks 20 --interval 0 --backend sim
py -3 -m unittest discover -s tests
```

Logs: `%TEMP%\neko-slime-ranch-autopilot\runtime\outbox.jsonl`. The first upgrade archives old logs as `outbox.prev.jsonl`. Afterwards, `outbox.cursor.json` records SDK-accepted submissions; rejected messages retry every second and survive plugin restarts. SDK acceptance does not confirm model generation or audio playback. An interruption between acceptance and checkpointing can cause one repeated submission.
