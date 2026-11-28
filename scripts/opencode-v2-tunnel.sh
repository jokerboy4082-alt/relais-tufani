#!/bin/bash
# Tunnel SSH du pont OpenCode v2 — MODE A LA DEMANDE (économe en quota GitHub).
#
# PROBLEME
# ---------
# Le quota GitHub Codespaces d'un compte gratuit = 120 h de compute.
# Une machine 2 cores compte DOUBLE (multiplicateur x2) => ~60 h reelles.
# Un tunnel SSH permanent garde le Codespace "actif" 24/7 : l'idle_timeout
# de 30 min ne se declenche jamais et le quota brule en continu.
# (Mesure : 67 h de presence x2 = 134 h > 120 h => quota depasse, GitHub
# renvoie alors HTTP 402 "billing issue" au demarrage.)
#
# SOLUTION
# --------
# Ce service ne maintient PLUS le tunnel en permanence. Il attend une
# demande du proxy local (fichier "wake"), monte le tunnel SSH sur le port
# TUNNEL_PORT, le garde ouvert tant qu'il y a de l'activite, puis le ferme.
# Quand l'activite cesse depuis IDLE_MINUTES, il ARRETE le Codespace pour
# ne plus facturer.
#
#   Hermes -> proxy:8771 (toujours ouvert) -> [wake] -> tunnel:8772 -> codespace:8771
#
# Le proxy est le seul point que Hermes voit : il ne refuse JAMAIS une
# connexion, il attend que le tunnel soit monte.
set -u

CS=opencode-v2-4j9j447xp7v435x76
GH_USER=jokerboy4082-alt
GH_TOKEN_FILE=/home/fantome/.config/opencode-v2/gh_token
TOR_HOME=/home/fantome/.config/opencode-v2/tor
TOR_SOCKS_PORT=9150
TOR_CONTROL_PORT=9151
TOR_COOKIE="$TOR_HOME/data/control_auth_cookie"
LOCAL_PORT=8771
TUNNEL_PORT=8772
REMOTE_PORT=8771
TOR_REMOTE_SOCKS=19050
TOR_REMOTE_CONTROL=19051
STATE_DIR=/home/fantome/.config/opencode-v2
WAKE_FILE="$STATE_DIR/wake"
IDLE_MINUTES=${IDLE_MINUTES:-10}
LOG=/tmp/opencode-v2-tunnel.log
export PATH=/home/fantome/.local/bin:/usr/local/bin:/usr/bin:/bin
export GH_TOKEN=""
export GITHUB_TOKEN=""

mkdir -p "$STATE_DIR"

log() { echo "[$(date +%H:%M:%S)] $*" >> "$LOG"; }

load_gh_token() {
  if [ ! -s "$GH_TOKEN_FILE" ]; then
    local t
    t=$(timeout 20 gh auth token --user "$GH_USER" 2>/dev/null || true)
    if [ -z "$t" ]; then
      log "ERREUR token GitHub introuvable pour $GH_USER"
      return 1
    fi
    printf '%s' "$t" > "$GH_TOKEN_FILE"
    chmod 600 "$GH_TOKEN_FILE"
  fi
  GH_TOKEN=$(cat "$GH_TOKEN_FILE")
  export GH_TOKEN GITHUB_TOKEN
}

ensure_tor() {
  # Tor est un service systemd separe : on ne le demarre JAMAIS en inline
  # (90 s de bootstrap + conflit de ports si deja lance).
  if ! ss -ltn 2>/dev/null | grep -q ":$TOR_SOCKS_PORT "; then
    log "Tor dedie absent -> delegation au service systemd (non bloquant)"
    ( timeout 30 systemctl --user restart opencode-v2-tor.service >> "$LOG" 2>&1 \
      || log "ECHEC delegation systemctl opencode-v2-tor.service" ) &
    disown 2>/dev/null || true
  fi
}

