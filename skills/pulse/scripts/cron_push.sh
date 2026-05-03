#!/bin/bash
# Pulse Cron Push — 每日简报定时推送
# 写入触发文件，由 aurora-dashboard cron executor 检测并执行 Pulse
#
# Cron 配置（10am 和 6pm）：
#   0 10 * * * /bin/bash /home/cosh/matrix-skills/skills/pulse/scripts/cron_push.sh
#   0 18 * * * /bin/bash /home/cosh/matrix-skills/skills/pulse/scripts/cron_push.sh

TRIGGER_FILE="$HOME/.openclaw/cron/pulse_trigger"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# 判断是早间还是晚间
HOUR=$(date '+%H')
if [ "$HOUR" -lt 14 ]; then
  SESSION_TYPE="morning"
else
  SESSION_TYPE="evening"
fi

# 写入触发文件
cat > "$TRIGGER_FILE" << EOF
{
  "triggered_at": "$TIMESTAMP",
  "session_type": "$SESSION_TYPE",
  "include_matrix_summary": true
}
EOF

echo "[$TIMESTAMP] Pulse trigger written (session: $SESSION_TYPE)" >> "$HOME/.openclaw/cron/pulse-cron.log"
