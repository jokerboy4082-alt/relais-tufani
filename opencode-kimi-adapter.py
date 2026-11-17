#!/usr/bin/env python3
"""Adaptateur Kimi Code / Hermes (OpenAI) -> OpenCode v2, mode CERVEAU.

MODE A (ce fichier) : Kimi/Hermes reste l'AGENT.
  - Kimi envoie sa demande + la liste de SES tools.
  - L adaptateur cree une session opencode v2 avec l agent "kimi-brain"
    (permissions shell/write/edit = "ask"), donc opencode NE PEUT PAS
    executer : il emet un tool_call en attente.
  - L adaptateur capte les evenements "session.tool.pending" et traduit
    chaque demande en tool_call OpenAI, renvoye a Kimi.
  - Kimi execute le tool SUR LA MACHINE DU CLIENT (le PC) et renvoie le
    resultat dans le tour suivant.

C est le mode correct : les fichiers atterrissent chez l utilisateur, et le
modele ne peut pas pretendre avoir execute une action.

Endpoints opencode v2 (decouverts, non documentees) :
  POST /api/session                     {"directory": "..."}      -> session
  POST /api/session/{id}/agent          {"agent": "kimi-brain"}
  POST /api/session/{id}/prompt         {"text": "..."}   (schema v2)
  POST /api/session/{id}/model          {"model": "..."}
  GET  /api/session/{id}/message
  GET  /api/event                        (SSE)
Auth : Authorization: Basic base64("opencode:" + password)
       password dans ~/.config/opencode/service.json
"""
import base64, http.server, json, os, sys, threading, time, urllib.error, urllib.request

OC_HOST = os.environ.get("OC_HOST", "127.0.0.1")
OC_PORT = int(os.environ.get("OC_PORT", "49374"))
OC_PASSWORD = os.environ.get("OC_PASSWORD", "")
OC_DIR = os.environ.get("OC_DIR", "/home/codespace")
OC_MODEL = os.environ.get("OC_MODEL", "opencode/crouton-alpha-free")
OC_AGENT = os.environ.get("OC_AGENT", "kimi-brain")
CHAIN_TOKEN = os.environ.get("CHAIN_TOKEN", "")
OC_TIMEOUT = int(os.environ.get("OC_TIMEOUT", "300"))
STALL_SEC = int(os.environ.get("STALL_SEC", "150"))

LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8770"))

MODEL_ALIASES = {
    "crouton-alpha-free": "opencode/crouton-alpha-free",
    "beam-free": "opencode/beam-free",
    "pulse-alpha-free": "opencode/pulse-alpha-free",
    "big-pickle": "opencode/big-pickle",
    "glm-5.3-flash-free": "opencode/glm-5.3-flash-free",
    "flint-40b-free": "opencode/flint-40b-free",
    "deepseek-v4.1-flash-free": "opencode/deepseek-v4.1-flash-free",
    "mimo-v2.6-flash-free": "opencode/mimo-v2.6-flash-free",
    "nemotron-3.5-lightning-free": "opencode/nemotron-3.5-lightning-free",
    "nemotron-3.5-super-vl-free": "opencode/nemotron-3.5-super-vl-free",
    "longcat-2.5-free": "opencode/longcat-2.5-free",
    "iceman-free": "opencode/iceman-free",
    "inkling-2-small-free": "opencode/inkling-2-small-free",
    "minimax-m3.1-flash-free": "opencode/minimax-m3.1-flash-free",
}

OC_BASE = "http://%s:%d" % (OC_HOST, OC_PORT)
BASIC = base64.b64encode(("opencode:" + OC_PASSWORD).encode()).decode()
OC_HEADERS = {"Authorization": "Basic " + BASIC, "Content-Type": "application/json"}


def log(msg):
    sys.stderr.write("[adapter] " + msg + "\n")
    sys.stderr.flush()


