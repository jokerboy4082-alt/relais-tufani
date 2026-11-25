#!/usr/bin/env python3
"""Rotation de l'IP de sortie Tor pour le pont OpenCode v2.

Le quota free OpenCode est indexe sur l'IP de SORTIE. Tor garde le meme
circuit (et donc la meme IP) pendant des heures : sans rotation, le quota
reste epuise meme apres un changement de WiFi. Ce module force un nouveau
circuit via le ControlPort de l'instance Tor dediee (SIGNAL NEWNYM).

Usage :
    python3 opencode-v2-tor-rotate.py newnym     # force un nouveau circuit
    python3 opencode-v2-tor-rotate.py ip          # affiche l'IP de sortie
    python3 opencode-v2-tor-rotate.py rotate-and-wait
"""
import os
import socket
import subprocess
import sys
import time

TOR_HOME = os.path.expanduser("~/.config/opencode-v2/tor")
CONTROL_PORT = 9151
SOCKS_PORT = 9150
COOKIE_PATH = os.path.join(TOR_HOME, "data", "control_auth_cookie")
IP_URL = "https://api.ipify.org"


def current_ip(timeout=25):
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout), "--socks5-hostname",
             "127.0.0.1:%d" % SOCKS_PORT, IP_URL],
            capture_output=True, text=True, timeout=timeout + 8,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def control(command, timeout=8):
    """Envoie une commande au ControlPort de Tor (cookie auth)."""
    try:
        cookie = open(COOKIE_PATH, "rb").read()
    except OSError:
        return "COOKIE_ABSENT"
    try:
        sock = socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(b"AUTHENTICATE " + cookie.hex().encode() + b"\r\n")
        auth = sock.recv(1024).decode("utf-8", "replace").strip()
        if not auth.startswith("250"):
            sock.close()
            return "AUTH_" + auth
        sock.sendall(command.encode() + b"\r\n")
        resp = sock.recv(1024).decode("utf-8", "replace").strip()
        sock.close()
        return resp
    except Exception as exc:
        return "ERR_" + repr(exc)[:80]


def newnym():
    """Force un nouveau circuit Tor → nouvelle IP de sortie."""
    before = current_ip()
    resp = control("SIGNAL NEWNYM")
    # Le nouveau circuit met quelques secondes a s'etablir : on attend que
    # l'IP change vraiment (max 60 s).
    after = before
    for _ in range(12):
        time.sleep(5)
        after = current_ip()
        if after and after != before:
            return {"ok": True, "before": before, "after": after, "signal": resp}
    return {"ok": False, "before": before, "after": after, "signal": resp}


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "ip"
    if action == "ip":
        print(current_ip())
    elif action == "newnym":
        result = newnym()
        print("%s %s -> %s" % (result["ok"], result["before"], result["after"]))
        sys.exit(0 if result["ok"] else 1)
    elif action == "rotate-and-wait":
        result = newnym()
        print("%s %s -> %s" % (result["ok"], result["before"], result["after"]))
        sys.exit(0 if result["ok"] else 1)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
