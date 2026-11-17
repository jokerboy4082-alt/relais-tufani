#!/bin/bash
# Lanceur serveur opencode v2 (dans le codespace, donnees isolees)
export XDG_DATA_HOME="$HOME/opencode-chain/data"
export XDG_CONFIG_HOME="$HOME/opencode-chain/config"
export OPENCODE_SERVER_PASSWORD="chaine-kali-2026"
mkdir -p "$HOME/opencode-chain/data" "$HOME/opencode-chain/config" "$HOME/opencode-chain/logs"
cd "$HOME/opencode-chain"
exec "$HOME/.opencode/bin/opencode" serve --port 49374 --hostname 127.0.0.1 >> "$HOME/opencode-chain/logs/opencode.log" 2>&1
