#!/bin/bash
# Rotation de l'IP de sortie Tor, executee DANS le Codespace.
#
# Le pont HTTP->SOCKS tourne dans le Codespace mais le Tor est sur le PC :
# la rotation doit donc traverser le tunnel SSH inverse pour agir sur le
# ControlPort de l'instance Tor dediee du PC (127.0.0.1:9151 cote PC).
#
# Le tunnel inverse expose le ControlPort en 19051 cote Codespace. Le cookie
# d'authentification (hex) est pousse par le tunnel local au demarrage.
set -u

CONTROL_PORT_LOCAL=19051
COOKIE_FILE=/home/codespace/opencode-v2/tor_cookie.hex

log() { echo "[rotate-tor] $*" >&2; }

if [ ! -s "$COOKIE_FILE" ]; then
  log "cookie ControlPort absent ($COOKIE_FILE) -> rotation impossible"
  exit 1
fi

# Lecture explicite en ASCII (le fichier est de l'hex, pas du binaire).
COOKIE_HEX=$(LC_ALL=C tr -d '\n\r ' < "$COOKIE_FILE")

if [ -z "$COOKIE_HEX" ]; then
  log "cookie ControlPort vide -> rotation impossible"
  exit 1
fi

python3 - "$CONTROL_PORT_LOCAL" "$COOKIE_HEX" <<'PY'
import socket
import sys

port = int(sys.argv[1])
cookie_hex = sys.argv[2]

try:
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
except Exception as exc:
    print("ERREUR: ControlPort Tor injoignable (%r)" % exc)
    sys.exit(1)

sock.settimeout(15)
sock.sendall(b"AUTHENTICATE " + cookie_hex.encode("ascii") + b"\r\n")
auth = sock.recv(1024).decode("ascii", "replace").strip()
if not auth.startswith("250"):
    print("ERREUR: authentification ControlPort refusee (%s)" % auth)
    sys.exit(1)

sock.sendall(b"SIGNAL NEWNYM\r\n")
resp = sock.recv(1024).decode("ascii", "replace").strip()
sock.close()

if not resp.startswith("250"):
    print("ERREUR: SIGNAL NEWNYM refuse (%s)" % resp)
    sys.exit(1)

print("NOUVEAU CIRCUIT TOR DEMANDE")
PY
