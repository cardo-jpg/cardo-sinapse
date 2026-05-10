import os
import re
import json
import uuid
import hmac
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from backend.db import get_conn, dict_cursor

load_dotenv()

router = APIRouter()

BASE_DIR  = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))

SECRET_KEY = os.getenv("SECRET_KEY", "sinapse-secret-2026")
USERS = {
    "victor": os.getenv("USER_VICTOR_PASSWORD", "C@rdobrain2026"),
    "jose":   os.getenv("USER_JOSE_PASSWORD",   "C@rdosinapse2026"),
    "jadna":  os.getenv("USER_JADNA_PASSWORD",  "C@rdosinapse2026"),
}
USER_DISPLAY = {"victor": "Victor", "jose": "José", "jadna": "Jadna"}

STATUSES = [
    {"id": "aberto",    "name": "Aberto",       "color": "#6b7280"},
    {"id": "progresso", "name": "Em progresso",  "color": "#3b82f6"},
    {"id": "revisao",   "name": "Em revisão",    "color": "#f59e0b"},
    {"id": "concluido", "name": "Concluído",     "color": "#22c55e"},
]
MEMBERS = [
    {"id": "victor", "name": "Victor"},
    {"id": "jose",   "name": "José"},
    {"id": "jadna",  "name": "Jadna"},
]


