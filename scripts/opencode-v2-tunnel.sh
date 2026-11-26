#!/bin/bash
# Tunnel SSH durable vers le Codespace opencode-v2 (compte jokerboy4082-alt).
#
# DEUX TUNNELS SSH dans la meme session :
#   -L 8771:127.0.0.1:8771   Hermes (PC) -> adaptateur (Codespace)
#   -R 19050:127.0.0.1:9150  Codespace -> Tor dedie du PC (SocksPort)
#   -R 19051:127.0.0.1:9151  Codespace -> ControlPort Tor du PC
#
# Pourquoi : le quota free OpenCode est indexe sur l'IP de SORTIE. En passant
# par Tor, cette IP change. Le ControlPort est aussi expose pour que le
# pont, en detectant un quota epuise, force un nouveau circuit (SIGNAL NEWNYM).
#
# PERMANENCE :
#  - compte GitHub FORCE a jokerboy4082-alt ;
#  - token lu dans un fichier dedie (mode 600) ;
#  - le Codespace est redemarre via l'API s'il est arrete ;
#  - le superviseur distant est relance s'il est mort ;
#  - TOUTES les commandes gh ont un timeout (un reseau coupe ne doit jamais
#    bloquer la boucle de reconnexion).
set -u

CS=opencode-v2-4j9j447xp7v435x76
GH_USER=jokerboy4082-alt
GH_TOKEN_FILE=/home/fantome/.config/opencode-v2/gh_token
TOR_HOME=/home/fantome/.config/opencode-v2/tor
TOR_SOCKS_PORT=9150
TOR_CONTROL_PORT=9151
TOR_COOKIE="$TOR_HOME/data/control_auth_cookie"
LOCAL_PORT=8771
REMOTE_PORT=8771
TOR_REMOTE_SOCKS=19050
TOR_REMOTE_CONTROL=19051
LOG=/tmp/opencode-v2-tunnel.log
export PATH=/home/fantome/.local/bin:/usr/local/bin:/usr/bin:/bin
export GH_TOKEN=""
export GITHUB_TOKEN=""

log() { echo "[$(date +%H:%M:%S)] $*" >> "$LOG"; }

load_gh_token() {
  if [ ! -s "$GH_TOKEN_FILE" ]; then
    local t
    t=$(timeout 20 gh auth token --user "$GH_USER" 2>/dev/null || true)
    if [ -z "$t" ]; then
      log "ERREUR token GitHub introuvable pour $GH_USER"
      return 1
    fi
    mkdir -p "$(dirname "$GH_TOKEN_FILE")"
    printf '%s' "$t" > "$GH_TOKEN_FILE"
    chmod 600 "$GH_TOKEN_FILE"
  fi
  GH_TOKEN=$(cat "$GH_TOKEN_FILE")
  export GH_TOKEN GITHUB_TOKEN
}

ensure_tor() {
  # L'instance Tor dediee est un SERVICE systemd separe
  # (opencode-v2-tor.service). Le tunnel ne la demarre JAMAIS en inline :
  # - Tor met ~90 s a bootstraper, un appel inline bloquait la boucle ;
  # - Tor etait deja lance => conflit de ports 9150/9151 ("Address
  #   already in use") et le tunnel partait en echec.
  # On delegue a systemd et on ne bloque pas : le tunnel monte meme si Tor
  # est lent, l'instance finira par etre prete pour la rotation d'IP.
  if ! ss -ltn 2>/dev/null | grep -q ":$TOR_SOCKS_PORT"; then
    log "Tor dedie absent -> delegation au service systemd (non bloquant)"
    # systemd gere le demarrage et le bootstrap (~90 s) : on ne bloque pas la
    # boucle de reconnexion du tunnel, sinon le port 8771 arrive tard.
    ( timeout 30 systemctl --user restart opencode-v2-tor.service >> "$LOG" 2>&1 \
      || log "ECHEC delegation systemctl opencode-v2-tor.service" ) &
    disown 2>/dev/null || true
  fi
}

push_tor_cookie() {
  # Le cookie d'auth du ControlPort Tor doit etre accessible dans le Codespace
  # pour que rotate-tor.sh puisse envoyer SIGNAL NEWNYM.
  # On le pousse en HEX ASCII via base64 : le transport `cat >` de
  # `gh codespace ssh` convertit le binaire et corrompt le cookie.
  #
  # IMPORTANT : ce push est fait EN ARRIERE-PLAN. Execute en inline il
  # bloquait la boucle de reconnexion du tunnel (le service systemd restait
  # occupe par cette commande au lieu de monter le port 8771).
  if [ ! -s "$TOR_COOKIE" ]; then
    log "cookie ControlPort Tor absent ($TOR_COOKIE)"
    return 0
  fi
  local hex b64
  hex=$(xxd -p "$TOR_COOKIE" | tr -d '\n')
  b64=$(printf '%s' "$hex" | base64 -w0)
  (
    timeout 60 gh codespace ssh -c "$CS" -- \
      "echo '$b64' | base64 -d | tr -d '\\n' > /home/codespace/opencode-v2/tor_cookie.hex && chmod 600 /home/codespace/opencode-v2/tor_cookie.hex" \
      >> "$LOG" 2>&1 < /dev/null
    log "cookie ControlPort Tor pousse vers le codespace"
  ) &
  disown 2>/dev/null || true
}

log "=== tunnel opencode-v2 demarre (pid $$) ==="

if [ -f /tmp/opencode-v2-tunnel.pid ]; then
  OLD=$(cat /tmp/opencode-v2-tunnel.pid 2>/dev/null || true)
  if [ -n "${OLD:-}" ] && kill -0 "$OLD" 2>/dev/null && [ "$OLD" != "$$" ]; then
    log "tunnel deja actif (pid $OLD) -> rien a faire"
    exit 0
  fi
