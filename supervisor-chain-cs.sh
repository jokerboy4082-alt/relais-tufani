#!/bin/bash
# Superviseur chaine codespace : opencode (49374) + adaptateur (8770)
# relance ce qui est mort, ne touche a rien qui est sain, un seul superviseur a la fois
LOG="$HOME/opencode-chain/logs/supervisor.log"
mkdir -p "$HOME/opencode-chain/logs"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

tcp_ok() {
  (exec 3<>/dev/tcp/127.0.0.1/"$1") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
  return 1
}

# anti-doublon : si un autre superviseur tourne, on sort
if pgrep -f "supervisor-chain-cs.sh" | grep -qv "^$$\$"; then
  ME=$(basename "$0")
  for pid in $(pgrep -f "$ME"); do
    [ "$pid" != "$$" ] && exit 0
  done
fi

# fichier PID (le tunnel kali teste ce fichier pour savoir si le superviseur vit)
echo $$ > /tmp/oc_supervisor.pid

log "=== superviseur codespace demarre ==="
while true; do
  if ! tcp_ok 49374; then
    log "opencode KO -> relance"
    pkill -f "opencode serve --port 49374" 2>/dev/null
    sleep 1
    nohup "$HOME/opencode-chain/start-opencode-cs.sh" >/dev/null 2>&1 &
    sleep 12
  fi
  if ! tcp_ok 8770; then
    log "adaptateur KO -> relance"
    pkill -f "opencode-kimi-adapter.py" 2>/dev/null
    sleep 1
    nohup "$HOME/opencode-chain/start-adapter-cs.sh" >/dev/null 2>&1 &
    sleep 5
  fi
  sleep 15
done
