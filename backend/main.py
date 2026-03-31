import os
import re
import json
import uuid
import hmac
import hashlib
import asyncio
import httpx
import threading
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import anthropic

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
DOCS_DIR = BASE_DIR / "documents"
CONVS_DIR = BASE_DIR / "logs" / "conversations"
TEMPLATES_DIR = BASE_DIR / "frontend" / "templates"
STATIC_DIR = BASE_DIR / "frontend" / "static"
CONVS_DIR.mkdir(parents=True, exist_ok=True)

SECRET_KEY = os.getenv("SECRET_KEY", "sinapse-secret-2026")
USERS = {
    "victor": os.getenv("USER_VICTOR_PASSWORD", "C@rdobrain2026"),
    "jose":   os.getenv("USER_JOSE_PASSWORD",   "C@rdosinapse2026"),
}
USER_DISPLAY = {
    "victor": "Victor",
    "jose":   "José",
}

CLICKUP_API_KEY = os.getenv("CLICKUP_API_KEY")
CLICKUP_WORKSPACE_ID = "36996433"

CLICKUP_AREA_LISTS = {
    "atendimento":  "901110232922",  # Atendimento & CS > Pendências Clientes
    "trafego":      "901109630621",  # Gestão de Tráfego > Tarefas Pontuais
    "redacao":      "901109640407",  # Redação > Copys
    "automacao":    "901110011374",  # Automações > Tarefas Pontuais
    "conteudo":     "901110712633",  # Conteúdo Cardô > Planejamento
}
CLICKUP_AREA_LABELS = {
    "atendimento": "Atendimento & CS",
    "trafego":     "Gestão de Tráfego",
    "redacao":     "Redação",
    "automacao":   "Automações",
    "conteudo":    "Conteúdo",
}

CLICKUP_MEMBERS = {
    "jadna": 81482162,
    "jose": 75499891,
    "jose carlos": 75499891,
    "victor dognini": 75384286,
    "victor": 54959381,
    "victor cardô": 54959381,
    "cardô": 54959381,
}

# Custom field "Cliente" — mesmo ID em todas as listas do espaço Operacional
CLICKUP_CLIENTE_FIELD_ID = "3c544f67-98ab-4fbf-8d0a-854b1d9803bf"
CLICKUP_CLIENTE_OPTIONS = {
    "CC":    "eb7390d2-1a7f-4b6a-a615-4f958d5f185c",
    "NF":    "c6359778-98b7-4a10-ab8d-3704642ea5db",
    "PV":    "34d40add-d727-400b-ba21-db8f0bbde001",
    "HIRE":  "97165964-7535-4111-b2d0-112a12de1c9e",
    "SRW":   "e2c89d2d-0ee7-4d55-9afb-84e049fa9abe",
    "SCALE": "ffb45136-aab2-469d-906f-d08d64bd2c6e",
    "HDLT":  "8f18de61-41a2-428a-93db-6dc62604e7a8",
}

GRANOLA_SUPABASE_PATH = Path.home() / "Library" / "Application Support" / "Granola" / "supabase.json"
GRANOLA_API_BASE = "https://api.granola.ai/v1"

# ── Google Ads Clients ───────────────────────────────────────────────────────
GOOGLE_ADS_CLIENTS = {
    "SRW": {"nome": "Speedrack West", "windsor_url": os.getenv("WINDSOR_SRW_URL", "")},
}

# ── Meta Ads ──────────────────────────────────────────────────────────────────
META_GRAPH_BASE = "https://graph.facebook.com/v21.0"
META_CLIENTS = {
    "HIRE":  {"name": "Hire Brazil",          "token": lambda: os.getenv("META_TOKEN_HIRE"),  "accounts": ["act_729365124577217", "act_2110912885722512"]},
    "CC":    {"name": "Conexão Cirúrgica",     "token": lambda: os.getenv("META_TOKEN_CC"),    "accounts": []},
    "NF":    {"name": "Grupo NF",              "token": lambda: os.getenv("META_TOKEN_NF"),    "accounts": []},
    "PV":    {"name": "Patricia Voggt",        "token": lambda: os.getenv("META_TOKEN_PV"),    "accounts": []},
    "SRW":   {"name": "Speedrack West",        "token": lambda: os.getenv("META_TOKEN_SRW"),   "accounts": []},
    "SCALE": {"name": "Scale Army",            "token": lambda: os.getenv("META_TOKEN_SCALE"), "accounts": []},
    "HDLT":  {"name": "Headlight Co",          "token": lambda: os.getenv("META_TOKEN_HDLT"),  "accounts": []},
}

# Doc ID da Documentação no ClickUp + ID das páginas "Atas de Reunião" por cliente
CLICKUP_DOC_ID = "1391ah-49691"
CLICKUP_ATAS_PAGES = {
    "CC":    "1391ah-49291",
    "HIRE":  "1391ah-49331",
    "NF":    "1391ah-33711",
    "PV":    "1391ah-49491",
    "SRW":   "1391ah-49551",
    "HDLT":  "1391ah-49811",
    "SCALE": "1391ah-49751",
}

# Lista padrão para novas tarefas: Pendências Clientes (Atendimento & CS)
CLICKUP_DEFAULT_LIST_ID = "901110232922"

# ── Sincronização automática ──────────────────────────────────────────────────

def run_sync():
    """Roda os scripts de sync em subprocesso."""
    scripts = ["sync_clickup.py", "sync_sheets.py"]
    for script in scripts:
        path = BASE_DIR / script
        if path.exists():
            print(f"[sync] Rodando {script}...")
            try:
                subprocess.run([sys.executable, str(path)], cwd=str(BASE_DIR),
                               capture_output=True, timeout=120)
            except Exception as e:
                print(f"[sync] Erro em {script}: {e}")
    print(f"[sync] Concluído em {datetime.now().strftime('%H:%M')}")

def start_sync_scheduler():
    """Roda sync ao iniciar e depois a cada 6 horas."""
    def loop():
        run_sync()
        t = threading.Timer(6 * 3600, loop)
        t.daemon = True
        t.start()
    t = threading.Thread(target=loop, daemon=True)
    t.start()

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_sync_scheduler()
    yield

app = FastAPI(lifespan=lifespan)
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ── Granola ───────────────────────────────────────────────────────────────────

# Cache em memória para o token renovado (usado no Railway onde não há arquivo local)
_granola_token_cache: dict = {}  # {"access_token": str, "refresh_token": str, "expires_at_ms": int}

def _is_token_expired_ms(obtained_at_ms: int, expires_in_s: int) -> bool:
    import time
    expiry_ms = obtained_at_ms + expires_in_s * 1000
    return time.time() * 1000 > expiry_ms - 5 * 60 * 1000  # 5 min de margem

def _do_refresh(refresh_token: str) -> dict:
    """Chama a API do Granola para renovar o token. Retorna novo dict de tokens ou {}."""
    import time as _time
    try:
        with httpx.Client() as c:
            r = c.post(
                f"{GRANOLA_API_BASE}/refresh-access-token",
                headers={"Content-Type": "application/json",
                         "x-granola-client-id": "granola-desktop", "x-granola-version": "2.0.0"},
                json={"refresh_token": refresh_token},
                timeout=10,
            )
        if r.status_code == 200:
            new_tokens = r.json()
            new_tokens["obtained_at"] = int(_time.time() * 1000)
            if not new_tokens.get("refresh_token"):
                new_tokens["refresh_token"] = refresh_token
            print("[granola] Token renovado com sucesso.")
            return new_tokens
    except Exception as e:
        print(f"[granola] Erro ao renovar token: {e}")
    return {}

def get_granola_token() -> str:
    global _granola_token_cache
    import time as _time

    # ── Modo Railway: GRANOLA_REFRESH_TOKEN como env var ──────────────────────
    env_refresh = os.getenv("GRANOLA_REFRESH_TOKEN")
    if env_refresh:
        cached = _granola_token_cache
        obtained = cached.get("obtained_at", 0)
        expires_in = cached.get("expires_in", 0)
        if cached.get("access_token") and obtained and not _is_token_expired_ms(obtained, expires_in):
            return cached["access_token"]
        # Cache vazio ou expirado: usa GRANOLA_TOKEN estático como fallback inicial
        if not cached.get("access_token"):
            static = os.getenv("GRANOLA_TOKEN", "")
            if static:
                _granola_token_cache = {
                    "access_token": static,
                    "refresh_token": env_refresh,
                    "obtained_at": int(_time.time() * 1000),
                    "expires_in": 0,  # força refresh na próxima
                }
        # Renova usando refresh_token
        print("[granola] Renovando token (Railway)...")
        new = _do_refresh(env_refresh)
        if new.get("access_token"):
            _granola_token_cache = new
            return new["access_token"]
        return _granola_token_cache.get("access_token", "")

    # ── Modo local: lê do arquivo do Granola app ───────────────────────────────
    try:
        data = json.loads(GRANOLA_SUPABASE_PATH.read_text())
        wt = data.get("workos_tokens", {})
        if isinstance(wt, str):
            wt = json.loads(wt)
        obtained = wt.get("obtained_at", 0)
        expires_in = wt.get("expires_in", 0)
        if obtained and expires_in and _is_token_expired_ms(obtained, expires_in):
            print("[granola] Token expirado, renovando...")
            new = _do_refresh(wt.get("refresh_token", ""))
            if new.get("access_token"):
                data["workos_tokens"] = json.dumps(new)
                GRANOLA_SUPABASE_PATH.write_text(json.dumps(data))
                return new["access_token"]
        return wt.get("access_token", "")
    except Exception:
        return ""

