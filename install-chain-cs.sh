#!/bin/bash
# Installation de la chaine relais opencode+adaptateur DANS le codespace.
# Source : le depot relais-tufani clone a la creation du codespace.
# Idempotent : reexecuter ne casse rien (reinstalle seulement ce qui manque).
set -u
SRC=""
for D in /workspaces/relais-tufani "$HOME/workspaces/relais-tufani" "$HOME/relais-tufani"; do
  [ -f "$D/install-chain-cs.sh" ] && SRC="$D" && break
done
CHAIN="$HOME/opencode-chain"

[ -n "$SRC" ] || { echo "ERREUR: depot relais-tufani introuvable"; exit 1; }

echo "[install] harmonisation des fins de ligne..."
sed -i 's/\r$//' "$SRC"/*.sh "$SRC"/agent/*.md 2>/dev/null

echo "[install] mise en place des fichiers..."
mkdir -p "$CHAIN/config/opencode/agent" "$CHAIN/logs"
cp -a "$SRC/opencode-kimi-adapter.py" "$CHAIN/"
cp -a "$SRC/agent/kimi-brain.md" "$CHAIN/config/opencode/agent/"
cp -a "$SRC/start-opencode-cs.sh" "$SRC/start-adapter-cs.sh" "$SRC/supervisor-chain-cs.sh" "$CHAIN/"
chmod +x "$CHAIN"/*.sh
sed -i 's/\r$//' "$CHAIN"/*.sh

echo "[install] opencode v2..."
if ! "$HOME/.opencode/bin/opencode" --version 2>/dev/null | grep -q "^2\."; then
  [ -f "$HOME/.opencode/bin/opencode" ] && mv "$HOME/.opencode/bin/opencode" "$HOME/.opencode/bin/opencode.old.$(date +%s)"
  cd /tmp || exit 1
  rm -rf ocx oc.tar.gz
  curl -fsSL -o oc.tar.gz https://github.com/sst/opencode/releases/download/v2.3.0/opencode-linux-x64.tar.gz || { echo "ERREUR telechargement opencode"; exit 1; }
  mkdir ocx && tar xzf oc.tar.gz -C ocx
  BIN=$(find /tmp/ocx -maxdepth 3 -type f -name opencode | head -1)
  [ -n "$BIN" ] || { echo "ERREUR binaire introuvable dans le tarball"; exit 1; }
  mkdir -p "$HOME/.opencode/bin"
  cp "$BIN" "$HOME/.opencode/bin/opencode" && chmod +x "$HOME/.opencode/bin/opencode"
fi
"$HOME/.opencode/bin/opencode" --version

echo "[install] lanceur attendu par le tunnel kali..."
printf '#!/bin/bash\nnohup "$HOME/opencode-chain/supervisor-chain-cs.sh" >/dev/null 2>&1 &\necho lance\n' > /home/codespace/start-supervisor.sh
chmod +x /home/codespace/start-supervisor.sh

echo "[install] relance propre du superviseur..."
pkill -f "supervisor-chain-cs[.]sh" 2>/dev/null
sleep 2
bash /home/codespace/start-supervisor.sh
sleep 20
P=$(cat /tmp/oc_supervisor.pid 2>/dev/null)
if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then echo "SUPERVISEUR_OK pid=$P"; else echo "SUPERVISEUR_KO"; fi
(exec 3<>/dev/tcp/127.0.0.1/8770) 2>/dev/null && echo ADAPT_OK || echo ADAPT_KO
(exec 3<>/dev/tcp/127.0.0.1/49374) 2>/dev/null && echo OC_OK || echo OC_KO

echo "[install] test PONG (120 s max)..."
printf '%s\n' '{"model":"beam-free","stream":false,"messages":[{"role":"user","content":"Dis uniquement PONG"}]}' > /tmp/pong-install.json
curl -s --max-time 120 -o /tmp/pong-install.out -w HTTP:%{http_code} -X POST -H "Content-Type: application/json" -d @/tmp/pong-install.json http://127.0.0.1:8770/v1/chat/completions 2>/dev/null
echo
head -c 300 /tmp/pong-install.out 2>/dev/null
echo
rm -f /tmp/pong-install.json /tmp/pong-install.out
echo "INSTALL_FINI"
