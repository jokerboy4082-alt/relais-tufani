#!/usr/bin/env python3
"""Adaptateur Hermes (OpenAI) -> OpenCode v2 (Codespace opencode-v2).

MODE CERVEAU : le modele ne peut rien executer. Il demande une action au
client (Hermes) via un bloc XML <tool_call ...>. Hermes execute l'action sur
la machine de l'utilisateur et renvoie le resultat au tour suivant.

Endpoints OpenCode v2 (API officielle) :
  POST /api/session                         -> {"data": {"id": ...}}
  POST /api/session/{id}/model              -> {"model": {...}}
  POST /api/session/{id}/prompt             -> {"text": "..."} (file d'attente)
  GET  /api/session/{id}/message            -> {"data": [...], "cursor": {...}}
  POST /api/experimental/session/{id}/wait  -> 204 quand le agent est idle

Auth : Basic base64("opencode:" + mot de passe), ou Bearer token.
"""
import base64
import http.server
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

OC_HOST = os.environ.get("OC_HOST", "127.0.0.1")
OC_PORT = int(os.environ.get("OC_PORT", "49374"))
OC_PASSWORD = os.environ.get("OC_PASSWORD", "")
OC_TOKEN = os.environ.get("OC_TOKEN", "")
OC_DIR = os.environ.get("OC_DIR", "/home/codespace/opencode-v2")
OC_MODEL = os.environ.get("OC_MODEL", "opencode/beam-free")
OC_AGENT = os.environ.get("OC_AGENT", "hermes-brain")
OC_TIMEOUT = int(os.environ.get("OC_TIMEOUT", "300"))

# Rotation d'IP Tor (quota free OpenCode indexe sur l'IP de sortie).
# Le ControlPort de l'instance Tor dediee du PC est expose en 19051 cote
# Codespace par le tunnel SSH inverse ; le cookie d'auth (hex) est pousse
# par le tunnel local au demarrage.
TOR_CONTROL_PORT = int(os.environ.get("TOR_CONTROL_PORT", "19051"))
TOR_COOKIE_FILE = os.environ.get("TOR_COOKIE_FILE", "/home/codespace/opencode-v2/tor_cookie.hex")
TOR_ROTATE_ON_QUOTA = os.environ.get("TOR_ROTATE_ON_QUOTA", "1") == "1"
QUOTA_MAX_ROTATIONS = int(os.environ.get("QUOTA_MAX_ROTATIONS", "2"))

LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8771"))

# Catalogue servi a Hermes (verifie sur opencode v2.4.1).
# tidepool-alpha-free est RETIRE : il echoue en 502 sur le pont text-only
# (modele vision cote opencode.ai, l'agent cerveau ne passe pas d'image).
MODELS = [
    "opencode/beam-free",
    "opencode/big-pickle",
    "opencode/deepseek-v4.1-flash-free",
    "opencode/flint-40b-free",
    "opencode/hy4-flash-free",
    "opencode/iceman-free",
    "opencode/mimo-v2.6-flash-free",
    "opencode/nemotron-3.5-lightning-free",
    "opencode/nemotron-3.5-super-vl-free",
    "opencode/seaglass-alpha-free",
    "opencode/tidepool-alpha-free",
]

OC_BASE = "http://%s:%d" % (OC_HOST, OC_PORT)
if OC_TOKEN:
    OC_HEADERS = {
        "Authorization": "Bearer " + OC_TOKEN,
        "Content-Type": "application/json",
    }
else:
    _basic = base64.b64encode(("opencode:" + OC_PASSWORD).encode()).decode()
    OC_HEADERS = {
        "Authorization": "Basic " + _basic,
        "Content-Type": "application/json",
    }


def log(msg):
    sys.stderr.write("[opencode-v2] " + str(msg) + "\n")
    sys.stderr.flush()


def is_quota_error(err):
    """Vrai si l'erreur OpenCode est un epuisement de quota free (429)."""
    if not err:
        return False
    blob = json.dumps(err) if not isinstance(err, str) else err
    blob = blob.lower()
    return (
        "free-limit" in blob
        or "freeusagelimiterror" in blob
        or "free usage limit" in blob
        or "quota" in blob
    )


