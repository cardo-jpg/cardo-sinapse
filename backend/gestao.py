import os
import re
import json
import uuid
import hmac
import hashlib
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent
DB_PATH  = BASE_DIR / "data" / "gestao.db"
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))

SECRET_KEY = os.getenv("SECRET_KEY", "sinapse-secret-2026")
USERS = {
    "victor": os.getenv("USER_VICTOR_PASSWORD", "C@rdobrain2026"),
    "jose":   os.getenv("USER_JOSE_PASSWORD",   "C@rdosinapse2026"),
}
USER_DISPLAY = {"victor": "Victor", "jose": "José"}

STATUSES = [
    {"id": "aberto",    "name": "Aberto",       "color": "#6b7280"},
    {"id": "progresso", "name": "Em progresso",  "color": "#3b82f6"},
    {"id": "revisao",   "name": "Em revisão",    "color": "#f59e0b"},
    {"id": "concluido", "name": "Concluído",     "color": "#22c55e"},
]
MEMBERS = [
    {"id": "victor", "name": "Victor"},
    {"id": "jose",   "name": "José"},
]


def _user_exists(username: str) -> bool:
    if username in USERS:
        return True
    try:
        con = sqlite3.connect(BASE_DIR / "data" / "users.db")
        row = con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        con.close()
        return row is not None
    except Exception:
        return False

def _verify(request: Request) -> Optional[str]:
    cookie = request.cookies.get("session", "")
    if ":" not in cookie:
        return None
    username, sig = cookie.split(":", 1)
    expected = hmac.new(SECRET_KEY.encode(), username.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected) and _user_exists(username):
        return username
    return None


