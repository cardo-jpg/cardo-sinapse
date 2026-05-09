"""
Briefings públicos — formulário guiado (wizard) para novos clientes.

Fluxo:
  1. Cliente acessa GET /briefing (público, sem auth)
  2. Preenche o wizard, envia → POST /api/briefing/submit
  3. Resposta é gravada em briefing_responses
  4. Tarefa é criada automaticamente em Onboarding & Implementação
  5. Cliente recebe link da resposta + opção de baixar PDF

Áudio:
  - POST /api/briefing/transcribe → recebe blob de áudio, envia pra
    Whisper API (OpenAI), retorna texto transcrito.
  - GET /api/briefing/audio-available → flag pro frontend habilitar
    o botão de gravação só se a key estiver setada.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from backend.db import get_conn, dict_cursor
from backend.gestao import _require

OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
WHISPER_URL       = "https://api.openai.com/v1/audio/transcriptions"

RESEND_API_KEY    = os.getenv("RESEND_API_KEY", "")
RESEND_FROM       = os.getenv("BRIEFING_EMAIL_FROM", "Sinapse <onboarding@resend.dev>")
NOTIFY_EMAIL      = os.getenv("BRIEFING_NOTIFY_EMAIL", "agencia@cardomarketing.com.br")
PUBLIC_BASE_URL   = os.getenv("PUBLIC_BASE_URL", "https://sinapse.cardomarketing.com.br")

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
        "Esse briefing é o primeiro passo pra entendermos profundamente seu negócio. "
        "Quanto mais detalhe você der, mais assertiva será nossa estratégia."
    ),
    "ritual": {
        "headline": "Seja bem-vindo(a) ao momento mais estratégico do seu negócio.",
        "lead": (
            "Antes de começar: respira fundo, separa uns 30-40 minutos sem interrupção, "
            "e responde com a maior sinceridade possível — desse processo nasce a estratégia. "
            "Suas respostas ficam salvas no navegador, então pode pausar e voltar quando quiser."
        ),
        "checklist": [
            "Conecte o AMBIENTE: ilumine bem o espaço e silencie notificações",
            "Coloque sua PLAYLIST de foco favorita",
            "Tenha sua BEBIDA FAVORITA por perto — café, chá, água",
            "Pode pausar e voltar — suas respostas ficam salvas",
            "Conecte a EMOÇÃO: isso aqui é sobre o seu sonho",
        ],
    },
    "sections": [

        # ── 1 / 6 ─────────────────────────────────────────────────────────────
        {
            "id": "sobre_voce",
            "title": "Sobre você",
            "subtitle": "Antes da empresa, queremos conhecer a pessoa por trás dela.",
            "fields": [
                {"id": "nome",      "label": "Seu nome completo",                  "type": "short_text", "required": True},
                {"id": "email",     "label": "Seu melhor e-mail",                  "type": "email",      "required": True},
                {"id": "whatsapp",  "label": "WhatsApp (com DDD)",                 "type": "short_text", "required": True},
                {"id": "cargo",     "label": "Qual seu cargo / papel no negócio?", "type": "short_text", "required": True},
                {"id": "sua_historia",
                 "label": "Em poucas palavras: quem é você por trás dessa empresa? Qual sua história com esse negócio?",
                 "type": "long_text", "required": True, "audio": True},
            ],
        },

        # ── 2 / 6 ─────────────────────────────────────────────────────────────
        {
            "id": "empresa",
            "title": "Sobre a sua empresa",
            "subtitle": "Agora sim, vamos falar do negócio.",
            "fields": [
                {"id": "empresa_nome",
                 "label": "Qual o nome da sua empresa?",
                 "type": "short_text", "required": True},

                {"id": "nicho",
                 "label": "Qual o nicho da sua empresa?",
                 "type": "single_select", "required": True,
                 "options": [
                     "Alimentação & Restaurantes", "Moda & Vestuário", "Beleza & Estética",
                     "Saúde & Bem-estar", "Educação & Cursos", "Imobiliário",
                     "Serviços B2B", "Serviços B2C", "E-commerce / Varejo",
                     "Indústria", "Tecnologia / SaaS", "Eventos & Entretenimento",
                     "Automotivo", "Construção & Reformas", "Agronegócio",
                     "Direito / Contabilidade", "Outro",
                 ]},

                {"id": "site_redes",
                 "label": "Site, Instagram ou principal canal digital",
                 "type": "short_text", "required": False,
                 "placeholder": "@suaempresa / suaempresa.com.br"},

                {"id": "tempo_mercado",
                 "label": "Há quanto tempo o negócio existe?",
                 "type": "short_text", "required": False},

                {"id": "proposito",
                 "label": "Em uma frase: o que sua empresa entrega ao mundo? Por que ela existe?",
                 "type": "long_text", "required": True, "audio": True},

                {"id": "diferenciais",
                 "label": "Quais são os diferenciais competitivos da sua empresa?",
                 "type": "long_text", "required": True, "audio": True},

                {"id": "referencias",
                 "label": "Quem são suas principais referências / modelos?",
                 "type": "long_text", "required": True, "audio": True,
                 "placeholder": "Marcas que você admira, dentro ou fora do seu nicho"},

                {"id": "concorrentes",
                 "label": "Quem são seus principais concorrentes diretos?",
                 "type": "long_text", "required": True, "audio": True,
                 "placeholder": "Liste nomes específicos — quanto mais, melhor"},

                {"id": "sazonalidade",
                 "label": "Como vocês se organizam com datas ao longo do ano? Trabalham com campanhas sazonais ou temáticas em alguma época?",
                 "type": "long_text", "required": True, "audio": True},

                {"id": "horarios",
                 "label": "Quais são os dias e horários de funcionamento?",
                 "type": "short_text", "required": True},

                {"id": "area_atendimento",
                 "label": "Qual a área de atendimento? (Região, Estado, Cidades…)",
                 "type": "short_text", "required": True},
            ],
        },

        # ── 3 / 6 ─────────────────────────────────────────────────────────────
        {
            "id": "produtos",
            "title": "Produtos & receita",
            "subtitle": "O que vende, quanto vende e o quanto vai investir.",
            "fields": [
                {"id": "produtos_mais_vendidos",
                 "label": "Quais o(s) produto(s) / serviço(s) você mais vende?",
                 "type": "long_text", "required": True, "audio": True},

                {"id": "produtos_maior_margem",
                 "label": "Quais o(s) produto(s) / serviço(s) com maior margem de lucro?",
                 "type": "long_text", "required": False, "audio": True},

                {"id": "ticket_medio",
                 "label": "Qual seu ticket médio?",
                 "type": "currency", "required": True},

                {"id": "verba_anuncios",
                 "label": "Qual será a verba mensal que investiremos em anúncios?",
                 "type": "currency", "required": True},
            ],
        },

        # ── 4 / 6 ─────────────────────────────────────────────────────────────
        {
            "id": "publico",
            "title": "Sobre o seu público",
            "subtitle": "Quem compra, como compra, e por quê.",
            "fields": [
                {"id": "comportamento_compra",
                 "label": "Qual o comportamento de compra do seu público? Por que, como e em que momento compram?",
                 "type": "long_text", "required": True, "audio": True},

                {"id": "frequencia_consumo",
                 "label": "Com qual frequência consomem o produto / serviço?",
                 "type": "long_text", "required": True, "audio": True},

                {"id": "faixa_etaria",
                 "label": "Qual a faixa etária do seu público alvo?",
                 "type": "short_text", "required": True,
                 "placeholder": "Ex: 25 a 45 anos"},

                {"id": "genero",
                 "label": "Existe algum gênero mais relevante no seu público alvo?",
                 "type": "short_text", "required": True},

                {"id": "profissao",
                 "label": "Existe alguma profissão mais relevante no seu público alvo?",
                 "type": "short_text", "required": True},

                {"id": "poder_aquisitivo",
                 "label": "Qual o poder aquisitivo do seu público alvo?",
                 "type": "short_text", "required": True,
                 "placeholder": "Classe A, B, C…"},
            ],
        },

        # ── 5 / 6 ─────────────────────────────────────────────────────────────
        {
            "id": "comercial",
            "title": "Sobre o seu comercial",
            "subtitle": "Pra entender o estágio operacional do funil.",
            "fields": [
                {"id": "tem_equipe_comercial",
                 "label": "Você tem equipe comercial?",
                 "type": "single_select", "required": True,
                 "options": [
                     "Sim, tenho uma equipe comercial que me traz resultados",
                     "Sim, tenho uma equipe, mas ainda não está me trazendo resultados",
                     "Eu mesmo faço meu comercial e tenho resultados",
                     "Eu mesmo faço meu comercial, mas ainda não tenho resultados",
                 ]},

                {"id": "treinamento_equipe",
                 "label": "Você dá treinamento para sua equipe comercial?",
                 "type": "single_select", "required": True,
                 "options": [
                     "Sim, pelo menos uma vez por mês",
                     "Sim, pelo menos uma vez por trimestre",
                     "Sim, mas só quando as vendas estão baixas",
                     "Não faço treinamentos com minha equipe",
                 ]},

                {"id": "tem_crm",
                 "label": "Você tem um CRM implementado na sua empresa?",
                 "type": "single_select", "required": True,
                 "options": [
                     "Sim, usamos um programa pago de CRM",
                     "Sim, usamos um programa gratuito de CRM",
                     "Sei o que é e a importância de utilizar, mas não usamos",
                     "Não sei o que é um CRM",
                 ]},

                {"id": "uso_crm",
                 "label": "Se tiver um CRM implementado, sua equipe faz uso frequente dele?",
                 "type": "single_select", "required": True,
                 "options": [
                     "Sim, utilizamos religiosamente",
                     "Sim, utilizamos mas com pouca frequência",
                     "Praticamente não utilizamos",
                     "Não temos CRM Implementado",
                 ]},

                {"id": "analisa_funil",
                 "label": "Você analisa as métricas do seu funil de vendas para buscar melhorias?",
                 "type": "single_select", "required": True,
                 "options": [
                     "Sim, frequentemente",
                     "Sim, raramente",
                     "Tenho controle das métricas, mas não analiso",
                     "Não tenho controle das métricas",
                 ]},

                {"id": "faturamento_6m",
                 "label": "Qual foi a média de faturamento mensal nos últimos 6 meses?",
                 "type": "currency", "required": True},

                {"id": "expectativa_6m",
                 "label": "Com base nas respostas anteriores, qual sua expectativa de faturamento para os próximos 6 meses?",
                 "type": "currency", "required": True},
            ],
        },

        # ── 6 / 6 ─────────────────────────────────────────────────────────────
        {
            "id": "parceria",
            "title": "Sobre a nossa parceria",
            "subtitle": "Pra fechar — visão de parceria, expectativas e contexto extra.",
            "fields": [
                {"id": "sucesso_parceria",
                 "label": "O que faria você considerar nossa parceria um sucesso?",
                 "type": "long_text", "required": True, "audio": True,
                 "placeholder": "Pode ser tangível (faturamento, leads) ou intangível (autoridade, paz operacional)"},

                {"id": "experiencias_passadas",
                 "label": "Você já trabalhou com agência ou marketing digital antes? Como foi a experiência? O que funcionou e o que não funcionou?",
                 "type": "long_text", "required": False, "audio": True},

                {"id": "maior_gargalo",
                 "label": "Qual é o maior gargalo do seu negócio hoje?",
                 "type": "long_text", "required": True, "audio": True,
                 "placeholder": "Operação? Comercial? Geração de demanda? Time? Posicionamento?"},

                {"id": "obs_extra",
                 "label": "Existe algo mais que devemos saber sobre você ou seu negócio antes de começarmos?",
                 "type": "long_text", "required": False, "audio": True},
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


async def _notify_email(response_id: str, client_name: str, client_email: str, empresa: str, responses: dict) -> bool:
    """
    Manda email pra equipe (default agencia@cardomarketing.com.br) com
    resumo + link interno + link PDF. Best-effort: nunca derruba o submit.
    """
    if not RESEND_API_KEY:
        return False

    nicho      = responses.get("nicho") or "—"
    if nicho == "Outro" and responses.get("nicho__other"):
        nicho = f'Outro — {responses["nicho__other"]}'
    whatsapp   = responses.get("whatsapp") or "—"
    historia   = (responses.get("sua_historia") or "").strip()
    proposito  = (responses.get("proposito")    or "").strip()
    diferencia = (responses.get("diferenciais") or "").strip()
    sucesso    = (responses.get("sucesso_parceria") or "").strip()
    gargalo    = (responses.get("maior_gargalo")    or "").strip()

    base       = PUBLIC_BASE_URL.rstrip("/")
    view_url   = f"{base}/briefing/r/{response_id}"
    pdf_url    = f"{base}/api/briefing/responses/{response_id}/pdf?print=1"

    def _short(text: str, n: int = 280) -> str:
        text = (text or "").strip()
        return (text[:n] + "…") if len(text) > n else text

    safe = {
        "name":     (client_name or "Cliente").replace("<", "&lt;"),
        "email":    (client_email or "").replace("<", "&lt;"),
        "empresa":  (empresa or "").replace("<", "&lt;"),
        "nicho":    nicho.replace("<", "&lt;"),
        "whatsapp": whatsapp.replace("<", "&lt;"),
        "historia": _short(historia).replace("\n", "<br>"),
        "proposito": _short(proposito).replace("\n", "<br>"),
        "diferencia": _short(diferencia).replace("\n", "<br>"),
        "sucesso":   _short(sucesso).replace("\n", "<br>"),
        "gargalo":   _short(gargalo).replace("\n", "<br>"),
        "view_url": view_url,
        "pdf_url":  pdf_url,
    }

    html = f"""
