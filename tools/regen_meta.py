# -*- coding: utf -*-
"""重建 NEKO 安装目录里的 plugin.meta.json（改完插件代码后必须执行）。

用法（在本仓库根目录）：
    py -3 tools/regen_meta.py            # 重建并重载插件
    py -3 tools/regen_meta.py --no-reload

背景：NEKO 的 /plugin/<id>/refresh 与 /reload 都不会重推 meta；
运行时校验 source_bytes 与目录实际字节数，不匹配就回退清单——
面板上的新按钮/新输入框会消失。必须用 NEKO 仓库自带的
write_packaged_metadata() 重新推导（它会隔离导入插件入口并算指纹），
然后 reload 让 NEKO 重新注册。
"""
import argparse
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# NEKO 本体仓库与本插件仓库是邻居（0NEKO-slime-ranch-autopilot/NEKO）
NEKO_ROOT = REPO.parent / "NEKO"
NEKO_PY = NEKO_ROOT / ".venv" / "Scripts" / "python.exe"
INSTALL = Path(r"D:\N.E.K.O\.neko-plugin-installations\plugins\slime_ranch_autopilot")
PLUGIN_ID = "slime_ranch_autopilot"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-reload", action="store_true", help="只重建 meta，不重载插件")
    args = ap.parse_args()

    if not NEKO_PY.exists():
        print("[ERR] 找不到 NEKO venv:", NEKO_PY)
        return 1
    if not INSTALL.exists():
        print("[ERR] 找不到安装目录:", INSTALL)
        return 1

    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "from pathlib import Path; "
        "from plugin.neko_plugin_cli.core.metadata_probe import write_packaged_metadata; "
        "p = write_packaged_metadata(source_dir=Path(r'%s'), target_dir=Path(r'%s')); "
        "print('meta written:', p)" % (NEKO_ROOT, INSTALL, INSTALL)
    )
    r = subprocess.run([str(NEKO_PY), "-c", code], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    print(out[-400:])
    if r.returncode != 0 or "meta written" not in r.stdout:
        print("[ERR] meta 推导失败")
        return 1

    if args.no_reload:
        return 0
    # 从 netstat 里找 NEKO 插件服务端口（有 /plugin/{id}/reload 路由的那个）
    import re
    ports = []
    netstat = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    for line in netstat.splitlines():
        m = re.search(r"127\.0\.0\.1:(\d+)\s+.*LISTENING", line)
        if m:
            ports.append(int(m.group(1)))
    for port in sorted(set(ports), reverse=True):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/plugin/{PLUGIN_ID}/reload",
                data=b"{}", method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", "replace")
            if '"success":true' in body.replace(" ", ""):
                print(f"[OK] 插件已重载（端口 {port}）")
                return 0
        except Exception:
            continue
    print("[WARN] 没找到可用的 reload 端口，请手动重载插件或重启 NEKO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
