"""
Publicação de atas e criação de tarefas no Sinapse (substitui o ClickUp).

Estrutura de destino das ATAS:
  Operacional > Gestão de Contas > [doc] Documentação
    > [SIGLA] Cliente
      > Atas de Reunião
        > DD/MM/AAAA - Título da reunião   ← novo doc

Pra cada cliente novo, o sistema cria automaticamente o nó intermediário.

Pra TARFAS: o frontend escolhe a list_id de destino (sugestão da IA + usuário valida).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Optional, Tuple

import markdown as _markdown_lib
from fastapi import APIRouter, HTTPException, Request

from backend.db import get_conn, dict_cursor
from backend.gestao import _require

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de normalização
# ─────────────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Lowercase + sem acentos pra comparação robusta."""
    import unicodedata
    if not s:
        return ""
    nfkd = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def _md_to_html(md: str) -> str:
    """Converte markdown da ata em HTML pro editor TipTap consumir."""
    if not md:
        return ""
    return _markdown_lib.markdown(md, extensions=["extra", "sane_lists", "nl2br"])


def _format_date_br(date_iso: str) -> str:
    """YYYY-MM-DD → DD/MM/AAAA. Aceita formato BR também (passthrough)."""
    if not date_iso:
        return ""
    try:
        if "/" in date_iso:
            return date_iso
        y, m, d = date_iso.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return date_iso