<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f6f1ea;font-family:'Inter',-apple-system,sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:32px 24px;color:#1d1d2e;">
    <div style="font-size:.7rem;text-transform:uppercase;letter-spacing:4px;color:#E97919;font-weight:700;margin-bottom:10px;">
      Briefing inicial recebido
    </div>
    <h1 style="font-size:1.7rem;line-height:1.2;color:#19196C;margin:0 0 24px;">
      {safe['empresa'] or safe['name']}
    </h1>

    <div style="background:#fff;border-radius:14px;padding:20px 24px;border:1px solid #e9e3da;margin-bottom:18px;">
      <div style="font-size:.75rem;text-transform:uppercase;letter-spacing:.1em;color:#6b6878;margin-bottom:10px;font-weight:600;">Contato</div>
      <div style="font-size:.95rem;line-height:1.7;">
        <strong>Nome:</strong> {safe['name']}<br>
        <strong>Email:</strong> <a href="mailto:{safe['email']}" style="color:#19196C">{safe['email']}</a><br>
        <strong>WhatsApp:</strong> {safe['whatsapp']}<br>
        <strong>Empresa:</strong> {safe['empresa'] or '—'}<br>
        <strong>Nicho:</strong> {safe['nicho']}
      </div>
    </div>

    {f'''
    <div style="background:#fff;border-radius:14px;padding:20px 24px;border:1px solid #e9e3da;margin-bottom:18px;">
      <div style="font-size:.75rem;text-transform:uppercase;letter-spacing:.1em;color:#E97919;margin-bottom:8px;font-weight:600;">Quem é por trás</div>
      <div style="font-size:.92rem;line-height:1.6;color:#3d3a4a;">{safe['historia']}</div>
    </div>''' if safe['historia'] else ''}

    {f'''
    <div style="background:#fff;border-radius:14px;padding:20px 24px;border:1px solid #e9e3da;margin-bottom:18px;">
      <div style="font-size:.75rem;text-transform:uppercase;letter-spacing:.1em;color:#E97919;margin-bottom:8px;font-weight:600;">Propósito</div>
      <div style="font-size:.92rem;line-height:1.6;color:#3d3a4a;">{safe['proposito']}</div>
    </div>''' if safe['proposito'] else ''}

    {f'''
    <div style="background:#fff;border-radius:14px;padding:20px 24px;border:1px solid #e9e3da;margin-bottom:18px;">
      <div style="font-size:.75rem;text-transform:uppercase;letter-spacing:.1em;color:#E97919;margin-bottom:8px;font-weight:600;">Maior gargalo hoje</div>
      <div style="font-size:.92rem;line-height:1.6;color:#3d3a4a;">{safe['gargalo']}</div>
    </div>''' if safe['gargalo'] else ''}

    {f'''
    <div style="background:#fff;border-radius:14px;padding:20px 24px;border:1px solid #e9e3da;margin-bottom:24px;">
      <div style="font-size:.75rem;text-transform:uppercase;letter-spacing:.1em;color:#E97919;margin-bottom:8px;font-weight:600;">O que seria parceria de sucesso</div>
      <div style="font-size:.92rem;line-height:1.6;color:#3d3a4a;">{safe['sucesso']}</div>
    </div>''' if safe['sucesso'] else ''}

    <div style="text-align:center;margin:32px 0;">
      <a href="{safe['view_url']}" style="display:inline-block;padding:14px 28px;background:#19196C;color:#fff;border-radius:99px;text-decoration:none;font-weight:600;font-size:.95rem;margin-right:8px;">
        Ver respostas completas
      </a>
      <a href="{safe['pdf_url']}" style="display:inline-block;padding:14px 28px;background:transparent;color:#19196C;border:1.5px solid #19196C;border-radius:99px;text-decoration:none;font-weight:600;font-size:.95rem;">
        Baixar PDF
      </a>
    </div>

    <div style="text-align:center;font-size:.78rem;color:#6b6878;padding-top:20px;border-top:1px solid #e9e3da;">
      Sinapse · Cardō Marketing · cardomarketing.com.br
    </div>
  </div>
