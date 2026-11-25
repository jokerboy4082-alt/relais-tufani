#!/bin/bash
# Instance Tor dediee au pont OpenCode v2.
#
# Pourquoi une 2e instance : le Tor systeme (root, ControlPort 9051) garde le
# meme circuit pendant des heures → meme IP de sortie → meme bucket de quota
# OpenCode (le quota free est par IP de sortie). Cette instance a son propre
# DataDirectory : on peut la relancer pour obtenir un NOUVEAU circuit et donc
# une NOUVELLE IP, ce qui reinitialise le quota.
#
# Authentification du ControlPort : CookieAuthentication 1. Tor ecrit le
# cookie dans <DataDirectory>/control_auth (lisible par l'utilisateur qui
# lance l'instance) → on peut envoyer SIGNAL NEWNYM pour forcer un nouveau
# circuit sans mot de passe a configurer.
set -u

TOR_HOME="$HOME/.config/opencode-v2/tor"
DATA_DIR="$TOR_HOME/data"
SOCKS_PORT=9150
CONTROL_PORT=9151
PID_FILE="$TOR_HOME/tor.pid"
LOG="$TOR_HOME/tor.log"

mkdir -p "$DATA_DIR"
chmod 700 "$TOR_HOME" "$DATA_DIR"

cat > "$TOR_HOME/torrc" <<EOF
SocksPort 127.0.0.1:$SOCKS_PORT
ControlPort 127.0.0.1:$CONTROL_PORT
DataDirectory $DATA_DIR
CookieAuthentication 1
EOF
chmod 600 "$TOR_HOME/torrc"

# Si l'instance tourne deja avec cette config, on la laisse.
if [ -f "$PID_FILE" ]; then
  OLD=$(cat "$PID_FILE" 2>/dev/null || true)
  if [ -n "${OLD:-}" ] && kill -0 "$OLD" 2>/dev/null; then
    echo "Tor dedie deja actif (pid $OLD)"
    exit 0
  fi
fi

echo "Demarrage de l'instance Tor dediee (socks:$SOCKS_PORT control:$CONTROL_PORT)"
setsid tor -f "$TOR_HOME/torrc" >> "$LOG" 2>&1 < /dev/null &
TOR_PID=$!
echo "$TOR_PID" > "$PID_FILE"
disown 2>/dev/null

# Attente du SocksPort ET du fichier de cookie
for _ in $(seq 1 45); do
  sleep 2
  if ss -ltn 2>/dev/null | grep -q ":$SOCKS_PORT " && [ -f "$DATA_DIR/control_auth" ]; then
    echo "Tor dedie pret (pid $TOR_PID, socks $SOCKS_PORT)"
    exit 0
  fi
done

echo "ECHEC: socks $SOCKS_PORT / cookie control_auth indisponibles"
tail -20 "$LOG"
exit 1