async def _granola_request(token: str, endpoint: str, payload: dict):
    """Faz uma requisição POST ao Granola e retorna (status_code, json_or_none)."""
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{GRANOLA_API_BASE}/{endpoint}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "x-granola-client-id": "granola-desktop", "x-granola-version": "2.0.0"},
            json=payload or {},
            timeout=20,
        )
        return r.status_code, r.json() if r.status_code == 200 else None

async def granola_post(endpoint: str, payload: dict = None):
    global _granola_token_cache
    token = get_granola_token()
    if not token:
        return None
    try:
        status, data = await _granola_request(token, endpoint, payload or {})
        if status == 200:
            return data
        if status == 401:
            print(f"[granola] 401 em {endpoint}, forçando refresh de token...")
            refresh_token = None
            env_refresh = os.getenv("GRANOLA_REFRESH_TOKEN")
            if env_refresh:
                refresh_token = _granola_token_cache.get("refresh_token") or env_refresh
            else:
                try:
                    raw = json.loads(GRANOLA_SUPABASE_PATH.read_text())
                    wt = raw.get("workos_tokens", {})
                    if isinstance(wt, str):
                        wt = json.loads(wt)
                    refresh_token = wt.get("refresh_token")
                except Exception:
                    pass
            if refresh_token:
                new = _do_refresh(refresh_token)
                if new.get("access_token"):
                    _granola_token_cache = new
                    status2, data2 = await _granola_request(new["access_token"], endpoint, payload or {})
                    if status2 == 200:
                        return data2
        print(f"[granola] Erro {status} em {endpoint}")
    except Exception as e:
        print(f"[granola] Exceção em {endpoint}: {e}")
    return None

async def granola_get_transcript(document_id: str) -> str:
    """Busca transcript via get-document-transcript (retorna gzip). Retorna texto formatado."""
    import gzip as _gzip
    token = get_granola_token()
    if not token:
        return ""
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(
                f"{GRANOLA_API_BASE}/get-document-transcript",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                         "x-granola-client-id": "granola-desktop", "x-granola-version": "2.0.0"},
                json={"document_id": document_id},
                timeout=20,
            )
            if r.status_code != 200:
                return ""
            raw = r.content
            try:
                segments = json.loads(_gzip.decompress(raw))
            except Exception:
                segments = json.loads(raw)
            if not isinstance(segments, list):
                return ""
            lines = []
            for s in segments:
                text = s.get("text", "").strip()
                source = s.get("source", "")
                if text:
                    speaker = "Sistema" if source == "system" else "Microfone"
                    lines.append(f"[{speaker}]: {text}")
            return "\n".join(lines)
        except Exception:
            return ""

def sanitize_ata_content(text: str) -> str:
    """Remove linhas que contêm apenas 'null' (com pontuação opcional)."""
    lines = text.splitlines()
    cleaned = [l for l in lines if not re.match(r'^\s*null[.,;]?\s*$', l, re.IGNORECASE)]
    return "\n".join(cleaned)


def prosemirror_to_markdown(node: dict, depth: int = 0) -> str:
    """Converte um nó ProseMirror JSON para Markdown."""
    ntype = node.get("type", "")
    content = node.get("content", [])

    if ntype == "doc":
        return "\n\n".join(prosemirror_to_markdown(c, depth) for c in content).strip()

    if ntype == "heading":
        level = node.get("attrs", {}).get("level", 1)
        text = "".join(prosemirror_node_text(c) for c in content)
        return "#" * level + " " + text

    if ntype == "paragraph":
        text = "".join(prosemirror_node_text(c) for c in content)
        return text

    if ntype == "bulletList":
        items = []
        for item in content:
            item_md = prosemirror_to_markdown(item, depth)
            # indenta sub-níveis
            lines = item_md.splitlines()
            if lines:
                items.append("  " * depth + "- " + lines[0])
                for l in lines[1:]:
                    items.append("  " * (depth + 1) + l)
        return "\n".join(items)

    if ntype == "orderedList":
        items = []
        for i, item in enumerate(content, 1):
            item_md = prosemirror_to_markdown(item, depth)
            lines = item_md.splitlines()
            if lines:
                items.append("  " * depth + f"{i}. " + lines[0])
                for l in lines[1:]:
                    items.append("  " * (depth + 1) + l)
        return "\n".join(items)

    if ntype == "listItem":
        parts = []
        for c in content:
            if c.get("type") in ("bulletList", "orderedList"):
                parts.append(prosemirror_to_markdown(c, depth + 1))
            else:
                parts.append(prosemirror_to_markdown(c, depth))
        return "\n".join(p for p in parts if p)

    if ntype == "hardBreak":
        return "\n"

    if ntype == "text":
        return prosemirror_node_text(node)

    # fallback: processa filhos
    return "\n".join(prosemirror_to_markdown(c, depth) for c in content)


def prosemirror_node_text(node: dict) -> str:
    """Extrai texto de um nó folha, aplicando marks (bold, italic, code)."""
    if node.get("type") != "text":
        return prosemirror_to_markdown(node)
    text = node.get("text", "")
    marks = node.get("marks", [])
    for mark in marks:
        mt = mark.get("type", "")
        if mt == "bold":
            text = f"**{text}**"
        elif mt == "italic":
            text = f"*{text}*"
        elif mt == "code":
            text = f"`{text}`"
        elif mt == "strike":
            text = f"~~{text}~~"
    return text


async def granola_get_panels_markdown(document_id: str) -> str:
    """Busca os painéis do documento e converte para Markdown."""
    token = get_granola_token()
    if not token:
        return ""
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(
                f"{GRANOLA_API_BASE}/get-document-panels",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                         "x-granola-client-id": "granola-desktop", "x-granola-version": "2.0.0"},
                json={"document_id": document_id},
                timeout=15,
            )
            if r.status_code != 200:
                return ""
            panels = r.json()
            if not isinstance(panels, list) or not panels:
                return ""
            parts = []
            for panel in panels:
                content = panel.get("content")
                if content:
                    md = prosemirror_to_markdown(content)
                    if md.strip():
                        parts.append(md.strip())
            return "\n\n".join(parts)
        except Exception as e:
            print(f"[granola] Erro ao buscar painéis: {e}")
            return ""


def extract_meeting_topic(title: str) -> str:
    """Extrai o tema da reunião do título do Granola, removendo prefixos e datas."""
    import re as _re
    t = title.strip()
    # Remove conteúdo entre colchetes: [Scale Army], [PRO], etc.
    t = _re.sub(r'\[.*?\]', '', t).strip()
    # Remove "Ata de Reunião" (template padrão do Granola)
    t = _re.sub(r'ata de reuni[aã]o', '', t, flags=_re.IGNORECASE).strip()
    # Remove padrões de data: DD/MM/YYYY, DD/MM, YYYY-MM-DD
    t = _re.sub(r'\b\d{1,2}[\/\-]\d{1,2}([\/\-]\d{2,4})?\b', '', t).strip()
    # Remove separadores sobressalentes no início/fim
    t = _re.sub(r'^[\s\-–|]+|[\s\-–|]+$', '', t).strip()
    return t

def extract_date_from_title(title: str) -> str:
    """Extrai data no formato YYYY-MM-DD do título. Ex: '18/02/2026' → '2026-02-18'"""
    if not title:
        return ""
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', title)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
    return ""

def parse_granola_transcript(transcript_data) -> str:
    if not isinstance(transcript_data, dict):
        return ""
    segments = transcript_data.get("transcript", [])
    if not segments:
        return ""
    lines = []
    for s in segments:
        if isinstance(s, dict) and s.get("text"):
            speaker = s.get("speaker", "?")
            lines.append(f"[{speaker}]: {s.get('text', '')}")
    return "\n".join(lines)

# ── ClickUp ───────────────────────────────────────────────────────────────────

CLICKUP_LISTS = {
    "pendências clientes": "901110232922",
    "pendencias clientes": "901110232922",
    "tarefas recorrentes": "901110235667",
    "painel de clientes": "901107173525",
}