</body></html>
"""

    subject = f"Novo briefing: {empresa or client_name or 'Cliente'}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={
                    "from":     RESEND_FROM,
                    "to":       [NOTIFY_EMAIL],
                    "reply_to": client_email or None,
                    "subject":  subject,
                    "html":     html,
                },
            )
        return r.status_code < 400
    except Exception:
        return False


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

@router.get("/briefing", response_class=HTMLResponse)
async def briefing_form(request: Request):
    return templates.TemplateResponse(
        "briefing_public.html",
        {
            "request":         request,
            "definition":      BRIEFING_DEFINITION,
            "audio_available": bool(OPENAI_API_KEY),
        },
    )


@router.get("/briefing/novo")
async def briefing_form_legacy():
    """Redireciona o link antigo pra nova URL canônica."""
    return RedirectResponse(url="/briefing", status_code=308)


@router.get("/api/briefing/audio-available")
async def briefing_audio_available():
    """Flag pro frontend saber se a transcrição está disponível."""
    return {"available": bool(OPENAI_API_KEY)}


@router.post("/api/briefing/transcribe")
async def briefing_transcribe(audio: UploadFile = File(...)):
    """
    Recebe um blob de áudio (webm/ogg/mp3/m4a) e devolve o texto
    transcrito pelo Whisper. Pública — qualquer um preenchendo o
    briefing pode chamar.
    """
    if not OPENAI_API_KEY:
        raise HTTPException(503, "Transcrição indisponível: OPENAI_API_KEY não configurada")

    # Aceita até ~25MB (limite do Whisper)
    raw = await audio.read()
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(413, "Áudio muito grande (máx 25MB)")
    if len(raw) < 1024:
        raise HTTPException(400, "Áudio muito curto")

    filename = audio.filename or "audio.webm"
    mime     = audio.content_type or "audio/webm"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                WHISPER_URL,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files={"file": (filename, raw, mime)},
                data={"model": "whisper-1", "language": "pt", "response_format": "json"},
            )
        if r.status_code >= 400:
            raise HTTPException(502, f"Whisper API erro {r.status_code}: {r.text[:200]}")
        data = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Falha ao chamar Whisper: {e}")

    return {"text": (data.get("text") or "").strip()}


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

    # Notificação por email (best-effort — nunca derruba o submit)
    try:
        await _notify_email(rid, client_name, client_email, empresa, responses)
    except Exception:
        pass

    return JSONResponse({
        "id":      rid,
        "task_id": task_id,
        "pdf_url": f"/api/briefing/responses/{rid}/pdf",
    })


@router.get("/api/briefing/responses/{response_id}/pdf", response_class=HTMLResponse)
async def briefing_response_pdf(response_id: str, request: Request):
    """
    Tela print-friendly que dispara window.print() automaticamente.
    Cliente salva como PDF pelo dialog nativo do browser — sem libs nativas.
    Pública: qualquer um com o response_id consegue baixar.
    """
    resp = _get_response(response_id)
    if not resp:
        raise HTTPException(404, "Briefing não encontrado")
    return templates.TemplateResponse(
        "briefing_pdf.html",
        {
            "request":    request,
            "definition": BRIEFING_DEFINITION,
            "response":   resp,
        },
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


@router.get("/api/briefings")
async def briefings_list(request: Request, q: Optional[str] = None, limit: int = 100):
    """Lista briefings recebidos (mais novos primeiro)."""
    _require(request)
    limit = max(1, min(int(limit or 100), 500))
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        if q:
            cur.execute("""
                SELECT id, client_name, client_email, responses, task_id, submitted_at
                FROM briefing_responses
                WHERE client_name ILIKE %s OR client_email ILIKE %s OR responses ILIKE %s
                ORDER BY submitted_at DESC
                LIMIT %s
            """, (f"%{q}%", f"%{q}%", f"%{q}%", limit))
        else:
            cur.execute("""
                SELECT id, client_name, client_email, responses, task_id, submitted_at
                FROM briefing_responses
                ORDER BY submitted_at DESC
                LIMIT %s
            """, (limit,))
        rows = cur.fetchall() or []
    finally:
        cur.close(); conn.close()

    items = []
    for r in rows:
        try:
            resp = json.loads(r.get("responses") or "{}")
        except Exception:
            resp = {}
        items.append({
            "id":           r["id"],
            "client_name":  r.get("client_name") or resp.get("nome") or "",
            "client_email": r.get("client_email") or resp.get("email") or "",
            "empresa":      resp.get("empresa_nome") or "",
            "whatsapp":     resp.get("whatsapp") or "",
            "nicho":        resp.get("nicho") or "",
            "task_id":      r.get("task_id"),
            "submitted_at": str(r["submitted_at"]) if r.get("submitted_at") else "",
        })
    return {"items": items, "total": len(items)}


@router.get("/briefings", response_class=HTMLResponse)
async def briefings_index(request: Request):
    """Painel interno listando todos os briefings recebidos."""
    _require(request)
    return templates.TemplateResponse(
        "briefings_index.html",
        {"request": request},
    )


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