def _user_exists(username: str) -> bool:
    if username in USERS:
        return True
    try:
        conn = get_conn()
        cur = dict_cursor(conn)
        cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        conn.close()
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
    return get_conn()


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS spaces (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                color      TEXT DEFAULT '#ff4d00',
                icon       TEXT DEFAULT '⚡',
                position   INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id         TEXT PRIMARY KEY,
                space_id   TEXT NOT NULL,
                name       TEXT NOT NULL,
                position   INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (space_id) REFERENCES spaces(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lists (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                space_id   TEXT NOT NULL,
                folder_id  TEXT,
                position   INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (space_id)  REFERENCES spaces(id)  ON DELETE CASCADE,
                FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id             TEXT PRIMARY KEY,
                list_id        TEXT NOT NULL,
                title          TEXT NOT NULL,
                description    TEXT DEFAULT '',
                assignees      TEXT DEFAULT '[]',
                due_date       TEXT,
                status         TEXT DEFAULT 'aberto',
                priority       TEXT DEFAULT 'normal',
                parent_task_id TEXT,
                position       INTEGER DEFAULT 0,
                created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (list_id) REFERENCES lists(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL DEFAULT 'Sem título',
                content    TEXT DEFAULT '',
                space_id   TEXT NOT NULL,
                folder_id  TEXT,
                position   INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (space_id)  REFERENCES spaces(id)  ON DELETE CASCADE,
                FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS features (
                id         TEXT PRIMARY KEY,
                space_id   TEXT NOT NULL,
                url        TEXT NOT NULL,
                label      TEXT NOT NULL,
                icon       TEXT DEFAULT '📋',
                position   INTEGER DEFAULT 0,
                FOREIGN KEY (space_id) REFERENCES spaces(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS document_versions (
                id         SERIAL PRIMARY KEY,
                doc_id     TEXT NOT NULL,
                title      TEXT NOT NULL DEFAULT '',
                content    TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS task_checklists (
                id         TEXT PRIMARY KEY,
                task_id    TEXT NOT NULL,
                name       TEXT NOT NULL DEFAULT 'Checklist',
                position   INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS task_checklist_items (
                id           TEXT PRIMARY KEY,
                checklist_id TEXT NOT NULL,
                text         TEXT NOT NULL,
                resolved     INTEGER DEFAULT 0,
                position     INTEGER DEFAULT 0,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (checklist_id) REFERENCES task_checklists(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS forms (
                id          TEXT PRIMARY KEY,
                list_id     TEXT NOT NULL,
                name        TEXT NOT NULL DEFAULT 'Formulário',
                description TEXT DEFAULT '',
                fields      TEXT DEFAULT '[]',
                settings    TEXT DEFAULT '{}',
                cover_url   TEXT DEFAULT '',
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (list_id) REFERENCES lists(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS form_submissions (
                id           TEXT PRIMARY KEY,
                form_id      TEXT NOT NULL,
                data         TEXT DEFAULT '{}',
                task_id      TEXT,
                submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (form_id) REFERENCES forms(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS list_view_settings (
                list_id     TEXT PRIMARY KEY,
                settings    TEXT NOT NULL DEFAULT '{}',
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_by  TEXT,
                FOREIGN KEY (list_id) REFERENCES lists(id) ON DELETE CASCADE
            )
        """)

        # ADD COLUMN migrations — PostgreSQL: use DO blocks to avoid errors on re-run
        migrations = [
            ("folders",   "color",          "TEXT DEFAULT '#6b7280'"),
            ("lists",     "color",          "TEXT DEFAULT '#6b7280'"),
            ("lists",     "icon",           "TEXT DEFAULT ''"),
            ("spaces",    "custom_statuses","TEXT DEFAULT ''"),
            ("spaces",    "archived",       "INTEGER DEFAULT 0"),
            ("folders",   "archived",       "INTEGER DEFAULT 0"),
            ("lists",     "archived",       "INTEGER DEFAULT 0"),
            ("spaces",    "permissions",    "TEXT DEFAULT ''"),
            ("folders",   "permissions",    "TEXT DEFAULT ''"),
            ("lists",     "permissions",    "TEXT DEFAULT ''"),
            ("documents", "parent_id",      "TEXT"),
            ("features",  "folder_id",      "TEXT"),
            ("lists",     "custom_fields",  "TEXT DEFAULT '[]'"),
            ("tasks",     "extra_data",     "TEXT DEFAULT '{}'"),
            ("tasks",     "cf_values",      "TEXT DEFAULT '{}'"),
            ("folders",   "custom_statuses","TEXT DEFAULT ''"),
            ("lists",     "custom_statuses","TEXT DEFAULT ''"),
        ]
        for table, col, col_def in migrations:
            cur.execute(f"""
                DO $$
                BEGIN
                    ALTER TABLE {table} ADD COLUMN {col} {col_def};
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$;
            """)

        conn.commit()

        # Seed spaces if empty
        cur.execute("SELECT COUNT(*) AS c FROM spaces")
        if cur.fetchone()['c'] == 0:
            _seed(conn, cur)

        # Seed features if empty
        cur.execute("SELECT COUNT(*) AS c FROM features")
        if cur.fetchone()['c'] == 0:
            _seed_features(conn, cur)

        # Ensure "comercial" space exists (idempotente — caso seed não tenha rodado)
        cur.execute(
            "INSERT INTO spaces (id,name,color,icon,position,created_at) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            ("comercial", "Comercial", "#22c55e", "💼", 2, datetime.now().isoformat()),
        )
        # Ensure CRM feature exists in Comercial (idempotente)
        cur.execute(
            "INSERT INTO features (id,space_id,url,label,icon,position) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            ("feat_crm", "comercial", "/crm", "CRM", "🎯", 0),
        )
        # Cleanup features antigos (clientes saíram, consolidados ou residuais)
        cur.execute("DELETE FROM features WHERE id IN ('feat_dash_nf', 'feat_dash_dft', 'feat_dash_hire', 'feat_dashboard')")
        # Ferramenta única de Dashboard (clientes com seletor interno)
        cur.execute(
            "INSERT INTO features (id,space_id,url,label,icon,position) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            ("feat_dash_clientes", "operacional", "/dashboards/clientes", "Dashboard", "📊", 10),
        )
        # Se já existe, normaliza o label pra 'Dashboard'
        cur.execute("UPDATE features SET label='Dashboard' WHERE id='feat_dash_clientes'")

        conn.commit()
    finally:
        cur.close()
        conn.close()


def _seed(conn, cur):
    now = datetime.now().isoformat()

    spaces = [
        ("gestao",      "Gestão",      "#19196c", "⚙️",  0),
        ("operacional", "Operacional", "#ff4d00", "🔧", 1),
        ("comercial",   "Comercial",   "#22c55e", "💼", 2),
    ]
    for sid, name, color, icon, pos in spaces:
        cur.execute(
            "INSERT INTO spaces (id,name,color,icon,position,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (sid, name, color, icon, pos, now),
        )

    folders = [
        ("op_trafego",  "operacional", "Tráfego Pago",    0),
        ("op_cs",       "operacional", "Customer Success", 1),
        ("op_conteudo", "operacional", "Conteúdo",         2),
        ("op_auto",     "operacional", "Automações",       3),
        ("gest_equipe", "gestao",      "Equipe",           0),
    ]
    for fid, sid, name, pos in folders:
        cur.execute(
            "INSERT INTO folders (id,space_id,name,position,created_at) VALUES (%s,%s,%s,%s,%s)",
            (fid, sid, name, pos, now),
        )

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
        cur.execute(
            "INSERT INTO lists (id,name,space_id,folder_id,position,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (lid, name, sid, fid, pos, now),
        )

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
        cur.execute(
            "INSERT INTO tasks (id,list_id,title,description,assignees,due_date,status,priority,parent_task_id,position,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (str(uuid.uuid4()), lid, title, "", assignees, due, status, priority, None, i, now),
        )

    conn.commit()


def _seed_features(conn, cur):
    rows = [
        ("feat_crm",       "comercial",   "/crm",                  "CRM",             "🎯", 0),
        ("feat_ata",       "operacional", "/ata",                  "Gerador de Atas", "📝", 1),
    ]
    for fid, sid, url, label, icon, pos in rows:
        cur.execute(
            "INSERT INTO features (id,space_id,url,label,icon,position) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
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
    if not p:
        return True
    try:
        return username in json.loads(p)
    except Exception:
        return True


@router.get("/api/gestao/tree")
async def api_tree(request: Request):
    username = _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM spaces  WHERE archived=0 ORDER BY position, name")
        spaces = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM folders WHERE archived=0 ORDER BY position, name")
        folders = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM lists   WHERE archived=0 ORDER BY position, name")
        lists = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT id,title,space_id,folder_id,position FROM documents WHERE parent_id IS NULL ORDER BY position, title")
        docs = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM features ORDER BY position")
        features = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    spaces  = [s for s in spaces  if _can_see(s, username)]
    folders = [f for f in folders if _can_see(f, username)]
    for l in lists:
        l["custom_fields"] = json.loads(l.get("custom_fields") or "[]")
    lists = [l for l in lists if _can_see(l, username)]

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
    doc_id    = str(uuid.uuid4())
    title     = data.get("title", "Sem título").strip() or "Sem título"
    content   = data.get("content", "")
    space_id  = data["space_id"]
    folder_id = data.get("folder_id") or None
    parent_id = data.get("parent_id") or None

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT COALESCE(MAX(position),0) FROM documents WHERE space_id=%s", (space_id,)
        )
        pos = (cur.fetchone()["coalesce"] or 0) + 1
        cur.execute(
            "INSERT INTO documents (id,title,content,space_id,folder_id,parent_id,position,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (doc_id, title, content, space_id, folder_id, parent_id, pos, now, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM documents WHERE id=%s", (doc_id,))
        doc = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"document": doc}


@router.get("/api/gestao/documents/{doc_id}")
async def api_get_document(doc_id: str, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM documents WHERE id=%s", (doc_id,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Documento não encontrado")
    return {"document": dict(row)}


@router.patch("/api/gestao/documents/{doc_id}")
async def api_patch_document(doc_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"title", "content"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "Nada para atualizar")
    now = datetime.now().isoformat()
    fields["updated_at"] = now

    set_clause = ", ".join(f"{k}=%s" for k in fields)
    values = list(fields.values()) + [doc_id]

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM documents WHERE id=%s", (doc_id,))
        current = dict(cur.fetchone())

        if "content" in fields:
            cur.execute(
                "SELECT created_at FROM document_versions WHERE doc_id=%s ORDER BY id DESC LIMIT 1",
                (doc_id,),
            )
            last_ver = cur.fetchone()
            should_snap = True
            if last_ver:
                try:
                    last_dt = datetime.fromisoformat(last_ver["created_at"])
                    should_snap = (datetime.now() - last_dt) > timedelta(minutes=5)
                except Exception:
                    pass
            if should_snap:
                cur.execute(
                    "INSERT INTO document_versions (doc_id, title, content, created_at) VALUES (%s,%s,%s,%s)",
                    (doc_id, current["title"], current["content"] or "", now),
                )
                # Keep at most 50 versions per doc
                cur.execute("""
                    DELETE FROM document_versions WHERE doc_id=%s AND id NOT IN (
                        SELECT id FROM document_versions WHERE doc_id=%s ORDER BY id DESC LIMIT 50
                    )
                """, (doc_id, doc_id))

        cur.execute(f"UPDATE documents SET {set_clause} WHERE id=%s", values)
        conn.commit()
        cur.execute("SELECT * FROM documents WHERE id=%s", (doc_id,))
        doc = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"document": doc}


@router.get("/api/gestao/documents/{doc_id}/versions")
async def api_doc_versions(doc_id: str, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT id, title, created_at FROM document_versions WHERE doc_id=%s ORDER BY id DESC",
            (doc_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"versions": rows}


@router.get("/api/gestao/documents/{doc_id}/versions/{vid}")
async def api_doc_version_get(doc_id: str, vid: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT * FROM document_versions WHERE id=%s AND doc_id=%s", (vid, doc_id)
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Versão não encontrada")
    return {"version": dict(row)}


@router.post("/api/gestao/documents/{doc_id}/versions/{vid}/restore")
async def api_doc_version_restore(doc_id: str, vid: int, request: Request):
    _require(request)
    now = datetime.now().isoformat()
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT * FROM document_versions WHERE id=%s AND doc_id=%s", (vid, doc_id)
        )
        ver = cur.fetchone()
        if not ver:
            raise HTTPException(404, "Versão não encontrada")
        ver = dict(ver)
        cur.execute("SELECT * FROM documents WHERE id=%s", (doc_id,))
        current = dict(cur.fetchone())
        cur.execute(
            "INSERT INTO document_versions (doc_id, title, content, created_at) VALUES (%s,%s,%s,%s)",
            (doc_id, current["title"], current["content"] or "", now),
        )
        cur.execute(
            "UPDATE documents SET title=%s, content=%s, updated_at=%s WHERE id=%s",
            (ver["title"], ver["content"], now, doc_id),
        )
        conn.commit()
        cur.execute("SELECT * FROM documents WHERE id=%s", (doc_id,))
        doc = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
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
    if not q:
        return {"docs": [], "tasks": []}

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id, title, content, space_id FROM documents")
        docs = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT t.id, t.title, t.status, t.priority, t.due_date, t.list_id, l.name as list_name "
            "FROM tasks t JOIN lists l ON l.id=t.list_id"
        )
        tasks = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

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
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        def _del_children(pid):
            cur.execute("SELECT id FROM documents WHERE parent_id=%s", (pid,))
            kids = [r["id"] for r in cur.fetchall()]
            for kid in kids:
                _del_children(kid)
                cur.execute("DELETE FROM documents WHERE id=%s", (kid,))
        _del_children(doc_id)
        cur.execute("DELETE FROM documents WHERE id=%s", (doc_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.get("/api/gestao/documents/{doc_id}/family")
async def api_document_family(doc_id: str, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id, title, parent_id FROM documents WHERE id=%s", (doc_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Documento não encontrado")
        current = dict(row)
        visited: set = set()
        while current.get("parent_id") and current["parent_id"] not in visited:
            visited.add(current["id"])
            cur.execute("SELECT id, title, parent_id FROM documents WHERE id=%s", (current["parent_id"],))
            parent = cur.fetchone()
            if not parent:
                break
            current = dict(parent)
        root_id = current["id"]

        def _descendants(pid, depth):
            cur.execute(
                "SELECT id, title, parent_id FROM documents WHERE parent_id=%s ORDER BY position, title",
                (pid,),
            )
            result = []
            for k in cur.fetchall():
                d = dict(k)
                d["depth"] = depth
                result.append(d)
                result.extend(_descendants(d["id"], depth + 1))
            return result

        cur.execute("SELECT id, title, parent_id FROM documents WHERE id=%s", (root_id,))
        root = dict(cur.fetchone())
        root["depth"] = 0
        family = [root] + _descendants(root_id, 1)
    finally:
        cur.close()
        conn.close()
    return {"family": family, "root_id": root_id}


@router.get("/api/gestao/archived")
async def api_archived(request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id,name,color,icon FROM spaces  WHERE archived=1")
        spaces = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT id,name,color,space_id FROM folders WHERE archived=1")
        folders = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT id,name,color,icon,space_id,folder_id FROM lists WHERE archived=1")
        lists = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"spaces": spaces, "folders": folders, "lists": lists}


@router.get("/api/gestao/lists/{list_id}/tasks")
async def api_list_tasks(list_id: str, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT * FROM tasks WHERE list_id=%s ORDER BY position, created_at", (list_id,)
        )
        tasks = [dict(r) for r in cur.fetchall()]
        task_ids = [t["id"] for t in tasks]
        checklists_map: dict = {}
        if task_ids:
            placeholders = ",".join(["%s"] * len(task_ids))
            cur.execute(f"SELECT * FROM task_checklists WHERE task_id IN ({placeholders}) ORDER BY position, created_at", task_ids)
            cls = [dict(r) for r in cur.fetchall()]
            cl_ids = [c["id"] for c in cls]
            items_map: dict = {}
            if cl_ids:
                ph2 = ",".join(["%s"] * len(cl_ids))
                cur.execute(f"SELECT * FROM task_checklist_items WHERE checklist_id IN ({ph2}) ORDER BY position, created_at", cl_ids)
                for item in cur.fetchall():
                    item = dict(item)
                    items_map.setdefault(item["checklist_id"], []).append(item)
            for cl in cls:
                cl["items"] = items_map.get(cl["id"], [])
                checklists_map.setdefault(cl["task_id"], []).append(cl)
    finally:
        cur.close()
        conn.close()
    for t in tasks:
        t["assignees"]   = json.loads(t.get("assignees") or "[]")
        t["extra_data"]  = json.loads(t.get("extra_data") or "{}")
        t["cf_values"]   = json.loads(t.get("cf_values")  or "{}")
        t["checklists"]  = checklists_map.get(t["id"], [])
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

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT COALESCE(MAX(position),0) FROM tasks WHERE list_id=%s", (list_id,))
        pos = (cur.fetchone()["coalesce"] or 0) + 1
        cur.execute(
            "INSERT INTO tasks (id,list_id,title,description,assignees,due_date,status,priority,parent_task_id,position,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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
        cur.execute("SELECT * FROM tasks WHERE id=%s", (tid,))
        task = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    task["assignees"] = json.loads(task["assignees"])
    return {"task": task}


@router.patch("/api/gestao/tasks/{task_id}")
async def api_update_task(task_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"title", "description", "assignees", "due_date", "status", "priority", "position", "extra_data", "cf_values"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    if "assignees" in fields:
        fields["assignees"] = json.dumps(fields["assignees"])
    if "extra_data" in fields and not isinstance(fields["extra_data"], str):
        fields["extra_data"] = json.dumps(fields["extra_data"])
    if "cf_values" in fields and not isinstance(fields["cf_values"], str):
        fields["cf_values"] = json.dumps(fields["cf_values"])

    set_clause = ", ".join(f"{k}=%s" for k in fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE tasks SET {set_clause} WHERE id=%s", [*fields.values(), task_id])
        conn.commit()
        cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Tarefa não encontrada")
    task = dict(row)
    task["assignees"]  = json.loads(task["assignees"] or "[]")
    task["extra_data"] = json.loads(task["extra_data"] or "{}")
    task["cf_values"]  = json.loads(task.get("cf_values") or "{}")
    return {"task": task}


@router.delete("/api/gestao/tasks/{task_id}")
async def api_delete_task(task_id: str, request: Request):
    _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.patch("/api/gestao/tasks/bulk")
async def api_bulk_update_tasks(request: Request):
    """
    Atualiza N tarefas com o mesmo patch.
    Body: { ids: [task_id, ...], patch: {status?, assignees?, due_date?, priority?, list_id?, cf_set?} }
    cf_set = { cf_id: value }  → merge no cf_values existente
    """
    _require(request)
    data = await request.json()
    ids = data.get("ids") or []
    patch = data.get("patch") or {}
    if not ids or not isinstance(ids, list):
        raise HTTPException(400, "ids vazio")

    allowed = {"status", "assignees", "due_date", "priority", "list_id"}
    fields = {k: v for k, v in patch.items() if k in allowed}
    cf_set = patch.get("cf_set") or {}

    if not fields and not cf_set:
        raise HTTPException(400, "Nenhum campo válido")

    if "assignees" in fields and not isinstance(fields["assignees"], str):
        fields["assignees"] = json.dumps(fields["assignees"])

    conn = get_conn()
    cur = dict_cursor(conn)
    updated = []
    try:
        for task_id in ids:
            # 1. fields diretos
            if fields:
                set_clause = ", ".join(f"{k}=%s" for k in fields)
                cur.execute(
                    f"UPDATE tasks SET {set_clause} WHERE id=%s",
                    [*fields.values(), task_id],
                )
            # 2. cf_values merge
            if cf_set:
                cur.execute("SELECT cf_values FROM tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
                if row:
                    try:
                        cv = json.loads(row.get("cf_values") or "{}")
                    except Exception:
                        cv = {}
                    if not isinstance(cv, dict):
                        cv = {}
                    for k, v in cf_set.items():
                        if v is None or v == "":
                            cv.pop(k, None)
                        else:
                            cv[k] = v
                    cur.execute(
                        "UPDATE tasks SET cf_values=%s WHERE id=%s",
                        (json.dumps(cv), task_id),
                    )
            updated.append(task_id)
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True, "updated": updated}


@router.post("/api/gestao/tasks/bulk-delete")
async def api_bulk_delete_tasks(request: Request):
    """Exclui N tarefas em lote. Body: { ids: [...] }"""
    _require(request)
    data = await request.json()
    ids = data.get("ids") or []
    if not ids or not isinstance(ids, list):
        raise HTTPException(400, "ids vazio")
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM tasks WHERE id = ANY(%s)", (ids,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True, "deleted": ids}


# ── Checklists ────────────────────────────────────────────────────────────────

@router.get("/api/gestao/tasks/{task_id}/checklists")
async def api_get_checklists(task_id: str, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM task_checklists WHERE task_id=%s ORDER BY position, created_at", (task_id,))
        checklists = [dict(r) for r in cur.fetchall()]
        for cl in checklists:
            cur.execute("SELECT * FROM task_checklist_items WHERE checklist_id=%s ORDER BY position, created_at", (cl["id"],))
            cl["items"] = [dict(i) for i in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"checklists": checklists}


@router.post("/api/gestao/tasks/{task_id}/checklists")
async def api_create_checklist(task_id: str, request: Request):
    _require(request)
    data = await request.json()
    name = (data.get("name") or "Checklist").strip()
    cid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT COALESCE(MAX(position),0) FROM task_checklists WHERE task_id=%s", (task_id,))
        pos = (cur.fetchone()["coalesce"] or 0) + 1
        cur.execute(
            "INSERT INTO task_checklists (id,task_id,name,position,created_at) VALUES (%s,%s,%s,%s,%s)",
            (cid, task_id, name, pos, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM task_checklists WHERE id=%s", (cid,))
        cl = dict(cur.fetchone())
        cl["items"] = []
    finally:
        cur.close()
        conn.close()
    return {"checklist": cl}


@router.patch("/api/gestao/checklists/{checklist_id}")
async def api_update_checklist(checklist_id: str, request: Request):
    _require(request)
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name obrigatório")
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE task_checklists SET name=%s WHERE id=%s", (name, checklist_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.delete("/api/gestao/checklists/{checklist_id}")
async def api_delete_checklist(checklist_id: str, request: Request):
    _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM task_checklists WHERE id=%s", (checklist_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.post("/api/gestao/checklists/{checklist_id}/items")
async def api_create_checklist_item(checklist_id: str, request: Request):
    _require(request)
    data = await request.json()
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text obrigatório")
    iid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT COALESCE(MAX(position),0) FROM task_checklist_items WHERE checklist_id=%s", (checklist_id,))
        pos = (cur.fetchone()["coalesce"] or 0) + 1
        cur.execute(
            "INSERT INTO task_checklist_items (id,checklist_id,text,resolved,position,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (iid, checklist_id, text, 0, pos, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM task_checklist_items WHERE id=%s", (iid,))
        item = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"item": item}


@router.patch("/api/gestao/checklist_items/{item_id}")
async def api_update_checklist_item(item_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"text", "resolved"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    set_clause = ", ".join(f"{k}=%s" for k in fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE task_checklist_items SET {set_clause} WHERE id=%s", [*fields.values(), item_id])
        conn.commit()
        cur.execute("SELECT * FROM task_checklist_items WHERE id=%s", (item_id,))
        item = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"item": item}


@router.delete("/api/gestao/checklist_items/{item_id}")
async def api_delete_checklist_item(item_id: str, request: Request):
    _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM task_checklist_items WHERE id=%s", (item_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
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

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT COALESCE(MAX(position),0) FROM spaces")
        pos = (cur.fetchone()["coalesce"] or 0) + 1
        cur.execute(
            "INSERT INTO spaces (id,name,color,icon,position,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (sid, name, data.get("color", "#ff4d00"), data.get("icon", "⚡"), pos, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM spaces WHERE id=%s", (sid,))
        space = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
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

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT COALESCE(MAX(position),0) FROM folders WHERE space_id=%s", (space_id,))
        pos = (cur.fetchone()["coalesce"] or 0) + 1
        cur.execute(
            "INSERT INTO folders (id,space_id,name,position,created_at) VALUES (%s,%s,%s,%s,%s)",
            (fid, space_id, name, pos, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM folders WHERE id=%s", (fid,))
        folder = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"folder": {**folder, "lists": []}}


@router.post("/api/gestao/lists")
async def api_create_list(request: Request):
    _require(request)
    data = await request.json()
    name      = (data.get("name") or "").strip()
    space_id  = data.get("space_id", "")
    if not name or not space_id:
        raise HTTPException(400, "name e space_id obrigatórios")
    lid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    folder_id = data.get("folder_id") or None

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT COALESCE(MAX(position),0) FROM lists WHERE space_id=%s", (space_id,))
        pos = (cur.fetchone()["coalesce"] or 0) + 1
        cur.execute(
            "INSERT INTO lists (id,name,space_id,folder_id,position,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (lid, name, space_id, folder_id, pos, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM lists WHERE id=%s", (lid,))
        lst = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"list": lst}


@router.delete("/api/gestao/spaces/{space_id}")
async def api_delete_space(space_id: str, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT label FROM features WHERE space_id=%s", (space_id,))
        tools = cur.fetchall()
        if tools:
            names = ", ".join(r["label"] for r in tools)
            raise HTTPException(409, f"Mova as ferramentas antes de excluir este espaço: {names}")
        cur.execute("DELETE FROM spaces WHERE id=%s", (space_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.patch("/api/gestao/spaces/{space_id}")
async def api_update_space(space_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"name", "color", "icon", "custom_statuses", "archived", "permissions"}
    fields = {k: v for k, v in data.items() if k in allowed and v is not None}
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        if not fields["name"]:
            raise HTTPException(400, "name não pode ser vazio")
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    set_clause = ", ".join(f"{k}=%s" for k in fields)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE spaces SET {set_clause} WHERE id=%s", [*fields.values(), space_id])
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.delete("/api/gestao/folders/{folder_id}")
async def api_delete_folder(folder_id: str, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT label FROM features WHERE folder_id=%s", (folder_id,))
        tools = cur.fetchall()
        if tools:
            names = ", ".join(r["label"] for r in tools)
            raise HTTPException(409, f"Mova as ferramentas antes de excluir esta pasta: {names}")
        cur.execute("DELETE FROM folders WHERE id=%s", (folder_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.patch("/api/gestao/folders/{folder_id}")
async def api_update_folder(folder_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"name", "color", "archived", "permissions", "custom_statuses"}
    fields = {k: v for k, v in data.items() if k in allowed and v is not None}
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        if not fields["name"]:
            raise HTTPException(400, "name não pode ser vazio")
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    set_clause = ", ".join(f"{k}=%s" for k in fields)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE folders SET {set_clause} WHERE id=%s", [*fields.values(), folder_id])
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.delete("/api/gestao/lists/{list_id}")
async def api_delete_list(list_id: str, request: Request):
    _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM lists WHERE id=%s", (list_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.patch("/api/gestao/lists/{list_id}")
async def api_update_list(list_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"name", "color", "icon", "archived", "permissions", "custom_fields", "custom_statuses"}
    fields = {k: v for k, v in data.items() if k in allowed and v is not None}
    if "custom_fields" in fields and not isinstance(fields["custom_fields"], str):
        fields["custom_fields"] = json.dumps(fields["custom_fields"])
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        if not fields["name"]:
            raise HTTPException(400, "name não pode ser vazio")
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    set_clause = ", ".join(f"{k}=%s" for k in fields)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE lists SET {set_clause} WHERE id=%s", [*fields.values(), list_id])
        conn.commit()
    finally:
        cur.close()
        conn.close()
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

    conn = get_conn()
    cur = conn.cursor()
    try:
        for item in items:
            fields: dict = {"position": item["position"]}
            if item_type == "folder" and "space_id" in item:
                fields["space_id"] = item["space_id"]
            if item_type in ("list", "feature"):
                if "space_id" in item:
                    fields["space_id"] = item["space_id"]
                if "folder_id" in item:
                    fields["folder_id"] = item.get("folder_id")
            set_clause = ", ".join(f"{k}=%s" for k in fields)
            cur.execute(f"UPDATE {table} SET {set_clause} WHERE id=%s", [*fields.values(), item["id"]])
        conn.commit()
    finally:
        cur.close()
        conn.close()
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
    if "folder_id" in data:
        fields["folder_id"] = data["folder_id"]
    set_clause = ", ".join(f"{k}=%s" for k in fields)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE features SET {set_clause} WHERE id=%s", [*fields.values(), feature_id])
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


# ── Duplicate ─────────────────────────────────────────────────────────────────

@router.post("/api/gestao/spaces/{space_id}/duplicate")
async def api_duplicate_space(space_id: str, request: Request):
    _require(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    now = datetime.now().isoformat()

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM spaces WHERE id=%s", (space_id,))
        orig = cur.fetchone()
        if not orig:
            raise HTTPException(404, "Espaço não encontrado")
        orig = dict(orig)
        new_name = body.get("name") or (orig["name"] + " (cópia)")
        include_tasks = body.get("include_tasks", True)
        new_sid = str(uuid.uuid4())

        cur.execute("SELECT COALESCE(MAX(position),0) FROM spaces")
        pos = (cur.fetchone()["coalesce"] or 0) + 1
        cur.execute(
            "INSERT INTO spaces (id,name,color,icon,position,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (new_sid, new_name, orig["color"], orig["icon"], pos, now),
        )

        folder_map = {}
        cur.execute("SELECT * FROM folders WHERE space_id=%s AND archived=0", (space_id,))
        for f in cur.fetchall():
            f = dict(f)
            new_fid = str(uuid.uuid4())
            folder_map[f["id"]] = new_fid
            cur.execute(
                "INSERT INTO folders (id,space_id,name,position,created_at) VALUES (%s,%s,%s,%s,%s)",
                (new_fid, new_sid, f["name"], f["position"], now),
            )

        cur.execute("SELECT * FROM lists WHERE space_id=%s AND archived=0", (space_id,))
        for l in cur.fetchall():
            l = dict(l)
            new_lid = str(uuid.uuid4())
            new_fid = folder_map.get(l["folder_id"]) if l["folder_id"] else None
            cur.execute(
                "INSERT INTO lists (id,name,space_id,folder_id,position,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (new_lid, l["name"], new_sid, new_fid, l["position"], now),
            )
            if include_tasks:
                cur.execute("SELECT * FROM tasks WHERE list_id=%s", (l["id"],))
                for t in cur.fetchall():
                    t = dict(t)
                    cur.execute(
                        "INSERT INTO tasks (id,list_id,title,description,assignees,due_date,status,priority,parent_task_id,position,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (str(uuid.uuid4()), new_lid, t["title"], t["description"], t["assignees"],
                         t["due_date"], t["status"], t["priority"], None, t["position"], now),
                    )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.post("/api/gestao/folders/{folder_id}/duplicate")
async def api_duplicate_folder(folder_id: str, request: Request):
    _require(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    now = datetime.now().isoformat()

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM folders WHERE id=%s", (folder_id,))
        orig = dict(cur.fetchone())
        new_name = body.get("name") or (orig["name"] + " (cópia)")
        target_space_id = body.get("target_space_id") or orig["space_id"]
        include_tasks = body.get("include_tasks", True)
        new_fid = str(uuid.uuid4())

        cur.execute("SELECT COALESCE(MAX(position),0) FROM folders WHERE space_id=%s", (target_space_id,))
        pos = (cur.fetchone()["coalesce"] or 0) + 1
        cur.execute(
            "INSERT INTO folders (id,space_id,name,position,created_at) VALUES (%s,%s,%s,%s,%s)",
            (new_fid, target_space_id, new_name, pos, now),
        )

        cur.execute("SELECT * FROM lists WHERE folder_id=%s", (folder_id,))
        for l in cur.fetchall():
            l = dict(l)
            new_lid = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO lists (id,name,space_id,folder_id,position,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (new_lid, l["name"], target_space_id, new_fid, l["position"], now),
            )
            if include_tasks:
                cur.execute("SELECT * FROM tasks WHERE list_id=%s", (l["id"],))
                for t in cur.fetchall():
                    t = dict(t)
                    cur.execute(
                        "INSERT INTO tasks (id,list_id,title,description,assignees,due_date,status,priority,parent_task_id,position,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (str(uuid.uuid4()), new_lid, t["title"], t["description"], t["assignees"],
                         t["due_date"], t["status"], t["priority"], None, t["position"], now),
                    )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.post("/api/gestao/lists/{list_id}/duplicate")
async def api_duplicate_list(list_id: str, request: Request):
    _require(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    now = datetime.now().isoformat()

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM lists WHERE id=%s", (list_id,))
        orig = dict(cur.fetchone())
        new_name = body.get("name") or (orig["name"] + " (cópia)")
        target_space_id  = body.get("target_space_id") or orig["space_id"]
        target_folder_id = body.get("target_folder_id") if "target_folder_id" in body else orig["folder_id"]
        include_tasks = body.get("include_tasks", True)
        new_lid = str(uuid.uuid4())

        cur.execute("SELECT COALESCE(MAX(position),0) FROM lists WHERE space_id=%s", (target_space_id,))
        pos = (cur.fetchone()["coalesce"] or 0) + 1
        cur.execute(
            "INSERT INTO lists (id,name,space_id,folder_id,position,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (new_lid, new_name, target_space_id, target_folder_id, pos, now),
        )

        if include_tasks:
            cur.execute("SELECT * FROM tasks WHERE list_id=%s", (list_id,))
            for t in cur.fetchall():
                t = dict(t)
                cur.execute(
                    "INSERT INTO tasks (id,list_id,title,description,assignees,due_date,status,priority,parent_task_id,position,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), new_lid, t["title"], t["description"], t["assignees"],
                     t["due_date"], t["status"], t["priority"], None, t["position"], now),
                )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


# ── Forms ─────────────────────────────────────────────────────────────────────

def _form_parse(form: dict) -> dict:
    form["fields"]   = json.loads(form.get("fields")   or "[]")
    form["settings"] = json.loads(form.get("settings") or "{}")
    form["submission_count"] = int(form.get("submission_count") or 0)
    return form


@router.get("/api/gestao/lists/{list_id}/forms")
async def api_list_forms(list_id: str, request: Request):
    _require(request)
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute("""
            SELECT f.*, COUNT(s.id) AS submission_count
            FROM forms f
            LEFT JOIN form_submissions s ON s.form_id = f.id
            WHERE f.list_id = %s
            GROUP BY f.id
            ORDER BY f.created_at ASC
        """, (list_id,))
        forms = [_form_parse(dict(r)) for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()
    return {"forms": forms}


@router.post("/api/gestao/lists/{list_id}/forms")
async def api_create_form(list_id: str, request: Request):
    _require(request)
    data = await request.json()
    fid  = str(uuid.uuid4())
    now  = datetime.now().isoformat()
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute(
            "INSERT INTO forms (id,list_id,name,description,fields,settings,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (fid, list_id,
             data.get("name", "Formulário"),
             data.get("description", ""),
             json.dumps(data.get("fields", [])),
             json.dumps(data.get("settings", {})),
             now),
        )
        conn.commit()
        cur.execute("SELECT * FROM forms WHERE id=%s", (fid,))
        form = _form_parse(dict(cur.fetchone()))
    finally:
        cur.close(); conn.close()
    return {"form": form}


@router.patch("/api/gestao/forms/{form_id}")
async def api_update_form(form_id: str, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"name", "description", "fields", "settings", "cover_url"}
    upd = {k: v for k, v in data.items() if k in allowed}
    if not upd:
        raise HTTPException(400, "Nenhum campo válido")
    for k in ("fields", "settings"):
        if k in upd and not isinstance(upd[k], str):
            upd[k] = json.dumps(upd[k])
    sets = ", ".join(f"{k}=%s" for k in upd)
    vals = list(upd.values()) + [form_id]
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE forms SET {sets} WHERE id=%s", vals)
        conn.commit()
        cur.execute("SELECT * FROM forms WHERE id=%s", (form_id,))
        form = _form_parse(dict(cur.fetchone()))
    finally:
        cur.close(); conn.close()
    return {"form": form}


@router.delete("/api/gestao/forms/{form_id}")
async def api_delete_form(form_id: str, request: Request):
    _require(request)
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute("DELETE FROM forms WHERE id=%s", (form_id,))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return {"ok": True}


@router.get("/api/gestao/forms/{form_id}/submissions")
async def api_form_submissions(form_id: str, request: Request):
    _require(request)
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT * FROM form_submissions WHERE form_id=%s ORDER BY submitted_at DESC",
            (form_id,),
        )
        subs = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()
    for s in subs:
        s["data"] = json.loads(s.get("data") or "{}")
    return {"submissions": subs, "count": len(subs)}


@router.delete("/api/gestao/forms/{form_id}/submissions/{sub_id}")
async def api_delete_submission(form_id: str, sub_id: str, request: Request):
    _require(request)
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute("DELETE FROM form_submissions WHERE id=%s AND form_id=%s", (sub_id, form_id))
        conn.commit()
    finally:
        cur.close(); conn.close()
    return {"ok": True}


@router.get("/f/{form_id}", response_class=HTMLResponse)
async def public_form_get(form_id: str, request: Request):
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM forms WHERE id=%s", (form_id,))
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not row:
        return HTMLResponse("<h1 style='font-family:sans-serif;padding:40px'>Formulário não encontrado</h1>", status_code=404)
    form = _form_parse(dict(row))
    return templates.TemplateResponse("form_public.html", {"request": request, "form": form})


@router.post("/f/{form_id}")
async def public_form_submit(form_id: str, request: Request):
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM forms WHERE id=%s", (form_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Formulário não encontrado")
        form     = _form_parse(dict(row))
        settings = form["settings"]
        body     = await request.json()

        title = f"Resposta — {form['name']}"
        for field in form["fields"]:
            val = body.get(field["id"], "")
            if val and field["type"] in ("short_text", "long_text", "email"):
                title = str(val)[:100]
                break

        desc = ""
        if settings.get("add_to_description", True):
            lines = []
            for field in form["fields"]:
                val = body.get(field["id"])
                if val is not None and val != "" and val != []:
                    if isinstance(val, list):
                        val = ", ".join(str(v) for v in val)
                    lines.append(f"**{field['label']}:** {val}")
            desc = "\n".join(lines)

        target_list_id = settings.get("target_list_id") or form["list_id"]
        assignees = json.dumps(settings.get("default_assignees") or [])

        tid = str(uuid.uuid4())
        now = datetime.now().isoformat()
        cur.execute("SELECT COALESCE(MAX(position),0) FROM tasks WHERE list_id=%s", (target_list_id,))
        pos = (cur.fetchone()["coalesce"] or 0) + 1
        cur.execute(
            "INSERT INTO tasks (id,list_id,title,description,assignees,status,priority,position,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tid, target_list_id, title, desc, assignees, "aberto", "normal", pos, now),
        )
        sid = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO form_submissions (id,form_id,data,task_id,submitted_at) VALUES (%s,%s,%s,%s,%s)",
            (sid, form_id, json.dumps(body), tid, now),
        )
        conn.commit()
        redirect_url = settings.get("redirect_url", "")
    finally:
        cur.close(); conn.close()
    return {"ok": True, "task_id": tid, "redirect_url": redirect_url}