def _require(request: Request) -> str:
    user = _verify(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")
    return user


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS spaces (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                color TEXT DEFAULT '#ff4d00',
                icon TEXT DEFAULT '⚡',
                position INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                name TEXT NOT NULL,
                position INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (space_id) REFERENCES spaces(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS lists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                space_id TEXT NOT NULL,
                folder_id TEXT,
                position INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (space_id) REFERENCES spaces(id) ON DELETE CASCADE,
                FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                list_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                assignees TEXT DEFAULT '[]',
                due_date TEXT,
                status TEXT DEFAULT 'aberto',
                priority TEXT DEFAULT 'normal',
                parent_task_id TEXT,
                position INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (list_id) REFERENCES lists(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'Sem título',
                content TEXT DEFAULT '',
                space_id TEXT NOT NULL,
                folder_id TEXT,
                position INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (space_id) REFERENCES spaces(id) ON DELETE CASCADE,
                FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS features (
                id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                url TEXT NOT NULL,
                label TEXT NOT NULL,
                icon TEXT DEFAULT '📋',
                position INTEGER DEFAULT 0,
                FOREIGN KEY (space_id) REFERENCES spaces(id) ON DELETE CASCADE
            );
        """)
        # migrations — add columns that may not exist in older DBs
        for sql in [
            "ALTER TABLE folders ADD COLUMN color          TEXT    DEFAULT '#6b7280'",
            "ALTER TABLE lists   ADD COLUMN color          TEXT    DEFAULT '#6b7280'",
            "ALTER TABLE lists   ADD COLUMN icon           TEXT    DEFAULT ''",
            "ALTER TABLE spaces  ADD COLUMN custom_statuses TEXT   DEFAULT ''",
            "ALTER TABLE spaces  ADD COLUMN archived       INTEGER DEFAULT 0",
            "ALTER TABLE folders ADD COLUMN archived       INTEGER DEFAULT 0",
            "ALTER TABLE lists   ADD COLUMN archived       INTEGER DEFAULT 0",
            "ALTER TABLE spaces  ADD COLUMN permissions    TEXT    DEFAULT ''",
            "ALTER TABLE folders ADD COLUMN permissions    TEXT    DEFAULT ''",
            "ALTER TABLE lists   ADD COLUMN permissions    TEXT    DEFAULT ''",
            "ALTER TABLE documents ADD COLUMN parent_id   TEXT",
            "ALTER TABLE features  ADD COLUMN folder_id   TEXT",
            "ALTER TABLE lists     ADD COLUMN custom_fields TEXT DEFAULT '[]'",
            "ALTER TABLE tasks     ADD COLUMN extra_data  TEXT DEFAULT '{}'",
            """CREATE TABLE IF NOT EXISTS document_versions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id     TEXT NOT NULL,
                title      TEXT NOT NULL DEFAULT '',
                content    TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
            )""",
        ]:
            try:
                conn.execute(sql); conn.commit()
            except Exception:
                pass

        if conn.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 0:
            _seed(conn)

        # seed features if table is empty
        if conn.execute("SELECT COUNT(*) FROM features").fetchone()[0] == 0:
            _seed_features(conn)

        conn.commit()


def _seed(conn):
    now = datetime.now().isoformat()

    spaces = [
        ("gestao",      "Gestão",      "#19196c", "⚙️",  0),
        ("operacional", "Operacional", "#ff4d00", "🔧", 1),
        ("comercial",   "Comercial",   "#22c55e", "💼", 2),
    ]
    for sid, name, color, icon, pos in spaces:
        conn.execute("INSERT INTO spaces (id,name,color,icon,position,created_at) VALUES (?,?,?,?,?,?)", (sid, name, color, icon, pos, now))

    folders = [
        ("op_trafego",  "operacional", "Tráfego Pago",    0),
        ("op_cs",       "operacional", "Customer Success", 1),
        ("op_conteudo", "operacional", "Conteúdo",         2),
        ("op_auto",     "operacional", "Automações",       3),
        ("gest_equipe", "gestao",      "Equipe",           0),
    ]
    for fid, sid, name, pos in folders:
        conn.execute("INSERT INTO folders (id,space_id,name,position,created_at) VALUES (?,?,?,?,?)", (fid, sid, name, pos, now))

    lists = [
        ("list_traf_tarefas", "Tarefas Pontuais",        "operacional", "op_trafego",  0),
        ("list_traf_pend",    "Pendências Clientes",      "operacional", "op_trafego",  1),
        ("list_cs_atend",     "Atendimento & CS",         "operacional", "op_cs",       0),
        ("list_cont_copys",   "Copys e Redação",          "operacional", "op_conteudo", 0),
        ("list_cont_plan",    "Planejamento de Conteúdo", "operacional", "op_conteudo", 1),
        ("list_auto",         "Tarefas de Automação",     "operacional", "op_auto",     0),
        ("list_gest_reu",     "Reuniões Internas",        "gestao",      "gest_equipe", 0),
    ]
    for lid, name, sid, fid, pos in lists:
        conn.execute("INSERT INTO lists (id,name,space_id,folder_id,position,created_at) VALUES (?,?,?,?,?,?)", (lid, name, sid, fid, pos, now))

    tasks = [
        ("list_traf_tarefas", "Configurar campanha Google Ads – Hire Brazil", '["victor"]', "2026-04-30", "progresso", "high"),
        ("list_traf_tarefas", "Revisar criativos Meta Ads – Scale Army",      '["jose"]',   "2026-04-25", "aberto",   "normal"),
        ("list_traf_tarefas", "Análise de performance mensal – Grupo NF",     '["victor"]', "2026-04-28", "revisao",  "normal"),
        ("list_traf_pend",    "Aguardando aprovação de verba – PV",            '["jose"]',   "2026-04-23", "aberto",   "high"),
        ("list_traf_pend",    "Acesso à conta Google Ads – SRW",              '["victor"]', "2026-04-26", "aberto",   "normal"),
        ("list_cs_atend",     "Follow-up reunião PV – proposta renovação",    '["jose"]',   "2026-04-24", "aberto",   "high"),
        ("list_cs_atend",     "Enviar relatório mensal – Conexão Cirúrgica",  '["victor"]', "2026-04-27", "aberto",   "normal"),
        ("list_cont_copys",   "Copy campanha Hire – WICI3",                   '["jose"]',   "2026-05-02", "aberto",   "low"),
        ("list_cont_copys",   "Roteiro vídeo institucional – SRW",            '["victor"]', "2026-05-05", "aberto",   "normal"),
        ("list_auto",         "Webhook notificações de leads – Scale Army",   '["victor"]', "2026-05-10", "aberto",   "normal"),
    ]
    for i, (lid, title, assignees, due, status, priority) in enumerate(tasks):
        conn.execute(
            "INSERT INTO tasks (id,list_id,title,description,assignees,due_date,status,priority,parent_task_id,position,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), lid, title, "", assignees, due, status, priority, None, i, now),
        )


def _seed_features(conn):
    rows = [
        ("feat_dashboard", "operacional", "/trafego", "Dashboards",     "📊", 0),
        ("feat_ata",       "operacional", "/ata",      "Gerador de Atas","📝", 1),
    ]
    for fid, sid, url, label, icon, pos in rows:
        conn.execute(
            "INSERT OR IGNORE INTO features (id,space_id,url,label,icon,position) VALUES (?,?,?,?,?,?)",
            (fid, sid, url, label, icon, pos),
        )
    conn.commit()


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/gestao", response_class=HTMLResponse)
async def page_gestao(request: Request):
    user = _verify(request)
    if not user:
        return RedirectResponse("/login")
    _ADMIN = {"victor"}
    _FP    = {"victor", "jadna"}
    resp = templates.TemplateResponse("gestao.html", {
        "request": request,
        "nav_username": USER_DISPLAY.get(user, user.capitalize()),
        "nav_user": user,
        "nav_is_admin": user in _ADMIN,
        "nav_fin_pessoais": user in _FP,
        "active_page": "gestao",
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


def _can_see(item: dict, username: str) -> bool:
    p = item.get("permissions") or ""
    if not p: return True
    try: return username in json.loads(p)
    except: return True


@router.get("/api/gestao/tree")
async def api_tree(request: Request):
    username = _require(request)
    with get_db() as conn:
        spaces   = [dict(r) for r in conn.execute("SELECT * FROM spaces  WHERE archived=0 ORDER BY position, name")]
        folders  = [dict(r) for r in conn.execute("SELECT * FROM folders WHERE archived=0 ORDER BY position, name")]
        lists    = [dict(r) for r in conn.execute("SELECT * FROM lists   WHERE archived=0 ORDER BY position, name")]
        docs     = [dict(r) for r in conn.execute("SELECT id,title,space_id,folder_id,position FROM documents WHERE parent_id IS NULL ORDER BY position, title")]
        features = [dict(r) for r in conn.execute("SELECT * FROM features ORDER BY position")]
    spaces  = [s for s in spaces  if _can_see(s, username)]
    folders = [f for f in folders if _can_see(f, username)]
    for l in lists:
        l["custom_fields"] = json.loads(l.get("custom_fields") or "[]")
    lists   = [l for l in lists   if _can_see(l, username)]
    for space in spaces:
        sfolds = [f for f in folders if f["space_id"] == space["id"]]
        for folder in sfolds:
            folder["lists"]     = [l for l in lists    if l["folder_id"] == folder["id"]]
            folder["documents"] = [d for d in docs     if d["folder_id"] == folder["id"]]
            folder["features"]  = [f for f in features if f.get("folder_id") == folder["id"]]
        space["folders"]      = sfolds
        space["direct_lists"] = [l for l in lists    if l["space_id"] == space["id"] and not l["folder_id"]]
        space["documents"]    = [d for d in docs     if d["space_id"] == space["id"] and not d["folder_id"]]
        space["features"]     = [f for f in features if f["space_id"] == space["id"] and not f.get("folder_id")]
    return {"spaces": spaces, "statuses": STATUSES, "members": MEMBERS}


# ── Documents ─────────────────────────────────────────────────────────────────

@router.post("/api/gestao/documents")
async def api_create_document(request: Request):
    _require(request)
    data = await request.json()
    now = datetime.now().isoformat()
    doc_id = str(uuid.uuid4())
    title = data.get("title", "Sem título").strip() or "Sem título"
    space_id = data["space_id"]
    folder_id = data.get("folder_id") or None
    parent_id = data.get("parent_id") or None
    with get_db() as conn:
        pos = (conn.execute(
            "SELECT COALESCE(MAX(position),0) FROM documents WHERE space_id=?", (space_id,)
        ).fetchone()[0] or 0) + 1
        conn.execute(
            "INSERT INTO documents (id,title,content,space_id,folder_id,parent_id,position,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (doc_id, title, "", space_id, folder_id, parent_id, pos, now, now),
        )
        conn.commit()
        doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    return {"document": doc}


@router.get("/api/gestao/documents/{doc_id}")
async def api_get_document(doc_id: str, request: Request):
    _require(request)
    with get_db() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row: raise HTTPException(404, "Documento não encontrado")
    return {"document": dict(row)}


@router.patch("/api/gestao/documents/{doc_id}")
async def api_patch_document(doc_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"title", "content"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields: raise HTTPException(400, "Nada para atualizar")
    now = datetime.now().isoformat()
    fields["updated_at"] = now
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [doc_id]
    with get_db() as conn:
        current = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
        # save version if content changed and last version is > 5 minutes old
        if "content" in fields:
            last_ver = conn.execute(
                "SELECT created_at FROM document_versions WHERE doc_id=? ORDER BY id DESC LIMIT 1",
                (doc_id,)
            ).fetchone()
            should_snap = True
            if last_ver:
                try:
                    last_dt = datetime.fromisoformat(last_ver[0])
                    should_snap = (datetime.now() - last_dt) > timedelta(minutes=5)
                except Exception:
                    pass
            if should_snap:
                conn.execute(
                    "INSERT INTO document_versions (doc_id, title, content, created_at) VALUES (?,?,?,?)",
                    (doc_id, current["title"], current["content"] or "", now),
                )
                # keep at most 50 versions per doc
                conn.execute("""
                    DELETE FROM document_versions WHERE doc_id=? AND id NOT IN (
                        SELECT id FROM document_versions WHERE doc_id=? ORDER BY id DESC LIMIT 50
                    )
                """, (doc_id, doc_id))
        conn.execute(f"UPDATE documents SET {set_clause} WHERE id=?", values)
        conn.commit()
        doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    return {"document": doc}


@router.get("/api/gestao/documents/{doc_id}/versions")
async def api_doc_versions(doc_id: str, request: Request):
    _require(request)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at FROM document_versions WHERE doc_id=? ORDER BY id DESC",
            (doc_id,)
        ).fetchall()
    return {"versions": [dict(r) for r in rows]}


@router.get("/api/gestao/documents/{doc_id}/versions/{vid}")
async def api_doc_version_get(doc_id: str, vid: int, request: Request):
    _require(request)
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM document_versions WHERE id=? AND doc_id=?", (vid, doc_id)
        ).fetchone()
    if not row: raise HTTPException(404, "Versão não encontrada")
    return {"version": dict(row)}


@router.post("/api/gestao/documents/{doc_id}/versions/{vid}/restore")
async def api_doc_version_restore(doc_id: str, vid: int, request: Request):
    _require(request)
    now = datetime.now().isoformat()
    with get_db() as conn:
        ver = conn.execute(
            "SELECT * FROM document_versions WHERE id=? AND doc_id=?", (vid, doc_id)
        ).fetchone()
        if not ver: raise HTTPException(404, "Versão não encontrada")
        ver = dict(ver)
        # save current state as a new version before restoring
        current = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
        conn.execute(
            "INSERT INTO document_versions (doc_id, title, content, created_at) VALUES (?,?,?,?)",
            (doc_id, current["title"], current["content"] or "", now),
        )
        # restore
        conn.execute(
            "UPDATE documents SET title=?, content=?, updated_at=? WHERE id=?",
            (ver["title"], ver["content"], now, doc_id),
        )
        conn.commit()
        doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    return {"document": doc}


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").strip()


def _snippet(text: str, query: str, radius: int = 60) -> str:
    lower = text.lower()
    pos = lower.find(query.lower())
    if pos < 0:
        return text[:radius * 2] + ("…" if len(text) > radius * 2 else "")
    start = max(0, pos - radius)
    end   = min(len(text), pos + len(query) + radius)
    snip  = ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")
    return snip


@router.get("/api/gestao/search")
async def api_search(q: str, request: Request):
    _require(request)
    q = q.strip()
    if not q: return {"docs": [], "tasks": []}
    with get_db() as conn:
        docs  = [dict(r) for r in conn.execute("SELECT id, title, content, space_id FROM documents")]
        tasks = [dict(r) for r in conn.execute(
            "SELECT t.id, t.title, t.status, t.priority, t.due_date, t.list_id, l.name as list_name "
            "FROM tasks t JOIN lists l ON l.id=t.list_id", ()
        )]

    q_low = q.lower()
    doc_results = []
    for d in docs:
        plain = _strip_html(d["content"])
        if q_low in d["title"].lower() or q_low in plain.lower():
            doc_results.append({
                "id": d["id"], "title": d["title"],
                "space_id": d["space_id"],
                "snippet": _snippet(plain, q),
            })

    task_results = []
    for t in tasks:
        if q_low in t["title"].lower():
            task_results.append({
                "id": t["id"], "title": t["title"],
                "status": t["status"], "priority": t["priority"],
                "list_id": t["list_id"], "list_name": t["list_name"],
            })

    return {"docs": doc_results[:20], "tasks": task_results[:20]}


@router.delete("/api/gestao/documents/{doc_id}")
async def api_delete_document(doc_id: str, request: Request):
    _require(request)
    with get_db() as conn:
        def _del_children(pid):
            kids = [r[0] for r in conn.execute("SELECT id FROM documents WHERE parent_id=?", (pid,))]
            for kid in kids:
                _del_children(kid)
                conn.execute("DELETE FROM documents WHERE id=?", (kid,))
        _del_children(doc_id)
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.commit()
    return {"ok": True}


@router.get("/api/gestao/documents/{doc_id}/family")
async def api_document_family(doc_id: str, request: Request):
    _require(request)
    with get_db() as conn:
        row = conn.execute("SELECT id, title, parent_id FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Documento não encontrado")
        # climb to root
        current = dict(row)
        visited: set = set()
        while current.get("parent_id") and current["parent_id"] not in visited:
            visited.add(current["id"])
            parent = conn.execute("SELECT id, title, parent_id FROM documents WHERE id=?", (current["parent_id"],)).fetchone()
            if not parent:
                break
            current = dict(parent)
        root_id = current["id"]

        def _descendants(pid, depth):
            kids = conn.execute(
                "SELECT id, title, parent_id FROM documents WHERE parent_id=? ORDER BY position, title", (pid,)
            ).fetchall()
            result = []
            for k in kids:
                d = dict(k); d["depth"] = depth
                result.append(d)
                result.extend(_descendants(d["id"], depth + 1))
            return result

        root = dict(conn.execute("SELECT id, title, parent_id FROM documents WHERE id=?", (root_id,)).fetchone())
        root["depth"] = 0
        family = [root] + _descendants(root_id, 1)
    return {"family": family, "root_id": root_id}


@router.get("/api/gestao/archived")
async def api_archived(request: Request):
    _require(request)
    with get_db() as conn:
        spaces  = [dict(r) for r in conn.execute("SELECT id,name,color,icon FROM spaces  WHERE archived=1")]
        folders = [dict(r) for r in conn.execute("SELECT id,name,color,space_id FROM folders WHERE archived=1")]
        lists   = [dict(r) for r in conn.execute("SELECT id,name,color,icon,space_id,folder_id FROM lists WHERE archived=1")]
    return {"spaces": spaces, "folders": folders, "lists": lists}


@router.get("/api/gestao/lists/{list_id}/tasks")
async def api_list_tasks(list_id: str, request: Request):
    _require(request)
    with get_db() as conn:
        tasks = [dict(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE list_id=? ORDER BY position, created_at", (list_id,)
        )]
    for t in tasks:
        t["assignees"] = json.loads(t.get("assignees") or "[]")
        t["extra_data"] = json.loads(t.get("extra_data") or "{}")
    return {"tasks": tasks}


@router.post("/api/gestao/tasks")
async def api_create_task(request: Request):
    _require(request)
    data = await request.json()
    title   = (data.get("title") or "").strip()
    list_id = data.get("list_id", "")
    if not title or not list_id:
        raise HTTPException(400, "title e list_id obrigatórios")
    tid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    with get_db() as conn:
        pos = (conn.execute("SELECT COALESCE(MAX(position),0) FROM tasks WHERE list_id=?", (list_id,)).fetchone()[0] or 0) + 1
        conn.execute(
            "INSERT INTO tasks (id,list_id,title,description,assignees,due_date,status,priority,parent_task_id,position,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, list_id, title,
             data.get("description", ""),
             json.dumps(data.get("assignees", [])),
             data.get("due_date") or None,
             data.get("status", "aberto"),
             data.get("priority", "normal"),
             data.get("parent_task_id") or None,
             pos, now),
        )
        conn.commit()
        task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    task["assignees"] = json.loads(task["assignees"])
    return {"task": task}


@router.patch("/api/gestao/tasks/{task_id}")
async def api_update_task(task_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"title", "description", "assignees", "due_date", "status", "priority", "position", "extra_data"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    if "assignees" in fields:
        fields["assignees"] = json.dumps(fields["assignees"])
    if "extra_data" in fields and not isinstance(fields["extra_data"], str):
        fields["extra_data"] = json.dumps(fields["extra_data"])
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with get_db() as conn:
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", [*fields.values(), task_id])
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Tarefa não encontrada")
    task = dict(row)
    task["assignees"] = json.loads(task["assignees"] or "[]")
    task["extra_data"] = json.loads(task["extra_data"] or "{}")
    return {"task": task}


@router.delete("/api/gestao/tasks/{task_id}")
async def api_delete_task(task_id: str, request: Request):
    _require(request)
    with get_db() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()
    return {"ok": True}


@router.post("/api/gestao/spaces")
async def api_create_space(request: Request):
    _require(request)
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name obrigatório")
    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    with get_db() as conn:
        pos = (conn.execute("SELECT COALESCE(MAX(position),0) FROM spaces").fetchone()[0] or 0) + 1
        conn.execute("INSERT INTO spaces (id,name,color,icon,position,created_at) VALUES (?,?,?,?,?,?)",
                     (sid, name, data.get("color", "#ff4d00"), data.get("icon", "⚡"), pos, now))
        conn.commit()
        space = dict(conn.execute("SELECT * FROM spaces WHERE id=?", (sid,)).fetchone())
    return {"space": {**space, "folders": [], "direct_lists": []}}


@router.post("/api/gestao/folders")
async def api_create_folder(request: Request):
    _require(request)
    data = await request.json()
    name     = (data.get("name") or "").strip()
    space_id = data.get("space_id", "")
    if not name or not space_id:
        raise HTTPException(400, "name e space_id obrigatórios")
    fid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    with get_db() as conn:
        pos = (conn.execute("SELECT COALESCE(MAX(position),0) FROM folders WHERE space_id=?", (space_id,)).fetchone()[0] or 0) + 1
        conn.execute("INSERT INTO folders (id,space_id,name,position,created_at) VALUES (?,?,?,?,?)", (fid, space_id, name, pos, now))
        conn.commit()
        folder = dict(conn.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone())
    return {"folder": {**folder, "lists": []}}


@router.post("/api/gestao/lists")
async def api_create_list(request: Request):
    _require(request)
    data = await request.json()
    name     = (data.get("name") or "").strip()
    space_id = data.get("space_id", "")
    if not name or not space_id:
        raise HTTPException(400, "name e space_id obrigatórios")
    lid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    folder_id = data.get("folder_id") or None
    with get_db() as conn:
        pos = (conn.execute("SELECT COALESCE(MAX(position),0) FROM lists WHERE space_id=?", (space_id,)).fetchone()[0] or 0) + 1
        conn.execute("INSERT INTO lists (id,name,space_id,folder_id,position,created_at) VALUES (?,?,?,?,?,?)", (lid, name, space_id, folder_id, pos, now))
        conn.commit()
        lst = dict(conn.execute("SELECT * FROM lists WHERE id=?", (lid,)).fetchone())
    return {"list": lst}


@router.delete("/api/gestao/spaces/{space_id}")
async def api_delete_space(space_id: str, request: Request):
    _require(request)
    with get_db() as conn:
        tools = conn.execute("SELECT label FROM features WHERE space_id=?", (space_id,)).fetchall()
        if tools:
            names = ", ".join(r[0] for r in tools)
            raise HTTPException(409, f"Mova as ferramentas antes de excluir este espaço: {names}")
        conn.execute("DELETE FROM spaces WHERE id=?", (space_id,))
        conn.commit()
    return {"ok": True}


@router.patch("/api/gestao/spaces/{space_id}")
async def api_update_space(space_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"name", "color", "icon", "custom_statuses", "archived", "permissions"}
    fields = {k: v for k, v in data.items() if k in allowed and v is not None}
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        if not fields["name"]: raise HTTPException(400, "name não pode ser vazio")
    if not fields: raise HTTPException(400, "Nenhum campo válido")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with get_db() as conn:
        conn.execute(f"UPDATE spaces SET {set_clause} WHERE id=?", [*fields.values(), space_id])
        conn.commit()
    return {"ok": True}


@router.delete("/api/gestao/folders/{folder_id}")
async def api_delete_folder(folder_id: str, request: Request):
    _require(request)
    with get_db() as conn:
        tools = conn.execute("SELECT label FROM features WHERE folder_id=?", (folder_id,)).fetchall()
        if tools:
            names = ", ".join(r[0] for r in tools)
            raise HTTPException(409, f"Mova as ferramentas antes de excluir esta pasta: {names}")
        conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
        conn.commit()
    return {"ok": True}


@router.patch("/api/gestao/folders/{folder_id}")
async def api_update_folder(folder_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"name", "color", "archived", "permissions"}
    fields = {k: v for k, v in data.items() if k in allowed and v is not None}
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        if not fields["name"]: raise HTTPException(400, "name não pode ser vazio")
    if not fields: raise HTTPException(400, "Nenhum campo válido")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with get_db() as conn:
        conn.execute(f"UPDATE folders SET {set_clause} WHERE id=?", [*fields.values(), folder_id])
        conn.commit()
    return {"ok": True}


@router.delete("/api/gestao/lists/{list_id}")
async def api_delete_list(list_id: str, request: Request):
    _require(request)
    with get_db() as conn:
        conn.execute("DELETE FROM lists WHERE id=?", (list_id,))
        conn.commit()
    return {"ok": True}


@router.patch("/api/gestao/lists/{list_id}")
async def api_update_list(list_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"name", "color", "icon", "archived", "permissions", "custom_fields"}
    fields = {k: v for k, v in data.items() if k in allowed and v is not None}
    if "custom_fields" in fields and not isinstance(fields["custom_fields"], str):
        fields["custom_fields"] = json.dumps(fields["custom_fields"])
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        if not fields["name"]: raise HTTPException(400, "name não pode ser vazio")
    if not fields: raise HTTPException(400, "Nenhum campo válido")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with get_db() as conn:
        conn.execute(f"UPDATE lists SET {set_clause} WHERE id=?", [*fields.values(), list_id])
        conn.commit()
    return {"ok": True}


# ── Reorder (drag & drop) ─────────────────────────────────────────────────────

@router.post("/api/gestao/reorder")
async def api_reorder(request: Request):
    _require(request)
    data = await request.json()
    item_type = data.get("type")
    items = data.get("items", [])
    table_map = {"space": "spaces", "folder": "folders", "list": "lists", "feature": "features"}
    table = table_map.get(item_type)
    if not table:
        raise HTTPException(400, "type inválido")
    with get_db() as conn:
        for item in items:
            fields: dict = {"position": item["position"]}
            if item_type == "folder" and "space_id" in item:
                fields["space_id"] = item["space_id"]
            if item_type in ("list", "feature"):
                if "space_id" in item:
                    fields["space_id"] = item["space_id"]
                if "folder_id" in item:
                    fields["folder_id"] = item.get("folder_id")  # may be None
            set_clause = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE {table} SET {set_clause} WHERE id=?", [*fields.values(), item["id"]])
        conn.commit()
    return {"ok": True}


# ── Features ──────────────────────────────────────────────────────────────────

@router.patch("/api/gestao/features/{feature_id}")
async def api_update_feature(feature_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"space_id", "folder_id", "position"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    # folder_id can be explicitly null
    if "folder_id" in data:
        fields["folder_id"] = data["folder_id"]
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with get_db() as conn:
        conn.execute(f"UPDATE features SET {set_clause} WHERE id=?", [*fields.values(), feature_id])
        conn.commit()
    return {"ok": True}


# ── Duplicate ─────────────────────────────────────────────────────────────────

@router.post("/api/gestao/spaces/{space_id}/duplicate")
async def api_duplicate_space(space_id: str, request: Request):
    _require(request)
    body = {}
    try: body = await request.json()
    except Exception: pass
    now = datetime.now().isoformat()
    with get_db() as conn:
        orig = conn.execute("SELECT * FROM spaces WHERE id=?", (space_id,)).fetchone()
        if not orig: raise HTTPException(404, "Espaço não encontrado")
        orig = dict(orig)
        new_name = body.get("name") or (orig["name"] + " (cópia)")
        include_tasks = body.get("include_tasks", True)
        new_sid = str(uuid.uuid4())
        pos = (conn.execute("SELECT COALESCE(MAX(position),0) FROM spaces").fetchone()[0] or 0) + 1
        conn.execute(
            "INSERT INTO spaces (id,name,color,icon,position,created_at) VALUES (?,?,?,?,?,?)",
            (new_sid, new_name, orig["color"], orig["icon"], pos, now),
        )
        folder_map = {}
        for f in conn.execute("SELECT * FROM folders WHERE space_id=? AND archived=0", (space_id,)).fetchall():
            f = dict(f); new_fid = str(uuid.uuid4()); folder_map[f["id"]] = new_fid
            conn.execute("INSERT INTO folders (id,space_id,name,position,created_at) VALUES (?,?,?,?,?)",
                         (new_fid, new_sid, f["name"], f["position"], now))
        for l in conn.execute("SELECT * FROM lists WHERE space_id=? AND archived=0", (space_id,)).fetchall():
            l = dict(l); new_lid = str(uuid.uuid4())
            new_fid = folder_map.get(l["folder_id"]) if l["folder_id"] else None
            conn.execute("INSERT INTO lists (id,name,space_id,folder_id,position,created_at) VALUES (?,?,?,?,?,?)",
                         (new_lid, l["name"], new_sid, new_fid, l["position"], now))
            if include_tasks:
                for t in conn.execute("SELECT * FROM tasks WHERE list_id=?", (l["id"],)).fetchall():
                    t = dict(t)
                    conn.execute(
                        "INSERT INTO tasks (id,list_id,title,description,assignees,due_date,status,priority,parent_task_id,position,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), new_lid, t["title"], t["description"], t["assignees"],
                         t["due_date"], t["status"], t["priority"], None, t["position"], now),
                    )
        conn.commit()
    return {"ok": True}


@router.post("/api/gestao/folders/{folder_id}/duplicate")
async def api_duplicate_folder(folder_id: str, request: Request):
    _require(request)
    body = {}
    try: body = await request.json()
    except Exception: pass
    now = datetime.now().isoformat()
    with get_db() as conn:
        orig = dict(conn.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone())
        new_name = body.get("name") or (orig["name"] + " (cópia)")
        target_space_id = body.get("target_space_id") or orig["space_id"]
        include_tasks = body.get("include_tasks", True)
        new_fid = str(uuid.uuid4())
        pos = (conn.execute("SELECT COALESCE(MAX(position),0) FROM folders WHERE space_id=?", (target_space_id,)).fetchone()[0] or 0) + 1
        conn.execute("INSERT INTO folders (id,space_id,name,position,created_at) VALUES (?,?,?,?,?)",
                     (new_fid, target_space_id, new_name, pos, now))
        for l in conn.execute("SELECT * FROM lists WHERE folder_id=?", (folder_id,)).fetchall():
            l = dict(l); new_lid = str(uuid.uuid4())
            conn.execute("INSERT INTO lists (id,name,space_id,folder_id,position,created_at) VALUES (?,?,?,?,?,?)",
                         (new_lid, l["name"], target_space_id, new_fid, l["position"], now))
            if include_tasks:
                for t in conn.execute("SELECT * FROM tasks WHERE list_id=?", (l["id"],)).fetchall():
                    t = dict(t)
                    conn.execute(
                        "INSERT INTO tasks (id,list_id,title,description,assignees,due_date,status,priority,parent_task_id,position,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), new_lid, t["title"], t["description"], t["assignees"],
                         t["due_date"], t["status"], t["priority"], None, t["position"], now),
                    )
        conn.commit()
    return {"ok": True}


@router.post("/api/gestao/lists/{list_id}/duplicate")
async def api_duplicate_list(list_id: str, request: Request):
    _require(request)
    body = {}
    try: body = await request.json()
    except Exception: pass
    now = datetime.now().isoformat()
    with get_db() as conn:
        orig = dict(conn.execute("SELECT * FROM lists WHERE id=?", (list_id,)).fetchone())
        new_name = body.get("name") or (orig["name"] + " (cópia)")
        target_space_id = body.get("target_space_id") or orig["space_id"]
        target_folder_id = body.get("target_folder_id") if "target_folder_id" in body else orig["folder_id"]
        include_tasks = body.get("include_tasks", True)
        new_lid = str(uuid.uuid4())
        pos = (conn.execute("SELECT COALESCE(MAX(position),0) FROM lists WHERE space_id=?", (target_space_id,)).fetchone()[0] or 0) + 1
        conn.execute("INSERT INTO lists (id,name,space_id,folder_id,position,created_at) VALUES (?,?,?,?,?,?)",
                     (new_lid, new_name, target_space_id, target_folder_id, pos, now))
        if include_tasks:
            for t in conn.execute("SELECT * FROM tasks WHERE list_id=?", (list_id,)).fetchall():
                t = dict(t)
                conn.execute(
                    "INSERT INTO tasks (id,list_id,title,description,assignees,due_date,status,priority,parent_task_id,position,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), new_lid, t["title"], t["description"], t["assignees"],
                     t["due_date"], t["status"], t["priority"], None, t["position"], now),
                )
        conn.commit()
    return {"ok": True}