# ─────────────────────────────────────────────────────────────────────────────
# Resolução da hierarquia Documentação > [SIGLA] Cliente > Atas de Reunião
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_client_name(cur, sigla: str) -> str:
    """Pega o nome canônico do cliente do Painel de Clientes."""
    cur.execute(
        "SELECT id FROM lists WHERE LOWER(name) IN ('painel de clientes','painel de cliente') LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        return ""
    cur.execute(
        "SELECT title FROM tasks WHERE list_id=%s AND title ILIKE %s LIMIT 1",
        (row["id"], f"[{sigla.upper()}]%"),
    )
    t = cur.fetchone()
    if not t:
        return ""
    m = re.match(r"^\s*\[[A-Za-z0-9]+\]\s*(.+?)\s*$", t["title"])
    return (m.group(1).strip() if m else "")


def _create_doc(cur, *, title: str, content: str, space_id: str,
                folder_id: Optional[str] = None, parent_id: Optional[str] = None) -> dict:
    doc_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    cur.execute(
        "SELECT COALESCE(MAX(position),0) FROM documents WHERE space_id=%s",
        (space_id,),
    )
    pos = (cur.fetchone()["coalesce"] or 0) + 1
    cur.execute(
        "INSERT INTO documents (id,title,content,space_id,folder_id,parent_id,position,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (doc_id, title, content, space_id, folder_id, parent_id, pos, now, now),
    )
    return {
        "id": doc_id, "title": title, "content": content,
        "space_id": space_id, "folder_id": folder_id, "parent_id": parent_id,
    }


def _resolve_or_create_meeting_parent(cur, sigla: str) -> dict:
    """
    Garante que existe a hierarquia:
      Operacional > Gestão de Contas > [doc] Documentação
        > [SIGLA] Cliente
          > Atas de Reunião   ← retorna esse

    Cria os nós intermediários se não existirem (cliente novo).
    """
    sigla = sigla.upper()

    # 1. Espaço Operacional
    cur.execute("SELECT id, name FROM spaces WHERE LOWER(name)=%s AND COALESCE(archived,0)=0 LIMIT 1", ("operacional",))
    space = cur.fetchone()
    if not space:
        raise HTTPException(404, "Espaço 'Operacional' não encontrado no Sinapse")

    # 2. Folder Gestão de Contas
    cur.execute(
        "SELECT id, name FROM folders WHERE space_id=%s AND %s = ANY(string_to_array(LOWER(name),'')) "
        "OR (space_id=%s AND LOWER(name) LIKE %s) LIMIT 1",
        (space["id"], "x", space["id"], "gest%o de contas"),
    )
    # query simples e direta (a anterior estava complicada; uso a direta)
    cur.execute(
        "SELECT id, name FROM folders WHERE space_id=%s AND COALESCE(archived,0)=0 ORDER BY position",
        (space["id"],),
    )
    folder = None
    for f in cur.fetchall():
        if _norm(f["name"]) == _norm("Gestão de Contas"):
            folder = f
            break
    if not folder:
        raise HTTPException(404, "Pasta 'Gestão de Contas' não encontrada em Operacional")

    # 3. Doc raiz "Documentação" no folder
    cur.execute(
        "SELECT id, title, space_id FROM documents WHERE folder_id=%s AND parent_id IS NULL ORDER BY position",
        (folder["id"],),
    )
    doc_root = None
    for d in cur.fetchall():
        if _norm(d["title"]) in (_norm("Documentação"), _norm("Documentacao")):
            doc_root = d
            break
    if not doc_root:
        raise HTTPException(
            404,
            "Documento raiz 'Documentação' não encontrado em Operacional > Gestão de Contas. "
            "Crie esse documento manualmente uma única vez.",
        )

    # 4. [SIGLA] Cliente — acha ou cria
    cliente_nome = _resolve_client_name(cur, sigla)
    cliente_title = f"[{sigla}] {cliente_nome}" if cliente_nome else f"[{sigla}]"
    cur.execute(
        "SELECT id, title FROM documents WHERE parent_id=%s AND title ILIKE %s LIMIT 1",
        (doc_root["id"], f"[{sigla}]%"),
    )
    doc_cliente = cur.fetchone()
    if not doc_cliente:
        doc_cliente = _create_doc(
            cur, title=cliente_title, content="",
            space_id=space["id"], folder_id=folder["id"], parent_id=doc_root["id"],
        )

    # 5. "Atas de Reunião" como subpágina — acha ou cria
    cur.execute(
        "SELECT id, title FROM documents WHERE parent_id=%s ORDER BY position",
        (doc_cliente["id"],),
    )
    doc_atas = None
    for d in cur.fetchall():
        if _norm(d["title"]) in (_norm("Atas de Reunião"), _norm("Atas de Reuniao"), _norm("Atas")):
            doc_atas = d
            break
    if not doc_atas:
        doc_atas = _create_doc(
            cur, title="Atas de Reunião", content="",
            space_id=space["id"], folder_id=folder["id"], parent_id=doc_cliente["id"],
        )

    return {
        "space_id":   space["id"],
        "folder_id":  folder["id"],
        "doc_root":   doc_root,
        "doc_cliente": dict(doc_cliente) if not isinstance(doc_cliente, dict) else doc_cliente,
        "doc_atas":   dict(doc_atas) if not isinstance(doc_atas, dict) else doc_atas,
        "client_name": cliente_nome,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/ata/sinapse/publish")
async def publish_ata_sinapse(request: Request):
    """
    Publica uma ata no Sinapse na hierarquia padrão.
    Body: { client_sigla, content (markdown), date (YYYY-MM-DD ou DD/MM/AAAA),
            meeting_title (opcional) }
    Retorna: { ok, doc_id, url, breadcrumb }
    """
    _require(request)
    body = await request.json()
    sigla         = (body.get("client_sigla") or "").strip().upper()
    content_md    = body.get("content") or ""
    date_iso      = body.get("date") or ""
    meeting_title = (body.get("meeting_title") or "").strip()

    if not sigla:
        raise HTTPException(400, "client_sigla obrigatório")
    if not content_md.strip():
        raise HTTPException(400, "content vazio")

    date_br = _format_date_br(date_iso)
    title   = f"{date_br} - {meeting_title}" if meeting_title else (date_br or "Ata sem data")

    conn = get_conn(); cur = dict_cursor(conn)
    try:
        nodes = _resolve_or_create_meeting_parent(cur, sigla)
        # Cria a ata como subpágina de "Atas de Reunião"
        ata = _create_doc(
            cur,
            title=title,
            content=_md_to_html(content_md),
            space_id=nodes["space_id"],
            folder_id=nodes["folder_id"],
            parent_id=nodes["doc_atas"]["id"],
        )
        conn.commit()
    finally:
        cur.close(); conn.close()

    breadcrumb = " · ".join([
        "Operacional",
        "Gestão de Contas",
        "Documentação",
        nodes["doc_cliente"]["title"],
        "Atas de Reunião",
        title,
    ])
    return {
        "ok":          True,
        "doc_id":      ata["id"],
        "url":         f"/gestao#doc:{ata['id']}",
        "breadcrumb":  breadcrumb,
        "client_name": nodes["client_name"],
    }


@router.get("/api/sinapse/lists")
async def sinapse_lists(request: Request):
    """Lista todas as listas ativas do Sinapse com breadcrumb (Espaço · Pasta · Lista)."""
    _require(request)
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute("""
            SELECT l.id, l.name AS list_name, l.icon,
                   l.space_id, l.folder_id,
                   s.name AS space_name,
                   COALESCE(f.name,'') AS folder_name
            FROM lists l
            LEFT JOIN spaces  s ON s.id = l.space_id
            LEFT JOIN folders f ON f.id = l.folder_id
            WHERE COALESCE(l.archived,0) = 0
              AND COALESCE(s.archived,0) = 0
              AND (f.id IS NULL OR COALESCE(f.archived,0) = 0)
            ORDER BY s.name, f.name, l.name
        """)
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()

    items = []
    for r in rows:
        breadcrumb_parts = [r.get("space_name") or ""]
        if r.get("folder_name"):
            breadcrumb_parts.append(r["folder_name"])
        breadcrumb_parts.append(r["list_name"])
        items.append({
            "id":          r["id"],
            "name":        r["list_name"],
            "icon":        r.get("icon") or "",
            "space_id":    r["space_id"],
            "space_name":  r.get("space_name") or "",
            "folder_id":   r.get("folder_id"),
            "folder_name": r.get("folder_name"),
            "breadcrumb":  " · ".join([p for p in breadcrumb_parts if p]),
        })
    return {"lists": items}


@router.post("/api/ata/sinapse/extract-tasks")
async def extract_tasks_for_ata(request: Request):
    """
    Pede pro Claude extrair tarefas da ata + sugerir lista do Sinapse.
    Body: { content, client_sigla, client_name }
    Retorna: { tasks: [{name, description, assignee, due_date, suggested_list_name, is_client}] }
    """
    _require(request)
    body = await request.json()
    ata_content   = body.get("content", "")
    client_sigla  = (body.get("client_sigla") or "").upper()
    client_name   = body.get("client_name", "")

    if not ata_content.strip():
        raise HTTPException(400, "content vazio")

    # Pega listas pra dar contexto ao modelo (assim ele sugere uma das que existe)
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute("""
            SELECT l.name AS list_name, COALESCE(f.name,'') AS folder_name, s.name AS space_name
            FROM lists l
            LEFT JOIN spaces s ON s.id = l.space_id
            LEFT JOIN folders f ON f.id = l.folder_id
            WHERE COALESCE(l.archived,0) = 0
            ORDER BY s.name, f.name, l.name
        """)
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()

    lists_desc = "\n".join(
        f"- {r['list_name']}"
        + (f"  (em {r.get('folder_name') or r['space_name']})" if (r.get("folder_name") or r.get("space_name")) else "")
        for r in rows
    ) or "(nenhuma lista mapeada)"

    # Importa lazy pra evitar circular import
    from backend.main import client as anthropic_client  # type: ignore

    prompt = f"""Analise esta ata de reunião e extraia TODAS as ações concretas que precisam ser executadas.
Inclua ações da Cardô Marketing E do cliente.

CLIENTE: {client_name} ({client_sigla})

ATA:
{ata_content}

LISTAS DISPONÍVEIS NO SINAPSE (use exatamente um nome dessa lista no campo suggested_list_name):
{lists_desc}

Retorne um JSON com a seguinte estrutura (apenas o JSON, sem texto adicional):
{{
  "tasks": [
    {{
      "name": "[{client_sigla}] Nome da tarefa conciso e acionável",
      "description": "Contexto adicional se necessário (pode ser vazio)",
      "assignee": "victor | jose | jadna | null",
      "due_date": "YYYY-MM-DD se prazo mencionado, senão null",
      "suggested_list_name": "Nome da lista do Sinapse mais apropriada",
      "is_client": true ou false
    }}
  ]
}}

Regras:
- Nome da tarefa: começa com [{client_sigla}], conciso, ação principal
- Para ações do cliente, assignee = null
- Se a ação envolve tráfego/ads/campanhas → escolha lista relacionada a Tráfego
- Se envolve onboarding/atendimento/cliente novo → lista relacionada a Onboarding ou Atendimento
- Se envolve tarefa pontual sem categoria clara → "Tarefas Pontuais"
- Se prazo mencionado (ex: "até sexta"), converta pra YYYY-MM-DD (hoje: {datetime.now().strftime('%Y-%m-%d')})
- Extraia APENAS ações concretas, não discussões"""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system="Você é um extrator de tarefas. Responda APENAS com JSON válido, sem texto antes ou depois, sem markdown.",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = next((b.text for b in response.content if hasattr(b, "text")), "{}").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        tasks_data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            raise HTTPException(500, "Não foi possível extrair tarefas da ata.")
        try:
            tasks_data = json.loads(match.group())
        except json.JSONDecodeError as e:
            raise HTTPException(500, f"Erro ao processar resposta do modelo: {e}")

    return tasks_data


def _parse_statuses_blob(raw: str):
    """Aceita JSON array OU JSON objeto {statuses:[...]}; retorna lista de {id,name,color} ou None."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("statuses") or []
    else:
        return None
    out = [s for s in items if isinstance(s, dict) and s.get("id")]
    return out or None


def _first_status_for_list(cur, list_id: str) -> str:
    """
    Retorna o ID do primeiro status efetivo da lista (custom > folder > space > default 'aberto').
    Espelha a lógica do frontend `get statuses()` em gestao.html.
    """
    try:
        cur.execute(
            "SELECT custom_statuses, space_id, folder_id FROM lists WHERE id=%s LIMIT 1",
            (list_id,),
        )
        row = cur.fetchone()
        if not row:
            return "aberto"
        statuses = _parse_statuses_blob(row.get("custom_statuses"))
        if statuses:
            return statuses[0]["id"]
        if row.get("folder_id"):
            cur.execute("SELECT custom_statuses FROM folders WHERE id=%s LIMIT 1", (row["folder_id"],))
            f = cur.fetchone()
            if f:
                statuses = _parse_statuses_blob(f.get("custom_statuses"))
                if statuses:
                    return statuses[0]["id"]
        if row.get("space_id"):
            cur.execute("SELECT custom_statuses FROM spaces WHERE id=%s LIMIT 1", (row["space_id"],))
            s = cur.fetchone()
            if s:
                statuses = _parse_statuses_blob(s.get("custom_statuses"))
                if statuses:
                    return statuses[0]["id"]
    except Exception:
        pass
    return "aberto"


@router.post("/api/ata/sinapse/create-tasks")
async def create_tasks_sinapse(request: Request):
    """
    Cria tarefas no Sinapse usando os list_ids escolhidos pelo usuário.
    Body: { tasks: [{name, description, assignee, due_date, list_id}], client_sigla }
    Retorna: { created: [...], errors: [...] }

    Status inicial: pega o PRIMEIRO custom_status da lista (ou folder/space na cascata).
    Assim a tarefa nasce com "A fazer" em listas de Tráfego/Redação, em vez do default 'aberto'.
    """
    _require(request)
    body = await request.json()
    tasks = body.get("tasks", []) or []

    created, errors = [], []
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        for t in tasks:
            name      = (t.get("name") or "").strip()
            list_id   = (t.get("list_id") or "").strip()
            if not name or not list_id:
                errors.append({"name": name, "error": "name ou list_id ausente"})
                continue
            description = t.get("description") or ""
            assignee    = (t.get("assignee") or "").strip().lower() or None
            due_date    = (t.get("due_date") or "").strip() or None
            status_id   = _first_status_for_list(cur, list_id)

            tid = str(uuid.uuid4())
            now = datetime.now().isoformat()
            assignees_json = json.dumps([assignee] if assignee else [])
            try:
                cur.execute(
                    "SELECT COALESCE(MAX(position),0) FROM tasks WHERE list_id=%s", (list_id,)
                )
                pos = (cur.fetchone()["coalesce"] or 0) + 1
                cur.execute(
                    "INSERT INTO tasks (id,list_id,title,description,assignees,due_date,status,priority,position,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (tid, list_id, name, description, assignees_json, due_date, status_id, "normal", pos, now),
                )
                conn.commit()
                created.append({"id": tid, "name": name, "list_id": list_id, "status": status_id})
            except Exception as e:
                conn.rollback()
                errors.append({"name": name, "error": str(e)})
    finally:
        cur.close(); conn.close()

    return {"created": created, "errors": errors}
