#!/bin/bash
# Deploie les fichiers du pont OpenCode v2 dans le Codespace.
# Utilise base64 sur ssh : le transport gh codespace ssh corrompt les fichiers
# binaires et le texte contenant des octets non-ASCII.
set -eu

CS="${CS:-opencode-v2-4j9j447xp7v435x76}"
DEST="${DEST:-/home/codespace/opencode-v2}"
export PATH=/home/fantome/.local/bin:/usr/local/bin:/usr/bin:/bin

deploy() {
  local src="$1" dst="$2" mode="${3:-644}"
  local b64
  b64=$(base64 -w0 < "$src")
  timeout 60 gh codespace ssh -c "$CS" -- \
    "echo '$b64' | base64 -d > '$dst' && chmod $mode '$dst' && echo OK:$dst" \
    | grep -q "^OK:" && echo "deploye: $dst" || { echo "ECHEC: $dst"; return 1; }
}

deploy /home/fantome/opencode-v2-supervisor-cs.sh "$DEST/supervisor.sh" 755
deploy /home/fantome/opencode-v2-http-socks-bridge.py "$DEST/http-socks-bridge.py" 644
deploy /home/fantome/opencode-v2-rotate-tor-cs.sh "$DEST/rotate-tor.sh" 755
deploy /home/fantome/opencode-v2-adapter.py "$DEST/opencode-v2-adapter.py" 644
deploy /home/fantome/opencode-v2-opencode.json "$DEST/opencode.json" 644

# Cookie d'authentification du ControlPort Tor (hex ASCII)
if [ -s /home/fantome/.config/opencode-v2/tor/data/control_auth_cookie ]; then
  HEX=$(xxd -p /home/fantome/.config/opencode-v2/tor/data/control_auth_cookie | tr -d '\n')
  timeout 60 gh codespace ssh -c "$CS" -- \
    "printf '%s' '$HEX' > '$DEST/tor_cookie.hex' && chmod 600 '$DEST/tor_cookie.hex' && echo OK" \
    | grep -q "^OK" && echo "deploye: tor_cookie.hex" || echo "ECHEC: tor_cookie.hex"
fi

echo "=== verification ==="
timeout 40 gh codespace ssh -c "$CS" -- \
  "ls -la '$DEST' | head -15; echo '--- test rotate ---'; bash '$DEST/rotate-tor.sh'"
