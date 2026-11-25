#!/bin/bash
# Synchronise l'etat complet du pont OpenCode v2 dans le depot GitHub.
#
# But : etre capable de tout reconstruire apres un reboot, un changement de
# machine, ou la destruction du Codespace. Le depot contient :
#   - les scripts du pont (tunnel, tor, pont HTTP->SOCKS, superviseur, rotation)
#   - l'etat OpenCode du Codespace (config + credential OAuth chiffre)
#   - le credential du ControlPort Tor (chiffre)
#
# Tout ce qui est secret est chiffre avec la passphrase locale (mode 600) :
# le depot ne contient JAMAIS de secret en clair.
set -u

CS="${CS:-opencode-v2-4j9j447xp7v435x76}"
GH_USER=jokerboy4082-alt
REPO=jokerboy4082-alt/relais-tufani
STATE_DIR=/home/fantome/.config/opencode-v2
BACKUP_DIR="$STATE_DIR/backup"
export PATH=/home/fantome/.local/bin:/usr/local/bin:/usr/bin:/bin
export GH_TOKEN=""
export GITHUB_TOKEN=""

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$STATE_DIR/backup.log"; }

load_gh_token() {
  if [ ! -s "$STATE_DIR/gh_token" ]; then
    local t
    t=$(timeout 20 gh auth token --user "$GH_USER" 2>/dev/null || true)
    [ -n "$t" ] || { log "ERREUR token GitHub introuvable"; return 1; }
    mkdir -p "$STATE_DIR"
    printf '%s' "$t" > "$STATE_DIR/gh_token"
    chmod 600 "$STATE_DIR/gh_token"
  fi
  GH_TOKEN=$(cat "$STATE_DIR/gh_token")
  export GH_TOKEN GITHUB_TOKEN
}

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# Le token GitHub est indispensable pour le push (le clone HTTPS nu n'a pas
# de credentials). Sans cet appel, $GH_TOKEN est vide et le push echoue.
load_gh_token || { log "ECHEC chargement du token GitHub"; exit 1; }
log "token GitHub $GH_USER charge (len=${#GH_TOKEN})"

# ---- 1. scripts du pont (non secrets) ----
SCRIPTS="opencode-v2-adapter.py opencode-v2-http-socks-bridge.py opencode-v2-tor.sh opencode-v2-tor-rotate.py opencode-v2-rotate-tor-cs.sh opencode-v2-supervisor-cs.sh opencode-v2-tunnel.sh opencode-v2-deploy.sh opencode-v2-backup.sh opencode-v2-opencode.json"

# ---- 2. etat OpenCode du Codespace ----
if timeout 60 gh codespace ssh -c "$CS" -- 'cat $HOME/.config/opencode/opencode.json' > "$BACKUP_DIR/opencode.json" 2>/dev/null && [ -s "$BACKUP_DIR/opencode.json" ]; then
  log "config opencode exportee"
else
  log "ECHEC export config opencode"
fi

if timeout 60 gh codespace ssh -c "$CS" -- 'export PATH=$HOME/.opencode/bin:$PATH; opencode auth export' > "$BACKUP_DIR/auth.json" 2>/dev/null && [ -s "$BACKUP_DIR/auth.json" ]; then
  log "credential opencode exporte"
else
  log "ECHEC export credential opencode"
fi

# ---- 3. cookie ControlPort Tor (chiffre) ----
if [ -s "$STATE_DIR/tor/data/control_auth_cookie" ]; then
  xxd -p "$STATE_DIR/tor/data/control_auth_cookie" | tr -d '\n' > "$BACKUP_DIR/tor_cookie.hex"
fi

# ---- 4. chiffrement ----
if command -v openssl >/dev/null 2>&1 && [ -s "$STATE_DIR/passphrase" ]; then
  for f in auth.json tor_cookie.hex; do
    [ -s "$BACKUP_DIR/$f" ] || continue
    openssl enc -aes-256-cbc -pbkdf2 -salt \
      -in "$BACKUP_DIR/$f" -out "$BACKUP_DIR/$f.enc" \
      -pass file:"$STATE_DIR/passphrase" 2>/dev/null \
      && rm -f "$BACKUP_DIR/$f" \
      && log "$f chiffre"
  done