fi
echo $$ > /tmp/opencode-v2-tunnel.pid

load_gh_token || exit 1
ensure_tor

if ss -ltn 2>/dev/null | grep -q ":$LOCAL_PORT "; then
  OLD_PID=$(ss -ltnp 2>/dev/null | grep ":$LOCAL_PORT " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)
  if [ -n "${OLD_PID:-}" ]; then
    kill -9 "$OLD_PID" 2>/dev/null && log "ancien tunnel $OLD_PID tue"
  fi
  sleep 2
fi

ensure_remote() {
  if timeout 25 gh codespace ssh -c "$CS" -- \
    "if [ -f /tmp/ocv2_supervisor.pid ] && kill -0 \$(cat /tmp/ocv2_supervisor.pid) 2>/dev/null; then echo SUP_ACTIF; else nohup bash /home/codespace/opencode-v2/start-supervisor.sh >/dev/null 2>&1 & echo SUP_RELANCE; fi" \
    >> "$LOG" 2>&1 < /dev/null; then
    log "superviseur distant verifie"
  else
    log "ECHEC supervision distante (timeout ou reseau indisponible)"
  fi
}

purge_stale_forwards() {
  # Une session SSH distante restee accrochee garde les forwards inverses
  # 19050/19051 (Tor socks + control). Au remount, le nouveau tunnel echoue
  # avec "remote port forwarding failed for listen port 19050" et
  # ExitOnForwardFailure tue toute la session : plus de port 8771 => plus de
  # pont.
  #
  # On tue les sessions sshd "codespace" qui ne sont PAS celle de la purge
  # courante. Celle-ci est identifiee par son age : la purge vient de s'ouvrir
  # (etimes < 60 s), les sessions orphelines sont plus anciennes.
  local out
  out=$(timeout 30 gh codespace ssh -c "$CS" -- \
    "for p in \$(ps -eo pid,etimes,cmd 2>/dev/null | grep '[s]shd: codespace' | awk '{print \$1\" \"\$2}'); do
       pid=\${p%% *}; age=\${p##* }
       if [ \"\$age\" -gt 90 ] 2>/dev/null; then
         kill -9 \$pid 2>/dev/null && echo KILLED:\$pid
       fi
     done; sleep 2; ss -ltn 2>/dev/null | grep -cE ':(19050|19051)' || true" \
    >> "$LOG" 2>&1 < /dev/null || true)
  if echo "$out" | grep -q "KILLED:"; then
    log "sessions sshd orphelines purgees (forwards 19050/19051 liberes)"
  fi
}

start_codespace() {
  local state
  state=$(timeout 25 gh api "/user/codespaces/$CS" --jq .state 2>/dev/null || echo unknown)
  log "etat du codespace: $state"
  if [ "$state" = "Available" ]; then
    return 0
  fi
  timeout 25 gh api -X POST "/user/codespaces/$CS/start" >> "$LOG" 2>&1 \
    && log "demarrage du codespace demande" \
    || log "echec du demarrage du codespace"
}

while true; do
  start_codespace
  ensure_tor
  # Libere les forwards inverses 19050/19051 detenus par une session SSH
  # orpheline AVANT de monter le nouveau tunnel (sinon ExitOnForwardFailure
  # tue la session et le port 8771 n'est jamais monte).
  purge_stale_forwards
  push_tor_cookie

  log "ouverture tunnel $LOCAL_PORT -> $REMOTE_PORT + Tor inverse (socks $TOR_REMOTE_SOCKS, control $TOR_REMOTE_CONTROL)"

  setsid gh codespace ssh -c "$CS" -- \
    -N \
    -L "$LOCAL_PORT:127.0.0.1:$REMOTE_PORT" \
    -R "$TOR_REMOTE_SOCKS:127.0.0.1:$TOR_SOCKS_PORT" \
    -R "$TOR_REMOTE_CONTROL:127.0.0.1:$TOR_CONTROL_PORT" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=20 \
    -o ServerAliveCountMax=6 \
    -o ConnectTimeout=20 \
    >> "$LOG" 2>&1 < /dev/null &
  TUNNEL_PID=$!
  disown 2>/dev/null

  PORT_OK=0
  for _ in $(seq 1 45); do
    sleep 2
    if ss -ltn 2>/dev/null | grep -q ":$LOCAL_PORT "; then
      PORT_OK=1
      log "port local pret (pid $TUNNEL_PID)"
      break
    fi
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
      log "tunnel mort au demarrage"
      break
    fi
  done

  if [ "$PORT_OK" -eq 1 ]; then
    ensure_remote
    while kill -0 "$TUNNEL_PID" 2>/dev/null; do
      sleep 20
      if ! ss -ltn 2>/dev/null | grep -q ":$LOCAL_PORT "; then
        log "port disparu -> relance du tunnel"
        break
      fi
      if curl -fsS --max-time 12 "http://127.0.0.1:$LOCAL_PORT/health" >/dev/null 2>&1; then
        log "health OK"
      else
        log "health KO -> relance superviseur distant"
        ensure_remote
        if ! curl -fsS --max-time 12 "http://127.0.0.1:$LOCAL_PORT/health" >/dev/null 2>&1; then
          log "tunnel toujours KO -> kill du processus gh ($TUNNEL_PID) et reconnexion"
          kill -9 "$TUNNEL_PID" 2>/dev/null || true
          pkill -P "$TUNNEL_PID" 2>/dev/null || true
          break
        fi
      fi
    done
  else
    log "tunnel non etabli -> le codespace sera relance au prochain tour"
    sleep 20
  fi

  log "tunnel arrete, relance dans 5s"
  sleep 5
done
