"""
Briefings públicos — formulário guiado (wizard) para novos clientes.

Fluxo:
  1. Cliente acessa GET /briefing/novo (público, sem auth)
  2. Preenche o wizard, envia → POST /api/briefing/submit
  3. Resposta é gravada em briefing_responses
  4. Tarefa é criada automaticamente em Onboarding & Implementação
  5. Cliente recebe link da resposta + opção de baixar PDF
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from backend.db import get_conn, dict_cursor
from backend.gestao import _require

router = APIRouter()

BASE_DIR  = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))

ONBOARDING_LIST_NAME = "Onboarding & Implementação"


# ─────────────────────────────────────────────────────────────────────────────
# DB schema
# ─────────────────────────────────────────────────────────────────────────────

def init_briefings_db() -> None:
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS briefing_responses (
                id           TEXT PRIMARY KEY,
                client_name  TEXT NOT NULL DEFAULT '',
                client_email TEXT NOT NULL DEFAULT '',
                responses    TEXT NOT NULL DEFAULT '{}',
                task_id      TEXT,
                submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Briefing definition (perguntas — placeholder até receber as reais do ClickUp)
# ─────────────────────────────────────────────────────────────────────────────

BRIEFING_DEFINITION: dict[str, Any] = {
    "title": "Vamos construir sua estratégia digital.",
    "subtitle": (
        "Esse questionário guia você por uma reflexão profunda sobre o seu negócio. "
        "Sinta-se à vontade para responder com calma — quanto mais detalhe, melhor "
        "podemos construir juntos."
    ),
    "ritual": {
        "headline": "Seja bem-vindo(a) ao momento mais estratégico do seu negócio.",
        "lead": (
            "Antes de começar, recomendamos que você crie um ambiente confortável: "
            "respira fundo, separa uns 30-40 minutos sem interrupção, e responde "
            "com a maior sinceridade possível — desse processo nasce a estratégia."
        ),
        "checklist": [
            "Conecte o AMBIENTE: ilumine bem o espaço e silencie notificações",
            "Coloque sua PLAYLIST de foco favorita",
            "Tenha sua BEBIDA FAVORITA por perto — café, chá, água",
            "Pode pausar e voltar — suas respostas ficam salvas no navegador",
            "Conecte a EMOÇÃO ao processo: isso aqui é sobre o seu sonho",
        ],
    },
    "sections": [
        {
            "id": "sobre_voce",
            "title": "Sobre você",
            "subtitle": "Pra começar, queremos te conhecer melhor.",
            "fields": [
                {"id": "nome",          "label": "Seu nome completo",                    "type": "short_text", "required": True},
                {"id": "email",         "label": "Seu melhor e-mail",                    "type": "email",      "required": True},
                {"id": "whatsapp",      "label": "WhatsApp (com DDD)",                   "type": "short_text", "required": True},
                {"id": "cargo",         "label": "Qual seu cargo / papel no negócio?",   "type": "short_text", "required": True},
                {"id": "como_chegou",   "label": "Como você chegou até a Cardō?",        "type": "long_text",  "required": False, "audio": True},
            ],
        },
        {
            "id": "empreendimento",
            "title": "Empreendimento e mercado",
            "subtitle": "Agora vamos falar do seu negócio.",
            "fields": [
                {"id": "empresa_nome", "label": "Nome da empresa / marca",                          "type": "short_text", "required": True},
                {"id": "site",         "label": "Site, Instagram ou principal canal digital",       "type": "short_text", "required": False},
                {"id": "tempo_mercado","label": "Há quanto tempo o negócio existe?",                 "type": "short_text", "required": False},
                {"id": "o_que_faz",    "label": "Em uma frase: o que sua empresa faz?",              "type": "long_text",  "required": True,  "audio": True},
                {"id": "publico_alvo", "label": "Quem é o seu cliente ideal? Como ele vive, pensa, sente?", "type": "long_text", "required": True, "audio": True},
                {"id": "concorrentes", "label": "Quem são seus principais concorrentes / referências?", "type": "long_text", "required": False, "audio": True},
                {"id": "diferencial",  "label": "O que diferencia você dos concorrentes?",            "type": "long_text", "required": True,  "audio": True},
            ],
        },
        {
            "id": "brand",
            "title": "Brand e posicionamento",
            "subtitle": "A alma da marca — propósito, voz, percepção.",
            "fields": [
                {"id": "missao",        "label": "Qual a missão / propósito do negócio?",                 "type": "long_text", "required": False, "audio": True},
                {"id": "valores",       "label": "Quais valores guiam a marca?",                          "type": "long_text", "required": False, "audio": True},
                {"id": "personalidade", "label": "Se a marca fosse uma pessoa, como ela seria?",          "type": "long_text", "required": False, "audio": True},
                {"id": "ja_visto",      "label": "Existe alguma marca (sua área ou outra) que admira pela comunicação? Por quê?", "type": "long_text", "required": False, "audio": True},
            ],
        },
        {
            "id": "atracao",
            "title": "Atração e produtos",
            "subtitle": "Como o cliente entra em contato com você hoje?",
            "fields": [
                {"id": "produtos",     "label": "Liste seus principais produtos/serviços e ticket médio", "type": "long_text", "required": True,  "audio": True},
                {"id": "carro_chefe",  "label": "Qual é o carro-chefe? O que mais vende?",                "type": "long_text", "required": True,  "audio": True},
                {"id": "ja_tentou",    "label": "Já fez ações de marketing digital antes? O que funcionou e o que não?", "type": "long_text", "required": False, "audio": True},
                {"id": "investimento", "label": "Qual o investimento mensal previsto em mídia paga?",     "type": "short_text", "required": False},
                {"id": "metas_90",     "label": "Quais metas você quer atingir nos próximos 90 dias?",    "type": "long_text", "required": True, "audio": True},
            ],
        },
        {
            "id": "operacao",
            "title": "Operação e logística",
            "subtitle": "Pra garantir uma entrega fluida.",
            "fields": [
                {"id": "horario",       "label": "Horário/dia preferido para reuniões",            "type": "short_text", "required": False},
                {"id": "responsaveis",  "label": "Quem mais participa das decisões de marketing?", "type": "long_text",  "required": False, "audio": True},
                {"id": "ferramentas",   "label": "Quais ferramentas vocês já usam? (CRM, e-mail, ads, analytics...)", "type": "long_text", "required": False, "audio": True},
                {"id": "obs",           "label": "Algo mais que devemos saber?",                   "type": "long_text",  "required": False, "audio": True},
            ],
        },
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_onboarding_list_id() -> Optional[str]:
    """Find the 'Onboarding & Implementação' list_id."""
    conn = get_conn()
    cur  = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT id FROM lists WHERE LOWER(name) = LOWER(%s) AND COALESCE(archived,0) = 0 LIMIT 1",
            (ONBOARDING_LIST_NAME,),
        )
        row = cur.fetchone()
        return row["id"] if row else None
    finally:
        cur.close()
        conn.close()


def _create_onboarding_task(list_id: str, response_id: str, client_name: str, empresa: str) -> Optional[str]:
    """Create a task in the Onboarding list pointing at this briefing response."""
    task_id = "t_" + uuid.uuid4().hex[:12]
    title   = f"Briefing — {empresa or client_name or 'Novo cliente'}"
    desc    = (
        f"Briefing inicial enviado por {client_name or 'cliente'}.\n\n"
        f"📋 Ver respostas: /briefing/r/{response_id}\n"
        f"📄 Baixar PDF: /api/briefing/responses/{response_id}/pdf"
    )
    extra   = json.dumps({
        "briefing_response_id": response_id,
        "briefing_client_name": client_name,
    })
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO tasks (id, list_id, title, description, status, extra_data, created_at)
            VALUES (%s, %s, %s, %s, 'aberto', %s, NOW())
            """,
            (task_id, list_id, title, desc, extra),
        )
        conn.commit()
        return task_id
    except Exception:
        conn.rollback()
        return None
    finally:
        cur.close()
        conn.close()


