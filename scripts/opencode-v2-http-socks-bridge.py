#!/bin/bash
# Pont HTTP -> SOCKS5 pour faire passer le trafic Node/OpenCode par Tor.
#
# OpenCode (Bun/Node) respecte HTTP_PROXY/HTTPS_PROXY mais ne parle pas SOCKS5.
# Ce petit serveur accepte les requetes HTTP CONNECT (HTTPS) et les requetes
# HTTP simples, puis les relaie via un proxy SOCKS5 (Tor).
#
# Il tourne DANS le Codespace et pointe vers le SocksPort Tor du PC, atteignable
# par le tunnel SSH inverse (-R).
#
# Usage:
#    http_proxy_bridge.py --listen 127.0.0.1:8899 --socks 127.0.0.1:19050
#    http_proxy_bridge.py --listen 127.0.0.1:8899 --socks 127.0.0.1:19050 \
#        --rotate-cmd "/home/codespace/opencode-v2/rotate-tor.sh"
#
# Quand une requete echoue (le CDN opencode.ai refuse car le quota free de
# l'IP de sortie est epuise), le pont peut demander une rotation d'IP Tor
# avant de reessayer : le quota est par IP, une nouvelle IP repart a zero.
import argparse
import select
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

BUF = 65536
ROTATE_LOCK = threading.Lock()


def socks5_connect(socks_addr, host, port, timeout=30):
    """Ouvre un tunnel SOCKS5 vers host:port, renvoie la socket."""
    s = socket.create_connection(socks_addr, timeout=timeout)
    s.settimeout(timeout)
    s.sendall(b"\x05\x01\x00")
    resp = s.recv(2)
    if len(resp) != 2 or resp[0] != 0x05:
        s.close()
        raise OSError("SOCKS5 handshake failed")
    try:
        host_b = socket.inet_aton(host)
        atyp = b"\x01"
    except OSError:
        host_b = host.encode("idna")
        atyp = b"\x03"
    req = b"\x05\x01\x00" + atyp
    if atyp == b"\x01":
        req += host_b + struct.pack("!H", port)
    else:
        req += bytes([len(host_b)]) + host_b + struct.pack("!H", port)
    s.sendall(req)
    head = s.recv(4)
    if len(head) != 4 or head[1] != 0x00:
        s.close()
        raise OSError("SOCKS5 connect refused (code=%s)" % (head[1] if len(head) > 1 else "?"))
    if head[3] == 0x01:
        s.recv(4 + 2)
    elif head[3] == 0x03:
        n = s.recv(1)[0]
        s.recv(n + 2)
    else:
        s.recv(16 + 2)
    return s


def pipe(a, b):
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 60)
            if x:
                break
            if not r:
                break
            for s in r:
                other = b if s is a else a
                data = s.recv(BUF)
                if not data:
                    return
                other.sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


class Handler(socketserver.StreamRequestHandler):
    timeout = 120

    def handle(self):
        try:
            request_line = self.rfile.readline(65536)
        except Exception:
            return
        if not request_line:
            return
        try:
            method, target, _version = request_line.decode("latin-1").split()
        except ValueError:
            return

        headers = []
        while True:
            line = self.rfile.readline(65536)
            if not line or line in (b"\r\n", b"\n"):
                break
            headers.append(line)

        if method.upper() == "CONNECT":
            host, _, port_s = target.partition(":")
            try:
                port = int(port_s)
            except ValueError:
                port = 443
            remote = None
            for attempt in range(2):
                try:
                    remote = socks5_connect(self.server.socks_addr, host, port)
                    break
                except Exception as exc:
                    self.server.log("CONNECT %s:%s echec %r" % (host, port, exc))
                    if attempt == 0 and self.server.rotate_cmd:
                        self.server.log("quota/echec detecte -> rotation d'IP Tor")
                        self.server.rotate_ip()
                        time.sleep(8)
            if remote is None:
                try:
                    self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                except Exception:
                    pass
                return
            try:
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
            except Exception:
                return
            self.server.log("CONNECT %s:%s OK" % (host, port))
            pipe(self.connection, remote)
            return

        if target.startswith("http://"):
            rest = target[len("http://"):]
            hostport, _, path = rest.partition("/")
            host, _, port_s = hostport.partition(":")
            try:
                port = int(port_s) if port_s else 80
            except ValueError:
                port = 80
            path = "/" + path
            remote = None
            for attempt in range(2):
                try:
                    remote = socks5_connect(self.server.socks_addr, host, port)
                    break
                except Exception as exc:
                    self.server.log("HTTP %s echec %r" % (hostport, exc))
                    if attempt == 0 and self.server.rotate_cmd:
                        self.server.rotate_ip()
                        time.sleep(8)
            if remote is None:
                return
            req = "%s %s HTTP/1.1\r\n" % (method, path)
            req += "".join(h.decode("latin-1") for h in headers)
            req += "\r\n"
            remote.sendall(req.encode("latin-1"))
            while True:
                try:
                    data = remote.recv(BUF)
                except Exception:
                    break
                if not data:
                    break
                try:
                    self.wfile.write(data)
                except Exception:
                    break
            remote.close()
            return

        try:
            self.wfile.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        except Exception:
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, socks_addr, rotate_cmd=None, verbose=True):
        self.socks_addr = socks_addr
        self.rotate_cmd = rotate_cmd
        self.verbose = verbose
        super().__init__(addr, Handler)

    def log(self, msg):
        if self.verbose:
            sys.stderr.write("[http-socks-bridge] %s\n" % msg)
            sys.stderr.flush()

    def rotate_ip(self):
        """Force un nouveau circuit Tor → nouvelle IP de sortie."""
        if not self.rotate_cmd:
            return
        with ROTATE_LOCK:
            try:
                out = subprocess.run(
                    self.rotate_cmd, shell=True, capture_output=True, text=True, timeout=180
                )
                self.log("rotation IP: %s" % (out.stdout or out.stderr).strip()[:200])
            except Exception as exc:
                self.log("rotation IP echoue: %r" % exc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:8899")
    ap.add_argument("--socks", default="127.0.0.1:19050")
    ap.add_argument("--rotate-cmd", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    lhost, _, lport = args.listen.rpartition(":")
    shost, _, sport = args.socks.rpartition(":")
    server = Server(
        (lhost, int(lport)),
        (shost, int(sport)),
        rotate_cmd=args.rotate_cmd or None,
        verbose=not args.quiet,
    )
    sys.stderr.write(
        "[http-socks-bridge] ecoute %s -> socks5 %s (rotation: %s)\n"
        % (args.listen, args.socks, args.rotate_cmd or "desactivee")
    )
    sys.stderr.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
