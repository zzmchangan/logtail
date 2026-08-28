"""agent 模式端到端回归自测 (确定性夹具, 无需终端).

用法: PYTHONPATH=. python3 tests/selftest_agent.py
覆盖: dump 全分支(match/count/-C/ctx-same/trace/exclude/level/focus/
correlate/json/summary/since)、monitor、--diagnose、退出码、模块导入。
"""
import json
import subprocess
import sys
import os
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = tempfile.mkdtemp(prefix="lt_selftest_")
os.makedirs(FIX + "/scene"); os.makedirs(FIX + "/guild")
with open(FIX + "/scene/a.log", "w") as f:
    f.write("[2026-08-27 10:00:01.000000] player=100 enter scene\n"
            "[2026-08-27 10:00:02.000000] [Info][1] tick normal s=aaaa&c=1\n"
            "[2026-08-27 10:00:03.000000] [Error][2] player=100 crash in dungeon\n"
            "[2026-08-27 10:00:04.000000] [Warn][3] player=200 slow frame\n"
            "[2026-08-27 10:00:05.000000] heartbeat ping\n")
with open(FIX + "/guild/b.log", "w") as f:
    f.write("[2026-08-27 10:00:01.500000] roleId: 100 join guild\n"
            "[2026-08-27 10:00:02.500000] [Info][4] BroadcastTopicUpdate ok\n"
            "[2026-08-27 10:00:03.500000] [Error][5] Guid:100 guild save fail\n"
            "[2026-08-27 10:00:04.500000] [Debug][6] detail dump\n")
CFG = FIX + "/cfg.yaml"
with open(CFG, "w") as f:
    f.write("log_sources:\n"
            "  - name: scene\n    path: %s/scene/\n    pattern: \"*.log\"\n"
            "  - name: guild\n    path: %s/guild/\n    pattern: \"*.log\"\n"
            "blacklist: [\"heartbeat\"]\nkeywords: []\n"
            "correlation_keys:\n"
            "  - name: player\n    extract: [\"Guid[:=] *(\\\\d+)\", \"roleId[:=] *(\\\\d+)\", \"player[:=] *(\\\\d+)\"]\n" % (FIX, FIX))


def run(*args):
    r = subprocess.run([sys.executable, "-m", "logtail", *args],
                       capture_output=True, text=True, timeout=30,
                       cwd=ROOT, env={"PYTHONPATH": "."})
    return r.returncode, r.stdout, r.stderr


def monitor(*args):
    return subprocess.run(["timeout", "-s", "INT", "3", sys.executable, "-m", "logtail",
                           "--agent", "--mode", "monitor", "--lines", "50", *args],
                          capture_output=True, text=True, timeout=30,
                          cwd=ROOT, env={"PYTHONPATH": "."})


C = ["--agent", "--config", CFG, "--wait", "1", "--lines", "50"]
F = ["--since", "8760h"]
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <<< " + str(detail)[:300]))
    if not cond:
        fails.append(name)


rc, out, err = run(*C, *F)
lines = out.strip().splitlines()
check("全量=8行(黑名单滤heartbeat)", len(lines) == 8, lines)
check("heartbeat 已滤", "heartbeat" not in out, out)
check("全局时间有序", all(lines[i][:26] <= lines[i + 1][:26] for i in range(len(lines) - 1)), lines)
check("混源", "scene" in out and "guild" in out, out)

rc, out, err = run(*C, *F, "--match", "player=100")
check("match 'player=100' 字面=2", len(out.strip().splitlines()) == 2, out)

rc, out, err = run(*C, *F, "--correlate", "player=100")
check("correlate player=100=4(跨写法归一化)", len(out.strip().splitlines()) == 4, out)
check("correlate 跨源", "scene" in out and "guild" in out, out)

rc, out, err = run(*C, *F, "--correlate", "player=999", "--summary")
d = json.loads(err.strip().splitlines()[-1])
check("correlate 假值 matched=0", out.strip() == "" and d["correlate"]["matched"] == 0, (out, err))
check("correlate lines_with_key=5", d["correlate"]["lines_with_key"] == 5, d["correlate"])

rc, out, err = run(*C, *F, "--correlate", "notakey=100")
check("未知key回退字面=4", len(out.strip().splitlines()) == 4, out)

rc, out, err = run(*C, *F, "--json")
recs = [json.loads(l) for l in out.strip().splitlines()]
check("json 8条字段齐", len(recs) == 8 and all(set(r) == {"ts", "ts_seconds", "source", "level", "text", "seq"} for r in recs), len(recs))

rc, out, err = run(*C, *F, "--summary")
d = json.loads(err.strip().splitlines()[-1])
check("summary 契约", d["kind"] == "logtail.summary" and d["total_files"] == 2 and d["matched"] == 8 and "latest_ts" in d, d)

rc, out, err = run(*C, *F, "--match", "re:[invalid")
check("坏正则 exit=2 且无 traceback", rc == 2 and "Traceback" not in err, (rc, err[:200]))

rc, out, err = run(*C, *F, "--match", "crash", "-C", "1")
srcs = {l.split("] ")[1].split()[0] for l in out.strip().splitlines()}
check("-C1 带异源", "scene" in srcs and "guild" in srcs, srcs)
rc, out, err = run(*C, *F, "--match", "crash", "--ctx-same", "2")
srcs = {l.split("] ")[1].split()[0] for l in out.strip().splitlines()}
check("ctx-same 只同源", srcs == {"scene"}, srcs)
rc, out, err = run(*C, *F, "--focus", "guild")
check("focus 只 guild", "guild" in out and "scene" not in out, out)
rc, out, err = run(*C, *F, "--level", "ERROR", "--count")
check("level ERROR count=2", out.strip() == "2", out)
rc, out, err = run(*C, *F, "--trace", "guild")
check("trace guild=2(正文子串)", len(out.strip().splitlines()) == 2, out)
rc, out, err = run("--diagnose", "--config", CFG)
d = json.loads(out.strip())
check("diagnose 契约", d["kind"] == "logtail.diagnose" and d["total_files"] == 2, d)

r = monitor("--config", CFG, "--match", "player=100")
check("monitor match=2", len(r.stdout.strip().splitlines()) == 2, r.stdout)
check("monitor 无 traceback", "Traceback" not in r.stderr, r.stderr[:200])
r = monitor("--config", CFG, "--correlate", "player=100")
check("monitor correlate=4", len(r.stdout.strip().splitlines()) == 4, r.stdout)

r = subprocess.run([sys.executable, "-c",
                    "import logtail.tui, logtail.cli, logtail.agent, logtail.correlate, "
                    "logtail.config, logtail.reader, logtail.rules, logtail.timeline, "
                    "logtail.timeparse, logtail.levelparse, logtail.models; print('ok')"],
                   capture_output=True, text=True, timeout=15, cwd=ROOT,
                   env={"PYTHONPATH": "."})
check("全部模块可导入", "ok" in r.stdout, r.stderr)

print()
print("=" * 40)
print("全部通过" if not fails else "失败: " + str(fails))
sys.exit(1 if fails else 0)