def _get_response(response_id: str) -> Optional[dict]:
    conn = get_conn()
    cur  = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT id, client_name, client_email, responses, task_id, submitted_at "
            "FROM briefing_responses WHERE id = %s",
            (response_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        try:
            row["responses"] = json.loads(row["responses"] or "{}")
        except Exception:
            row["responses"] = {}
        return row
    finally:
        cur.close()
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Public routes (no auth)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/briefing/novo", response_class=HTMLResponse)
async def briefing_form(request: Request):
    return templates.TemplateResponse(
        "briefing_public.html",
        {
            "request":    request,
            "definition": BRIEFING_DEFINITION,
        },
    )


@router.post("/api/briefing/submit")
async def briefing_submit(request: Request):
    body = await request.json()
    responses    = body.get("responses") or {}
    client_name  = (body.get("client_name") or responses.get("nome") or "").strip()
    client_email = (body.get("client_email") or responses.get("email") or "").strip()
    empresa      = (responses.get("empresa_nome") or client_name).strip()

    rid = "br_" + uuid.uuid4().hex[:14]
    payload_json = json.dumps(responses, ensure_ascii=False)

    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO briefing_responses (id, client_name, client_email, responses)
            VALUES (%s, %s, %s, %s)
            """,
            (rid, client_name, client_email, payload_json),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

    # Create task in Onboarding & Implementação list (best-effort)
    list_id = _find_onboarding_list_id()
    task_id = None
    if list_id:
        task_id = _create_onboarding_task(list_id, rid, client_name, empresa)
        if task_id:
            conn = get_conn(); cur = conn.cursor()
            try:
                cur.execute("UPDATE briefing_responses SET task_id=%s WHERE id=%s", (task_id, rid))
                conn.commit()
            finally:
                cur.close(); conn.close()

    return JSONResponse({
        "id":      rid,
        "task_id": task_id,
        "pdf_url": f"/api/briefing/responses/{rid}/pdf",
    })


@router.get("/api/briefing/responses/{response_id}/pdf")
async def briefing_response_pdf(response_id: str):
    """Public PDF download — anyone with the response_id can grab it."""
    resp = _get_response(response_id)
    if not resp:
        raise HTTPException(404, "Briefing não encontrado")
    try:
        from weasyprint import HTML  # type: ignore
    except Exception as e:
        raise HTTPException(500, f"Geração de PDF indisponível: {e}")

    html = templates.get_template("briefing_pdf.html").render(
        request=None,
        definition=BRIEFING_DEFINITION,
        response=resp,
    )
    pdf_bytes = HTML(string=html, base_url=str(BASE_DIR)).write_pdf()
    safe_name = (resp.get("client_name") or "briefing").replace(" ", "_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="briefing-{safe_name}.pdf"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Internal routes (require auth)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/briefing/responses/{response_id}")
async def briefing_response_json(response_id: str, request: Request):
    _require(request)
    resp = _get_response(response_id)
    if not resp:
        raise HTTPException(404, "Briefing não encontrado")
    return JSONResponse({
        "id":           resp["id"],
        "client_name":  resp["client_name"],
        "client_email": resp["client_email"],
        "responses":    resp["responses"],
        "task_id":      resp["task_id"],
        "submitted_at": str(resp["submitted_at"]),
        "definition":   BRIEFING_DEFINITION,
    })


@router.get("/briefing/r/{response_id}", response_class=HTMLResponse)
async def briefing_response_view(response_id: str, request: Request):
    """Internal view (auth) — equipe vê as respostas formatadas."""
    _require(request)
    resp = _get_response(response_id)
    if not resp:
        raise HTTPException(404, "Briefing não encontrado")
    return templates.TemplateResponse(
        "briefing_response.html",
        {
            "request":    request,
            "definition": BRIEFING_DEFINITION,
            "response":   resp,
        },
    )
