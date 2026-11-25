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

LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8771"))

# Catalogue servi a Hermes (verifie sur opencode v2.4.1).
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
    arg_pattern = re.compile(r"<arg\s+key=\"([^\"]+)\"\s*>(.*?)(?:</arg>|(?=<arg)|$)", re.S | re.I)

    for match in pattern.finditer(text):
        name = match.group(1).strip()
        args = {}
        for arg_match in arg_pattern.finditer(match.group(2)):
            value = arg_match.group(2).strip()
            # nettoie les balises residuelles
            value = re.sub(r"</?arg[^>]*>", "", value).strip()
            args[arg_match.group(1).strip()] = value
        calls.append((name, args))
    clean = re.sub(r"<tool_call.*?(?:</tool_call>|(?=<tool_call)|$)", "", text, flags=re.S | re.I).strip()
    clean = re.sub(r"<arg[^>]*>.*?(?:</arg>|(?=<arg)|$)", "", clean, flags=re.S | re.I).strip()
    return calls, clean


def normalize_tool_name(name):
    table = CLIENT_TOOLS["hermes"]
    if not name:
        return None
    candidate = name.strip()
    for oc_name, client_name in table.items():
        if candidate == client_name:
            return oc_name
    low = candidate.lower()
    for oc_name, client_name in table.items():
        if low == client_name.lower():
            return oc_name
    if low in table:
        return low
    return None


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


def oc_model(name):
    if not name:
        return OC_MODEL
    if name in MODELS:
        return name
    bare = name.split("/")[-1]
    for model in MODELS:
        if model.split("/")[-1] == bare:
            return model
    return OC_MODEL


# ---------------------------------------------------------------- execution
def extract_text(messages):
    """Retourne (texte, tool_calls_openai, statut) depuis les messages OpenCode."""
    texts = []
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
                if part.get("type") == "text" and part.get("text"):
                    texts.append(part["text"])
                elif part.get("type") == "tool":
                    tool_parts.append(part)
        elif kind == "idle":
            outcome = msg.get("outcome")
    full = "\n".join(texts)
    xml_calls, clean = parse_xml_tool_calls(full)
    calls = []
    for name, args in xml_calls:
        oc_name = normalize_tool_name(name)
        if not oc_name:
            continue
        calls.append(
            {
                "id": "call_xml_%02d" % len(calls),
                "type": "function",
                "function": {
                    "name": CLIENT_TOOLS["hermes"][oc_name],
                    "arguments": json.dumps(convert_args(oc_name, args)),
                },
            }
        )
    return clean, calls, outcome or "idle"


def run_session(prompt, model):
    provider_id = model.split("/")[0]
    model_id = model.split("/")[-1]
    model_ref = {"providerID": provider_id, "id": model_id}

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
        return "", [], "session-failed", (created or {})

    # Le modele est deja pose a la creation ; on le reaffirme pour être sûr.
    oc_request("POST", "/api/session/%s/model" % session_id, {"model": model_ref}, timeout=60)

    queued = oc_request("POST", "/api/session/%s/prompt" % session_id, {"text": prompt}, timeout=60)
    if queued and "_error" in queued:
        return "", [], "prompt-failed", queued

    deadline = time.time() + OC_TIMEOUT
    while time.time() < deadline:
        listed = oc_request(
            "GET", "/api/session/%s/message?order=asc&limit=100" % session_id, timeout=60
        )
        messages = (listed or {}).get("data") or []
        idle = [m for m in messages if isinstance(m, dict) and m.get("type") == "idle"]
        if idle:
            text, calls, outcome = extract_text(messages)
            if outcome == "failed":
                err = None
                for msg in idle:
                    if msg.get("error"):
                        err = msg["error"]
                return text, calls, "failed", err
            return text, calls, outcome or "idle", None
        time.sleep(1.0)

    listed = oc_request("GET", "/api/session/%s/message?order=asc&limit=100" % session_id, timeout=60)
    messages = (listed or {}).get("data") or []
    text, calls, outcome = extract_text(messages)
    return text, calls, outcome or "timeout", None


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
            return self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": m, "object": "model", "owned_by": "opencode-v2", "created": int(time.time())}
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
        model = oc_model(body.get("model"))
        tools = body.get("tools") or []
        valid_names = client_tool_names(tools)
        schemas = tool_schemas(tools)
        prompt = build_prompt(body.get("messages"), tools)
        if not prompt.strip():
            return self._json(400, {"error": {"message": "empty prompt"}})

        text, calls, outcome, err = run_session(prompt, model)
        log("model=%s outcome=%s chars=%d calls=%d" % (model, outcome, len(text), len(calls)))

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
                    "shell ni aux fichiers. Tu dois repondre EXCLUSIVEMENT par un "
                    "bloc XML <tool_call ...> pour demander l'action au client."
                )
                text2, calls2, outcome2, err2 = run_session(retry_prompt, model)
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

        if outcome == "session-failed":
            return self._json(502, {"error": {"message": "opencode session failed", "detail": err}})
        if outcome == "prompt-failed":
            return self._json(502, {"error": {"message": "opencode prompt failed", "detail": err}})
        if outcome == "failed":
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
        model = oc_model(body.get("model"))
        prompt = build_prompt(body.get("messages"), [])
        text, calls, outcome, err = run_session(prompt, model)
        content = []
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