def oc_request(method, path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(OC_BASE + path, data=data, headers=OC_HEADERS, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read()
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw[:2000].decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read()[:600].decode("utf-8", "replace")}
    except Exception as e:
        return {"_error": "EXC", "_body": repr(e)[:300]}


# ---------------------------------------------------------------- SSE
class EventStream:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.connected = False

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def _loop(self):
        while not self.stop.is_set():
            try:
                self._run()
            except Exception:
                pass
            if self.stop.is_set():
                break
            time.sleep(3)

    def _run(self):
        req = urllib.request.Request(
            OC_BASE + "/api/event",
            headers={"Authorization": "Basic " + BASIC, "Accept": "text/event-stream"})
        try:
            r = urllib.request.urlopen(req, timeout=600)
        except Exception:
            return
        self.connected = True
        try:
            for raw in r:
                if self.stop.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload.startswith(":"):
                    continue
                try:
                    ev = json.loads(payload)
                except Exception:
                    continue
                with self.lock:
                    self.events.append(ev)
        except Exception:
            pass
        finally:
            self.connected = False


STREAM = EventStream().start()


# ---------------------------------------------------------------- mapping tools
# Deux niveaux :
#  1) le modele peut demander un tool DU CLIENT par son nom exact (Bash, Write,
#     terminal, read_file...) -> on le renvoie tel quel, les args sont deja
#     dans le bon schema (le prompt contient les schemas).
#  2) opencode peut emettre un tool interne (shell, read, write...) -> on le
#     traduit vers le tool du client equivalent.
# opencode -> client, pour chaque client :
CLIENT_TOOLS = {
    "kimi": {
        "shell": "Bash", "bash": "Bash",
        "read": "Read", "write": "Write", "edit": "Edit", "patch": "Edit",
        "glob": "Glob", "grep": "Grep",
        "webfetch": "FetchURL", "url": "FetchURL",
        "todowrite": "TodoList", "todo": "TodoList",
        "task": "Agent",
    },
    "hermes": {
        "shell": "terminal", "bash": "terminal",
        "read": "read_file", "write": "write_file", "edit": "patch", "patch": "patch",
        "glob": "search_files", "grep": "search_files", "list": "search_files",
        "webfetch": "web_extract", "websearch": "web_search",
        "todowrite": "todo_list", "task": "delegate_task",
    },
}
# arguments opencode -> arguments du tool client
CONVERT = {
    "kimi": {
        "shell": lambda i: {"command": _cmd(i), "cwd": i.get("cwd") or i.get("workdir")},
        "read": lambda i: {"path": _path(i), "line_offset": i.get("offset"),
                           "n_lines": i.get("limit")},
        "write": lambda i: {"path": _path(i), "content": i.get("content") or ""},
        "edit": lambda i: {"path": _path(i), "old_string": _first(i, "oldString", "old_string"),
                           "new_string": _first(i, "newString", "new_string"),
                           "replace_all": bool(i.get("replaceAll") or i.get("replace_all"))},
        "glob": lambda i: {"pattern": i.get("pattern") or "*", "path": i.get("path")},
        "grep": lambda i: {"pattern": i.get("pattern") or "", "path": i.get("path")},
        "webfetch": lambda i: {"url": i.get("url") or ""},
        "todowrite": lambda i: {"todos": i.get("todos") or i.get("items") or []},
        "task": lambda i: {"prompt": i.get("prompt") or i.get("description") or "",
                           "description": i.get("description") or ""},
    },
    "hermes": {
        "shell": lambda i: {"command": _cmd(i), "workdir": i.get("workdir")},
        "read": lambda i: {"path": _path(i), "offset": i.get("offset"),
                           "limit": i.get("limit")},
        "write": lambda i: {"path": _path(i), "content": i.get("content") or ""},
        "edit": lambda i: {"path": _path(i),
                           "old_string": _first(i, "oldString", "old_string"),
                           "new_string": _first(i, "newString", "new_string"),
                           "replace_all": bool(i.get("replaceAll") or i.get("replace_all"))},
        "glob": lambda i: {"pattern": i.get("pattern") or "*", "path": i.get("path"),
                           "target": "files"},
        "grep": lambda i: {"pattern": i.get("pattern") or "", "path": i.get("path"),
                           "target": "content"},
        "list": lambda i: {"pattern": "*", "path": i.get("path") or ".", "target": "files"},
        "webfetch": lambda i: {"urls": [i.get("url")] if i.get("url") else []},
        "websearch": lambda i: {"query": i.get("query") or i.get("q") or ""},
        "todowrite": lambda i: {"todos": i.get("todos") or i.get("items") or []},
        "task": lambda i: {"tasks": i.get("tasks") or []},
    },
}


def _cmd(inp):
    c = inp.get("command")
    if isinstance(c, list):
        c = " ".join(str(x) for x in c)
    return c or ""


def _path(inp):
    return inp.get("path") or inp.get("filePath") or ""


def _first(inp, *keys):
    for k in keys:
        if inp.get(k) is not None:
            return inp[k]
    return ""


def detect_client(tools):
    """Deduit le client (kimi/hermes) a partir des tools qu il a envoyes."""
    names = set()
    for t in tools or []:
        fn = t.get("function") or t
        if fn.get("name"):
            names.add(fn["name"])
    if not names:
        return "kimi", CLIENT_TOOLS["kimi"]
    if names & {"terminal", "read_file", "write_file", "execute_code", "web_extract"}:
        return "hermes", CLIENT_TOOLS["hermes"]
    if names & {"Bash", "Read", "Write", "Glob", "Grep", "FetchURL", "TodoList"}:
        return "kimi", CLIENT_TOOLS["kimi"]
    return "kimi", CLIENT_TOOLS["kimi"]


def client_tool_names(tools):
    """Noms exacts des tools du client (pour savoir si un nom demande est valide)."""
    out = set()
    for t in tools or []:
        fn = t.get("function") or t
        if fn.get("name"):
            out.add(fn["name"])
    return out


def tool_schemas(tools):
    """Schemas des tools du client : nom -> (description, props, required)."""
    out = {}
    for t in tools or []:
        fn = t.get("function") or t
        nm = fn.get("name")
        if not nm:
            continue
        params = fn.get("parameters") or {}
        out[nm] = (fn.get("description") or "", params.get("properties") or {},
                   params.get("required") or [])
    return out


def sanitize_args(args, schema):
    """Ne garde que les arguments declares dans le schema du tool du client.
    Si le schema est inconnu, on garde tout (le client validera)."""
    if not schema:
        return args
    props = schema[1]
    if not props:
        return args
    return {k: v for k, v in (args or {}).items() if k in props}


def map_tool_name(oc_name, table=None):
    """opencode -> nom de tool du client (ou None)."""
    table = table or CLIENT_TOOLS["kimi"]
    return table.get((oc_name or "").lower())


def convert_args(oc_name, inp, client="kimi"):
    """Adapte les arguments opencode au schema du tool du client."""
    inp = inp or {}
    conv = CONVERT.get(client, CONVERT["kimi"])
    fn = conv.get((oc_name or "").lower())
    if fn is None:
        return dict(inp)
    out = fn(inp)
    # on ne garde pas les valeurs None
    return {k: v for k, v in out.items() if v is not None}


def parse_xml_tool_calls(text):
    """Extrait les appels d outil du format XML demande dans le prompt.

        <tool_call name="terminal"><arg key="command">ls -la</arg></tool_call>

    Retourne (calls, texte_nettoye) avec calls = [(nom, {args})].
    """
    calls = []
    if not text:
        return calls, ""
    import re as _re
    pat = _re.compile(
        r"<tool_call\s+name=\"([^\"]+)\"\s*>(.*?)</tool_call>", _re.S | _re.I)
    arg_pat = _re.compile(r"<arg\s+key=\"([^\"]+)\"\s*>(.*?)</arg>", _re.S | _re.I)

    def unescape(s):
        out = s.replace("<", "<").replace(">", ">")
        out = out.replace(chr(34), chr(39)).replace("&apos;", chr(39))
        out = out.replace("&", "&")
        return out

    for m in pat.finditer(text):
        name = m.group(1).strip()
        if not name:
            continue
        args = {}
        for am in arg_pat.finditer(m.group(2)):
            args[am.group(1).strip()] = unescape(am.group(2).strip())
        calls.append((name, args))
    clean = pat.sub("", text).strip()
    return calls, clean


def normalize_tool_name(name, table, client):
    """Le modele peut nommer l outil comme dans le prompt (ex: terminal) ou
    comme opencode (ex: shell). On accepte les deux."""
    if not name:
        return None
    n = name.strip()
    for oc_name, client_name in table.items():
        if n == client_name:
            return oc_name
    low = n.lower()
    for oc_name, client_name in table.items():
        if low == client_name.lower():
            return oc_name
    # le modele a parfois utilise le nom opencode
    if low in table:
        return low
    return None


# ---------------------------------------------------------------- attente
def wait_brain(session_id, timeout=None, table=None, client="kimi",
               valid_names=None, schemas=None):
    """Attend la reponse du cerveau.

    Renvoie (texte, tool_calls_openai, statut).
    Les tool_calls proviennent des evenements session.tool.pending
    (permission "ask" => opencode ne les execute pas).
    """
    timeout = timeout or OC_TIMEOUT
    t0 = time.time()
    last_ev = time.time()
    texts = {}
    reasonings = {}
    pending = {}          # id -> {name, input}
    tool_names = {}       # id d appel -> nom (session.tool.input.started)
    asked = set()         # ids d appels en attente de permission
    seen = 0
    last_msg = 0.0
    table = table or CLIENT_TOOLS["kimi"]
    valid_names = valid_names or set()
    schemas = schemas or {}

    log(f"wait_brain start: session={session_id}, timeout={timeout}")

    while time.time() - t0 < timeout:
        time.sleep(0.3)
        with STREAM.lock:
            batch = STREAM.events[seen:]
            seen = len(STREAM.events)
        if batch:
            log('wait_brain: got ' + str(len(batch)) + ' events')
            last_ev = time.time()
        elif time.time() - last_ev > STALL_SEC:
            break
        for ev in batch:
            typ = ev.get("type", "")
            d = ev.get("data") or {}
            sid = d.get("sessionID") or d.get("sessionId")
            if sid and sid != session_id:
                continue
            if typ == "session.tool.input.started":
                # v2.2.5 : le nom de l outil n est QUE dans cet evenement
                if d.get("id"):
                    tool_names[d.get("id")] = d.get("name") or "tool"
            elif typ in ("permission.asked", "session.permission.asked"):
                src = d.get("source") or {}
                tid = src.get("id") or d.get("id")
                if tid:
                    asked.add(tid)
                    if tid not in pending:
                        pending[tid] = {"name": tool_names.get(tid) or d.get("action") or "tool",
                                        "input": d.get("input") or {},
                                        "raw_id": tid}
            elif typ == "session.tool.called":
                tid = d.get("id") or d.get("toolCallID") or ("t" + str(len(pending)))
                entry = {"name": d.get("name") or tool_names.get(tid) or d.get("tool") or d.get("action") or "tool",
                         "input": d.get("input") or d.get("args") or d.get("resources") or {},
                         "raw_id": tid}
                # executed=False => opencode attend une permission que personne
                # ne repondra : c est une demande au CLIENT. executed absent =>
                # ancien format, on traite aussi comme une demande.
                if d.get("executed") is False or "executed" not in d or tid in asked:
                    pending[tid] = entry
            elif typ in ("session.tool.pending", "session.tool.requested",
                         "session.tool.proposed"):
                tid = d.get("id") or d.get("toolCallID") or ("t" + str(len(pending)))
                pending[tid] = {"name": d.get("name") or tool_names.get(tid) or d.get("tool") or d.get("action") or "tool",
                                "input": d.get("input") or d.get("args") or d.get("resources") or {},
                                "raw_id": tid}
            elif typ == "session.text.delta":
                mid = d.get("assistantMessageID") or d.get("messageID") or "m"
                texts[mid] = texts.get(mid, "") + (d.get("delta") or d.get("text") or "")
            elif typ == "session.reasoning.delta":
                mid = d.get("assistantMessageID") or d.get("messageID") or "m"
                reasonings[mid] = reasonings.get(mid, "") + (
                    d.get("delta") or d.get("text") or "")

        # des tool_calls en attente => on repond IMMEDIATEMENT a Kimi
        if pending:
            calls = []
            for tid, p in pending.items():
                raw_name = p["name"]
                if raw_name in valid_names:
                    # le modele a deja demande un tool du CLIENT : on respecte
                    kname = raw_name
                    args = sanitize_args(p["input"], schemas.get(kname))
                else:
                    kname = map_tool_name(raw_name, table)
                    if not kname:
                        log("outil opencode inconnu (ignore): %s" % raw_name)
                        continue
                    args = convert_args(raw_name, p["input"], client)
                calls.append({
                    "id": "call_" + str(p["raw_id"])[-24:],
                    "type": "function",
                    "function": {"name": kname, "arguments": json.dumps(args)},
                })
            if calls:
                return ("".join(texts.values()), calls, "tool-called",
                        "".join(reasonings.values()))

        # pas de tool_call opencode : le modele a peut-etre demande un outil
        # du CLIENT directement dans son texte (format XML du prompt).
        if not pending:
            xml_calls, clean = parse_xml_tool_calls("".join(texts.values()))
            if xml_calls:
                calls = []
                for cname, cargs in xml_calls:
                    if cname in valid_names:
                        # nom deja connu du client -> on garde, args filtres
                        kname = cname
                        args = sanitize_args(cargs, schemas.get(kname))
                    else:
                        oc_name = normalize_tool_name(cname, table, client)
                        if not oc_name:
                            log("nom d outil inconnu du modele (ignore): %s" % cname)
                            continue
                        kname = table[oc_name]
                        args = convert_args(oc_name, cargs, client)
                    calls.append({
                        "id": "call_xml_%02d" % len(calls),
                        "type": "function",
                        "function": {"name": kname, "arguments": json.dumps(args)},
                    })
                if calls:
                    return (clean, calls, "xml-tool-called",
                            "".join(reasonings.values()))

        # pas de tool_call : on attend la fin (idle / execution terminee)
        if time.time() - last_msg > 2.0:
            last_msg = time.time()
            msgs = oc_request("GET", "/api/session/%s/message" % session_id, timeout=30)
            data = (msgs or {}).get("data")
            if isinstance(data, list):
                done = False
                for m in data:
                    if not isinstance(m, dict):
                        continue
                    if m.get("type") == "idle":
                        done = True
                    elif m.get("type") == "assistant":
                        content = m.get("content") or []
                        if isinstance(content, str) and content:
                            texts[m.get("id") or "m"] = content
                        else:
                            buf = []
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    buf.append(c.get("text") or "")
                            if buf:
                                texts[m.get("id") or "m"] = "".join(buf)
                if done and texts:
                    full = "".join(texts.values())
                    # derniere verification XML : le texte peut avoir ete
                    # assemble en plusieurs deltas, le bloc XML n est complet
                    # qu a la fin.
                    xml_calls, clean = parse_xml_tool_calls(full)
                    if xml_calls:
                        calls = []
                        for cname, cargs in xml_calls:
                            if cname in valid_names:
                                kname = cname
                                args = sanitize_args(cargs, schemas.get(kname))
                            else:
                                oc_name = normalize_tool_name(cname, table, client)
                                if not oc_name:
                                    continue
                                kname = table[oc_name]
                                args = convert_args(oc_name, cargs, client)
                            calls.append({
                                "id": "call_xml_%02d" % len(calls),
                                "type": "function",
                                "function": {"name": kname, "arguments": json.dumps(args)},
                            })
                        if calls:
                            return (clean, calls, "xml-tool-called",
                                    "".join(reasonings.values()))
                    return (full, [], "idle", "".join(reasonings.values()))

    return ("".join(texts.values()), [], "timeout", "".join(reasonings.values()))


# ---------------------------------------------------------------- prompt

def _example_tool(action_tools):
    """Choisit un bon exemple de bloc XML : un tool d'action reel du client,
    avec ses vrais arguments. Priorite aux outils d'action courante."""
    PRIO = [("Bash", "command", "ls -la"), ("terminal", "command", "ls -la"),
            ("Write", "path", "/tmp/fichier.txt"),
            ("write_file", "path", "/tmp/fichier.txt"),
            ("Read", "path", "/tmp/fichier.txt"),
            ("read_file", "path", "/tmp/fichier.txt")]
    names = {n for n, _d, _a in action_tools}
    for nm, arg, val in PRIO:
        if nm in names:
            return nm, [(arg, val)]
    if action_tools:
        nm, _d, _a = action_tools[0]
        return nm, [("value", "...")]
    return "Bash", [("command", "ls -la")]


def build_brain_prompt(messages, kimi_tools):
    """Construit le prompt du cerveau : demande + outils DISPONIBLES chez Kimi.

    On ne dit JAMAIS au modele qu'il peut executer : il doit DEMANDER.
    """
    parts = []
    for m in messages or []:
        role = m.get("role") or "user"
        c = m.get("content")
        if isinstance(c, str):
            txt = c
        elif isinstance(c, list):
            buf = []
            for p in c:
                if isinstance(p, dict):
                    t = p.get("type")
                    if t == "text":
                        buf.append(p.get("text") or "")
                    elif t == "tool_result":
                        buf.append("[RESULTAT D UN OUTIL]\n" + str(p.get("content") or ""))
                    elif t == "tool_use":
                        buf.append("[APPEL D OUTIL precedent] %s %s" % (
                            p.get("name"), json.dumps(p.get("input") or {})))
                else:
                    buf.append(str(p))
            txt = "\n".join(buf)
        else:
            txt = str(c or "")
        if not txt.strip():
            continue
        if role == "system":
            # On NE relaie PAS le system prompt du client : il decrit un agent
            # ("You are Kimi Code CLI... use tools"), ce qui ferait croire au
            # cerveau qu'il doit agir. On le remplace par le role cerveau.
            parts.append("[ROLE] Tu es le cerveau d'un agent qui execute les "
                         "actions pour toi (voir les instructions de demande "
                         "d outil ci-dessus).")
        elif role == "assistant":
            for tcall in (m.get("tool_calls") or []):
                fn = tcall.get("function") or {}
                parts.append("[APPEL D OUTIL DEJA EXECUTE PAR LE CLIENT] %s %s" % (
                    fn.get("name") or "?", fn.get("arguments") or ""))
            if txt.strip():
                parts.append("ASSISTANT :\n" + txt)
        elif role == "tool":
            parts.append("### RESULTAT D UN OUTIL (l'action a DEJA ete executee "
                         "par le client, voici ce qu'il a obtenu) ###\n" + txt)
        else:
            parts.append(txt)

    user_text = "\n\n".join(parts)

    # On retire les <system-reminder> auto de Kimi : ils parlent des
    # permissions du client et embrouillent le modele.
    import re as _re
    user_text = _re.sub(r"<system-reminder>.*?</system-reminder>", "", user_text, flags=_re.S).strip()

    header = ""
    if kimi_tools:
        names = []
        for t in kimi_tools:
            fn = t.get("function") or t
            nm = fn.get("name")
            if nm:
                names.append(nm)
        # On ne liste que les tools d'action : on exclut les outils de meta
        # (skills, tools, memoire) et les tools MCP (le client les gere).
        EXCLUDE = {"skills_list", "tool_describe", "tool_search", "clarify",
                   "memory", "skill_manage", "skill_view", "text_to_speech",
                   "browser_vault_list", "browser_vault_unlock",
                   "browser_vault_enter_code", "browser_vault_fill",
                   "browser_vault_save_login"}
        action_tools = []
        for t in kimi_tools:
            fn = t.get("function") or t
            nm = fn.get("name")
            if not nm or nm in EXCLUDE or nm.startswith("mcp__"):
                continue
            params = fn.get("parameters") or {}
            props = params.get("properties") or {}
            req = params.get("required") or []
            arglist = ", ".join(
                "%s%s" % (k, " (requis)" if k in req else "")
                for k in list(props.keys())[:8]) or "aucun"
            action_tools.append((nm, (fn.get("description") or "")[:150], arglist))

        lines = ["[OUTILS DISPONIBLES CHEZ LE CLIENT]",
                 "Tu ne peux PAS executer d'action toi-meme : tu es un cerveau.",
                 "Pour agir, tu DOIS demander un outil au client. Le client l'executera",
                 "sur la machine de l'utilisateur et te renverra le resultat.",
                 "",
                 "Noms d'outils a utiliser EXACTEMENT (n'invente pas d'autre nom) :"]
        for nm, desc, args in action_tools[:32]:
            lines.append("- %s : %s | args: %s" % (nm, desc, args))
        # L exemple utilise un VRAI nom de tool du client : sinon le modele
        # copie l exemple (terminal) alors que le client attend Bash.
        ex_name, ex_args = _example_tool(action_tools)
        lines += ["",
                  "Si l action demande necessite un outil, ta REPONSE doit contenir",
                  "un bloc XML de la forme exacte suivante (et rien d autre) :",
                  '<tool_call name="%s"><arg key="%s">%s</arg></tool_call>' % (
                      ex_name, ex_args[0][0], ex_args[0][1]),
                  "",
                  "Ne pretend JAMAIS avoir execute une commande, lu ou cree un fichier.",
                  "Si tu n'as pas recu de resultat d'outil, l'action n'a pas ete faite.",
                  "Si la demande est une ACTION (creer, ecrire, lire, lancer, "
                  "supprimer...), tu DOIS repondre avec le bloc XML : ne reponds "
                  "pas \"pret\" et ne demande pas de precisions.",
                  "",
                  "REGLE IMPORTANTE : si le CONTEXTE contient un bloc "
                  "### RESULTAT D UN OUTIL ###, l action a DEJA ete executee. "
                  "Dans ce cas tu ne redemandes PAS l outil : tu analyses le "
                  "resultat et tu reponds en texte (en confirmant ce qui a ete "
                  "fait).",
                  ""]
        header = "\n".join(lines)

    # Role cerveau en tete : il prime sur le reste du contexte.
    brain_role = ("[ROLE CERVEAU] Tu n es PAS un agent : tu es le cerveau d un "
                  "agent. Tu ne peux executer aucune action toi-meme. Ton unique "
                  "moyen d agir est de demander un outil au client via un bloc "
                  "XML <tool_call ...>. Ton system prompt d origine (celui du "
                  "client) decrit un agent : ignore ses instructions d exécution, "
                  "suis celles-ci.\n\n")
    return brain_role + header + user_text


# ---------------------------------------------------------------- serveur
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
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
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n > 0 else b""

    def _auth_ok(self):
        h = self.headers.get("Authorization", "")
        return h == "Bearer " + CHAIN_TOKEN

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/")
        if p.endswith("/health"):
            return self._json(200, {"ok": True, "oc": OC_BASE, "sse": STREAM.connected,
                                    "mode": "brain", "agent": OC_AGENT})
        if CHAIN_TOKEN and not self._auth_ok():
            return self._json(401, {"error": {"message": "token invalide"}})
        if p.endswith("/models"):
            data = [{"id": k, "object": "model", "owned_by": "opencode-zen",
                     "created": int(time.time())} for k in sorted(MODEL_ALIASES)]
            return self._json(200, {"object": "list", "data": data})
        return self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        raw = self._read_body()
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        p = self.path.split("?")[0].rstrip("/")
        if CHAIN_TOKEN and not self._auth_ok():
            return self._json(401, {"error": {"message": "token invalide"}})
        if p.endswith("/chat/completions"):
            return self._chat(body)
        if p.endswith("/messages"):
            return self._anthropic(body)
        return self._json(404, {"error": {"message": "not found"}})

    # ---- openai ----
    XML_OPEN = "<tool" "_call"        # debut d un bloc d appel d outil XML
    XML_CLOSE = "</tool" "_call>"     # fin d un bloc d appel d outil XML

    def _chat(self, body):
        want_stream = bool(body.get("stream"))
        model = oc_model(body.get("model"))
        kimi_tools = body.get("tools") or []
        client, table = detect_client(kimi_tools)
        valid_names = client_tool_names(kimi_tools)
        schemas = tool_schemas(kimi_tools)
        prompt = build_brain_prompt(body.get("messages"), kimi_tools)
        if not prompt.strip():
            return self._json(400, {"error": {"message": "empty prompt"}})

        sid = oc_request("POST", "/api/session", {"title": "chaine-hermes"})
        sid = ((sid or {}).get("data") or {}).get("id") if isinstance(sid, dict) else None
        if not sid:
            return self._json(502, {"error": {"message": "session creation failed"}})
        oc_request("POST", "/api/session/%s/agent" % sid, {"agent": OC_AGENT})
        _pp, _sep, _mi = model.partition("/")
        if not _sep:
            _pp, _mi = "opencode", model
        oc_request("POST", "/api/session/%s/model" % sid, {"model": {"providerID": _pp, "id": _mi}})
        oc_request("POST", "/api/session/%s/prompt" % sid, {"text": prompt})

        model_out = body.get("model") or model
        cid = "chatcmpl-" + str(int(time.time() * 1000))
        created = int(time.time())

        if want_stream:
            base = {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model_out}
            return self._chat_stream(sid, base, model=model, table=table,
                                     client=client, valid_names=valid_names,
                                     schemas=schemas)

        text, tool_calls, status, reasoning = wait_brain(
            sid, table=table, client=client, valid_names=valid_names, schemas=schemas)
        log("session=%s model=%s client=%s status=%s text=%d chars reasoning=%d tool_calls=%d" % (
            sid, model, client, status, len(text), len(reasoning), len(tool_calls)))

        if not text and not tool_calls:
            # le cerveau n a rien produit (erreur modele / timeout) : une
            # reponse 502 vaut mieux qu un message assistant vide.
            return self._json(502, {"error": {
                "message": "le cerveau n a rien repondu (statut %s)" % status}})

        msg = {"role": "assistant", "content": text}
        if reasoning:
            msg["reasoning_content"] = reasoning
        if tool_calls:
            msg["tool_calls"] = tool_calls
            msg["content"] = text or None
        return self._json(200, {
            "id": cid, "object": "chat.completion", "created": created,
            "model": model_out,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    # ---- streaming temps reel (SSE OpenAI) ----
    def _chat_stream(self, sid, base, model="", table=None, client="kimi",
                     valid_names=None, schemas=None):
        """Relaie au client les deltas du cerveau au fil de l'eau.

        - session.reasoning.delta -> chunk {"reasoning_content": ...}
        - session.text.delta      -> chunk {"content": ...} (hors blocs XML)
        - bloc XML complet        -> chunk tool_calls (format Kimi verifie)
        - fin / stall / timeout   -> finish_reason + [DONE] TOUJOURS emis,
          pour que le client ne reste jamais suspendu dans le silence.
        """
        table = table or CLIENT_TOOLS["kimi"]
        valid_names = valid_names or set()
        schemas = schemas or {}
        dead = [False]
        first_delta = [True]
        try:
            self._sse_start()
        except Exception:
            dead[0] = True

        t0 = time.time()
        last_activity = time.time()
        seen = 0
        sent = 0              # nb de caracteres du texte deja emis
        full = ""             # texte accumule (deltas texte)
        reasoning = ""
        pending = {}          # outils demandes par opencode
        tool_names = {}       # id d appel -> nom (session.tool.input.started)
        asked = set()         # ids d appels en attente de permission
        msg_texts = {}        # secours : textes finaux des messages
        prev_len = -1         # taille du texte au passage precedent
        idle_at = None
        last_poll = 0.0
        STALL = 150           # s sans aucun progres -> on conclut
        HARD = 900            # garde-fou absolu

        def send(delta, finish=None, usage=None):
            if dead[0]:
                return False
            obj = dict(base, choices=[{"index": 0, "delta": delta,
                                       "finish_reason": finish}])
            if usage is not None:
                obj["usage"] = usage
            try:
                self._sse(obj)
                return True
            except Exception:
                dead[0] = True
                return False

        def push_text(piece):
            if not piece:
                return
            d = {}
            if first_delta[0]:
                d["role"] = "assistant"
                first_delta[0] = False
            d["content"] = piece
            send(d)

        def push_reasoning(piece):
            if not piece:
                return
            d = {}
            if first_delta[0]:
                d["role"] = "assistant"
                first_delta[0] = False
            d["reasoning_content"] = piece
            send(d)

        def emit_safe():
            # n'emet que le texte hors des blocs XML ; un bloc ouvert ou a
            # moitie ecrit est retenu (il deviendra un chunk tool_calls).
            nonlocal sent
            while True:
                i = full.find(self.XML_OPEN, sent)
                if i == -1:
                    j = len(full)
                    k = min(len(self.XML_OPEN) - 1, len(full) - sent)
                    while k > 0:
                        if self.XML_OPEN[:k] == full[len(full) - k:]:
                            j = len(full) - k
                            break
                        k -= 1
                    if j > sent:
                        push_text(full[sent:j])
                        sent = j
                    return
                if i > sent:
                    push_text(full[sent:i])
                    sent = i
                e = full.find(self.XML_CLOSE, i)
                if e == -1:
                    return            # bloc encore ouvert : on garde tout
                sent = e + len(self.XML_CLOSE)

        def release_rest():
            # libere le texte retenu sauf un bloc XML encore ouvert
            nonlocal sent
            i = full.rfind(self.XML_OPEN, sent)
            limit = len(full)
            if i != -1 and full.find(self.XML_CLOSE, i) == -1:
                limit = i
            if limit > sent:
                push_text(full[sent:limit])
                sent = limit

        def build_calls(xml_calls):
            calls = []
            for tid, p in pending.items():
                raw_name = p["name"]
                if raw_name in valid_names:
                    kname = raw_name
                    args = sanitize_args(p["input"], schemas.get(kname))
                else:
                    kname = map_tool_name(raw_name, table)
                    if not kname:
                        log("outil opencode inconnu (ignore): %s" % raw_name)
                        continue
                    args = convert_args(raw_name, p["input"], client)
                calls.append({
                    "id": "call_" + str(p["raw_id"])[-24:],
                    "type": "function",
                    "function": {"name": kname, "arguments": json.dumps(args)},
                })
            for cname, cargs in xml_calls:
                if cname in valid_names:
                    kname = cname
                    args = sanitize_args(cargs, schemas.get(kname))
                else:
                    oc_name = normalize_tool_name(cname, table, client)
                    if not oc_name:
                        log("nom d outil inconnu du modele (ignore): %s" % cname)
                        continue
                    kname = table[oc_name]
                    args = convert_args(oc_name, cargs, client)
                calls.append({
                    "id": "call_xml_%02d" % len(calls),
                    "type": "function",
                    "function": {"name": kname, "arguments": json.dumps(args)},
                })
            return calls

        status = "timeout"
        calls_out = []
        while True:
            time.sleep(0.3)
            with STREAM.lock:
                batch = STREAM.events[seen:]
                seen = len(STREAM.events)
            for ev in batch:
                typ = ev.get("type", "")
                d = ev.get("data") or {}
                sidv = d.get("sessionID") or d.get("sessionId")
                if sidv and sidv != sid:
                    continue
                if typ == "session.tool.input.started":
                    # v2.2.5 : le nom de l outil n est QUE dans cet evenement
                    if d.get("id"):
                        tool_names[d.get("id")] = d.get("name") or "tool"
                    last_activity = time.time()
                elif typ in ("permission.asked", "session.permission.asked"):
                    src = d.get("source") or {}
                    tid = src.get("id") or d.get("id")
                    if tid:
                        asked.add(tid)
                        if tid not in pending:
                            pending[tid] = {"name": tool_names.get(tid) or d.get("action") or "tool",
                                            "input": d.get("input") or {},
                                            "raw_id": tid}
                    last_activity = time.time()
                elif typ == "session.tool.called":
                    tid = d.get("id") or d.get("toolCallID") or ("t" + str(len(pending)))
                    entry = {"name": d.get("name") or tool_names.get(tid) or d.get("tool") or d.get("action") or "tool",
                             "input": d.get("input") or d.get("args") or d.get("resources") or {},
                             "raw_id": tid}
                    if d.get("executed") is False or "executed" not in d or tid in asked:
                        pending[tid] = entry
                    last_activity = time.time()
                elif typ in ("session.tool.pending", "session.tool.requested",
                             "session.tool.proposed"):
                    tid = d.get("id") or d.get("toolCallID") or ("t" + str(len(pending)))
                    pending[tid] = {"name": d.get("name") or tool_names.get(tid) or d.get("tool") or d.get("action") or "tool",
                                    "input": d.get("input") or d.get("args") or d.get("resources") or {},
                                    "raw_id": tid}
                    last_activity = time.time()
                elif typ == "session.text.delta":
                    piece = d.get("delta") or d.get("text") or ""
                    if piece:
                        full += piece
                        last_activity = time.time()
                elif typ == "session.reasoning.delta":
                    piece = d.get("delta") or d.get("text") or ""
                    if piece:
                        reasoning += piece
                        push_reasoning(piece)
                        last_activity = time.time()
            emit_safe()

            # 1) opencode demande un outil au client -> reponse immediate
            calls_out = build_calls([])
            if calls_out:
                status = "tool-called"
                break

            # 2) blocs XML complets dans le texte : on attend un passage
            # stable (le modele ecrit parfois plusieurs blocs de suite)
            stable = (len(full) == prev_len)
            prev_len = len(full)
            if full and stable:
                xml_calls, _clean = parse_xml_tool_calls(full)
                calls_out = build_calls(xml_calls)
                if calls_out:
                    status = "xml-tool-called"
                    break

            if dead[0]:
                status = "client-gone"
                break

            # 3) fin de session (idle) : poll des messages, comme wait_brain
            if time.time() - last_poll > 2.0:
                last_poll = time.time()
                msgs = oc_request("GET", "/api/session/%s/message" % sid, timeout=30)
                data = (msgs or {}).get("data")
                if isinstance(data, list):
                    for m in data:
                        if not isinstance(m, dict):
                            continue
                        if m.get("type") == "idle":
                            if idle_at is None:
                                idle_at = time.time()
                        elif m.get("type") == "assistant":
                            content = m.get("content") or []
                            key = m.get("id") or "m"
                            if isinstance(content, str) and content:
                                if msg_texts.get(key) != content:
                                    msg_texts[key] = content
                                    last_activity = time.time()
                            elif isinstance(content, list):
                                buf = []
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        buf.append(c.get("text") or "")
                                if buf:
                                    t2 = "".join(buf)
                                    if msg_texts.get(key) != t2:
                                        msg_texts[key] = t2
                                        last_activity = time.time()
            if idle_at is not None and (full or msg_texts
                                        or time.time() - idle_at > 4.0):
                status = "idle"
                break

            # 4) securites : plus aucun progres, ou garde-fou absolu
            if time.time() - last_activity > STALL:
                status = "stall-timeout"
                break
            if time.time() - t0 > HARD:
                status = "hard-timeout"
                break

        # conclusion : dernier passage d extraction des blocs XML
        if status not in ("tool-called", "xml-tool-called"):
            if not full and msg_texts:
                full = "".join(msg_texts.values())
                emit_safe()
            if full:
                xml_calls, _clean = parse_xml_tool_calls(full)
                calls_out = build_calls(xml_calls)
                if calls_out:
                    status = "xml-tool-called"

        if not calls_out and not full and not reasoning and status != "client-gone":
            push_text("(Le cerveau n'a rien repondu (statut %s) — reessaie.)" % status)

        if calls_out:
            # Format UNIQUE verifie empiriquement (2026-11-06) : un seul chunk
            # avec "role":"assistant" + "tool_calls", puis le chunk de
            # finish_reason. Kimi IGNORE le tool_call si le chunk qui le porte
            # n'a pas "role":"assistant", ou si un chunk "content" vide precede.
            for i, tc in enumerate(calls_out):
                send({"role": "assistant",
                      "tool_calls": [{"index": i, "id": tc["id"], "type": "function",
                                      "function": {"name": tc["function"]["name"],
                                                   "arguments": tc["function"]["arguments"]}}]})
            send({}, "tool_calls")
        else:
            release_rest()
            send({}, "stop")
        send({}, None, usage={"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0})
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception:
            pass
        try:
            self.wfile.close()
        except Exception:
            pass
        self.close_connection = True
        log("stream session=%s model=%s client=%s status=%s text=%d chars reasoning=%d calls=%d" % (
            sid, model, client, status, len(full), len(reasoning), len(calls_out)))

    # ---- anthropic ----
    def _anthropic(self, body):
        model = oc_model(body.get("model"))
        prompt = build_brain_prompt(body.get("messages"), [])
        sid = oc_request("POST", "/api/session", {"title": "chaine-hermes"})
        sid = ((sid or {}).get("data") or {}).get("id") if isinstance(sid, dict) else None
        if not sid:
            return self._json(502, {"error": {"type": "api_error", "message": "session failed"}})
        oc_request("POST", "/api/session/%s/agent" % sid, {"agent": OC_AGENT})
        _pp, _sep, _mi = model.partition("/")
        if not _sep:
            _pp, _mi = "opencode", model
        oc_request("POST", "/api/session/%s/model" % sid, {"model": {"providerID": _pp, "id": _mi}})
        oc_request("POST", "/api/session/%s/prompt" % sid, {"text": prompt})
        text, tool_calls, status, _reasoning = wait_brain(sid)
        content = []
        if text:
            content.append({"type": "text", "text": text})
        for tc in tool_calls:
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception:
                args = {}
            content.append({"type": "tool_use", "id": tc["id"].replace("call_", "toolu_"),
                            "name": tc["function"]["name"], "input": args})
        if not content:
            content = [{"type": "text", "text": ""}]
        return self._json(200, {
            "id": "msg_" + str(int(time.time() * 1000)), "type": "message",
            "role": "assistant", "model": body.get("model") or model,
            "content": content,
            "stop_reason": "tool_use" if tool_calls else "end_turn",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })


def oc_model(name):
    if not name:
        return OC_MODEL
    n = name.split("/")[-1]
    if n in MODEL_ALIASES:
        return MODEL_ALIASES[n]
    if name in MODEL_ALIASES.values():
        return name
    return OC_MODEL


if __name__ == "__main__":
    if not OC_PASSWORD:
        sys.exit("OC_PASSWORD manquant")
    srv = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log("ecoute %s:%d -> opencode %s (MODE CERVEAU, agent=%s, model=%s)" % (
        LISTEN_HOST, LISTEN_PORT, OC_BASE, OC_AGENT, OC_MODEL))
    srv.serve_forever()
