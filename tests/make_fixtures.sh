#!/usr/bin/env bash
# 生成验收用日志夹具: 创建 gateway / logic / scene 三种源目录,
# 写入真实日志文件, 供运行时产生实时流与轮转。
set -euo pipefail

BASE="${1:-/tmp/lt_logs}"
mkdir -p "$BASE/gateway" "$BASE/logic" "$BASE/scene"

# gateway: 普通日志, 含可选黑名单词 heartbeat/DEBUG
cat > "$BASE/gateway/gw.log" <<'EOF'
[10:00:00.010] gateway boot ok
[10:00:00.020] gateway heartbeat tick
[10:00:00.030] gateway C2S_UseItem item_id=5001
EOF

# logic: 含高亮目标 item_id / timeout / player
cat > "$BASE/logic/logic.log" <<'EOF'
[10:00:00.011] logic handle UseItem player=10086
[10:00:00.021] logic DEBUG payload
[10:00:00.031] logic timeout on player=10086 op=UseItem
EOF

# scene: 符合 scene_*.log 模式
cat > "$BASE/scene/scene_1.log" <<'EOF'
[10:00:00.012] scene enter combat monster_id=101
[10:00:00.022] scene keepalive
[10:00:00.032] scene hp player=9999 damage=500
EOF

echo "fixtures written under $BASE"
echo "- gateway/gw.log"
echo "- logic/logic.log"
echo "- scene/scene_1.log"
