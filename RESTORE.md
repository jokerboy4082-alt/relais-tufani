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