def create_clickup_task(name: str, description: str = "", list_name: str = "", due_date: str = "") -> dict:
    list_id = CLICKUP_LISTS.get(list_name.lower().strip(), CLICKUP_DEFAULT_LIST_ID)
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    payload = {"name": name}
    if description:
        payload["description"] = description
    if due_date:
        try:
            dt = datetime.strptime(due_date, "%Y-%m-%d")
            payload["due_date"] = int(dt.timestamp() * 1000)
        except Exception:
            pass
    r = httpx.post(url, headers={"Authorization": CLICKUP_API_KEY}, json=payload, timeout=15)
    if r.status_code in (200, 201):
        data = r.json()
        return {"ok": True, "id": data.get("id"), "url": data.get("url"), "name": data.get("name")}
    return {"ok": False, "error": r.text}

# ── Claude tools ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "criar_tarefa_clickup",
        "description": (
            "Cria uma tarefa no ClickUp. Use quando o usuário pedir para criar uma tarefa, "
            "pendência, to-do ou atividade. Confirme os detalhes antes se necessário."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nome/título da tarefa"},
                "description": {"type": "string", "description": "Descrição ou detalhes da tarefa (opcional)"},
                "list_name": {
                    "type": "string",
                    "description": "Lista de destino: 'Pendências Clientes' (padrão), 'Tarefas Recorrentes', 'Painel de Clientes'"
                },
                "due_date": {"type": "string", "description": "Data de entrega no formato YYYY-MM-DD (opcional)"},
            },
            "required": ["name"],
        },
    }
]

# ── Especialistas ─────────────────────────────────────────────────────────────

SPECIALIST_KEYWORDS = {
    "google_ads": [
        "google ads", "google", "search", "p-max", "pmax", "performance max",
        "keyword", "palavra-chave", "quality score", "roas", "cpc", "ctr",
        "lance", "bidding", "smart bidding", "shopping", "display", "dsa",
        "grupo de anúncio", "ad group", "extensão", "campanha search",
        "índice de pesquisa", "termo de pesquisa", "correspondência",
    ],
    "meta_ads": [
        "meta", "facebook", "instagram", "meta ads", "conjunto de anúncio",
        "cpl", "custo por lead", "engajamento", "público", "lookalike",
        "remarketing", "pixel", "capi", "conversions api", "stories",
        "reels", "carrossel", "frequência", "cpm", "alcance",
        "campanha de conversão", "campanha de engajamento", "lead ads",
    ],
    "criativo": [
        "copy", "criativo", "roteiro", "script", "hook", "headline",
        "chamada para ação", "cta", "copywriting", "texto do anúncio",
        "vídeo", "imagem", "formato", "anúncio em vídeo", "prova social",
        "depoimento", "unboxing", "tom de comunicação", "narrativa",
    ],
    "atendimento": [
        "reunião", "próximos passos", "pendência", "histórico",
        "relatório", "apresentação", "proposta", "onboarding",
        "feedback do cliente", "o que falar", "como comunicar",
    ],
}

SPECIALIST_PROMPTS = {
    "google_ads": """
--- MODO ESPECIALISTA: Google Ads ---
Você está respondendo como especialista em Google Ads. Aprofunde-se em:
- Estrutura de campanhas: Search, P-Max, Shopping, Display, DSA — quando usar cada uma
- Estratégias de lance: Target CPA, Target ROAS, Maximizar Conversões, CPC Manual
- Palavras-chave: tipos de correspondência, Quality Score, índice de pesquisa, negativação
- Diagnóstico de métricas: CTR, CPC médio, Taxa de Conversão, Custo/Conv., Parcela de Impressões
- P-Max: vantagens, limitações, quando pausar, assets necessários
- Estrutura de grupos de anúncios e RSAs (Responsive Search Ads)
- Google Ads para B2B vs B2C — diferenças práticas
Use os dados de campanha do cliente (se disponíveis nos documentos) para dar recomendações específicas.
""",
    "meta_ads": """
--- MODO ESPECIALISTA: Meta Ads ---
Você está respondendo como especialista em Meta Ads (Facebook/Instagram). Aprofunde-se em:
- Estrutura: campanha → conjunto de anúncios → anúncio; objetivos disponíveis
- Públicos: interesses, comportamentos, lookalike, remarketing, lista de clientes
- Criativos: formatos (vídeo, carrossel, estático), proporções recomendadas por posicionamento
- Métricas: CPL, CPM, CTR, Frequência, ROAS, Custo por Resultado
- Campanha de engajamento vs conversão direta — quando usar cada uma
- Pixel e Conversions API (CAPI) — configuração e importância
- Qualidade de lead: estratégias de qualificação dentro do Meta
- Testes A/B e Creative Testing no Meta
Use os dados de campanha do cliente (se disponíveis nos documentos) para dar recomendações específicas.
""",
    "criativo": """
--- MODO ESPECIALISTA: Estratégia Criativa ---
Você está respondendo como especialista em criativo para tráfego pago. Aprofunde-se em:
- Estrutura de copy: hook → problema → solução → prova social → CTA
- Roteiros de vídeo por duração: 15s (só hook+CTA), 30s, 60s, 1min30
- Headlines e descrições para Search (RSA): variações, pins, relevância
- Formatos que performam por plataforma: Meta (vídeo curto, UGC) vs Google (texto)
- Como usar prova social: depoimentos, unboxings, cases, números
- Testes criativos: o que testar primeiro, como ler os resultados
- Tom de comunicação: adaptar por segmento (B2B vs B2C, produto vs serviço)
- Criativos para reengajamento vs prospecção
Use o histórico de criativos e tom do cliente (se disponível na ficha) para sugerir algo aderente à marca.
""",
    "atendimento": """
--- MODO ESPECIALISTA: Gestão de Cliente ---
Você está respondendo como especialista em atendimento de agência. Aprofunde-se em:
- Como comunicar resultados: positivos (destaque o impacto) e negativos (contexto + próximo passo)
- Preparação para reuniões: o que revisar, o que antecipar, o que propor
- Gestão de expectativas: como falar de prazos, testes e incertezas
- Documentação de decisões e próximos passos
- Identificação de oportunidades de expansão com o cliente
Use o histórico de ações e ficha do cliente ativo para contextualizar respostas.
""",
}

def classify_specialist(messages: list):
    """Identifica o especialista mais adequado com base na última mensagem do usuário."""
    last_user = next(
        (m["content"] for m in reversed(messages)
         if m["role"] == "user" and isinstance(m["content"], str)),
        ""
    ).lower()
    scores = {k: 0 for k in SPECIALIST_KEYWORDS}
    for specialist, keywords in SPECIALIST_KEYWORDS.items():
        for kw in keywords:
            if kw in last_user:
                scores[specialist] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None

# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é o assistente interno da Cardô, uma agência de tráfego pago.

Seu papel é ajudar os colaboradores da Cardô a resolverem problemas, tomar decisões e executar tarefas da maneira como a Cardô trabalha — com base nos documentos, processos e conhecimento interno da agência.

{client_context}

COMO VOCÊ DEVE AGIR:
- Responda sempre de forma direta e prática
- Use o conhecimento dos documentos internos da Cardô para embasar suas respostas
- Quando houver um jeito "Cardô" de fazer algo (descrito nos documentos), priorize esse jeito
- Se não souber algo específico da Cardô, diga claramente e sugira onde buscar a informação
- Seja objetivo — os colaboradores estão no meio do trabalho

CRIAÇÃO DE TAREFAS NO CLICKUP:
- Quando o usuário pedir para criar uma tarefa, use a ferramenta criar_tarefa_clickup
- Listas disponíveis: "Pendências Clientes" (padrão), "Tarefas Recorrentes", "Painel de Clientes"
- Após criar, confirme com o nome e link da tarefa

INFORMAÇÕES CONFIDENCIAIS — NUNCA REVELE:
- Valores de fee mensal, mensalidade ou qualquer remuneração dos clientes
- Se perguntado sobre esses valores, responda: "Essa informação é confidencial e não posso compartilhar."

CONTEXTO DA CARDÔ:
A Cardô é uma agência de tráfego pago que atende clientes no Brasil e nos Estados Unidos.
Os colaboradores precisam de respostas rápidas e alinhadas com os processos internos da agência.

DOCUMENTOS INTERNOS DISPONÍVEIS:
{documents}
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

MAX_ADS_ROWS = 60  # Linhas de dados de performance incluídas no contexto

