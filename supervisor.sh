#!/bin/bash
# Superviseur OpenCode v2 + adaptateur Hermes dans le Codespace opencode-v2.
# Aucun secret en clair : le mot de passe du serveur OpenCode est genere au
# premier lancement et stocke dans un fichier chmod 600 lisible du seul user.
set -u

HOME_DIR="${HOME:-/home/codespace}"
CHAIN="$HOME_DIR/opencode-v2"
OC_BIN="$HOME_DIR/.opencode/bin/opencode"
OC_PORT=49374
ADAPTER_PORT=8771
SECRETS="$CHAIN/secrets"
LOG_DIR="$CHAIN/logs"

export PATH="$HOME_DIR/.opencode/bin:$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$CHAIN" "$LOG_DIR" "$SECRETS"
chmod 700 "$SECRETS"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG_DIR/supervisor.log"; }

tcp_ok() {
  (exec 3<>/dev/tcp/127.0.0.1/"$1") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
  return 1
}

# --- anti doublon ---
if [ -f /tmp/ocv2_supervisor.pid ]; then
  OLD=$(cat /tmp/ocv2_supervisor.pid 2>/dev/null || true)
  if [ -n "${OLD:-}" ] && kill -0 "$OLD" 2>/dev/null && [ "$OLD" != "$$" ]; then
    log "superviseur deja actif (pid $OLD) -> rien a faire"
    exit 0
  fi
fi
echo $$ > /tmp/ocv2_supervisor.pid

# --- secret du serveur OpenCode (genere une seule fois) ---
if [ ! -s "$SECRETS/server_password" ]; then
  head -c 32 /dev/urandom | base64 | tr -d '\n' > "$SECRETS/server_password"
  chmod 600 "$SECRETS/server_password"
  log "mot de passe serveur genere"
fi
OC_PASSWORD=$(cat "$SECRETS/server_password")

start_opencode() {
  log "demarrage opencode service"
  cd "$CHAIN" || exit 1
  OPENCODE_SERVER_PASSWORD="$OC_PASSWORD" \
  OPENCODE_CONFIG="$CHAIN/opencode.json" \
  nohup "$OC_BIN" serve --port "$OC_PORT" --hostname 127.0.0.1 \
    >> "$LOG_DIR/opencode.log" 2>&1 &
  echo $! > "$SECRETS/opencode.pid"
}

start_adapter() {
  log "demarrage adaptateur"
  cd "$CHAIN" || exit 1
  OC_HOST=127.0.0.1 \
  OC_PORT="$OC_PORT" \
  OC_PASSWORD="$OC_PASSWORD" \
  OC_DIR="$CHAIN" \
  OC_MODEL=opencode/beam-free \
  OC_AGENT=hermes-brain \
  OC_TIMEOUT=300 \
  LISTEN_HOST=127.0.0.1 \
  LISTEN_PORT="$ADAPTER_PORT" \
  nohup python3 "$CHAIN/opencode-v2-adapter.py" >> "$LOG_DIR/adapter.log" 2>&1 &
  echo $! > "$SECRETS/adapter.pid"
}

log "=== superviseur opencode-v2 demarre (pid $$) ==="

while true; do
  if ! tcp_ok "$OC_PORT"; then
    log "opencode KO -> relance"
    if [ -f "$SECRETS/opencode.pid" ]; then
      kill -9 "$(cat "$SECRETS/opencode.pid")" 2>/dev/null || true
    fi
    start_opencode
    sleep 10
  fi
  if ! tcp_ok "$ADAPTER_PORT"; then
    log "adaptateur KO -> relance"
    if [ -f "$SECRETS/adapter.pid" ]; then
      kill -9 "$(cat "$SECRETS/adapter.pid")" 2>/dev/null || true
    fi
    start_adapter
    sleep 4
  fi
  sleep 12
done
