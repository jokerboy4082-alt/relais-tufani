#!/bin/bash
# Superviseur OpenCode v2 + pont HTTP->SOCKS + adaptateur Hermes (Codespace opencode-v2).
#
# CHAINE RESEAU (par defaut : IP DIRECTE du Codespace) :
#   Hermes (PC) --SSH -L 8771--> Codespace
#   OpenCode (Codespace) --sans proxy--> opencode.ai
#
# Pourquoi IP directe : les IP de sortie Tor sont blacklistees par OpenCode
# (verifie le 2026-11-25 : via Tor => 429 FreeUsageLimitError meme avec un
# compte neuf et quota intact ; via IP directe => reponse OK). Le quota free
# est donc lie au reseau, mais Tor EST le reseau bloque.
#
# MODE TOR (optionnel, pour les quotas IP par reseau) :
#   TOR_MODE=1 → le trafic passe par le pont HTTP->SOCKS (relais Tor du PC
#   via le tunnel SSH inverse -R 19050). Le pont force une rotation d'IP
#   (SIGNAL NEWNYM) quand une connexion echoue.
#   A n'utiliser que si les IP directes sont, elles aussi, a quota epuise.
#
# Les secrets (mot de passe du serveur OpenCode) ne sont jamais en clair :
# ils sont generes au premier lancement dans un fichier chmod 600.
set -u

HOME_DIR="${HOME:-/home/codespace}"
CHAIN="$HOME_DIR/opencode-v2"
OC_BIN="$HOME_DIR/.opencode/bin/opencode"
OC_PORT=49374
ADAPTER_PORT=8771
BRIDGE_PORT=8899
SOCKS_PORT=19050
SECRETS="$CHAIN/secrets"
LOG_DIR="$CHAIN/logs"
TOR_MODE="${TOR_MODE:-0}"

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

start_bridge() {
  log "demarrage pont HTTP->SOCKS (:$BRIDGE_PORT -> socks:$SOCKS_PORT, rotation active)"
  cd "$CHAIN" || exit 1
  setsid python3 "$CHAIN/http-socks-bridge.py" \
    --listen "127.0.0.1:$BRIDGE_PORT" \
    --socks "127.0.0.1:$SOCKS_PORT" \
    --rotate-cmd "bash $CHAIN/rotate-tor.sh" \
    >> "$LOG_DIR/bridge.log" 2>&1 < /dev/null &
  echo $! > "$SECRETS/bridge.pid"
  disown 2>/dev/null
}

start_opencode() {
  if [ "$TOR_MODE" = "1" ]; then
    log "demarrage opencode service (MODE TOR via proxy)"
    PROXY_ENV=(
      HTTP_PROXY="http://127.0.0.1:$BRIDGE_PORT"
      HTTPS_PROXY="http://127.0.0.1:$BRIDGE_PORT"
      http_proxy="http://127.0.0.1:$BRIDGE_PORT"
      https_proxy="http://127.0.0.1:$BRIDGE_PORT"
      ALL_PROXY="socks5h://127.0.0.1:$SOCKS_PORT"
      all_proxy="socks5h://127.0.0.1:$SOCKS_PORT"
    )
  else
    log "demarrage opencode service (IP DIRECTE du codespace)"
    PROXY_ENV=()
  fi
  cd "$CHAIN" || exit 1
  env OPENCODE_SERVER_PASSWORD="$OC_PASSWORD" \
      OPENCODE_CONFIG="$CHAIN/opencode.json" \
      "${PROXY_ENV[@]}" \
      nohup "$OC_BIN" serve --port "$OC_PORT" --hostname 127.0.0.1 \
      >> "$LOG_DIR/opencode.log" 2>&1 &
  echo $! > "$SECRETS/opencode.pid"
  disown 2>/dev/null
}

start_adapter() {
  log "demarrage adaptateur"
  cd "$CHAIN" || exit 1
  OC_HOST=127.0.0.1 \
  OC_PORT="$OC_PORT" \
  OC_PASSWORD="$OC_PASSWORD" \
  OC_DIR="$CHAIN" \
  OC_MODEL=opencode/big-pickle \
  OC_AGENT=hermes-brain \
  OC_TIMEOUT=300 \
  LISTEN_HOST=127.0.0.1 \
  LISTEN_PORT="$ADAPTER_PORT" \
  TOR_CONTROL_PORT=19051 \
  TOR_COOKIE_FILE="$CHAIN/tor_cookie.hex" \
  TOR_ROTATE_ON_QUOTA=1 \
  QUOTA_MAX_ROTATIONS=2 \
  setsid python3 "$CHAIN/opencode-v2-adapter.py" >> "$LOG_DIR/adapter.log" 2>&1 < /dev/null &
  echo $! > "$SECRETS/adapter.pid"
  disown 2>/dev/null
}

log "=== superviseur opencode-v2 demarre (pid $$) — TOR_MODE=$TOR_MODE ==="

while true; do
  # Le pont Tor n'est demarre qu'en mode TOR.
  if [ "$TOR_MODE" = "1" ] && ! tcp_ok "$BRIDGE_PORT"; then
    log "pont Tor KO -> relance"
    start_bridge
    sleep 3
  fi
  if ! tcp_ok "$OC_PORT"; then
    log "opencode KO -> relance"
    if [ -f "$SECRETS/opencode.pid" ]; then
      kill -9 "$(cat "$SECRETS/opencode.pid")" 2>/dev/null || true
    fi
    start_opencode
    sleep 12
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