def read_file_truncated(filepath: Path) -> str:
    """Para arquivos google_ads_*.md, retorna apenas cabeçalho + últimas MAX_ADS_ROWS linhas de dados."""
    content = filepath.read_text(encoding="utf-8")
    if not filepath.name.startswith("google_ads_"):
        return content
    lines = content.splitlines()
    # Encontra a linha de cabeçalho da tabela (começa com "| Campaign" ou "| ")
    header_idx = next((i for i, l in enumerate(lines) if l.startswith("|") and i > 0), None)
    if header_idx is None:
        return content
    # Linhas de metadata (antes da tabela) + cabeçalho da tabela (2 linhas: header + separador)
    meta = lines[:header_idx]
    table_header = lines[header_idx:header_idx + 2]
    data_rows = [l for l in lines[header_idx + 2:] if l.startswith("|")]
    recent_rows = data_rows[-MAX_ADS_ROWS:]
    truncated = meta + table_header + recent_rows
    note = f"\n_(Exibindo últimas {len(recent_rows)} de {len(data_rows)} linhas)_"
    return "\n".join(truncated) + note

def load_documents(client_id: str = None):
    docs = []
    # Documentos globais (raiz de documents/) — apenas .md
    for filepath in sorted(DOCS_DIR.glob("*.md")):
        content = read_file_truncated(filepath)
        docs.append(f"=== {filepath.name} ===\n{content}")
    # Documentos do cliente selecionado — apenas .md, ignora .txt (exports de chat)
    if client_id:
        for subdir in DOCS_DIR.iterdir():
            if subdir.is_dir() and f"- {client_id.upper()}" in subdir.name.upper():
                for filepath in sorted(subdir.glob("*.md")):
                    content = read_file_truncated(filepath)
                    docs.append(f"=== {subdir.name}/{filepath.name} ===\n{content}")
    else:
        # Sem cliente: carrega só as fichas de todos os clientes
        for subdir in sorted(DOCS_DIR.iterdir()):
            if subdir.is_dir():
                for filepath in sorted(subdir.glob("ficha_*.md")):
                    content = filepath.read_text(encoding="utf-8")
                    docs.append(f"=== {subdir.name}/{filepath.name} ===\n{content}")
    return "\n\n".join(docs) if docs else "Nenhum documento interno carregado ainda."

def save_conversation(conv_id, user, messages, title=None, client=None, client_name=None):
    path = CONVS_DIR / f"{conv_id}.json"
    data = json.loads(path.read_text()) if path.exists() else {
        "id": conv_id, "user": user,
        "title": title or "Nova conversa",
        "created_at": datetime.now().isoformat(), "messages": []
    }
    data["messages"] = messages
    data["updated_at"] = datetime.now().isoformat()
    if title:
        data["title"] = title
    if client:
        data["client"] = client
    if client_name:
        data["client_name"] = client_name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def load_conversation(conv_id):
    path = CONVS_DIR / f"{conv_id}.json"
    return json.loads(path.read_text()) if path.exists() else None

