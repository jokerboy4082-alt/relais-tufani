#!/usr/bin/env python3
"""Proxy local "a la demande" pour le pont OpenCode v2.

PROBLEME
---------
Le Codespace GitHub facture son temps de compute (2 cores = multiplicateur x2).
Compte GitHub gratuit = 120 h de compute incluses, donc ~60 h reelles.
Un tunnel SSH permanent garde le Codespace "actif" 24/7 : l'idle_timeout de
30 min de GitHub ne se declenche JAMAIS, et le quota est brule en continu.

SOLUTION
--------
Ce proxy ecoute en permanence sur le port LOCAL (celui que Hermes voit) et
ne monte le tunnel SSH que lorsqu'une requete arrive. Quand aucune requete
n'a circule depuis IDLE_STOP_MINUTES, il DEMARRE le Codespace en arriere-plan
(pour qu'il soit pret pour la prochaine requete) ou l'arrete completement.

Flux :
    Hermes -> proxy:8771 -> [file d'attente] -> tunnel SSH -> codespace:8771

Le proxy ne repond jamais "connection refused" : si le Codespace dort, la
requete attend (avec timeout) que le tunnel soit monte.
"""

import http.server
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time

LOCAL_PORT = int(os.environ.get("LOCAL_PORT", "8771"))
REMOTE_HOST = "127.0.0.1"
REMOTE_PORT = int(os.environ.get("REMOTE_PORT", "8771"))
TUNNEL_HOST = os.environ.get("TUNNEL_HOST", "127.0.0.1")
TUNNEL_PORT = int(os.environ.get("TUNNEL_PORT", "8772"))
IDLE_STOP_MINUTES = int(os.environ.get("IDLE_STOP_MINUTES", "10"))
WAKE_TIMEOUT = int(os.environ.get("WAKE_TIMEOUT", "240"))
STATE_DIR = os.environ.get("STATE_DIR", "/home/fantome/.config/opencode-v2")
ACTIVITY_FILE = os.path.join(STATE_DIR, "last_activity")
WAKE_FILE = os.path.join(STATE_DIR, "wake")
TUNNEL_LOG_FILE = os.environ.get("TUNNEL_LOG_FILE", "/tmp/opencode-v2-tunnel.log")
LOG_FILE = os.environ.get("LOG_FILE", "/tmp/opencode-v2-proxy.log")

os.makedirs(STATE_DIR, exist_ok=True)

