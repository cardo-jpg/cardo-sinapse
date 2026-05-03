from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from backend.gestao import _require
from backend.db import get_conn, dict_cursor

router = APIRouter()
BASE_DIR  = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))

DEFAULT_STAGES = [
    ("prospeccao",  "Prospecção",       "#7986cb", 1),
    ("qualificado", "Qualificado",      "#ff9800", 2),
    ("proposta",    "Proposta Enviada", "#26c6da", 3),
    ("negociacao",  "Em Negociação",    "#ab47bc", 4),
    ("ganho",       "Ganho",            "#4caf50", 5),
    ("perdido",     "Perdido",          "#ef5350", 6),
]


def init_crm_db():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crm_stages (
                id       TEXT    PRIMARY KEY,
                name     TEXT    NOT NULL,
                color    TEXT    DEFAULT '#666',
                position INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crm_deals (
                id         SERIAL  PRIMARY KEY,
                stage_id   TEXT    NOT NULL REFERENCES crm_stages(id),
                title      TEXT    NOT NULL,
                company    TEXT    DEFAULT '',
                contact    TEXT    DEFAULT '',
                email      TEXT    DEFAULT '',
                phone      TEXT    DEFAULT '',
                value      REAL    DEFAULT 0,
                owner      TEXT    DEFAULT '',
                origem     TEXT    DEFAULT '',
                due_date   TEXT    DEFAULT '',
                notes      TEXT    DEFAULT '',
                position   INTEGER DEFAULT 0,
                created_at TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Seed stages
        cur.execute("SELECT id FROM crm_stages")
        existing = {r[0] for r in cur.fetchall()}
        for sid, name, color, pos in DEFAULT_STAGES:
            if sid not in existing:
                cur.execute(
                    "INSERT INTO crm_stages (id, name, color, position) VALUES (%s,%s,%s,%s)",
                    (sid, name, color, pos)
                )
        conn.commit()
    finally:
        cur.close()
        conn.close()


@router.get("/crm", response_class=HTMLResponse)
async def crm_page(request: Request):
    user = _require(request)
    return templates.TemplateResponse("crm.html", {"request": request, "user": user})


@router.get("/api/crm/board")
async def api_crm_board(request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM crm_stages ORDER BY position")
        stages = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM crm_deals ORDER BY position, created_at")
        deals = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"stages": stages, "deals": deals}


@router.post("/api/crm/deals")
async def api_crm_create_deal(request: Request):
    _require(request)
    body = await request.json()
    stage_id = body.get("stage_id", "prospeccao")
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            """INSERT INTO crm_deals (stage_id, title, company, contact, email, phone, value, owner, origem, due_date, notes)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (stage_id, title,
             body.get("company", ""), body.get("contact", ""),
             body.get("email", ""), body.get("phone", ""),
             float(body.get("value") or 0),
             body.get("owner", ""), body.get("origem", ""),
             body.get("due_date", ""), body.get("notes", ""))
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.execute("SELECT * FROM crm_deals WHERE id=%s", (new_id,))
        deal = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"deal": deal}


@router.patch("/api/crm/deals/{deal_id}")
async def api_crm_update_deal(deal_id: int, request: Request):
    _require(request)
    body = await request.json()
    allowed = ["stage_id", "title", "company", "contact", "email", "phone",
               "value", "owner", "origem", "due_date", "notes", "position"]
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "no fields")
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        sets = ", ".join(f"{k}=%s" for k in fields)
        cur.execute(
            f"UPDATE crm_deals SET {sets} WHERE id=%s",
            list(fields.values()) + [deal_id]
        )
        conn.commit()
        cur.execute("SELECT * FROM crm_deals WHERE id=%s", (deal_id,))
        deal = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"deal": deal}


@router.delete("/api/crm/deals/{deal_id}")
async def api_crm_delete_deal(deal_id: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM crm_deals WHERE id=%s", (deal_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}
