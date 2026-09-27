"""宿主工具桩：给 ``scripts/ops/host_maintenance_install.sh`` 的假根目录用例用（#884 / #891 宿主侧，Trace #898 W4）。

与 ``fake_db_switch.py`` 的 bash 桩分开：本脚本要读 ``show`` 的十来个属性并「等下一整分轮」，桩需要模拟
巡检单元的轮次。桩是一个 Python 小程序：

- 单元文件与 drop-in 从 ``FAKE_UNIT_DIR`` 实读，``FragmentPath`` / ``DropInPaths`` / ``ExecStart`` / ``User`` /
  ``TimeoutStartUSec`` 按 systemd 的叠加规则现算（空 ``ExecStart=`` 清空、非空覆盖）；
- 轮次状态在 ``FAKE_STATE/units.json``：每读一次 ``InvocationID`` 计一次轮询，满 ``FAKE_ROUND_AFTER``
  （缺省 2）次就「跑完一轮」——换新 ``InvocationID``，结果取 ``FAKE_STATE/rounds.txt`` 第一行
  （``success 0`` / ``exit-code 1``，读后删去；文件空或不存在按成功）；``FAKE_STATE/no_rounds`` 存在则永不出新轮；
- 定时器是否 active 以 ``FAKE_STATE/<单元>.active`` 为准；每次调用逐行追加到 ``FAKE_STATE/calls.log``。

遇到没模拟的调用形状一律非零退出，让用例红在明处。
"""

from __future__ import annotations

import sys
from pathlib import Path

FAKE_SYSTEMCTL = r'''#!__PYTHON__
import json, os, sys
from pathlib import Path

st = Path(os.environ["FAKE_STATE"])
ud = Path(os.environ["FAKE_UNIT_DIR"])
args = sys.argv[1:]
with (st / "calls.log").open("a", encoding="utf-8") as log:
    log.write("systemctl " + " ".join(args) + "\n")
MONITOR = "lingxi-host-monitor.service"


def files(unit):
    frag = ud / unit
    drops = sorted((ud / (unit + ".d")).glob("*.conf")) if (ud / (unit + ".d")).is_dir() else []
    return ([frag] if frag.is_file() else []), drops


def last(unit, key):
    frag, drops = files(unit)
    val = ""
    for f in frag + drops:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                val = line[len(key) + 1 :]
    return val


def load():
    p = st / "units.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def unit_state(units, unit):
    return units.setdefault(unit, {"inv": "inv-0", "result": "success", "status": 0, "active": "inactive", "polls": 0, "n": 0})


def new_round(u):
    u["n"] += 1
    u["inv"] = "inv-%d" % u["n"]
    u["polls"] = 0
    rounds = st / "rounds.txt"
    lines = rounds.read_text(encoding="utf-8").splitlines() if rounds.exists() else []
    outcome = lines.pop(0) if lines else "success 0"
    if rounds.exists():
        rounds.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    result, status = outcome.split()
    u["result"], u["status"] = result, int(status)
    u["active"] = "inactive" if result == "success" else "failed"


cmd = args[0] if args else ""
rest = args[1:]
if cmd == "daemon-reload":
    sys.exit(0)
if cmd == "is-active":
    active = (st / (rest[0] + ".active")).exists()
    print("active" if active else "inactive")
    sys.exit(0 if active else 3)
if cmd == "cat":
    frag, drops = files(rest[0])
    if not frag:
        sys.exit(1)
    for f in frag + drops:
        print("# " + str(f))
        print(f.read_text(encoding="utf-8"))
    sys.exit(0)
if cmd != "show":
    print("fake systemctl：未模拟的调用 " + cmd, file=sys.stderr)
    sys.exit(64)

props, value, unit, i = [], False, "", 0
while i < len(rest):
    if rest[i] == "-p":
        props += rest[i + 1].split(","); i += 2
    elif rest[i] == "--value":
        value = True; i += 1
    else:
        unit = rest[i]; i += 1
units = load()
u = unit_state(units, unit)
frag, drops = files(unit)
out = []
for p in props:
    if p == "FragmentPath":
        v = str(frag[0]) if frag else ""
    elif p == "DropInPaths":
        v = " ".join(str(d) for d in drops)
    elif p == "ExecStart":
        cmdline = ""
        for f in frag + drops:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.startswith("ExecStart="):
                    cmdline = line[len("ExecStart="):].lstrip("-@:+!")
        v = ("{ path=%s ; argv[]=%s ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
             % (cmdline.split(" ")[0], cmdline)) if cmdline else ""
    elif p == "User":
        v = last(unit, "User")
    elif p == "TimeoutStartUSec":
        t = last(unit, "TimeoutStartSec")
        v = (t + "s") if t else "1min 30s"
    elif p == "InvocationID":
        if unit == MONITOR and not (st / "no_rounds").exists():
            u["polls"] += 1
            if u["polls"] >= int(os.environ.get("FAKE_ROUND_AFTER", "2")):
                new_round(u)
        v = u["inv"]
    elif p == "ActiveState":
        v = u["active"]
    elif p == "Result":
        v = u["result"]
    elif p == "ExecMainStatus":
        v = str(u["status"])
    elif p == "ExecMainStartTimestamp":
        v = "Sun 2026-09-27 00:%02d:00 UTC" % u["n"]
    else:
        v = ""
    out.append(v if value else p + "=" + v)
(st / "units.json").write_text(json.dumps(units), encoding="utf-8")
print("\n".join(out))
'''

# 注入点背后的「解释器」：-c 时打印 FAKE_PY_VERSION 文件里的版本（缺省 3.12.3）；
# 其余调用（候选脚本 --help 的导入自检）看 FAKE_STATE/py_help_fail，存在即非零退出，模拟 09-20 那种导入失败。
FAKE_PYTHON = r"""#!/usr/bin/env bash
st="${FAKE_STATE:?}"
printf 'python %s\n' "$*" >> "$st/calls.log"
if [[ "${1:-}" == -c ]]; then cat "$st/py_version" 2>/dev/null || echo 3.12.3; exit 0; fi
[[ -f "$st/py_help_fail" ]] && { echo "ImportError: cannot import name 'UTC'" >&2; exit 1; }
exit 0
"""


def write_systemctl(path: Path) -> Path:
    """写 systemctl 桩（shebang 指向当前解释器），返回路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_SYSTEMCTL.replace("__PYTHON__", sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


def write_python(path: Path) -> Path:
    """写注入点背后的假解释器，返回路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_PYTHON, encoding="utf-8")
    path.chmod(0o755)
    return path
