#!/usr/bin/env bash
# logtail 全量测试入口: 单元 + 集成 + 模糊 + 冒烟 + agent 端到端回归.
# 用法: bash tests/run_all.sh   (或 PYTHONPATH=. python3 tests/run_all.py)
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=.

FAIL=0
step() {
  echo
  echo "==================================================================="
  echo "== $1"
  echo "==================================================================="
}

run() {  # run <名称> <命令...>
  local name="$1"; shift
  if "$@"; then
    echo "[PASS] $name"
  else
    echo "[FAIL] $name"
    FAIL=1
  fi
}

step "单元测试 (tests/unit)"
run "unit" python3 -m unittest discover -s tests/unit

step "集成测试 (tests/integration) — CLI 子进程端到端"
run "integration" python3 -m unittest discover -s tests/integration

step "模糊测试 (tests/fuzz) — 固定种子可复现"
run "fuzz" python3 -m unittest discover -s tests/fuzz

step "模块冒烟 (tests/smoke_test.py)"
run "smoke" python3 tests/smoke_test.py

step "Agent 端到端回归 (tests/selftest_agent.py)"
run "selftest" python3 tests/selftest_agent.py

echo
echo "==================================================================="
if [ "$FAIL" -eq 0 ]; then
  echo "全部测试通过 ✅"
else
  echo "存在失败 ❌"
fi
exit $FAIL