def list_conversations():
    convs = []
    for path in sorted(CONVS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        data = json.loads(path.read_text())
        convs.append({
            "id": data["id"], "user": data.get("user", ""),
            "title": data.get("title", "Sem título"),
            "updated_at": data.get("updated_at", data.get("created_at", "")),
            "client": data.get("client", ""),
            "client_name": data.get("client_name", ""),
        })
    return convs[:40]

def make_session_token(username: str) -> str:
    sig = hmac.new(SECRET_KEY.encode(), username.encode(), hashlib.sha256).hexdigest()
    return f"{username}:{sig}"

def verify_session(request: Request) -> Optional[str]:
    cookie = request.cookies.get("session", "")
    if ":" not in cookie:
        return None
    username, sig = cookie.split(":", 1)
    expected = hmac.new(SECRET_KEY.encode(), username.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected) and username in USERS:
        return username
    return None

# ── Rotas ─────────────────────────────────────────────────────────────────────

def _nav_base(request: Request, active_page: str = "") -> dict:
    username = verify_session(request) or ""
    display = USER_DISPLAY.get(username, username.capitalize()) if username else ""
    return {"nav_username": display, "active_page": active_page, "nav_clients": None, "nav_current_client": None}

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not verify_session(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse("home.html", {"request": request, **_nav_base(request, "home")})

@app.get("/conversar", response_class=HTMLResponse)
async def conversar(request: Request):
    if not verify_session(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse("chat.html", {"request": request, **_nav_base(request, "conversar")})

@app.get("/ata", response_class=HTMLResponse)
async def ata_page(request: Request):
    if not verify_session(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse("ata.html", {"request": request, **_nav_base(request, "ata")})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    if USERS.get(username) == password:
        token = make_session_token(username)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("session", token, max_age=86400 * 7)
        return response
    return RedirectResponse("/login?error=1", status_code=303)

@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session")
    return response

@app.get("/api/conversations")
async def get_conversations(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    return JSONResponse(list_conversations())

@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str, request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    path = CONVS_DIR / f"{conv_id}.json"
    if path.exists():
        path.unlink()
        return JSONResponse({"ok": True})
    raise HTTPException(status_code=404)

@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str, request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    data = load_conversation(conv_id)
    if not data:
        raise HTTPException(status_code=404)
    return JSONResponse(data)

@app.post("/chat")
async def chat(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)

    body = await request.json()
    messages = body.get("messages", [])
    user_name = body.get("user", "colaborador")
    conv_id = body.get("conv_id") or str(uuid.uuid4())
    client_id = body.get("client")
    client_name = body.get("client_name")

    documents = load_documents(client_id)
    if client_id and client_name:
        client_context = (
            f"CLIENTE ATIVO NESTA CONVERSA: {client_name} ({client_id})\n"
            f"O colaborador está trabalhando com este cliente. Todas as perguntas sem contexto explícito "
            f"se referem a {client_name}. Priorize as informações da ficha deste cliente nas suas respostas."
        )
    else:
        client_context = "Nenhum cliente específico selecionado. Responda de forma geral sobre a Cardô."

    specialist = classify_specialist(messages)
    specialist_prompt = SPECIALIST_PROMPTS.get(specialist, "") if specialist else ""

    system = SYSTEM_PROMPT.format(documents=documents, client_context=client_context)
    if specialist_prompt:
        system = system + specialist_prompt

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system,
        messages=messages,
        tools=TOOLS,
    )

    task_created = None

    # Verifica se o modelo quer usar uma ferramenta
    if response.stop_reason == "tool_use":
        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block and tool_block.name == "criar_tarefa_clickup":
            inp = tool_block.input
            result = create_clickup_task(
                name=inp.get("name", ""),
                description=inp.get("description", ""),
                list_name=inp.get("list_name", ""),
                due_date=inp.get("due_date", ""),
            )
            task_created = result

            # Devolve resultado da tool para o modelo gerar resposta final
            tool_result_messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }]},
            ]
            followup = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                messages=tool_result_messages,
                tools=TOOLS,
            )
            reply = next((b.text for b in followup.content if hasattr(b, "text")), "Tarefa criada.")
            messages = tool_result_messages + [{"role": "assistant", "content": reply}]
        else:
            reply = next((b.text for b in response.content if hasattr(b, "text")), "")
            messages = messages + [{"role": "assistant", "content": reply}]
    else:
        reply = next((b.text for b in response.content if hasattr(b, "text")), "")
        messages = messages + [{"role": "assistant", "content": reply}]

    first_user = next((m["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], str)), "")
    title = first_user[:60] + ("..." if len(first_user) > 60 else "")
    save_conversation(conv_id, user_name, messages, title, client=client_id, client_name=client_name)

    return JSONResponse({"reply": reply, "conv_id": conv_id, "task_created": task_created, "specialist": specialist})

@app.get("/api/granola/meetings")
async def get_granola_meetings(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    docs = await granola_post("get-documents")
    if not isinstance(docs, list):
        return JSONResponse({"error": "Não foi possível conectar ao Granola."}, status_code=503)
    meetings = []
    for doc in docs:
        if doc.get("deleted_at"):
            continue
        title = doc.get("title") or "Sem título"
        if title.startswith("[PRO]"):
            continue
        date = extract_date_from_title(title) or (doc.get("created_at") or "")[:10]
        meetings.append({
            "id": doc.get("id"),
            "title": title,
            "date": date,
            "has_content": bool(doc.get("notes_plain") or doc.get("notes_markdown") or doc.get("valid_meeting")),
            "has_summary": bool(doc.get("summary")),
        })
    meetings.sort(key=lambda x: x["date"], reverse=True)
    return JSONResponse(meetings[:30])


@app.post("/api/ata/generate")
async def generate_ata(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    meeting_id = body.get("meeting_id")

    docs = await granola_post("get-documents")
    if not isinstance(docs, list):
        raise HTTPException(status_code=503, detail="Erro ao conectar ao Granola.")

    doc = next((d for d in docs if d.get("id") == meeting_id), None)
    if not doc:
        raise HTTPException(status_code=404, detail="Reunião não encontrada.")

    doc_title = doc.get("title") or ""
    date = extract_date_from_title(doc_title) or (doc.get("created_at") or "")[:10]

    ata_text = sanitize_ata_content(doc.get("notes_markdown") or doc.get("notes_plain") or "")

    if not ata_text:
        ata_text = await granola_get_panels_markdown(meeting_id)

    if not ata_text:
        transcript = await granola_get_transcript(meeting_id)
        if not transcript:
            raise HTTPException(status_code=422, detail="Esta reunião não tem notas nem transcript disponível.")
        ata_text = transcript

    return JSONResponse({"ata": ata_text, "title": doc_title, "date": date})


@app.post("/api/ata/publish")
async def publish_ata(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    content = body.get("content", "")
    client_sigla = body.get("client_sigla", "").upper()
    date_iso = body.get("date", "")  # formato YYYY-MM-DD
    meeting_title = body.get("meeting_title", "").strip()

    # Converter data para DD/MM/AAAA (padrão do ClickUp)
    try:
        y, m, d = date_iso.split("-")
        date_str = f"{d}/{m}/{y}"
    except Exception:
        date_str = date_iso

    topic = extract_meeting_topic(meeting_title) if meeting_title else ""
    page_title = f"{date_str} - {topic}" if topic else date_str

    parent_page_id = CLICKUP_ATAS_PAGES.get(client_sigla)
    if not parent_page_id:
        raise HTTPException(status_code=400, detail=f"Cliente '{client_sigla}' não tem página de Atas mapeada.")

    headers = {"Authorization": CLICKUP_API_KEY, "Content-Type": "application/json"}

    # Criar sub-página dentro de "Atas de Reunião" do cliente
    r = httpx.post(
        f"https://api.clickup.com/api/v3/workspaces/{CLICKUP_WORKSPACE_ID}/docs/{CLICKUP_DOC_ID}/pages",
        headers=headers,
        json={
            "name": page_title,
            "parent_page_id": parent_page_id,
            "content": content,
            "content_format": "text/md",
        },
        timeout=15,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Erro ao criar página no ClickUp: {r.text}")

    resp = r.json()
    page_id = resp.get("id")
    actual_parent = resp.get("parent_page_id")
    print(f"[ATA] Página criada: {page_id} | parent_page_id enviado={parent_page_id} | retornado={actual_parent}")
    page_url = f"https://app.clickup.com/{CLICKUP_WORKSPACE_ID}/v/dc/{CLICKUP_DOC_ID}/{page_id}"
    return JSONResponse({"ok": True, "url": page_url})


@app.post("/api/ata/extract-tasks")
async def extract_tasks(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    ata_content = body.get("content", "")
    client_sigla = body.get("client_sigla", "").upper()
    client_name = body.get("client_name", "")

    members_list = "\n".join(f"- {n}" for n in CLICKUP_MEMBERS)

    areas_desc = "\n".join(f'- "{k}": {v}' for k, v in CLICKUP_AREA_LABELS.items())

    prompt = f"""Analise esta ata de reunião e extraia TODAS as ações concretas que precisam ser executadas.
Inclua ações da Cardô Marketing E do cliente.

CLIENTE: {client_name} ({client_sigla})

ATA:
{ata_content}

MEMBROS DA EQUIPE CARDÔ disponíveis para atribuição:
{members_list}

ÁREAS disponíveis (use exatamente uma dessas chaves):
{areas_desc}

Retorne um JSON com a seguinte estrutura (apenas o JSON, sem texto adicional):
{{
  "tasks": [
    {{
      "name": "[{client_sigla}] Nome da tarefa conciso e acionável",
      "description": "Contexto adicional se necessário (pode ser vazio)",
      "assignee": "nome do membro se mencionado, senão null",
      "due_date": "YYYY-MM-DD se prazo mencionado, senão null",
      "area": "chave da área responsável (atendimento/trafego/redacao/automacao/conteudo)",
      "is_client": true ou false
    }}
  ]
}}

Regras:
- Nome da tarefa: começa com [{client_sigla}], conciso e descreve a ação principal
- Para ações do cliente, assignee = null e area = "atendimento"
- Para ações da Cardô: classifique pela área responsável
  - Google Ads, Meta Ads, campanhas, tráfego → "trafego"
  - Copy, texto, script, anúncio → "redacao"
  - Automação, planilha, integração → "automacao"
  - Post, conteúdo orgânico, stories → "conteudo"
  - Reunião, relatório, alinhamento, atendimento → "atendimento"
- Se prazo mencionado (ex: "até sexta", "até final da semana"), converta para data real (hoje: {datetime.now().strftime('%Y-%m-%d')})
- Extraia APENAS ações concretas, não discussões ou observações"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system="Você é um extrator de tarefas. Responda APENAS com JSON válido, sem texto antes ou depois, sem markdown, sem blocos de código.",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = next((b.text for b in response.content if hasattr(b, "text")), "{}")
    raw = raw.strip()
    # Remove markdown code fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()
    # Try direct parse first
    try:
        tasks_data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: extract outermost {} block
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            raise HTTPException(status_code=500, detail="Não foi possível extrair tarefas da ata.")
        try:
            tasks_data = json.loads(match.group())
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail=f"Erro ao processar resposta do modelo: {e}")
    return JSONResponse(tasks_data)


@app.post("/api/ata/create-tasks")
async def create_tasks_from_ata(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    tasks = body.get("tasks", [])

    headers = {"Authorization": CLICKUP_API_KEY, "Content-Type": "application/json"}
    created = []
    errors = []

    client_sigla = (body.get("client_sigla") or "").upper()

    # Due date = tomorrow (local date) at start of day, in ms
    from datetime import date as _date
    tomorrow = _date.today() + timedelta(days=1)
    due_ms = int(datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, 0, 0).timestamp() * 1000)

    # Custom field option for this client
    cliente_option_id = CLICKUP_CLIENTE_OPTIONS.get(client_sigla)

    JOSE_ID = 75499891
    VICTOR_ID = 54959381

    for task in tasks:
        name = task.get("name", "")
        description = task.get("description", "")
        area = (task.get("area") or "atendimento").lower().strip()
        is_client = task.get("is_client", False)

        list_id = CLICKUP_AREA_LISTS.get(area, CLICKUP_DEFAULT_LIST_ID)

        # Usa due_date da tarefa se fornecido (YYYY-MM-DD), senão amanhã
        task_due = task.get("due_date")
        if task_due:
            try:
                from datetime import date as _date2
                y, m, d = map(int, task_due.split("-"))
                task_due_ms = int(datetime(y, m, d, 9, 0, 0).timestamp() * 1000)
            except Exception:
                task_due_ms = due_ms
        else:
            task_due_ms = due_ms

        payload = {
            "name": name,
            "status": "a fazer",
            "due_date": task_due_ms,
            "due_date_time": False,
        }
        if description:
            payload["description"] = description
        # Custom field "Cliente" — sem tags
        if cliente_option_id:
            payload["custom_fields"] = [
                {"id": CLICKUP_CLIENTE_FIELD_ID, "value": cliente_option_id}
            ]
        # Assignee: tráfego → José; outros → Victor
        payload["assignees"] = [JOSE_ID if area == "trafego" else VICTOR_ID]

        r = httpx.post(
            f"https://api.clickup.com/api/v2/list/{list_id}/task",
            headers=headers,
            json=payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            data = r.json()
            created.append({"name": name, "url": data.get("url"), "id": data.get("id")})
        else:
            errors.append({"name": name, "error": r.text})

    return JSONResponse({"created": created, "errors": errors})


@app.get("/tarefas", response_class=HTMLResponse)
async def tarefas_page(request: Request):
    if not verify_session(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse("tarefas.html", {"request": request, **_nav_base(request, "tarefas")})


@app.post("/api/tarefas/extrair")
async def extrair_tarefas_livre(request: Request):
    """Extrai tarefas de texto livre ou imagem (base64)."""
    if not verify_session(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    texto = body.get("texto", "")
    imagem_b64 = body.get("imagem_b64", "")   # data:image/...;base64,...
    cliente = body.get("cliente", "")

    members_list = "\n".join(f"- {n}" for n in CLICKUP_MEMBERS)
    areas_desc = "\n".join(f'- "{k}": {v}' for k, v in CLICKUP_AREA_LABELS.items())
    hoje = datetime.now().strftime("%Y-%m-%d")
    client_sigla = cliente.upper()

    system_prompt = "Você é um extrator de tarefas. Responda APENAS com JSON válido, sem texto antes ou depois, sem markdown, sem blocos de código."

    instrucao = f"""Analise o conteúdo abaixo (pode ser uma conversa, lista de pendências, print ou texto livre) e extraia TODAS as ações concretas que precisam ser executadas.

CLIENTE: {client_sigla or "não informado"}
HOJE: {hoje}

MEMBROS DA EQUIPE CARDÔ disponíveis:
{members_list}

ÁREAS disponíveis:
{areas_desc}

Retorne JSON:
{{
  "tasks": [
    {{
      "name": "[{client_sigla or 'TAG'}] Título conciso e acionável",
      "description": "Contexto adicional (pode ser vazio)",
      "assignee": "nome do membro se mencionado, senão null",
      "due_date": "YYYY-MM-DD se prazo mencionado, senão null",
      "area": "atendimento|trafego|redacao|automacao|conteudo",
      "is_client": true ou false
    }}
  ]
}}

Regras:
- Extraia APENAS ações concretas, não discussões
- O título deve descrever a ação de forma literal, sem adicionar contexto inferido. Use exatamente o que foi pedido, sem completar com suposições como "nas campanhas", "nas automações", "no site", etc.
- Google Ads, Meta Ads, tráfego → "trafego"
- Copy, texto, script → "redacao"
- Automação, planilha, integração → "automacao"
- Post, stories, conteúdo orgânico → "conteudo"
- Reunião, relatório, alinhamento → "atendimento"
- Ações do cliente: is_client=true, area="atendimento"
- Se o cliente não foi informado, use "TASK" como prefixo"""

    if imagem_b64:
        # Remove o prefixo data:image/...;base64,
        if "," in imagem_b64:
            media_type_part, data_part = imagem_b64.split(",", 1)
            media_type = media_type_part.split(":")[1].split(";")[0] if ":" in media_type_part else "image/jpeg"
        else:
            data_part = imagem_b64
            media_type = "image/jpeg"
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data_part}},
            {"type": "text", "text": instrucao + ("\n\nTEXTO ADICIONAL:\n" + texto if texto else "")},
        ]
    else:
        content = instrucao + "\n\nCONTEÚDO:\n" + texto

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )
    raw = next((b.text for b in response.content if hasattr(b, "text")), "{}")
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        tasks_data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            raise HTTPException(status_code=500, detail="Não foi possível extrair tarefas.")
        tasks_data = json.loads(m.group())
    return JSONResponse(tasks_data)

# ── Meta Ads ──────────────────────────────────────────────────────────────────

from datetime import date as _date

def _compute_period(preset: str, since: str, until: str):
    """Return (since, until, prev_since, prev_until) as YYYY-MM-DD strings."""
    today = _date.today()
    if since and until:
        ds = _date.fromisoformat(since)
        du = _date.fromisoformat(until)
        delta = (du - ds).days + 1
        pu = ds - timedelta(days=1)
        ps = pu - timedelta(days=delta - 1)
        return since, until, ps.isoformat(), pu.isoformat()
    p = preset or "last_7d"
    if p == "today":
        s, u = today, today
        ps2, pu2 = today - timedelta(days=1), today - timedelta(days=1)
    elif p == "yesterday":
        s = u = today - timedelta(days=1)
        ps2 = pu2 = today - timedelta(days=2)
    elif p == "last_7d":
        s, u = today - timedelta(days=6), today
        ps2, pu2 = today - timedelta(days=13), today - timedelta(days=7)
    elif p == "last_30d":
        s, u = today - timedelta(days=29), today
        ps2, pu2 = today - timedelta(days=59), today - timedelta(days=30)
    elif p == "this_month":
        s = today.replace(day=1); u = today
        delta = (u - s).days + 1
        pu2 = s - timedelta(days=1); ps2 = pu2 - timedelta(days=delta - 1)
    elif p == "last_month":
        first_this = today.replace(day=1)
        u = first_this - timedelta(days=1); s = u.replace(day=1)
        pu2 = s - timedelta(days=1); ps2 = pu2.replace(day=1)
    else:
        s, u = today - timedelta(days=6), today
        ps2, pu2 = today - timedelta(days=13), today - timedelta(days=7)
    return s.isoformat(), u.isoformat(), ps2.isoformat(), pu2.isoformat()

_LEAD_TYPES = (
    "lead", "onsite_conversion.lead_grouped",
    "onsite_conversion.messaging_first_reply", "contact_total",
)
_PURCHASE_TYPES = ("purchase", "omni_purchase", "offsite_conversion.fb_pixel_purchase")
_INS_FIELDS = "spend,impressions,clicks,reach,actions,video_play_actions"

def _extract_action(actions: list, types: tuple) -> int:
    return sum(int(float(a.get("value", 0))) for a in (actions or []) if a.get("action_type") in types)

async def _meta_paginate(path: str, token: str, params: dict) -> list:
    """Generic paginated fetch for any Meta API path."""
    items = []
    async with httpx.AsyncClient() as c:
        url = f"{META_GRAPH_BASE}/{path}"
        qp = {"access_token": token, "limit": 100, **params}
        while url:
            try:
                r = await c.get(url, params=qp, timeout=25)
                if r.status_code != 200:
                    print(f"[meta] {r.status_code}: {r.text[:200]}")
                    break
                body = r.json()
                items.extend(body.get("data", []))
                url = body.get("paging", {}).get("next")
                qp = {}
            except Exception as e:
                print(f"[meta] paginate err: {e}"); break
    return items

async def _meta_get_all(account_id: str, token: str, params: dict) -> list:
    items = []
    async with httpx.AsyncClient() as c:
        url = f"{META_GRAPH_BASE}/{account_id}/insights"
        qp = {"access_token": token, "limit": 100, **params}
        while url:
            try:
                r = await c.get(url, params=qp, timeout=25)
                if r.status_code != 200:
                    print(f"[meta] {r.status_code}: {r.text[:200]}")
                    break
                body = r.json()
                items.extend(body.get("data", []))
                url = body.get("paging", {}).get("next")
                qp = {}
            except Exception as e:
                print(f"[meta] erro: {e}"); break
    return items

async def _get_insights(account_id: str, token: str, since: str, until: str, level: str) -> list:
    name_f = {"campaign": "campaign_name", "adset": "adset_name"}.get(level, "")
    extra  = ",campaign_name" if level == "adset" else ""
    fields = f"{name_f}{extra},{_INS_FIELDS}" if name_f else _INS_FIELDS
    return await _meta_get_all(account_id, token, {
        "fields": fields,
        "time_range": json.dumps({"since": since, "until": until}),
        "level": level,
    })

async def _get_daily_insights(account_id: str, token: str, since: str, until: str) -> list:
    return await _meta_get_all(account_id, token, {
        "fields": _INS_FIELDS,
        "time_range": json.dumps({"since": since, "until": until}),
        "time_increment": 1, "level": "account",
    })

def _row_metrics(row: dict) -> dict:
    spend = float(row.get("spend", 0))
    imp   = int(row.get("impressions", 0))
    clk   = int(row.get("clicks", 0))
    reach = int(row.get("reach", 0))
    acts  = row.get("actions", [])
    vpa   = row.get("video_play_actions", [])
    leads = _extract_action(acts, _LEAD_TYPES)
    purch = _extract_action(acts, _PURCHASE_TYPES)
    video_views = _extract_action(vpa, ("video_view",))
    thruplays   = _extract_action(acts, ("video_thruplay_watched",))
    return {
        "spend": spend, "impressions": imp, "clicks": clk, "reach": reach,
        "leads": leads, "purchases": purch,
        "video_views": video_views, "thruplays": thruplays,
        "ctr":  clk / imp * 100 if imp else 0,
        "cpm":  spend / imp * 1000 if imp else 0,
        "cpc":  spend / clk if clk else 0,
        "cpl":  spend / leads if leads else None,
    }

def _merge_totals(rows: list) -> dict:
    t = {"spend": 0.0, "impressions": 0, "clicks": 0, "reach": 0,
         "leads": 0, "purchases": 0, "video_views": 0, "thruplays": 0}
    for r in rows:
        for k in t: t[k] += r.get(k, 0)
    sp, im, cl, ld = t["spend"], t["impressions"], t["clicks"], t["leads"]
    return {
        **t,
        "ctr": round(cl / im * 100 if im else 0, 2),
        "cpm": round(sp / im * 1000 if im else 0, 2),
        "cpc": round(sp / cl if cl else 0, 2),
        "cpl": round(sp / ld if ld else 0, 2) or None,
        "spend": round(sp, 2),
    }

def _pct(curr, prev):
    if not prev or prev == 0 or curr is None: return None
    return round((curr - prev) / abs(prev) * 100, 1)

async def _fetch_account_name(account_id: str, token: str) -> str:
    async with httpx.AsyncClient() as c:
        try:
            r = await c.get(f"{META_GRAPH_BASE}/{account_id}",
                params={"fields": "name", "access_token": token}, timeout=8)
            if r.status_code == 200:
                return r.json().get("name", account_id)
        except Exception:
            pass
    return account_id

@app.get("/trafego", response_class=HTMLResponse)
async def trafego_page(request: Request, client: str = None):
    username = verify_session(request)
    if not username:
        return RedirectResponse("/login")
    configured = [s for s, cfg in META_CLIENTS.items() if cfg["token"]()]
    clients_info = {}
    for s in configured:
        cfg = META_CLIENTS[s]
        token = cfg["token"]()
        acc_list = []
        for acc_id in cfg["accounts"]:
            name = await _fetch_account_name(acc_id, token) if token else acc_id
            acc_list.append({"id": acc_id, "name": name})
        clients_info[s] = {"name": cfg["name"], "accounts": acc_list}
    selected_client = client if client and client in clients_info else (configured[0] if configured else "")
    # Build nav_clients with display names for sidebar
    nav_clients = {s: {"display_name": cfg["name"]} for s, cfg in META_CLIENTS.items() if cfg["token"]()} if configured else None
    gads_configured = {s: cfg["nome"] for s, cfg in GOOGLE_ADS_CLIENTS.items() if cfg.get("windsor_url")}
    meta_configured = {s: cfg["name"] for s, cfg in META_CLIENTS.items() if cfg["token"]() and cfg["accounts"]}
    return templates.TemplateResponse("trafego.html", {
        "request": request,
        "configured_clients": configured,
        "clients_info": json.dumps(clients_info),
        "selected_client": selected_client,
        "nav_username": USER_DISPLAY.get(username, username.capitalize() if username else ""),
        "active_page": "trafego",
        "nav_clients": nav_clients,
        "nav_current_client": selected_client,
        "gads_clients": json.dumps(gads_configured),
    })

@app.get("/api/trafego/metrics")
async def get_trafego_metrics(
    request: Request,
    client_sigla: str = "HIRE",
    preset: str = "last_7d",
    since: str = None,
    until: str = None,
    account_id: str = None,
    level: str = "campaign",
):
    if not verify_session(request): raise HTTPException(status_code=401)
    cfg = META_CLIENTS.get(client_sigla.upper())
    if not cfg or not cfg["accounts"]:
        raise HTTPException(status_code=404, detail="Cliente não configurado para Meta Ads.")
    token = cfg["token"]()
    if not token:
        raise HTTPException(status_code=503, detail=f"Token Meta não configurado para {client_sigla}.")

    d_since, d_until, p_since, p_until = _compute_period(
        preset if not (since and until) else None, since, until
    )
    accounts = ([account_id] if account_id and account_id in cfg["accounts"] else cfg["accounts"])
    name_key = "campaign_name" if level == "campaign" else "adset_name"

    curr_acc_rows, prev_acc_rows, merged = [], [], {}

    for acc in accounts:
        for row in await _get_insights(acc, token, d_since, d_until, "account"):
            curr_acc_rows.append(_row_metrics(row))
        for row in await _get_insights(acc, token, p_since, p_until, "account"):
            prev_acc_rows.append(_row_metrics(row))
        for row in await _get_insights(acc, token, d_since, d_until, level):
            name = row.get(name_key, "")
            m = _row_metrics(row)
            if name in merged:
                for k in ("spend","impressions","clicks","reach","leads","purchases"):
                    merged[name][k] += m[k]
            else:
                merged[name] = {**m, "name": name}
                if level == "adset":
                    merged[name]["campaign_name"] = row.get("campaign_name", "")

    summary_curr = _merge_totals(curr_acc_rows)
    summary_prev = _merge_totals(prev_acc_rows)
    summary = {k: v for k, v in summary_curr.items()}
    for k in ("spend","impressions","clicks","reach","leads","ctr","cpm","cpc","cpl"):
        summary[f"{k}_delta"] = _pct(summary_curr.get(k), summary_prev.get(k))

    avg_cpl = summary_curr.get("cpl")
    items = []
    for row in merged.values():
        sp, im, cl, ld = row["spend"], row["impressions"], row["clicks"], row["leads"]
        row["ctr"] = round(cl / im * 100 if im else 0, 2)
        row["cpm"] = round(sp / im * 1000 if im else 0, 2)
        row["cpc"] = round(sp / cl if cl else 0, 2)
        row["cpl"] = round(sp / ld if ld else 0, 2) or None
        row["spend"] = round(sp, 2)
        risks = []
        if row["ctr"] < 0.5 and im > 1000:        risks.append("CTR baixo")
        if row["cpl"] and avg_cpl and row["cpl"] > avg_cpl * 2: risks.append("CPL alto")
        if sp > 30 and ld == 0 and im > 500:       risks.append("Sem conversões")
        row["risks"] = risks
        items.append(row)
    items.sort(key=lambda x: x["spend"], reverse=True)

    return JSONResponse({
        "client": cfg["name"],
        "since": d_since, "until": d_until,
        "prev_since": p_since, "prev_until": p_until,
        "accounts": accounts, "level": level,
        "summary": summary, "items": items,
    })

@app.get("/api/trafego/daily")
async def get_trafego_daily(
    request: Request,
    client_sigla: str = "HIRE",
    preset: str = "last_7d",
    since: str = None,
    until: str = None,
    account_id: str = None,
):
    if not verify_session(request): raise HTTPException(status_code=401)
    cfg = META_CLIENTS.get(client_sigla.upper())
    if not cfg or not cfg["accounts"]:
        raise HTTPException(status_code=404, detail="Cliente não configurado.")
    token = cfg["token"]()
    if not token:
        raise HTTPException(status_code=503, detail="Token não configurado.")

    d_since, d_until, _, _ = _compute_period(
        preset if not (since and until) else None, since, until
    )
    accounts = ([account_id] if account_id and account_id in cfg["accounts"] else cfg["accounts"])

    by_date: dict = {}
    for acc in accounts:
        for row in await _get_daily_insights(acc, token, d_since, d_until):
            day = row.get("date_start", "")
            if day not in by_date:
                by_date[day] = {"date": day, "spend": 0.0, "impressions": 0, "clicks": 0, "leads": 0}
            by_date[day]["spend"]       += float(row.get("spend", 0))
            by_date[day]["impressions"] += int(row.get("impressions", 0))
            by_date[day]["clicks"]      += int(row.get("clicks", 0))
            by_date[day]["leads"]       += _extract_action(row.get("actions", []), _LEAD_TYPES)

    days = sorted(by_date.values(), key=lambda x: x["date"])
    for d in days:
        d["spend"] = round(d["spend"], 2)
    return JSONResponse({"days": days})

async def _fetch_one_ad_creative(c: httpx.AsyncClient, ad_id: str, token: str) -> tuple:
    """Fetch creative info for a single ad."""
    try:
        r = await c.get(f"{META_GRAPH_BASE}/{ad_id}", params={
            "fields": "effective_status,creative{thumbnail_url,image_url,instagram_permalink_url,effective_object_story_id}",
            "access_token": token,
        }, timeout=15)
        if r.status_code != 200:
            return ad_id, {}
        data = r.json()
        creative = data.get("creative", {})
        thumbnail = creative.get("image_url") or creative.get("thumbnail_url", "")
        link = creative.get("instagram_permalink_url", "")
        if not link:
            story_id = creative.get("effective_object_story_id", "")
            if story_id:
                link = f"https://www.facebook.com/{story_id}"
        effective_status = data.get("effective_status", "UNKNOWN")
        return ad_id, {"thumbnail": thumbnail, "link": link, "effective_status": effective_status}
    except Exception as e:
        print(f"[meta creative {ad_id}] {e}")
        return ad_id, {}

async def _fetch_ad_creatives_by_ids(ad_ids: list, token: str) -> dict:
    """Fetch creative info for each ad concurrently (batches of 20)."""
    result = {}
    async with httpx.AsyncClient() as c:
        for i in range(0, len(ad_ids), 20):
            batch = ad_ids[i:i+20]
            tasks = [_fetch_one_ad_creative(c, ad_id, token) for ad_id in batch]
            pairs = await asyncio.gather(*tasks)
            for ad_id, info in pairs:
                if info:
                    result[ad_id] = info
    return result

@app.get("/api/trafego/creatives")
async def get_trafego_creatives(
    request: Request,
    client_sigla: str = "HIRE",
    preset: str = "last_7d",
    since: str = None,
    until: str = None,
    account_id: str = None,
):
    if not verify_session(request): raise HTTPException(status_code=401)
    cfg = META_CLIENTS.get(client_sigla.upper())
    if not cfg or not cfg["accounts"]:
        raise HTTPException(status_code=404, detail="Cliente não configurado.")
    token = cfg["token"]()
    if not token:
        raise HTTPException(status_code=503, detail="Token não configurado.")

    d_since, d_until, _, _ = _compute_period(
        preset if not (since and until) else None, since, until
    )
    accounts = ([account_id] if account_id and account_id in cfg["accounts"] else cfg["accounts"])

    all_ads = []
    for acc in accounts:
        rows = await _meta_get_all(acc, token, {
            "fields": f"ad_id,ad_name,campaign_id,campaign_name,{_INS_FIELDS}",
            "time_range": json.dumps({"since": d_since, "until": d_until}),
            "level": "ad",
        })
        ad_ids = [r.get("ad_id", "") for r in rows if r.get("ad_id")]
        creatives = await _fetch_ad_creatives_by_ids(ad_ids, token)
        for row in rows:
            ad_id = row.get("ad_id", "")
            m = _row_metrics(row)
            cinfo = creatives.get(ad_id, {})
            sp = m["spend"]; ld = m["leads"]; im = m["impressions"]; cl = m["clicks"]
            vv = m["video_views"]
            all_ads.append({
                "id": ad_id,
                "name": row.get("ad_name", ""),
                "campaign_id": row.get("campaign_id", ""),
                "campaign_name": row.get("campaign_name", ""),
                "thumbnail": cinfo.get("thumbnail", ""),
                "link": cinfo.get("link", ""),
                "effective_status": cinfo.get("effective_status", "UNKNOWN"),
                "spend": round(sp, 2),
                "impressions": im, "clicks": cl, "reach": m["reach"],
                "leads": ld, "video_views": vv, "thruplays": m["thruplays"],
                "ctr":  round(cl / im * 100 if im else 0, 2),
                "cpm":  round(sp / im * 1000 if im else 0, 2),
                "cpc":  round(sp / cl if cl else 0, 2),
                "cpl":  round(sp / ld if ld else 0, 2) or None,
                "hook_rate": round(vv / im * 100 if im and vv else 0, 2),
            })

    all_ads.sort(key=lambda x: x["spend"], reverse=True)
    return JSONResponse({"ads": all_ads, "since": d_since, "until": d_until})


# ── Google Ads Dashboard ─────────────────────────────────────────────────────

def _windsor_fetch(windsor_url: str, fields: str, preset: str) -> list:
    from urllib.parse import urlparse, parse_qs, urlencode
    parsed = urlparse(windsor_url)
    api_key = parse_qs(parsed.query).get("api_key", [""])[0]
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    url = f"{base}?{urlencode({'api_key': api_key, 'date_preset': preset, 'fields': fields})}"
    with httpx.Client(timeout=30) as c:
        r = c.get(url)
        r.raise_for_status()
    return r.json().get("data", [])

@app.get("/gads", response_class=HTMLResponse)
async def gads_page(request: Request, client: str = "SRW"):
    username = verify_session(request)
    if not username:
        return RedirectResponse("/login")
    configured = [s for s, cfg in GOOGLE_ADS_CLIENTS.items() if cfg.get("windsor_url")]
    selected = client if client in configured else (configured[0] if configured else "")
    gads_names = {s: cfg["nome"] for s, cfg in GOOGLE_ADS_CLIENTS.items() if cfg.get("windsor_url")}
    meta_names  = {s: cfg["name"] for s, cfg in META_CLIENTS.items() if cfg["token"]() and cfg["accounts"]}
    has_meta = selected in meta_names
    return templates.TemplateResponse("gads.html", {
        "request": request,
        "configured_clients": configured,
        "selected_client": selected,
        "gads_clients": json.dumps(gads_names),
        "meta_clients": json.dumps(meta_names),
        "has_meta": has_meta,
        "nav_username": USER_DISPLAY.get(username, username.capitalize()),
        "active_page": "gads",
    })

@app.get("/api/gads/metrics")
async def get_gads_metrics(request: Request, client_sigla: str = "SRW", preset: str = "last_30d"):
    if not verify_session(request): raise HTTPException(status_code=401)
    cfg = GOOGLE_ADS_CLIENTS.get(client_sigla.upper())
    if not cfg or not cfg.get("windsor_url"):
        raise HTTPException(status_code=404, detail="Cliente não configurado para Google Ads.")
    rows = await asyncio.to_thread(
        _windsor_fetch, cfg["windsor_url"],
        "campaign,clicks,conversions,impressions,spend", preset
    )
    campaigns: dict = {}
    for row in rows:
        name = row.get("campaign", "")
        if not name: continue
        if name not in campaigns:
            campaigns[name] = {"clicks": 0, "conversions": 0.0, "impressions": 0, "spend": 0.0}
        campaigns[name]["clicks"]      += int(row.get("clicks", 0) or 0)
        campaigns[name]["conversions"] += float(row.get("conversions", 0) or 0)
        campaigns[name]["impressions"] += int(row.get("impressions", 0) or 0)
        campaigns[name]["spend"]       += float(row.get("spend", 0) or 0)
    items = []
    for name, d in sorted(campaigns.items(), key=lambda x: -x[1]["spend"]):
        cl, cv, im, sp = d["clicks"], d["conversions"], d["impressions"], d["spend"]
        items.append({
            "name": name,
            "clicks": cl, "conversions": round(cv, 1),
            "impressions": im, "spend": round(sp, 2),
            "cpc": round(sp / cl, 2) if cl else 0,
            "cpa": round(sp / cv, 2) if cv else 0,
            "ctr": round(cl / im * 100, 2) if im else 0,
        })
    total_sp = sum(i["spend"] for i in items)
    total_cl = sum(i["clicks"] for i in items)
    total_cv = sum(i["conversions"] for i in items)
    total_im = sum(i["impressions"] for i in items)
    summary = {
        "spend": round(total_sp, 2),
        "clicks": total_cl,
        "conversions": round(total_cv, 1),
        "impressions": total_im,
        "cpc": round(total_sp / total_cl, 2) if total_cl else 0,
        "cpa": round(total_sp / total_cv, 2) if total_cv else 0,
        "ctr": round(total_cl / total_im * 100, 2) if total_im else 0,
    }
    return JSONResponse({"client": cfg["nome"], "preset": preset, "summary": summary, "items": items})

@app.get("/api/gads/daily")
async def get_gads_daily(request: Request, client_sigla: str = "SRW", preset: str = "last_30d"):
    if not verify_session(request): raise HTTPException(status_code=401)
    cfg = GOOGLE_ADS_CLIENTS.get(client_sigla.upper())
    if not cfg or not cfg.get("windsor_url"):
        raise HTTPException(status_code=404, detail="Cliente não configurado.")
    rows = await asyncio.to_thread(
        _windsor_fetch, cfg["windsor_url"],
        "clicks,conversions,spend,date", preset
    )
    by_date: dict = {}
    for row in rows:
        day = (row.get("date", "") or "")[:10]
        if not day: continue
        if day not in by_date:
            by_date[day] = {"date": day, "spend": 0.0, "clicks": 0, "conversions": 0.0}
        by_date[day]["spend"]       += float(row.get("spend", 0) or 0)
        by_date[day]["clicks"]      += int(row.get("clicks", 0) or 0)
        by_date[day]["conversions"] += float(row.get("conversions", 0) or 0)
    days = sorted(by_date.values(), key=lambda x: x["date"])
    for d in days:
        d["spend"] = round(d["spend"], 2)
        d["conversions"] = round(d["conversions"], 1)
    return JSONResponse({"days": days})

@app.get("/api/gads/keywords")
async def get_gads_keywords(request: Request, client_sigla: str = "SRW", preset: str = "last_30d"):
    if not verify_session(request): raise HTTPException(status_code=401)
    cfg = GOOGLE_ADS_CLIENTS.get(client_sigla.upper())
    if not cfg or not cfg.get("windsor_url"):
        raise HTTPException(status_code=404, detail="Cliente não configurado.")
    rows = await asyncio.to_thread(
        _windsor_fetch, cfg["windsor_url"],
        "campaign,keyword_text,clicks,conversions,spend", preset
    )
    by_campaign: dict = {}
    for row in rows:
        camp = row.get("campaign", "")
        kw   = row.get("keyword_text", "")
        if not camp or not kw: continue
        if camp not in by_campaign:
            by_campaign[camp] = {}
        if kw not in by_campaign[camp]:
            by_campaign[camp][kw] = {"clicks": 0, "conversions": 0.0, "spend": 0.0}
        by_campaign[camp][kw]["clicks"]      += int(row.get("clicks", 0) or 0)
        by_campaign[camp][kw]["conversions"] += float(row.get("conversions", 0) or 0)
        by_campaign[camp][kw]["spend"]       += float(row.get("spend", 0) or 0)
    result = {}
    for camp, kws in by_campaign.items():
        result[camp] = sorted(
            [{"kw": k, "clicks": v["clicks"], "conversions": round(v["conversions"],1), "spend": round(v["spend"],2)}
             for k, v in kws.items()],
            key=lambda x: -x["spend"]
        )
    return JSONResponse(result)

@app.get("/logs", response_class=HTMLResponse)
async def view_logs(request: Request):
    if not verify_session(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse("logs.html", {"request": request, "convs": list_conversations(), **_nav_base(request, "logs")})