fi

# ---- 5. push dans le depot ----
REPO_DIR="$HOME/.cache/opencode-v2-repo"
rm -rf "$REPO_DIR"
if ! timeout 90 git clone --depth 1 "https://github.com/$REPO.git" "$REPO_DIR" >/dev/null 2>&1; then
  log "ECHEC clone du depot"
  exit 1
fi

mkdir -p "$REPO_DIR/state" "$REPO_DIR/scripts"
for f in $SCRIPTS; do
  [ -f "/home/fantome/$f" ] && cp "/home/fantome/$f" "$REPO_DIR/scripts/$f"
done
[ -s "$BACKUP_DIR/opencode.json" ] && cp "$BACKUP_DIR/opencode.json" "$REPO_DIR/state/opencode.json"
[ -s "$BACKUP_DIR/auth.json.enc" ] && cp "$BACKUP_DIR/auth.json.enc" "$REPO_DIR/state/auth.json.enc"
[ -s "$BACKUP_DIR/tor_cookie.hex.enc" ] && cp "$BACKUP_DIR/tor_cookie.hex.enc" "$REPO_DIR/state/tor_cookie.hex.enc"

# Documentation de la restauration
cat > "$REPO_DIR/RESTORE.md" <<'EOF'
# Restauration du pont OpenCode v2

## Prérequis
- Un PC avec `tor`, `gh`, `git`, `openssl` (Debian/Kali)
- Le compte GitHub `jokerboy4082-alt` avec accès au dépôt
- Un Codespace créé depuis `jokerboy4082-alt/relais-tufani`

## Étapes

1. Scripts et services
   ```bash
   cp scripts/* ~/
   chmod +x ~/opencode-v2-*.sh
   cp scripts/opencode-v2-opencode.json ~/.config/opencode-v2/tor/opencode.json 2>/dev/null || true
   ```

2. Secrets locaux (chiffres dans state/)
   ```bash
   mkdir -p ~/.config/opencode-v2/tor/data
   # passphrase : celle de la machine d'origine (ABSENTE du depot)
   openssl enc -d -aes-256-cbc -pbkdf2 -salt \
     -in state/auth.json.enc -out /tmp/auth.json -pass file:~/.config/opencode-v2/passphrase
   ```

3. Tor dédié + service
   ```bash
   ~/.config/opencode-v2/tor/data/...   # DataDirectory recréé par opencode-v2-tor.sh
   systemctl --user enable --now opencode-v2-tor.service
   ```

4. Codespace + déploiement
   ```bash
   CS=<nom-du-codespace>
   ./opencode-v2-deploy.sh
   ```

5. Tunnel + provider Hermes
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now opencode-v2-tunnel.service
   hermes config set providers.opencode-v2.name "OpenCode v2 (Codespace jokerboy4082-alt)"
   hermes config set providers.opencode-v2.base_url "http://127.0.0.1:8771/v1"
   hermes config set providers.opencode-v2.api_key "opencode-v2-local"
   ```

## La passphrase de chiffrement n'est PAS dans le depot
Elle est dans `~/.config/opencode-v2/passphrase` (mode 600) sur la machine
d'origine. Sans elle, `state/auth.json.enc` est inexploitable.
EOF

cd "$REPO_DIR" || exit 1
git config user.email "opencode-v2@localhost"
git config user.name "opencode-v2-backup"
git add -A state scripts RESTORE.md 2>/dev/null || true

if git diff --cached --quiet; then
  log "rien a pousser (depot deja a jour)"
else
  git commit -m "sync: pont opencode v2 complet (tor + rotation) $(date +%F)" >/dev/null 2>&1 \
    || { log "ECHEC commit"; exit 1; }
  # On pousse via une URL qui porte le token, puis on restaure l'URL propre
  # (le clone HTTPS nu n'a pas de credentials).
  git remote set-url origin "https://$GH_USER:${GH_TOKEN}@github.com/$REPO.git"
  if git push origin HEAD:main >/dev/null 2>&1; then
    git remote set-url origin "https://github.com/$REPO.git"
    log "depot synchronise sur $REPO"
  else
    git remote set-url origin "https://github.com/$REPO.git"
    log "ECHEC push"
    exit 1
  fi
fi
exit 0
