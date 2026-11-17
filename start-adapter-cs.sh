#!/bin/bash
# Lanceur adaptateur Hermes<->OpenCode (dans le codespace)
# ecoute 127.0.0.1:8770 = le port que le tunnel kali (ssh -R 8771) attend
export OC_HOST=127.0.0.1
export OC_PORT=49374
export OC_PASSWORD=chaine-kali-2026
export OC_DIR="$HOME/opencode-chain"
export OC_MODEL=opencode/beam-free
export OC_AGENT=kimi-brain
export CHAIN_TOKEN=
export OC_TIMEOUT=300
export LISTEN_HOST=127.0.0.1
export LISTEN_PORT=8770
exec python3 "$HOME/opencode-chain/opencode-kimi-adapter.py" >> "$HOME/opencode-chain/logs/adapter.log" 2>&1