_lock = threading.Lock()
_tunnel_up = False
_last_request = 0.0


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    try:
        with open(LOG_FILE, "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def touch_activity():
    global _last_request
    _last_request = time.time()
    try:
        with open(ACTIVITY_FILE, "w") as fh:
            fh.write(str(_last_request))
    except Exception:
        pass


def wake_tunnel():
    """Demande au service tunnel de monter le tunnel SSH.

    Le fichier "wake" est le canal entre le proxy (qui ecoute en permanence)
    et le service tunnel (qui, lui, dort pour economiser le quota GitHub).
    """
    try:
        with open(WAKE_FILE, "w") as fh:
            fh.write(str(time.time()))
    except Exception as exc:
        log("ecriture du fichier wake impossible: %r" % exc)


def read_activity():
    try:
        with open(ACTIVITY_FILE) as fh:
            return float(fh.read().strip())
    except Exception:
        return 0.0


def tunnel_alive():
    """Le tunnel est joignable si le port local du tunnel repond."""
    try:
        s = socket.create_connection((TUNNEL_HOST, TUNNEL_PORT), timeout=3)
        s.close()
        return True
    except Exception:
        return False


def wait_for_tunnel(deadline):
    """Attend que le tunnel SSH soit monte (le service tunnel le fait).

    Si le service tunnel signale un blocage (Codespace que GitHub refuse de
    demarrer, quota/billing), on rend la main immediatement au lieu de faire
    attendre Hermes pendant des minutes : un message clair vaut mieux qu'un
    timeoutmute.
    """
    last_size = -1
    while time.time() < deadline:
        if tunnel_alive():
            return True
        blocked = tunnel_blocked_reason()
        if blocked:
            log("pont bloque par GitHub: %s" % blocked)
            return False
        time.sleep(2)
    return False


def tunnel_blocked_reason():
    """Detecte un blocage du service tunnel (Codespace non demarrable)."""
    try:
        with open(TUNNEL_LOG_FILE) as fh:
            lines = fh.readlines()
    except Exception:
        return None
    for line in reversed(lines[-40:]):
        if "billing issue" in line or "HTTP 402" in line:
            return "GitHub refuse de demarrer le Codespace (quota/billing epuise)"
        if "codespace toujours indisponible" in line:
            return "le Codespace ne demarre pas"
        if "codespace pret" in line or "tunnel monte" in line:
            return None
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _proxy(self, method):
        global _tunnel_up
        touch_activity()

        # Si le tunnel n'est pas monte, on patiente : Hermes ne doit jamais
        # voir une connexion refusee (c'est ce qui produisait les
        # "APIConnectionError" a repetition).
        if not tunnel_alive():
            log("requete recue, tunnel absent -> reveil du pont")
            wake_tunnel()
            touch_activity()  # empeche l'arret automatique pendant l'attente
            deadline = time.time() + WAKE_TIMEOUT
            if not wait_for_tunnel(deadline):
                reason = tunnel_blocked_reason()
                if reason:
                    self._error(
                        503,
                        "Pont OpenCode indisponible : %s. Le quota GitHub Codespaces "
                        "est epuise (HTTP 402). Il se reinitialise au 1er du mois, ou "
                        "ajoute une carte de paiement sur le compte GitHub pour "
                        "continuer au-dela du quota gratuit." % reason,
                    )
                else:
                    self._error(504, "Le pont OpenCode n'a pas demarre dans le delai (%ss). "
                                    "Le Codespace est peut-etre bloque par GitHub (quota/billing)." % WAKE_TIMEOUT)
                return

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        headers = {}
        for key in ("Content-Type", "Accept", "Authorization", "OpenAI-Beta"):
            val = self.headers.get(key)
            if val:
                headers[key] = val

        try:
            conn = socket.create_connection((TUNNEL_HOST, TUNNEL_PORT), timeout=WAKE_TIMEOUT)
        except Exception as exc:
            self._error(502, "Tunnel indisponible: %r" % exc)
            return

        try:
            path = self.path
            req = "%s %s HTTP/1.1\r\n" % (method, path)
            req += "Host: %s:%d\r\n" % (TUNNEL_HOST, TUNNEL_PORT)
            req += "Connection: close\r\n"
            for key, val in headers.items():
                req += "%s: %s\r\n" % (key, val)
            req += "Content-Length: %d\r\n\r\n" % len(body)
            conn.sendall(req.encode() + body)

            conn.settimeout(WAKE_TIMEOUT)
            chunks = []
            while True:
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    break
                if not data:
                    break
                chunks.append(data)
            raw = b"".join(chunks)

            if not raw:
                self._error(502, "Reponse vide du pont distant")
                return

            head, _, payload = raw.partition(b"\r\n\r\n")
            status_line = head.split(b"\r\n")[0] if head else b"HTTP/1.1 502 Bad Gateway"
            try:
                status = int(status_line.split()[1])
            except Exception:
                status = 502
            resp_headers = []
            for line in head.split(b"\r\n")[1:]:
                name, _, val = line.partition(b":")
                lname = name.decode("latin-1").strip()
                if lname.lower() in ("transfer-encoding", "connection"):
                    continue
                resp_headers.append((lname, val.decode("latin-1").strip()))

            self.send_response(status)
            for name, val in resp_headers:
                self.send_header(name, val)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        touch_activity()

    def _error(self, code, message):
        payload = json.dumps({"error": {"message": message}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_PUT(self):
        self._proxy("PUT")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    srv = Server(("127.0.0.1", LOCAL_PORT), Handler)
    log("proxy a la demande demarre sur 127.0.0.1:%d -> tunnel %s:%d "
        "(arret auto du codespace apres %d min sans requete)"
        % (LOCAL_PORT, TUNNEL_HOST, TUNNEL_PORT, IDLE_STOP_MINUTES))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