def rotate_tor_ip():
    """Force un nouveau circuit Tor via le ControlPort expose (19051).

    Le quota free OpenCode est indexe sur l'IP de sortie : un nouveau circuit
    change l'IP et debloque. Le ControlPort est sur le PC (instance Tor
    dediee) ; le tunnel SSH inverse l'expose en 19051 cote Codespace. Le
    cookie d'auth (hex ASCII) a ete pousse par le tunnel local.
    """
    try:
        with open(TOR_COOKIE_FILE, "r") as fh:
            cookie_hex = fh.read().strip().replace("\n", "").replace("\r", "").replace(" ", "")
    except Exception as exc:
        log("rotation Tor impossible: cookie illisible (%r)" % exc)
        return False
    if not cookie_hex:
        log("rotation Tor impossible: cookie vide")
        return False
    try:
        sock = socket.create_connection(("127.0.0.1", TOR_CONTROL_PORT), timeout=15)
    except Exception as exc:
        log("rotation Tor impossible: ControlPort %d injoignable (%r)" % (TOR_CONTROL_PORT, exc))
        return False
    try:
        sock.settimeout(15)
        sock.sendall(b"AUTHENTICATE " + cookie_hex.encode("ascii") + b"\r\n")
        auth = sock.recv(1024).decode("ascii", "replace").strip()
        if not auth.startswith("250"):
            log("rotation Tor: authentification refusee (%s)" % auth)
            return False
        sock.sendall(b"SIGNAL NEWNYM\r\n")
        resp = sock.recv(1024).decode("ascii", "replace").strip()
        if not resp.startswith("250"):
            log("rotation Tor: SIGNAL NEWNYM refuse (%s)" % resp)
            return False
        log("rotation Tor: nouveau circuit demande")
        return True
    except Exception as exc:
        log("rotation Tor: erreur (%r)" % exc)
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def oc_request(method, path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        OC_BASE + path, data=data, headers=OC_HEADERS, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {"_status": resp.status}
            try:
                return json.loads(raw)
            except Exception:
                return {"_raw": raw[:2000].decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        body = exc.read()[:600].decode("utf-8", "replace")
        return {"_error": exc.code, "_body": body}
    except Exception as exc:
        return {"_error": "EXC", "_body": repr(exc)[:300]}


# ---------------------------------------------------------------- tools
CLIENT_TOOLS = {
    "hermes": {
        "shell": "terminal",
        "bash": "terminal",
        "read": "read_file",
        "write": "write_file",
        "edit": "patch",
        "patch": "patch",
        "glob": "search_files",
        "grep": "search_files",
        "list": "search_files",
        "webfetch": "web_extract",
        "websearch": "web_search",
        "todowrite": "todo_list",
        "task": "delegate_task",
    },
}

CONVERT = {
    "hermes": {
        "shell": lambda i: {"command": _cmd(i), "workdir": i.get("cwd") or i.get("workdir")},
        "read": lambda i: {"path": _path(i), "offset": i.get("offset"), "limit": i.get("limit")},
        "write": lambda i: {"path": _path(i), "content": i.get("content") or ""},
        "edit": lambda i: {
            "path": _path(i),
            "old_string": _first(i, "oldString", "old_string"),
            "new_string": _first(i, "newString", "new_string"),
            "replace_all": bool(i.get("replaceAll") or i.get("replace_all")),
        },
        "glob": lambda i: {"pattern": i.get("pattern") or "*", "path": i.get("path")},
        "grep": lambda i: {"pattern": i.get("pattern") or "", "path": i.get("path")},
        "list": lambda i: {"pattern": "*", "path": i.get("path") or ".", "target": "files"},
        "webfetch": lambda i: {"urls": [i.get("url")] if i.get("url") else []},
        "websearch": lambda i: {"query": i.get("query") or i.get("q") or ""},
        "todowrite": lambda i: {"todos": i.get("todos") or i.get("items") or []},
        "task": lambda i: {"tasks": i.get("tasks") or []},
    },
}


def _cmd(inp):
    cmd = inp.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    return cmd or ""


def _path(inp):
    return inp.get("path") or inp.get("filePath") or ""


def _first(inp, *keys):
    for key in keys:
        if inp.get(key) is not None:
            return inp[key]
    return ""


def detect_client(tools):
    names = set()
    for tool in tools or []:
        fn = tool.get("function") or tool
        if fn.get("name"):
            names.add(fn["name"])
    if names & {"terminal", "read_file", "write_file", "execute_code", "web_extract"}:
        return "hermes"
    return "hermes"


def client_tool_names(tools):
    out = set()
    for tool in tools or []:
        fn = tool.get("function") or tool
        if fn.get("name"):
            out.add(fn["name"])
    return out


def tool_schemas(tools):
    out = {}
    for tool in tools or []:
        fn = tool.get("function") or tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        out[name] = (
            fn.get("description") or "",
            params.get("properties") or {},
            params.get("required") or [],
        )
    return out


def sanitize_args(args, schema):
    if not schema:
        return args
    props = schema[1]
    if not props:
        return args
    return {k: v for k, v in (args or {}).items() if k in props}


def map_tool_name(oc_name):
    return CLIENT_TOOLS["hermes"].get((oc_name or "").lower())


def convert_args(oc_name, inp):
    fn = CONVERT["hermes"].get((oc_name or "").lower())
    if fn is None:
        return dict(inp or {})
    out = fn(inp or {})
    return {k: v for k, v in out.items() if v is not None}


def parse_xml_tool_calls(text):
    """Extrait les appels d'outil du format XML demande dans le prompt.

        <tool_call name="terminal"><arg key="command">ls -la</arg></tool_call>

    Le modele coupe parfois la balise fermante : on accepte aussi la forme
    ouverte (le dernier <arg> est alors pris en compte).

    Retourne (calls, texte_nettoye) avec calls = [(nom, {args})].
    """
    calls = []
    if not text:
        return calls, ""
    pattern = re.compile(
        r"<tool_call\s+name=\"([^\"]+)\"\s*>(.*?)(?:</tool_call>|(?=<tool_call)|$)",
        re.S | re.I,
    )
    # L'argument se termine sur </arg>, la balise suivante <arg>, OU une
    # balise de fin mal nommee par le modele (</rfl:parameter>, </tool_call>).
    arg_pattern = re.compile(
        r"<arg\s+key=\"([^\"]+)\"\s*>(.*?)(?:</arg>|(?=<arg)|(?=</rfl:)|(?=</tool_call>)|$)",
        re.S | re.I,
    )

    for match in pattern.finditer(text):
        name = match.group(1).strip()
        args = {}
        for arg_match in arg_pattern.finditer(match.group(2)):
            value = arg_match.group(2).strip()
            # nettoie les balises residuelles : </arg>, mais aussi les balises
            # de fin mal nommees par le modele (</rfl:parameter>, </tool_call>, ...).
            value = re.sub(r"</?arg[^>]*>", "", value)
            value = re.sub(r"</?rfl:[^>]*>", "", value)
            value = re.sub(r"</?tool_call[^>]*>", "", value)
            value = re.sub(r"^\s*</tool_call>\s*$", "", value)
            value = value.strip()
            args[arg_match.group(1).strip()] = value
        calls.append((name, args))
    clean = re.sub(r"<tool_call.*?(?:</tool_call>|(?=<tool_call)|$)", "", text, flags=re.S | re.I).strip()
    clean = re.sub(r"<arg[^>]*>.*?(?:</arg>|(?=<arg)|$)", "", clean, flags=re.S | re.I).strip()
    return calls, clean


def normalize_tool_name(name, valid_names=None):
    """Resout le nom d'outil demande par le modele vers un nom d'outil Hermes.

    Priorite :
      1. la table de correspondance modele-opencode -> Hermes (read -> read_file,
         shell -> terminal, write -> write_file, edit -> patch, ...) : c'est
         elle qui garantit que le nom final existe COTE HERMES ;
      2. le nom demande tel quel, s'il fait partie des outils que Hermes a
         reellement envoyes dans la requete (memory, skill_view, ...) ;
      3. None si l'outil est inconnu (l'appel est alors ignore).

    ``valid_names`` est l'ensemble des noms d'outils reels de la requete.
    """
    table = CLIENT_TOOLS["hermes"]
    if not name:
        return None
    candidate = name.strip()
    # 1) Table OpenCode -> Hermes (prioritaire : le nom final doit exister
    #    cote Hermes, sinon l'appel echoue).
    for oc_name, client_name in table.items():
        if candidate == client_name:
            return oc_name
    low = candidate.lower()
    for oc_name, client_name in table.items():
        if low == client_name.lower():
            return oc_name
    if low in table:
        return low
    # 2) Outil Hermes direct (memory, skill_view, ...) : on garde le nom.
    if valid_names:
        for tool_name in valid_names:
            if candidate == tool_name:
                return candidate
    return None


def hermes_tool_name(oc_name, valid_names=None):
    """Nom FINAL du tool a appeler chez Hermes.

    Pour les outils de la table (read, shell, ...) on renvoie le nom Hermes
    correspondant. Pour un outil Hermes direct, on renvoie son propre nom.
    """
    table = CLIENT_TOOLS["hermes"]
    if oc_name in table:
        return table[oc_name]
    if valid_names and oc_name in valid_names:
        return oc_name
    return table.get((oc_name or "").lower(), oc_name)


def convert_args(oc_name, inp):
    fn = CONVERT["hermes"].get((oc_name or "").lower())
    if fn is None:
        return dict(inp or {})
    out = fn(inp or {})
    return {k: v for k, v in out.items() if v is not None}


_ACTION_PATTERNS = (
    r"\bcr[ée]e?\b",
    r"\bcr[ée]er\b",
    r"\b[ée]cris\b",
    r"\b[ée]crire\b",
    r"\blance?\b",
    r"\blancer\b",
    r"\bex[ée]cute?\b",
    r"\bex[ée]cuter\b",
    r"\bsupprime?\b",
    r"\bsupprimer\b",
    r"\binstalle?\b",
    r"\binstaller\b",
    r"\blis\b",
    r"\blire\b",
    r"\bopen\b",
    r"\bopens?\b",
    r"\bwrite\b",
    r"\bcreate\b",
    r"\brun\b",
    r"\bexecute\b",
    r"\bdelete\b",
    r"\bremove\b",
    r"\binstall\b",
    r"\bread\b",
    r"\bfais\b",
    r"\bfait\b",
)


_FALSE_CLAIM_PATTERNS = (
    r"j['’]ai\s+(?:cre[eé]|[ée]crit|ex[ée]cut[ée]|lanc[ée]|supprim[ée]|install[ée]|pu\s+(?:cr[ée]er|[ée]crire|ex[ée]cuter|lancer))",
    r"c['’]est\s+(?:fait|cr[ée][ée]|termin[ée]|install[ée]|ex[ée]cut[ée]|supprim[ée]|fait)",
    r"v[ée]rification\s+effectu[ée]e",
    r"avec\s+succ[èe]s",
    r"le\s+fichier\s+.*?\s+(?:a\s+)?[ée]t[ée]\s+cr[ée][ée]",
    r"fichier\s+.*?\s+cr[ée][ée]",
    r"ex[ée]cut[ée]\s+avec",
    r"bien\s+(?:cr[ée][ée]|fait|lanc[ée]|install[ée])",
    r"r[ée]ussi\s+(?:cr[ée]er|[ée]crire|ex[ée]cuter|lancer)",
    r"op[ée]ration\s+effectu[ée]e",
)


def _contains_false_execution_claim(text):
    lowered = (text or "").lower()
    return any(re.search(pattern, lowered) for pattern in _FALSE_CLAIM_PATTERNS)


def _looks_like_action_request(text):
    """Detecte une demande d'action sur le systeme de fichiers / shell."""
    lowered = (text or "").lower()
    return any(re.search(pattern, lowered) for pattern in _ACTION_PATTERNS)


def build_prompt(messages, tools):
    parts = []
    action_requested = False
    for msg in messages or []:
        role = msg.get("role") or "user"
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            buf = []
            for part in content:
                if isinstance(part, dict):
                    kind = part.get("type")
                    if kind == "text":
                        buf.append(part.get("text") or "")
                    elif kind == "tool_result":
                        buf.append("[RESULTAT D UN OUTIL]\n" + str(part.get("content") or ""))
                    elif kind == "tool_use":
                        buf.append(
                            "[APPEL D OUTIL PRECEDENT] %s %s"
                            % (part.get("name"), json.dumps(part.get("input") or {}))
                        )
                else:
                    buf.append(str(part))
            text = "\n".join(buf)
        else:
            text = str(content or "")
        if not text.strip():
            continue
        if role == "system":
            parts.append(
                "[ROLE] Tu es le cerveau d'un agent qui execute les actions pour toi "
                "(voir les instructions de demande d'outil ci-dessous)."
            )
        elif role == "assistant":
            parts.append("ASSISTANT :\n" + text)
        elif role == "tool":
            parts.append(
                "### RESULTAT D UN OUTIL (l'action a DEJA ete executee par le client) ###\n"
                + text
            )
        else:
            parts.append(text)
            action_requested = _looks_like_action_request(text)

    user_text = "\n\n".join(parts)
    user_text = re.sub(r"<system-reminder>.*?</system-reminder>", "", user_text, flags=re.S).strip()

    lines = [
        "[OUTILS DISPONIBLES CHEZ LE CLIENT]",
        "Tu ne peux PAS executer d'action toi-meme : tu es un cerveau.",
        "Pour agir, tu DOIS demander un outil au client. Le client l'executera",
        "sur la machine de l'utilisateur et te renverra le resultat.",
        "",
        "Noms d'outils a utiliser EXACTEMENT (n'invente pas d'autre nom) :",
    ]
    action_tools = []
    for tool in tools or []:
        fn = tool.get("function") or tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        required = params.get("required") or []
        arglist = (
            ", ".join("%s%s" % (k, " (requis)" if k in required else "") for k in list(props.keys())[:8])
            or "aucun"
        )
        action_tools.append((name, (fn.get("description") or "")[:150], arglist))

    for name, desc, args in action_tools[:40]:
        lines.append("- %s : %s | args: %s" % (name, desc, args))

    example_name, example_args = _example_tool(action_tools)
    lines += [
        "",
        "Si l'action demande necessite un outil, ta REPONSE doit contenir",
        "un bloc XML de la forme exacte suivante (et rien d'autre) :",
        '<tool_call name="%s"><arg key="%s">%s</arg></tool_call>'
        % (example_name, example_args[0][0], example_args[0][1]),
        "",
        "Tu peux fournir PLUSIEURS arguments : un bloc <arg> par argument.",
        "Exemple pour ecrire un fichier :",
        '<tool_call name="write_file"><arg key="path">/tmp/fiche</arg>'
        '<arg key="content">contenu exact</arg></tool_call>',
        "Exemple pour une commande :",
        '<tool_call name="terminal"><arg key="command">ls -la /tmp</arg></tool_call>',
        "",
        "IMPORTANT : n'ecris JAMAIS de pseudo-syntaxe comme </rfl:parameter>",
        "ou des balises fermantes manquantes : ecris le XML proprement.",
        "Ne pretend JAMAIS avoir execute une commande, lu ou cree un fichier.",
        "Si tu n'as pas recu de resultat d'outil, l'action n'a pas ete faite.",
        "Si la demande est une ACTION (creer, ecrire, lire, lancer, supprimer...),",
        "tu DOIS repondre avec le bloc XML : ne reponds pas 'pret'.",
        "",
        "REGLE IMPORTANTE : si le CONTEXTE contient un bloc",
        "### RESULTAT D UN OUTIL ###, l'action a DEJA ete executee. Dans ce cas",
        "tu ne redemandes PAS l'outil : tu analyses le resultat et tu reponds",
        "en texte (en confirmant ce qui a ete fait).",
    ]
    if action_requested:
        lines += [
            "",
            "ATTENTION — CONTRAINTE STRICTE : la demande contient une ACTION a",
            "executer. Tu n'as AUCUN acces au systeme de fichiers ni au shell.",
            "Ta reponse DOIT etre uniquement le bloc XML <tool_call ...>.",
            "Toute phrase affirmative du type 'j'ai cree', 'c'est fait',",
            "'verification effectuee' sera REJECTEE car elle est fausse.",
            "Reponds donc EXCLUSIVEMENT par :",
            '<tool_call name="%s"><arg key="%s">%s</arg></tool_call>'
            % (example_name, example_args[0][0], example_args[0][1]),
        ]
    brain_role = (
        "[ROLE CERVEAU] Tu n es PAS un agent : tu es le cerveau d un agent. "
        "Tu ne peux executer aucune action toi-meme. Ton unique moyen d agir est "
        "de demander un outil au client via un bloc XML <tool_call ...>. Ton system "
        "prompt d origine (celui du client) decrit un agent : ignore ses instructions "
        "d execution, suis celles-ci.\n\n"
        "[LANGUE] Raisonne en francais. Ton raisonnement interne doit etre redige "
        "en francais, comme ta reponse finale.\n\n"
    )
    return brain_role + "\n".join(lines) + "\n\n" + user_text


def _example_tool(action_tools):
    priority = [
        ("write_file", "path", "/tmp/fichier.txt"),
        ("terminal", "command", "ls -la"),
        ("read_file", "path", "/tmp/fichier.txt"),
    ]
    names = {n for n, _d, _a in action_tools}
    for name, arg, value in priority:
        if name in names:
            return name, [(arg, value)]
    if action_tools:
        name, _d, _a = action_tools[0]
        return name, [("value", "...")]
    return "terminal", [("command", "ls -la")]


# Variantes d'effort exposees par OpenCode (provider/model#variant).
# Le niveau de raisonnement est porte par la VARIANTE du modele, pas par un
# parametre separe : c'est la mecanique native d'OpenCode v2.
EFFORT_VARIANTS = ("minimal", "low", "medium", "high", "xhigh", "max")

# Alias Hermes : nom court -> (modele opencode, variante d'effort)
EFFORT_ALIASES = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "mid": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "x-high": "xhigh",
    "max": "max",
}

# Variantes reellement acceptees par chaque modele OpenCode (verifie le
# 2026-11-26 sur /api/model). Un modele sans variante n'accepte pas d'effort :
# lui en envoyer une fait echouer la requete. Les variantes non supportees
# sont donc repliees sur la meilleure variante disponible.
MODEL_VARIANTS = {
    "opencode/beam-free": ["high", "low", "max", "medium", "xhigh"],
    "opencode/deepseek-v4.1-flash-free": ["high", "low", "max"],
    "opencode/flint-40b-free": ["high", "low", "max", "medium", "xhigh"],
    "opencode/hy4-flash-free": ["high", "none"],
    "opencode/nemotron-3.5-super-vl-free": ["high", "low", "medium", "minimal", "none", "xhigh"],
    "opencode/seaglass-alpha-free": ["high", "low", "max", "medium", "minimal", "xhigh"],
    "opencode/tidepool-alpha-free": ["high", "low", "max", "medium", "minimal", "xhigh"],
}

# Ordre de preference pour replier une variante non supportee.
_VARIANT_FALLBACK = ("max", "xhigh", "high", "medium", "low", "minimal")


def resolve_variant(model, variant):
    """Replie la variante demandee sur une variante acceptee par le modele.

    Retourne None si le modele n'accepte aucune variante d'effort : le modele
    sera alors utilise tel quel (son effort par defaut).
    """
    supported = MODEL_VARIANTS.get(model)
    if supported is None:
        # Modele inconnu du catalogue fige : on ne force rien (pas de variante).
        return None
    if not supported:
        return None
    if variant and variant in supported:
        return variant
    for candidate in _VARIANT_FALLBACK:
        if candidate in supported:
            return candidate
    return None


def _requested_effort(body):
    """Extrait le niveau d'effort demande par Hermes dans le corps de la requete.

    Hermes peut l'exprimer de plusieurs facons selon le transport :
      - extra_body.reasoning.effort  (transport chat_completions standard)
      - reasoning_effort             (niveau racine, ex. Kimi / TokenHub)
      - extra_body.thinking.effort
    On normalise vers les variantes OpenCode v2.
    """
    candidates = []

    def _walk(node, depth=0):
        if depth > 4 or not isinstance(node, dict):
            return
        for key in ("effort", "reasoning_effort"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip().lower())
        for key in ("reasoning", "thinking", "extra_body"):
            if key in node:
                _walk(node[key], depth + 1)

    _walk(body)

    # Le corps peut aussi porter reasoning_effort a la racine.
    root_effort = body.get("reasoning_effort")
    if isinstance(root_effort, str) and root_effort.strip():
        candidates.append(root_effort.strip().lower())

    for value in candidates:
        if value == "none":
            return "minimal"
        mapped = EFFORT_ALIASES.get(value)
        if mapped:
            return mapped
    return None


def split_variant(name):
    """Separe ``provider/model#variant`` en (modele canonique, variante).

    Retourne (modele, None) si aucune variante n'est demandee.
    """
    if not name:
        return OC_MODEL, None
    base, sep, variant = name.partition("#")
    if not sep:
        return oc_model(base), None
    variant = variant.strip().lower()
    if variant not in EFFORT_VARIANTS:
        return oc_model(base), None
    return oc_model(base), variant


def oc_model(name):
    """Resout le nom de modele vers son id canonique OpenCode."""
    if not name:
        return OC_MODEL
    # On ignore un eventuel #variant pour la resolution de l'id.
    base = name.split("#")[0]
    if base in MODELS:
        return base
    bare = base.split("/")[-1]
    for model in MODELS:
        if model.split("/")[-1] == bare:
            return model
    return OC_MODEL


# ---------------------------------------------------------------- execution
def _native_tool_calls(tool_parts, valid_names):
    """Convertit les parties `tool` natives d'OpenCode en tool_calls OpenAI.

    OpenCode emet ses appels d'outils comme des parties ``{"type": "tool",
    "name": ..., "state": {"input": {...}}}``. Quand la permission de l'agent
    est ``deny``/``ask``, l'appel reste en attente (``executed: false``) :
    c'est exactement l'appel que le CLIENT (Hermes) doit executer chez lui.
    On les convertit donc en tool_calls OpenAI.
    """
    calls = []
    for part in tool_parts or []:
        if not isinstance(part, dict):
            continue
        name = part.get("name") or ""
        state = part.get("state") or {}
        status = state.get("status")
        # Une partie deja executee par OpenCode (completed + executed=true)
        # n'est PAS un appel a repasser au client : son resultat est deja connu.
        if part.get("executed") is True and status == "completed":
            continue
        if status == "error":
            continue
        raw_input = state.get("input")
        if isinstance(raw_input, str):
            try:
                raw_input = json.loads(raw_input)
            except Exception:
                raw_input = {"value": raw_input} if raw_input else {}
        if not isinstance(raw_input, dict):
            raw_input = {}

        oc_name = normalize_tool_name(name, valid_names)
        if not oc_name:
            continue
        calls.append(
            {
                "id": part.get("id") or "call_native_%02d" % len(calls),
                "type": "function",
                "function": {
                    "name": hermes_tool_name(oc_name, valid_names),
                    "arguments": json.dumps(convert_args(oc_name, raw_input)),
                },
            }
        )
    return calls


def extract_text(messages, valid_names=None):
    """Retourne (texte, tool_calls_openai, statut, reasoning) depuis les messages OpenCode.

    OpenCode expose le raisonnement dans un bloc ``{"type": "reasoning", "text": ...}``
    avec ``native.reasoningField == "reasoning_content"``. Hermes (client
    OpenAI-compatible) attend ce texte dans le champ ``reasoning_content`` de la
    reponse : on le recovere pour qu'il soit affiche.

    Le modele ecrit parfois le bloc XML ``<tool_call>`` dans le RAISONNEMENT
    plutot que dans la reponse finale (il "pense" l'appel au lieu de
    l'emettre). On le cherche donc dans les deux : un appel d'outil place dans
    le raisonnement doit quand meme etre execute par Hermes.

    ``valid_names`` = noms des outils que Hermes a reellement envoyes.
    """
    texts = []
    reasoning_parts = []
    tool_parts = []
    outcome = None
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        kind = msg.get("type")
        if kind == "assistant":
            for part in msg.get("content") or []:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "text" and part.get("text"):
                    texts.append(part["text"])
                elif part_type == "reasoning":
                    if part.get("text"):
                        reasoning_parts.append(part["text"])
                elif part_type == "tool":
                    tool_parts.append(part)
        elif kind == "idle":
            outcome = msg.get("outcome")
    full = "\n".join(texts)
    reasoning = "\n".join(reasoning_parts).strip()

    # 0) Appels d'outils NATIFS d'OpenCode (parties "tool") : c'est le
    #    mecanisme principal. L'agent "cerveau" a les actions en deny/ask,
    #    donc l'appel reste en attente : c'est Hermes qui doit l'executer.
    calls = _native_tool_calls(tool_parts, valid_names)

    # 1) Bloc XML dans la reponse finale (cas nominal du protocole XML).
    xml_calls, clean = parse_xml_tool_calls(full)
    # 2) Bloc XML dans le raisonnement (le modele "pense" l'appel) : on le
    #    recupere aussi, sinon l'action demandee n'est jamais executee.
    if not xml_calls and reasoning:
        reasoning_calls, _ = parse_xml_tool_calls(reasoning)
        xml_calls = reasoning_calls

    for name, args in xml_calls:
        oc_name = normalize_tool_name(name, valid_names)
        if not oc_name:
            continue
        calls.append(
            {
                "id": "call_xml_%02d" % len(calls),
                "type": "function",
                "function": {
                    "name": hermes_tool_name(oc_name, valid_names),
                    "arguments": json.dumps(convert_args(oc_name, args)),
                },
            }
        )
    return clean, calls, outcome or "idle", reasoning


def run_session(prompt, model, variant=None, valid_names=None, _rotation=0):
    provider_id = model.split("/")[0]
    model_id = model.split("/")[-1]
    model_ref = {"providerID": provider_id, "id": model_id}
    # La variante porte le niveau d'effort de raisonnement chez OpenCode v2.
    if variant:
        model_ref["variant"] = variant

    created = oc_request(
        "POST",
        "/api/session",
        {
            "location": {"directory": OC_DIR},
            "agent": OC_AGENT,
            "model": model_ref,
        },
        timeout=60,
    )
    session_id = ((created or {}).get("data") or {}).get("id")
    if not session_id:
        # Le quota peut tomber dès la création de session (429 renvoyé par
        # l'API). Meme traitement : rotation Tor + retry.
        if (
            TOR_ROTATE_ON_QUOTA
            and is_quota_error(created)
            and _rotation < QUOTA_MAX_ROTATIONS
        ):
            log("quota free epuise a la creation de session -> rotation Tor (%d/%d)"
                % (_rotation + 1, QUOTA_MAX_ROTATIONS))
            if rotate_tor_ip():
                time.sleep(8)
                return run_session(prompt, model, variant, valid_names, _rotation + 1)
        return "", [], "session-failed", (created or {}), ""

    # Le modele est deja pose a la creation ; on le reaffirme pour être sûr.
    oc_request("POST", "/api/session/%s/model" % session_id, {"model": model_ref}, timeout=60)

    queued = oc_request("POST", "/api/session/%s/prompt" % session_id, {"text": prompt}, timeout=60)
    if queued and "_error" in queued:
        if (
            TOR_ROTATE_ON_QUOTA
            and is_quota_error(queued)
            and _rotation < QUOTA_MAX_ROTATIONS
        ):
            log("quota free epuise a l'envoi du prompt -> rotation Tor (%d/%d)"
                % (_rotation + 1, QUOTA_MAX_ROTATIONS))
            if rotate_tor_ip():
                time.sleep(8)
                return run_session(prompt, model, variant, valid_names, _rotation + 1)
        return "", [], "prompt-failed", queued, ""

    deadline = time.time() + OC_TIMEOUT
    while time.time() < deadline:
        listed = oc_request(
            "GET", "/api/session/%s/message?order=asc&limit=100" % session_id, timeout=60
        )
        messages = (listed or {}).get("data") or []
        idle = [m for m in messages if isinstance(m, dict) and m.get("type") == "idle"]
        if idle:
            text, calls, outcome, reasoning = extract_text(messages, valid_names)
            if outcome == "failed":
                err = None
                for msg in idle:
                    if msg.get("error"):
                        err = msg["error"]
                # Quota free epuise (429) : on force un nouveau circuit Tor
                # (nouvelle IP de sortie) et on retente la requete.
                if (
                    TOR_ROTATE_ON_QUOTA
                    and is_quota_error(err)
                    and _rotation < QUOTA_MAX_ROTATIONS
                ):
                    log("quota free epuise -> rotation Tor + retry (%d/%d)"
                        % (_rotation + 1, QUOTA_MAX_ROTATIONS))
                    if rotate_tor_ip():
                        # Laisse Tor etablir le nouveau circuit avant de reessayer.
                        time.sleep(8)
                        return run_session(
                            prompt, model, variant, valid_names, _rotation + 1
                        )
                return text, calls, "failed", err, reasoning
            return text, calls, outcome or "idle", None, reasoning
        time.sleep(1.0)

    listed = oc_request("GET", "/api/session/%s/message?order=asc&limit=100" % session_id, timeout=60)
    messages = (listed or {}).get("data") or []
    text, calls, outcome, reasoning = extract_text(messages, valid_names)
    return text, calls, outcome or "timeout", None, reasoning


# ---------------------------------------------------------------- serveur
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.flush()
        except Exception:
            pass

    def _sse(self, obj):
        self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
        self.wfile.flush()

    def _read_body(self):
        size = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(size) if size > 0 else b""

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/models"):
            # Catalogue au format OpenRouter : Hermes y lit
            # ``supported_parameters`` / ``reasoning`` pour savoir que la route
            # accepte un controle de raisonnement, et donc envoyer l'effort
            # demande par l'utilisateur (medium / high / max).
            # Les niveaux proposes sont ceux que le modele accepte vraiment.
            def _efforts_for(model_id):
                supported = MODEL_VARIANTS.get(model_id)
                if supported is None:
                    return list(EFFORT_VARIANTS)
                return [v for v in supported if v != "none"] or list(EFFORT_VARIANTS)

            return self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": m,
                            "object": "model",
                            "owned_by": "opencode-v2",
                            "created": int(time.time()),
                            "supported_parameters": ["reasoning", "tools", "tool_choice", "temperature"],
                            "reasoning": {
                                "supported_efforts": _efforts_for(m),
                                "mandatory": False,
                            },
                        }
                        for m in MODELS
                    ],
                },
            )
        if path.endswith("/health"):
            probe = oc_request("GET", "/api/info", timeout=8)
            healthy = "_error" not in (probe or {})
            return self._json(
                200 if healthy else 503,
                {
                    "ok": healthy,
                    "provider": "opencode-v2",
                    "codespace": "opencode-v2-4j9j447xp7v435x76",
                    "oc": OC_BASE,
                    "model": OC_MODEL,
                    "error": (probe or {}).get("_body"),
                },
            )
        return self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        raw = self._read_body()
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/chat/completions"):
            return self._chat(body)
        if path.endswith("/messages"):
            return self._anthropic(body)
        return self._json(404, {"error": {"message": "not found"}})

    def _chat(self, body):
        want_stream = bool(body.get("stream"))
        model, variant = split_variant(body.get("model"))
        # Hermes peut demander un niveau d'effort : soit via le nom du modele
        # (#high), soit via extra_body.reasoning.effort / reasoning_effort.
        # Chez OpenCode v2, l'effort est une VARIANTE du modele.
        if not variant:
            variant = _requested_effort(body)
        # On replie la variante sur ce que le modele accepte vraiment : sinon
        # la requete echoue (variant=minimal sur beam-free, par exemple).
        variant = resolve_variant(model, variant)
        tools = body.get("tools") or []
        valid_names = client_tool_names(tools)
        schemas = tool_schemas(tools)
        log("outils recus de Hermes (%d): %s" % (len(valid_names), sorted(valid_names)))
        prompt = build_prompt(body.get("messages"), tools)
        if not prompt.strip():
            return self._json(400, {"error": {"message": "empty prompt"}})

        text, calls, outcome, err, reasoning = run_session(prompt, model, variant, valid_names)
        log("model=%s variant=%s outcome=%s chars=%d calls=%d reasoning=%d chars"
            % (model, variant or "-", outcome, len(text), len(calls), len(reasoning)))

        # Garde-fou : si la demande portait sur une action et que le modele
        # affirme l'avoir executee sans passer par un outil, on le rappelle
        # une fois (relance corrective) puis on refuse de relayer ce mensonge.
        last_user = ""
        if body.get("messages"):
            last = body["messages"][-1]
            if isinstance(last, dict) and isinstance(last.get("content"), str):
                last_user = last["content"]
        if not calls and _looks_like_action_request(last_user):
            if _contains_false_execution_claim(text):
                log("FURIEUX fausse affirmation d'execution -> relance corrective")
                retry_prompt = (
                    prompt
                    + "\n\n[CORRECTION] Ta reponse precedente affirme une execution "
                    "que tu n'as pas pu faire : c'est FAUX. Tu n'as aucun acces au "
                    "shell ni aux fichiers de l'utilisateur. Pour demander une "
                    "action au client, appelle l'outil correspondant (read, "
                    "terminal, write_file, ...) ou reponds par un bloc XML "
                    "<tool_call ...>. N'affirme jamais avoir execute une commande "
                    "ou cree un fichier."
                )
                text2, calls2, outcome2, err2, reasoning2 = run_session(retry_prompt, model, variant, valid_names)
                log("relance corrective: outcome=%s chars=%d calls=%d" % (outcome2, len(text2), len(calls2)))
                if calls2:
                    text, calls = text2, calls2
                elif not _contains_false_execution_claim(text2):
                    text, calls = text2, calls2
                else:
                    return self._json(
                        502,
                        {
                            "error": {
                                "message": "opencode a affirme une execution sans outil : reponse refusee",
                                "detail": text2[:500],
                            }
                        },
                    )
                if reasoning2:
                    reasoning = reasoning2

        if outcome == "session-failed":
            if is_quota_error(err):
                return self._json(
                    429,
                    {
                        "error": {
                            "message": "Quota free OpenCode epuise (rotation Tor deja tentee). "
                                       "Reessaie apres le reset du quota.",
                            "detail": err,
                        }
                    },
                )
            return self._json(502, {"error": {"message": "opencode session failed", "detail": err}})
        if outcome == "prompt-failed":
            if is_quota_error(err):
                return self._json(
                    429,
                    {
                        "error": {
                            "message": "Quota free OpenCode epuise (rotation Tor deja tentee). "
                                       "Reessaie apres le reset du quota.",
                            "detail": err,
                        }
                    },
                )
            return self._json(502, {"error": {"message": "opencode prompt failed", "detail": err}})
        if outcome == "failed":
            if is_quota_error(err):
                return self._json(
                    429,
                    {
                        "error": {
                            "message": "Quota free OpenCode epuise (rotation Tor deja tentee). "
                                       "Reessaie apres le reset du quota.",
                            "detail": err,
                        }
                    },
                )
            return self._json(
                502,
                {"error": {"message": "opencode run failed", "detail": err}},
            )

        model_out = body.get("model") or model
        cid = "chatcmpl-" + str(int(time.time() * 1000))
        created = int(time.time())

        if want_stream:
            self._sse_start()
            base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_out}
            # Le raisonnement arrive d'abord, dans un delta dedie : c'est le
            # format qu'attend Hermes (delta.reasoning_content).
            if reasoning:
                self._sse(
                    dict(
                        base,
                        choices=[
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "reasoning_content": reasoning},
                                "finish_reason": None,
                            }
                        ],
                    )
                )
            if calls:
                for index, call in enumerate(calls):
                    self._sse(
                        dict(
                            base,
                            choices=[
                                {
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "tool_calls": [
                                            {
                                                "index": index,
                                                "id": call["id"],
                                                "type": "function",
                                                "function": {
                                                    "name": call["function"]["name"],
                                                    "arguments": call["function"]["arguments"],
                                                },
                                            }
                                        ],
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        )
                    )
                self._sse(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]))
            else:
                self._sse(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": text or " "}, "finish_reason": None}]))
                self._sse(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]))
            self._sse(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": None}], usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            try:
                self.wfile.close()
            except Exception:
                pass
            self.close_connection = True
            return

        msg = {"role": "assistant", "content": text}
        if reasoning:
            msg["reasoning_content"] = reasoning
        if calls:
            msg["tool_calls"] = calls
            msg["content"] = text or None
        return self._json(
            200,
            {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model_out,
                "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )

    def _anthropic(self, body):
        model, variant = split_variant(body.get("model"))
        if not variant:
            variant = _requested_effort(body)
        variant = resolve_variant(model, variant)
        valid_names = client_tool_names(body.get("tools") or [])
        prompt = build_prompt(body.get("messages"), body.get("tools") or [])
        text, calls, outcome, err, reasoning = run_session(prompt, model, variant, valid_names)
        content = []
        # Le protocole Anthropic attend un bloc "thinking" pour le raisonnement.
        if reasoning:
            content.append({"type": "thinking", "thinking": reasoning, "signature": ""})
        if text:
            content.append({"type": "text", "text": text})
        for call in calls:
            try:
                args = json.loads(call["function"]["arguments"])
            except Exception:
                args = {}
            content.append(
                {
                    "type": "tool_use",
                    "id": call["id"].replace("call_", "toolu_"),
                    "name": call["function"]["name"],
                    "input": args,
                }
            )
        if not content:
            content = [{"type": "text", "text": ""}]
        return self._json(
            200,
            {
                "id": "msg_" + str(int(time.time() * 1000)),
                "type": "message",
                "role": "assistant",
                "model": body.get("model") or model,
                "content": content,
                "stop_reason": "tool_use" if calls else "end_turn",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        )


if __name__ == "__main__":
    if not OC_PASSWORD and not OC_TOKEN:
        sys.exit("OC_PASSWORD ou OC_TOKEN manquant")
    server = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log(
        "ecoute %s:%d -> opencode %s (mode cerveau, agent=%s, model=%s)"
        % (LISTEN_HOST, LISTEN_PORT, OC_BASE, OC_AGENT, OC_MODEL)
    )
    server.serve_forever()