push_tor_cookie() {
  # Pousse le cookie d'auth du ControlPort Tor dans le Codespace (hex ASCII
  # via base64 : le transport "cat >" de gh codespace ssh corrompt le binaire).
  # EN ARRIERE-PLAN : en inline il bloquait la boucle de reconnexion.
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

codespace_state() {
  timeout 25 gh api "/user/codespaces/$CS" --jq .state 2>/dev/null || echo unknown
}

start_codespace() {
  local state tries
  state=$(codespace_state)
  log "etat du codespace: $state"
  if [ "$state" = "Available" ]; then
    return 0
  fi
  for tries in 1 2 3; do
    if timeout 25 gh api -X POST "/user/codespaces/$CS/start" >> "$LOG" 2>&1; then
      log "demarrage du codespace demande (essai $tries)"
      break
    fi
    log "echec du demarrage du codespace (essai $tries)"
    sleep 10
  done
  # Attendre que le codespace soit REELLEMENT pret : sans cette attente le
  # tunnel ssh echoue ("too many codespaces starting").
  for tries in $(seq 1 24); do
    sleep 10
    state=$(codespace_state)
    if [ "$state" = "Available" ]; then
      log "codespace pret (Available) apres $((tries * 10))s"
      sleep 12
      return 0
    fi
    log "codespace encore $state, attente..."
  done
  log "codespace toujours indisponible apres 240s"
  return 1
}

stop_codespace() {
  log "arret du codespace (inactivite > ${IDLE_MINUTES}min) -> economie de quota"
  timeout 30 gh api -X POST "/user/codespaces/$CS/stop" >> "$LOG" 2>&1 \
    && log "codespace arrete" \
    || log "echec de l'arret du codespace"
}

purge_stale_forwards() {
  # Une session sshd orpheline garde les forwards inverses 19050/19051 ;
  # au remont, ExitOnForwardFailure tue la session et le port 8772 ne
  # monte jamais. On tue les sessions de plus de 90 s (la session courante
  # de purge est plus jeune).
  local out
  out=$(timeout 30 gh codespace ssh -c "$CS" -- \
    "for p in \$(ps -eo pid,etimes,cmd 2>/dev/null | grep '[s]shd: codespace' | awk '{print \$1\" \"\$2}'); do
       pid=\${p%% *}; age=\${p##* }
       if [ \"\$age\" -gt 90 ] 2>/dev/null; then
         kill -9 \$pid 2>/dev/null && echo KILLED:\$pid
       fi
     done" \
    >> "$LOG" 2>&1 < /dev/null || true)
  if echo "$out" | grep -q "KILLED:"; then
    log "sessions sshd orphelines purgees"
  fi
}

mount_tunnel() {
  # Deux tentatives : avec les forwards inverses Tor, puis SANS eux.
  # ExitOnForwardFailure=yes avorte toute la session si un forward distant
  # echoue : le pont (8772) ne doit jamais dependre de la rotation Tor.
  local attempt=1
  while [ "$attempt" -le 2 ]; do
    local extra_args=()
    if [ "$attempt" -eq 1 ]; then
      extra_args=(
        -R "$TOR_REMOTE_SOCKS:127.0.0.1:$TOR_SOCKS_PORT"
        -R "$TOR_REMOTE_CONTROL:127.0.0.1:$TOR_CONTROL_PORT"
      )
      log "montage du tunnel $TUNNEL_PORT -> $REMOTE_PORT + Tor inverse"
    else
      log "montage du tunnel $TUNNEL_PORT -> $REMOTE_PORT (SANS forwards Tor)"
    fi

    setsid gh codespace ssh -c "$CS" -- \
      -N \
      -L "$TUNNEL_PORT:127.0.0.1:$REMOTE_PORT" \
      "${extra_args[@]}" \
      -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=20 \
      -o ServerAliveCountMax=6 \
      -o ConnectTimeout=20 \
      >> "$LOG" 2>&1 < /dev/null &
    TUNNEL_PID=$!
    disown 2>/dev/null

    local ok=0
    for _ in $(seq 1 45); do
      sleep 2
      if ss -ltn 2>/dev/null | grep -q ":$TUNNEL_PORT "; then
        ok=1
        log "tunnel monte sur $TUNNEL_PORT (pid $TUNNEL_PID)"
        break
      fi
      if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
        log "tunnel mort au demarrage"
        break
      fi
    done
    if [ "$ok" -eq 1 ]; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 5
  done
  return 1
}

log "=== service tunnel opencode-v2 demarre (mode a la demande, idle=${IDLE_MINUTES}min) ==="
load_gh_token || exit 1

while true; do
  # Attendre une demande du proxy (fichier "wake") ou une activite recente.
  WAITING=0
  while [ ! -f "$WAKE_FILE" ]; do
    if [ "$WAITING" -eq 0 ]; then
      log "en attente d'une requete (codespace arret possible)"
      WAITING=1
    fi
    sleep 10
  done
  rm -f "$WAKE_FILE" 2>/dev/null || true

  ensure_tor
  start_codespace || { log "demarrage du codespace impossible"; sleep 30; continue; }
  purge_stale_forwards
  push_tor_cookie

  if ! mount_tunnel; then
    log "montage du tunnel impossible -> nouvel essai dans 20s"
    sleep 20
    continue
  fi

  # Superviseur distant : opencode + adaptateur doivent tourner.
  timeout 25 gh codespace ssh -c "$CS" -- \
    "if [ -f /tmp/ocv2_supervisor.pid ] && kill -0 \$(cat /tmp/ocv2_supervisor.pid) 2>/dev/null; then echo SUP_ACTIF; else nohup bash /home/codespace/opencode-v2/start-supervisor.sh >/dev/null 2>&1 & echo SUP_RELANCE; fi" \
    >> "$LOG" 2>&1 < /dev/null || log "ECHEC supervision distante"

  # Surveiller l'activite : le proxy ecrit dans last_activity a chaque
  # requete. Sans requete depuis IDLE_MINUTES, on ferme le tunnel et on
  # ARRETE le codespace (c'est lui qui brule le quota).
  LAST_SEEN=$(date +%s)
  while kill -0 "$TUNNEL_PID" 2>/dev/null; do
    sleep 20
    ACT=$(cat "$STATE_DIR/last_activity" 2>/dev/null || echo 0)
    case "$ACT" in
      ''|*[!0-9]*) ACT=0 ;;
    esac
    NOW=$(date +%s)
    if [ "$ACT" -gt 0 ]; then
      DIFF=$((NOW - ACT))
      LAST_SEEN=$ACT
      if [ "$DIFF" -lt 300 ]; then
        # activite recente : on garde le tunnel
        continue
      fi
    else
      DIFF=$((NOW - LAST_SEEN))
      if [ "$DIFF" -lt 300 ]; then
        continue
      fi
    fi
    log "inactivite de $((DIFF / 60)) min -> fermeture du tunnel"
    kill -9 "$TUNNEL_PID" 2>/dev/null || true
    pkill -P "$TUNNEL_PID" 2>/dev/null || true
    sleep 3
    stop_codespace
    break
  done

  log "tunnel arrete, retour en attente"
  sleep 5
done
