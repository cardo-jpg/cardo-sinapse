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
from backend.db import get_conn, dict_cursor
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build as gapi_build
from backend.gestao import router as gestao_router, init_db as gestao_init_db, page_gestao as gestao_page
from backend.financeiro import router as financeiro_router, init_db as financeiro_init_db, migrar_siga_startup
from backend.fin_pessoais import router as fp_router, init_db as fp_init_db
from backend.crm import router as crm_router, init_crm_db
from backend.briefings import router as briefings_router, init_briefings_db
from backend.atas_sinapse import router as atas_sinapse_router
from backend.habitos import router as habitos_router, init_db as habitos_init_db
from backend.inicio import router as inicio_router
from backend.clientes import router as clientes_router, init_db as clientes_init_db
from backend.whatsapp import router as whatsapp_router, init_db as whatsapp_init_db

load_dotenv()

BASE_DIR = Path(__file__).parent.parent


def _sa_creds(scopes: list[str]):
    """Carrega service account credentials do env var (Railway) ou do arquivo local."""
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        info = json.loads(sa_json)
        return service_account.Credentials.from_service_account_info(info, scopes=scopes)
    sa_path = BASE_DIR / "service_account.json"
    return service_account.Credentials.from_service_account_file(str(sa_path), scopes=scopes)
DOCS_DIR = BASE_DIR / "documents"
CONVS_DIR = BASE_DIR / "logs" / "conversations"
TEMPLATES_DIR = BASE_DIR / "frontend" / "templates"
STATIC_DIR = BASE_DIR / "frontend" / "static"
CONVS_DIR.mkdir(parents=True, exist_ok=True)

SECRET_KEY           = os.getenv("SECRET_KEY", "sinapse-secret-2026")
GREENN_WEBHOOK_TOKEN    = os.getenv("GREENN_WEBHOOK_TOKEN", "")
RD_STATION_CRM_TOKEN   = os.getenv("RD_STATION_CRM_TOKEN", "69ec14e6208dc300173fc00c")
DFT_PERF_SHEET_ID      = "17CuuYKxf13NHpJHRAZPoGW9_1Ni_xnIcVTeDQHXb5yQ"
DFT_PERF_SHEET_GID     = 550644796  # aba Google Ads
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
# Status inicial varia por lista (cada lista tem seu próprio conjunto de statuses)
CLICKUP_AREA_STATUS = {
    "atendimento":  "to do",
    "trafego":      "a fazer",
    "redacao":      "a fazer",
    "automacao":    "a fazer",
    "conteudo":     "a fazer",
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
    "DFT":   "fd139028-ba1e-4011-afa5-df0f04902e55",
}

GRANOLA_SUPABASE_PATH = Path.home() / "Library" / "Application Support" / "Granola" / "supabase.json"
GRANOLA_CACHE_PATH = Path.home() / "Library" / "Application Support" / "Granola" / "cache-v6.json"
GRANOLA_API_BASE = "https://api.granola.ai/v1"

# ── WICI 2 — Hire Brazil (Workshop Intensivo Carreira Internacional) ─────────
WICI2_SHEET_ID      = "1KjDx2tEpWdyWusjLFwW5SmeAEsA-aiH1ULOPgGQ4E-s"
WICI2_ABA_VENDAS    = "Green - Todas as Vendas"
WICI2_ABA_META      = "Meta Ads"
WICI2_ABA_METAS     = "Metas"
WICI2_ABA_RMKT      = "Ads RMKT"
WICI2_GRUPO         = int(os.getenv("WICI2_GRUPO", "0"))   # membros do grupo (atualizar via env)
WICI2_PREVISTO = {
    "total":    90_000.0,
    "workshop": 75_000.0,
    "lembrete":  1_750.0,
    "mentoria":  8_500.0,
    "corredor":    300.0,
}

# Product keyword → category (priority order, first match wins)
# NOTE: lembrete/corredor must come before "workshop" because their product names
# also contain the substring "Workshop Intensivo de Carreira Internacional".
_WICI2_PRODUCTS = [
    ("lembrete",  "Acesso"),
    ("corredor",  "Combo:"),
    ("mentoria",  "Análise de currículo"),
    ("workshop",  "Workshop Intensivo de Carreira Internacional"),
]

def _wici2_product_category(nome: str) -> str:
    nome_l = nome.strip().lower()
    for cat, kw in _WICI2_PRODUCTS:
        if kw.lower() in nome_l:
            return cat
    return "outro"

def _wici2_campaign_type(camp: str) -> str:
    if "Corredor" in camp:
        return "corredor"
    if "_Q" in camp:
        return "quente"
    if "_F" in camp:
        return "frio"
    return ""

def _wici2_parse_brl(s: str) -> float:
    try:
        return float(s.replace("R$ ", "").replace("R$", "").replace(".", "").replace(",", ".").strip())
    except Exception:
        return 0.0

def _wici2_parse_date(s: str):
    """Parse date string from column A (formats: dd/mm/yyyy HH:MM:SS or dd/mm/yyyy)."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None

def _wici2_fetch_metrics(date_start=None, date_end=None, profile: str = "") -> dict:
    """
    Lê as abas do Google Sheets e calcula todas as métricas do WICI 2.
    date_start / date_end: datetime.date para filtrar por período.
    profile: "" = geral, "malu" = campanhas com MO, "hire" = campanhas sem MO.
    """
    creds = _sa_creds(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = gapi_build("sheets", "v4", credentials=creds)
    sheets = svc.spreadsheets().values()

    # ── Metas (Canal | Meta de Ingressos Vendidos | Data de Inicio | Data de Fim) ──
    try:
        metas_rows = sheets.get(spreadsheetId=WICI2_SHEET_ID, range=WICI2_ABA_METAS).execute().get("values", [])[1:]
    except Exception:
        metas_rows = []

    meta_total = 1500
    meta_trafego = 1120
    meta_organico = 380
    data_inicio_geral = None
    data_fim_geral = None

    for row in metas_rows:
        if len(row) < 2:
            continue
        canal = row[0].strip().lower()
        raw = str(row[1]).replace(".", "").replace(",", "").strip() if row[1] else ""
        meta_val = int(raw) if raw.isdigit() else None
        d_inicio = _wici2_parse_date(row[2]) if len(row) > 2 else None
        d_fim    = _wici2_parse_date(row[3]) if len(row) > 3 else None

        if "tráfego" in canal or "trafego" in canal:
            if meta_val:
                meta_trafego = meta_val
        elif "orgânico" in canal or "organico" in canal:
            if meta_val:
                meta_organico = meta_val
        elif "total" in canal:
            if meta_val:
                meta_total = meta_val

        if d_inicio and (data_inicio_geral is None or d_inicio < data_inicio_geral):
            data_inicio_geral = d_inicio
        if d_fim and (data_fim_geral is None or d_fim > data_fim_geral):
            data_fim_geral = d_fim

    # Total de dias de lançamento: diferença (sem +1) entre início mais cedo e fim mais tardio
    if data_inicio_geral and data_fim_geral:
        total_dias_lancamento = max(1, (data_fim_geral - data_inicio_geral).days)
    else:
        total_dias_lancamento = 40  # fallback

    meta_diaria_geral    = round(meta_total    / total_dias_lancamento, 1)
    meta_diaria_org      = round(meta_organico / total_dias_lancamento, 1)
    meta_diaria_trafego  = round(meta_trafego  / total_dias_lancamento, 1)

    # ── Meta Ads (gasto por campanha) ─────────────────────────────────────────
    meta_rows_raw = sheets.get(spreadsheetId=WICI2_SHEET_ID, range=WICI2_ABA_META).execute().get("values", [])[1:]

    invest_total = invest_q = invest_f = invest_cor = 0.0
    invest_por_dia: dict = {}
    for row in meta_rows_raw:
        if len(row) < 4:
            continue
        row_date = _wici2_parse_date(row[0]) if row[0] else None
        if date_start and row_date and row_date < date_start:
            continue
        if date_end and row_date and row_date > date_end:
            continue
        try:
            cost = float(row[3].replace(",", "."))
        except Exception:
            continue
        camp = row[1] if len(row) > 1 else ""
        has_mo = "MO" in camp
        if profile == "malu" and not has_mo: continue
        if profile == "hire" and has_mo:     continue
        invest_total += cost
        if row_date:
            dk = row_date.strftime("%Y-%m-%d")
            invest_por_dia[dk] = invest_por_dia.get(dk, 0.0) + cost
        ct = _wici2_campaign_type(camp)
        if ct == "quente":
            invest_q += cost
        elif ct == "frio":
            invest_f += cost
        elif ct == "corredor":
            invest_cor += cost

    invest_workshop = invest_total - invest_cor

    # ── Vendas ────────────────────────────────────────────────────────────────
    vendas_rows_raw = sheets.get(spreadsheetId=WICI2_SHEET_ID, range=WICI2_ABA_VENDAS).execute().get("values", [])[1:]

    emails_uniq: set = set()
    fat_by_cat: dict = {cat: 0.0 for cat, _ in _WICI2_PRODUCTS}
    fat_traf = fat_org = fat_q = fat_f = 0.0
    ws_traf = ws_org = ws_q = ws_f = 0

    vendas_por_dia: dict = {}      # date_str → {"total": int, "org": int}
    vendas_por_origem: dict = {}   # src → count (vendas orgânicas)
    vendas_por_posic: dict = {}    # placement → count (vendas Meta Ads)
    all_ws_dates: list = []

    for row in vendas_rows_raw:
        if len(row) < 5:
            continue
        row_date  = _wici2_parse_date(row[0]) if row[0] else None
        if date_start and row_date and row_date < date_start:
            continue
        if date_end and row_date and row_date > date_end:
            continue

        product     = row[4].strip() if len(row) > 4 else ""
        src         = row[5].strip() if len(row) > 5 else ""
        camp_utm    = row[6].strip() if len(row) > 6 else ""
        utm_medium  = row[7].strip() if len(row) > 7 else ""
        # utm_term (col J, index 9): placement para paid (Instagram_Stories…) ou slug de campanha para org
        placement   = row[9].strip() if len(row) > 9 else ""
        email       = row[2].lower().strip() if len(row) > 2 else ""
        valor       = _wici2_parse_brl(row[3]) if len(row) > 3 else 0.0
        cat         = _wici2_product_category(product)
        sale_has_mo = "MO" in camp_utm
        if profile == "malu" and not sale_has_mo: continue
        if profile == "hire" and sale_has_mo:     continue

        ct = _wici2_campaign_type(camp_utm)
        # tráfego pago = ig ou facebook; todo o resto é orgânico
        is_ig_fb = src.lower() in ("ig", "facebook", "metaads", "meta")

        # Hire: só campanhas de tráfego pago (exclui orgânico)
        if profile == "hire" and ct == "" and not is_ig_fb:
            continue

        if email:
            emails_uniq.add(email)
        if cat in fat_by_cat:
            fat_by_cat[cat] += valor

        if ct == "quente":
            fat_q += valor
        elif ct == "frio":
            fat_f += valor
        elif is_ig_fb:
            fat_traf += valor
        else:
            fat_org += valor

        if cat == "workshop":
            if row_date:
                all_ws_dates.append(row_date)
                dk = row_date.strftime("%Y-%m-%d")
                if dk not in vendas_por_dia:
                    vendas_por_dia[dk] = {"total": 0, "org": 0, "traf": 0}
                vendas_por_dia[dk]["total"] += 1

            # posicionamento: utm_term para TODAS as vendas workshop
            if placement:
                vendas_por_posic[placement] = vendas_por_posic.get(placement, 0) + 1

            if ct == "quente":
                ws_q += 1
                if row_date:
                    vendas_por_dia[row_date.strftime("%Y-%m-%d")]["traf"] += 1
            elif ct == "frio":
                ws_f += 1
                if row_date:
                    vendas_por_dia[row_date.strftime("%Y-%m-%d")]["traf"] += 1
            elif is_ig_fb:
                ws_traf += 1
                if row_date:
                    vendas_por_dia[row_date.strftime("%Y-%m-%d")]["traf"] += 1
            else:
                ws_org += 1
                # origem orgânica: usa utm_medium (manychat, youtube, whatsapp, email…)
                medium_key = utm_medium if utm_medium else src
                if medium_key:
                    vendas_por_origem[medium_key] = vendas_por_origem.get(medium_key, 0) + 1
                if row_date:
                    vendas_por_dia[row_date.strftime("%Y-%m-%d")]["org"] += 1

    # ── Totais derivados ──────────────────────────────────────────────────────
    vendas_trafego = ws_q + ws_f + ws_traf
    vendas_org     = ws_org
    fat_trafego    = fat_q + fat_f + fat_traf
    fat_geral      = sum(fat_by_cat.values())
    valor_bruto    = sum(_wici2_parse_brl(r[3]) for r in vendas_rows_raw if len(r) > 3)
    compradores    = len(emails_uniq)
    ws_total       = ws_q + ws_f + ws_traf + ws_org
    fat_ws         = fat_by_cat["workshop"]
    ticket         = round(fat_ws / ws_total, 2) if ws_total else None

    # ── Médias diárias ────────────────────────────────────────────────────────
    today    = datetime.now().date()
    ref_date = date_end if date_end else today
    if date_start:
        base_date = date_start
    else:
        base_date = data_inicio_geral if data_inicio_geral else (min(all_ws_dates) if all_ws_dates else ref_date)
    dias_vendendo = max(1, (ref_date - base_date).days + 1)

    media_geral   = round(ws_total       / dias_vendendo, 1)
    media_org     = round(ws_org         / dias_vendendo, 1)
    media_trafego = round(vendas_trafego / dias_vendendo, 1)

    # ── Dados para gráficos ───────────────────────────────────────────────────
    sorted_dias  = sorted(vendas_por_dia.items())
    chart_labels = [d for d, _ in sorted_dias]
    chart_total  = [v["total"] for _, v in sorted_dias]
    chart_org    = [v["org"]   for _, v in sorted_dias]
    chart_traf   = [v.get("traf", 0) for _, v in sorted_dias]
    chart_cpa    = [
        round(invest_por_dia[dk] / v["traf"], 2) if v.get("traf") and dk in invest_por_dia else None
        for dk, v in sorted_dias
    ]

    origem_sorted = sorted(vendas_por_origem.items(), key=lambda x: -x[1])[:10]
    posic_sorted  = sorted(vendas_por_posic.items(),  key=lambda x: -x[1])[:10]

    def safe_div(a, b):
        return round(a / b, 2) if b else None

    def taxa_pct(a, b):
        return round(a / b * 100, 2) if b else None

    return {
        # investimentos
        "invest_total":    round(invest_total, 2),
        "invest_workshop": round(invest_workshop, 2),
        "invest_quente":   round(invest_q, 2),
        "invest_frio":     round(invest_f, 2),
        "invest_corredor": round(invest_cor, 2),
        # faturamentos
        "fat_geral":    round(fat_geral, 2),
        "fat_lembrete": round(fat_by_cat["lembrete"], 2),
        "fat_mentoria": round(fat_by_cat["mentoria"], 2),
        "fat_corredor": round(fat_by_cat["corredor"], 2),
        "fat_trafego":  round(fat_trafego, 2),
        "fat_org":      round(fat_org, 2),
        "fat_quente":   round(fat_q, 2),
        "fat_frio":     round(fat_f, 2),
        "valor_bruto":  round(valor_bruto, 2),
        # contagens
        "compradores":    compradores,
        "ws_total":       ws_total,
        "grupo":          WICI2_GRUPO or None,
        "vendas_trafego": vendas_trafego,
        "vendas_org":     vendas_org,
        "vendas_quente":  ws_q,
        "vendas_frio":    ws_f,
        "ticket_medio":   ticket,
        # derivados
        "cpa":         safe_div(invest_total, vendas_trafego),
        "cpa_quente":  safe_div(invest_q,     ws_q),
        "cpa_frio":    safe_div(invest_f,     ws_f),
        "roas_geral":  safe_div(fat_geral,    invest_total),
        "roas_quente": safe_div(fat_q,        invest_q),
        "roas_frio":   safe_div(fat_f,        invest_f),
        # taxas (% do previsto de investimento)
        "taxa_invest_total":    taxa_pct(invest_total,          WICI2_PREVISTO["total"]),
        "taxa_invest_workshop": taxa_pct(invest_workshop, WICI2_PREVISTO["workshop"]),
        "invest_lembrete":      0.0,
        "taxa_invest_lembrete": 0.0,
        "invest_mentoria":      0.0,
        "taxa_invest_mentoria": 0.0,
        "taxa_invest_corredor": taxa_pct(invest_cor,   WICI2_PREVISTO["corredor"]),
        # metas totais
        "meta_total":    meta_total,
        "meta_trafego":  meta_trafego,
        "meta_organico": meta_organico,
        # gauges (% da meta total atingida)
        "gauge_geral_pct":   round(ws_total       / meta_total    * 100, 1) if meta_total    else 0,
        "gauge_org_pct":     round(ws_org         / meta_organico * 100, 1) if meta_organico else 0,
        "gauge_trafego_pct": round(vendas_trafego / meta_trafego  * 100, 1) if meta_trafego  else 0,
        # médias e metas diárias
        "media_geral":         media_geral,
        "media_org":           media_org,
        "media_trafego":       media_trafego,
        "meta_diaria_geral":   meta_diaria_geral,
        "meta_diaria_org":     meta_diaria_org,
        "meta_diaria_trafego": meta_diaria_trafego,
        "dias_vendendo":       dias_vendendo,
        # dados para gráficos
        "chart_dias": {"labels": chart_labels, "total": chart_total, "org": chart_org, "traf": chart_traf, "cpa": chart_cpa},
        "chart_origem":  [{"label": k, "count": v} for k, v in origem_sorted],
        "chart_posicao": [{"label": k, "count": v} for k, v in posic_sorted],
        "temperatura":   {"frio": ws_f, "quente": ws_q + ws_traf, "org": ws_org},
    }

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
    "DFT":   {"name": "DFT Logística",         "token": lambda: os.getenv("META_TOKEN_DFT"),   "accounts": []},
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
    "DFT":   "1391ah-50411",
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

# ── CPA Monitor ──────────────────────────────────────────────────────────────
META_TOKEN_MMF   = os.getenv("META_TOKEN_MMF", "")
META_ACCOUNT_MMF = os.getenv("META_ACCOUNT_ID_MMF", "act_729365124577217")
CPA_LIMIT        = 75.0
_cpa_alerts: list = []  # armazena ações do dia

def _meta_api(path: str, method: str = "GET", data: dict = None) -> dict:
    import urllib.request, urllib.parse
    url = f"https://graph.facebook.com/v19.0/{path}"
    if method == "GET":
        url += ("&" if "?" in url else "?") + f"access_token={META_TOKEN_MMF}"
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read())
    else:
        params = (data or {})
        params["access_token"] = META_TOKEN_MMF
        payload = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

def _run_cpa_monitor():
    global _cpa_alerts
    if not META_TOKEN_MMF:
        return
    today = datetime.now().date()
    d_start = today - timedelta(days=1)  # últimas 48h = ontem + hoje
    try:
        trafego = _wici2_fetch_trafego(date_start=d_start, date_end=today, profile="")
    except Exception as e:
        print(f"[cpa] Erro ao buscar tráfego: {e}")
        return

    # Regra: campanha MMF que gastou >= R$75 E não teve nenhuma venda nas últimas 48h
    GASTO_MINIMO = 75.0
    problemas = [c for c in trafego.get("campanhas", [])
                 if c["nome"].startswith("MMF")
                 and c.get("invest", 0) >= GASTO_MINIMO
                 and c.get("vendas", 0) == 0]
    if not problemas:
        print(f"[cpa] {today}: nenhuma campanha MMF com gasto >= R${GASTO_MINIMO} sem vendas nas 48h")
        return

    # Busca IDs das campanhas no Meta
    try:
        resp = _meta_api(f"{META_ACCOUNT_MMF}/campaigns?fields=id,name,status,daily_budget&limit=200")
        meta_map = {c["name"]: c for c in resp.get("data", [])}
    except Exception as e:
        print(f"[cpa] Erro ao buscar campanhas Meta: {e}")
        return

    acoes = []
    for camp in problemas:
        nome = camp["nome"]
        meta = meta_map.get(nome)
        if not meta or meta["status"] != "ACTIVE":
            continue
        camp_id = meta["id"]
        budget  = meta.get("daily_budget", "5000")
        try:
            # Desativa
            _meta_api(camp_id, "POST", {"status": "PAUSED"})
            # Cria cópia pausada
            nova = _meta_api(f"{META_ACCOUNT_MMF}/campaigns", "POST", {
                "name": nome + "_CPA_COPY",
                "objective": "OUTCOME_SALES",
                "status": "PAUSED",
                "special_ad_categories": "[]",
                "daily_budget": budget,
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            })
            nova_id = nova.get("id", "")
            # Copia adsets e ads
            adsets = _meta_api(f"{camp_id}/adsets?fields=id&limit=20").get("data", [])
            for ads in adsets:
                _meta_api(f"{ads['id']}/copies", "POST", {
                    "campaign_id": nova_id, "status_option": "PAUSED"
                })
            acoes.append({
                "data": today, "campanha": nome,
                "investido": round(camp.get("invest", 0), 2),
                "acao": "Desativada + cópia criada pausada",
                "nova_id": nova_id,
            })
            print(f"[cpa] {nome} | R${camp.get('invest',0):.2f} gasto, 0 vendas → desativada, cópia {nova_id}")
        except Exception as e:
            print(f"[cpa] Erro em {nome}: {e}")

    if acoes:
        _cpa_alerts = acoes + [a for a in _cpa_alerts if a["data"] != today]

def _schedule_cpa_monitor():
    """Roda o monitor de CPA todo dia às 22h."""
    def loop():
        now = datetime.now()
        target = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        t = threading.Timer(wait, lambda: (_run_cpa_monitor(), loop()))
        t.daemon = True
        t.start()
    threading.Thread(target=loop, daemon=True).start()

@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_users_db()
    gestao_init_db()
    financeiro_init_db()
    fp_init_db()
    init_crm_db()
    init_briefings_db()
    habitos_init_db()
    clientes_init_db()
    whatsapp_init_db()
    start_sync_scheduler()
    _schedule_cpa_monitor()
    await migrar_siga_startup()
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://lp.dftlogistica.com.br", "https://cardo-jpg.github.io"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.include_router(gestao_router)
app.include_router(financeiro_router)
app.include_router(fp_router)
app.include_router(crm_router)
app.include_router(briefings_router)
app.include_router(atas_sinapse_router)
app.include_router(habitos_router)
app.include_router(inicio_router)
app.include_router(clientes_router)
app.include_router(whatsapp_router)
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

def _strip_md_simple(text: str, max_chars: int = 4000) -> str:
    """Trunca texto longo preservando início e fim."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + f"\n\n[…conteúdo truncado, {len(text) - max_chars} chars omitidos…]\n\n" + text[-half:]


def _normalize(s: str) -> str:
    """Lowercase + remove acentos pra comparar nomes."""
    import unicodedata
    if not s: return ""
    nfkd = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _resolve_client_name(client_id: str) -> str:
    """Busca o nome canônico do cliente no Painel de Clientes do Sinapse."""
    if not client_id: return ""
    try:
        conn = get_conn(); cur = dict_cursor(conn)
        try:
            cur.execute(
                "SELECT id FROM lists WHERE LOWER(name) IN ('painel de clientes','painel de cliente') LIMIT 1"
            )
            row = cur.fetchone()
            if not row: return ""
            cur.execute(
                "SELECT title FROM tasks WHERE list_id=%s AND title ILIKE %s LIMIT 1",
                (row["id"], f"[{client_id.upper()}]%"),
            )
            t = cur.fetchone()
            if not t: return ""
            # Extrai "Conexão Cirúrgica" de "[CC] Conexão Cirúrgica"
            m = re.match(r"^\s*\[[A-Za-z0-9]+\]\s*(.+?)\s*$", t["title"])
            return (m.group(1).strip() if m else "")
        finally:
            cur.close(); conn.close()
    except Exception:
        return ""


def _sinapse_client_context(client_id: str) -> list[str]:
    """
    Coleta contexto vivo do cliente direto do banco do Sinapse:
      - Ficha = task [SIGLA] no Painel de Clientes (descrição + cf_values rotulados)
      - Documents do Sinapse cujo title contém [SIGLA] OU que estão em pasta cujo nome contém a sigla/nome
    """
    if not client_id: return []
    sigla = client_id.upper()
    out = []
    try:
        conn = get_conn(); cur = dict_cursor(conn)
        try:
            # 1. Painel de Clientes — pega a task do cliente
            cur.execute(
                "SELECT id, custom_fields FROM lists WHERE LOWER(name) IN ('painel de clientes','painel de cliente') LIMIT 1"
            )
            painel = cur.fetchone()
            if painel:
                cur.execute(
                    "SELECT title, description, status, cf_values FROM tasks "
                    "WHERE list_id=%s AND title ILIKE %s LIMIT 1",
                    (painel["id"], f"[{sigla}]%"),
                )
                task = cur.fetchone()
                if task:
                    cf_defs_raw = painel.get("custom_fields") or "[]"
                    try:
                        cf_defs = json.loads(cf_defs_raw) if isinstance(cf_defs_raw, str) else cf_defs_raw
                    except Exception:
                        cf_defs = []
                    cf_label = {cf.get("id"): cf.get("name", cf.get("id", "")) for cf in (cf_defs or []) if isinstance(cf, dict)}
                    try:
                        cfv = json.loads(task.get("cf_values") or "{}")
                    except Exception:
                        cfv = {}
                    lines = [f"# Ficha do cliente — {task['title']}",
                             f"_Status:_ {task.get('status') or '—'}"]
                    if cfv:
                        lines.append("\n## Dados (custom fields)")
                        for cid, val in cfv.items():
                            if val in (None, "", []): continue
                            lines.append(f"- **{cf_label.get(cid, cid)}**: {val}")
                    if (task.get("description") or "").strip():
                        lines.append(f"\n## Descrição\n{task['description'].strip()}")
                    out.append(f"=== Sinapse · Painel de Clientes · {task['title']} ===\n" + "\n".join(lines))

            # 2. Documents do Sinapse:
            #    - title contém [SIGLA] ou o nome do cliente (matches diretos)
            #    - pasta tem [SIGLA] ou o nome no nome
            #    - OU descendente recursivo de qualquer match acima (ex: atas
            #      publicadas pelo wizard ficam em Documentação > [SIGLA] Cliente
            #      > Atas de Reunião > DD/MM - Título — o título da ata não
            #      bate, mas o avô bate)
            cliente_nome = _resolve_client_name(sigla)
            patterns = [f"%[{sigla}]%"]
            if cliente_nome:
                patterns.append(f"%{cliente_nome}%")

            placeholders_t = " OR ".join(["title ILIKE %s"] * len(patterns))
            placeholders_f = " OR ".join(["f.name ILIKE %s"] * len(patterns))
            sql = f"""
                WITH RECURSIVE doc_tree AS (
                    SELECT id FROM documents WHERE {placeholders_t}
                  UNION
                    SELECT d.id FROM documents d
                      LEFT JOIN folders f ON f.id = d.folder_id
                     WHERE {placeholders_f}
                  UNION
                    SELECT d.id FROM documents d
                      INNER JOIN doc_tree dt ON d.parent_id = dt.id
                )
                SELECT DISTINCT d.title, d.content, d.updated_at,
                       COALESCE(s.name, '') AS space_name,
                       COALESCE(f.name, '') AS folder_name,
                       COALESCE(p.title, '') AS parent_title
                FROM documents d
                LEFT JOIN spaces  s ON s.id = d.space_id
                LEFT JOIN folders f ON f.id = d.folder_id
                LEFT JOIN documents p ON p.id = d.parent_id
                WHERE d.id IN (SELECT id FROM doc_tree)
                ORDER BY d.updated_at DESC
                LIMIT 30
            """
            params = patterns + patterns
            cur.execute(sql, params)
            rows = cur.fetchall() or []
            for r in rows:
                title = r.get("title") or "Sem título"
                content = r.get("content") or ""
                # Strip HTML grosseiramente — TipTap salva como HTML; mantém texto legível
                txt = re.sub(r"<[^>]+>", " ", content)
                txt = re.sub(r"\s+", " ", txt).strip()
                trail_parts = [p for p in [r.get("space_name"), r.get("folder_name"), r.get("parent_title")] if p]
                breadcrumb = " · ".join(trail_parts)
                header = f"=== Sinapse · {breadcrumb}/{title} ===" if breadcrumb else f"=== Sinapse · {title} ==="
                out.append(header + "\n" + _strip_md_simple(txt, 4000))
        finally:
            cur.close(); conn.close()
    except Exception as e:
        # Best-effort — se Sinapse não estiver disponível, segue só com arquivos
        out.append(f"=== [aviso] não conseguiu ler Sinapse: {e} ===")
    return out


def _client_subdir_for(client_id: str) -> Optional[Path]:
    """
    Acha a subpasta documents/Cliente - X/ que corresponde à sigla.
    Estratégia: usa o nome canônico do Painel de Clientes pra fazer match
    fuzzy com os nomes das pastas (acento-insensitive).
    """
    if not client_id: return None
    sigla = client_id.upper()
    # 1. Tenta pelo nome canônico do Painel de Clientes
    nome = _resolve_client_name(sigla)
    if nome:
        nome_norm = _normalize(nome)
        for sub in DOCS_DIR.iterdir():
            if sub.is_dir() and nome_norm in _normalize(sub.name):
                return sub
    # 2. Fallback: a sigla aparece literal no nome da pasta (caso "- HIRE")
    needle = f"- {sigla}".lower()
    for sub in DOCS_DIR.iterdir():
        if sub.is_dir() and needle in sub.name.lower():
            return sub
    return None


def load_documents(client_id: str = None):
    docs = []
    # 1. Documentos globais do disco (cardôpedia, docs gerais)
    for filepath in sorted(DOCS_DIR.glob("*.md")):
        content = read_file_truncated(filepath)
        docs.append(f"=== {filepath.name} ===\n{content}")

    if client_id:
        # 2. CONTEXTO VIVO DO SINAPSE: ficha (Painel de Clientes) + documents
        docs.extend(_sinapse_client_context(client_id))
        # 3. Arquivos .md da pasta do cliente (se existir)
        sub = _client_subdir_for(client_id)
        if sub:
            for filepath in sorted(sub.glob("*.md")):
                content = read_file_truncated(filepath)
                docs.append(f"=== {sub.name}/{filepath.name} ===\n{content}")
    else:
        # Sem cliente: só fichas de cada subpasta
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

# ── User DB ───────────────────────────────────────────────────────────────────
_ADMIN_USERS        = {"victor"}           # fallback apenas
_FIN_PESSOAIS_USERS = {"victor", "jadna"}  # fallback apenas


def _hash_pwd(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return salt.hex() + ":" + key.hex()


def _check_pwd(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


def _db_get_user(username: str) -> Optional[dict]:
    try:
        conn = get_conn()
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _db_all_users() -> list:
    try:
        conn = get_conn()
        cur = dict_cursor(conn)
        cur.execute("SELECT username, display_name, created_at FROM users ORDER BY created_at")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception:
        return []


def _user_is_admin(username: str) -> bool:
    try:
        conn = get_conn()
        cur = dict_cursor(conn)
        cur.execute("SELECT is_admin FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return bool(row and row["is_admin"])
    except Exception:
        return username in _ADMIN_USERS


def _user_has_fin_pessoais(username: str) -> bool:
    try:
        conn = get_conn()
        cur = dict_cursor(conn)
        cur.execute("SELECT fin_pessoais FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return bool(row and row["fin_pessoais"])
    except Exception:
        return username in _FIN_PESSOAIS_USERS


def _init_users_db():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username      TEXT PRIMARY KEY,
                display_name  TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrations: add permission columns
        for col_def in [
            "ALTER TABLE users ADD COLUMN is_admin    INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN fin_pessoais INTEGER DEFAULT 0",
        ]:
            cur.execute(f"""
                DO $$
                BEGIN
                    {col_def};
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$;
            """)
        conn.commit()

        dc = dict_cursor(conn)
        # Migrate hardcoded users
        for uname, pwd in USERS.items():
            dc.execute("SELECT 1 FROM users WHERE username=%s", (uname,))
            if not dc.fetchone():
                display = USER_DISPLAY.get(uname, uname.capitalize())
                dc.execute(
                    "INSERT INTO users (username, display_name, password_hash) VALUES (%s,%s,%s)",
                    (uname, display, _hash_pwd(pwd)),
                )
        # Ensure jadna exists
        dc.execute("SELECT 1 FROM users WHERE username='jadna'")
        if not dc.fetchone():
            dc.execute(
                "INSERT INTO users (username, display_name, password_hash) VALUES (%s,%s,%s)",
                ("jadna", "Jadna", _hash_pwd("Sinapse@2026")),
            )
        # Seed flags
        for uname in _ADMIN_USERS:
            dc.execute("UPDATE users SET is_admin=1 WHERE username=%s", (uname,))
        for uname in _FIN_PESSOAIS_USERS:
            dc.execute("UPDATE users SET fin_pessoais=1 WHERE username=%s", (uname,))
        dc.close()
        conn.commit()
    finally:
        cur.close()
        conn.close()


def make_session_token(username: str) -> str:
    sig = hmac.new(SECRET_KEY.encode(), username.encode(), hashlib.sha256).hexdigest()
    return f"{username}:{sig}"

def verify_session(request: Request) -> Optional[str]:
    cookie = request.cookies.get("session", "")
    if ":" not in cookie:
        return None
    username, sig = cookie.split(":", 1)
    expected = hmac.new(SECRET_KEY.encode(), username.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return username if _db_get_user(username) else None

# ── Rotas ─────────────────────────────────────────────────────────────────────

def _nav_base(request: Request, active_page: str = "") -> dict:
    username = verify_session(request) or ""
    user = _db_get_user(username) if username else None
    display = user["display_name"] if user else ""
    return {
        "nav_username": display,
        "nav_user": username,
        "nav_is_admin": _user_is_admin(username) if username else False,
        "nav_fin_pessoais": _user_has_fin_pessoais(username) if username else False,
        "active_page": active_page,
        "nav_clients": None,
        "nav_current_client": None,
    }

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return await gestao_page(request)

@app.get("/conversar", response_class=HTMLResponse)
async def conversar(request: Request):
    if not verify_session(request):
        return RedirectResponse("/login")
    resp = templates.TemplateResponse("chat.html", {"request": request, **_nav_base(request, "conversar")})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

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
    user = _db_get_user(username)
    if user and _check_pwd(password, user["password_hash"]):
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



@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    username = verify_session(request)
    if not username:
        return RedirectResponse("/login")
    if username not in _ADMIN_USERS:
        raise HTTPException(status_code=403, detail="Acesso restrito")
    return templates.TemplateResponse("config.html", {"request": request, **_nav_base(request, "config")})

# ── Admin: User Management API ────────────────────────────────────────────────
def _require_admin(request: Request) -> str:
    username = verify_session(request)
    if not username:
        raise HTTPException(status_code=401, detail="Não autenticado")
    if not _user_is_admin(username):
        raise HTTPException(status_code=403, detail="Acesso restrito")
    return username

def _db_all_users_full():
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT username, display_name, created_at, is_admin, fin_pessoais FROM users ORDER BY created_at"
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return rows

@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    _require_admin(request)
    return _db_all_users_full()

@app.post("/api/admin/users")
async def admin_create_user(request: Request):
    _require_admin(request)
    body = await request.json()
    username     = (body.get("username") or "").strip().lower()
    display      = (body.get("display_name") or "").strip()
    password     = body.get("password") or ""
    is_admin     = 1 if body.get("is_admin") else 0
    fin_pessoais = 1 if body.get("fin_pessoais") else 0
    if not username or not display or not password:
        raise HTTPException(400, "username, display_name e password são obrigatórios")
    if not username.replace("_", "").replace(".", "").isalnum():
        raise HTTPException(400, "Username deve conter apenas letras, números, _ ou .")
    if len(password) < 6:
        raise HTTPException(400, "Senha deve ter ao menos 6 caracteres")
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
        if cur.fetchone():
            raise HTTPException(400, "Usuário já existe")
        cur.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin, fin_pessoais) VALUES (%s,%s,%s,%s,%s)",
            (username, display, _hash_pwd(password), is_admin, fin_pessoais),
        )
        conn.commit()
        return {"ok": True}
    finally:
        cur.close()
        conn.close()

@app.patch("/api/admin/users/{username}/flags")
async def admin_update_flags(username: str, request: Request):
    caller = _require_admin(request)
    body = await request.json()
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
        if not cur.fetchone():
            raise HTTPException(404, "Usuário não encontrado")
        if "is_admin" in body:
            if username == caller and not body["is_admin"]:
                raise HTTPException(400, "Não pode remover seu próprio acesso de admin")
            cur.execute("UPDATE users SET is_admin=%s WHERE username=%s", (1 if body["is_admin"] else 0, username))
        if "fin_pessoais" in body:
            cur.execute("UPDATE users SET fin_pessoais=%s WHERE username=%s", (1 if body["fin_pessoais"] else 0, username))
        conn.commit()
        return {"ok": True}
    finally:
        cur.close()
        conn.close()

@app.put("/api/admin/users/{username}/password")
async def admin_reset_password(username: str, request: Request):
    _require_admin(request)
    body = await request.json()
    password = body.get("password") or ""
    if len(password) < 6:
        raise HTTPException(400, "Senha deve ter ao menos 6 caracteres")
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
        if not cur.fetchone():
            raise HTTPException(404, "Usuário não encontrado")
        cur.execute("UPDATE users SET password_hash=%s WHERE username=%s", (_hash_pwd(password), username))
        conn.commit()
        return {"ok": True}
    finally:
        cur.close()
        conn.close()

@app.delete("/api/admin/users/{username}")
async def admin_delete_user(username: str, request: Request):
    caller = _require_admin(request)
    if username == caller:
        raise HTTPException(400, "Não é possível excluir seu próprio usuário")
    if _user_is_admin(username):
        raise HTTPException(400, "Não é possível excluir um administrador")
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM users WHERE username=%s", (username,))
        conn.commit()
        return {"ok": True}
    finally:
        cur.close()
        conn.close()

@app.get("/api/conversations")
async def get_conversations(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    return JSONResponse(list_conversations())


@app.get("/api/cerebro/clients")
async def cerebro_clients(request: Request):
    """
    Retorna os clientes ATIVOS do Painel de Clientes do Sinapse.
    Usado pra popular a tela inicial do Cérebro dinamicamente.
    Best-effort: se algo falha, devolve {"clients": [], "error": "..."}
    em vez de 500 — assim a UI mostra mensagem útil em vez de travar.
    """
    if not verify_session(request):
        raise HTTPException(status_code=401)
    rows = []
    err  = None
    list_id = None
    try:
        conn = get_conn(); cur = dict_cursor(conn)
        try:
            # Variantes de nome aceitas (alguns clientes nomeiam diferente)
            for name in ("Painel de Clientes", "Painel de Cliente", "Painel de clientes"):
                cur.execute("SELECT id, name FROM lists WHERE LOWER(name) = LOWER(%s) LIMIT 1", (name,))
                row = cur.fetchone()
                if row:
                    list_id = row["id"]
                    break
            if list_id:
                cur.execute(
                    "SELECT title, status FROM tasks WHERE list_id=%s ORDER BY title",
                    (list_id,),
                )
                rows = cur.fetchall() or []
        finally:
            cur.close(); conn.close()
    except Exception as e:
        err = str(e)

    clients = []
    for r in rows:
        # Filtra por status='ativo' (case insensitive)
        status = str(r.get("status") or "").strip().lower()
        if status != "ativo":
            continue
        title = (r.get("title") or "").strip()
        m = re.match(r"^\s*\[([A-Za-z0-9]+)\]\s*(.+?)\s*$", title)
        if not m:
            words = [w for w in re.split(r"\s+", title) if w]
            sigla = "".join(w[0].upper() for w in words[:3]) if words else "?"
            clients.append({"sigla": sigla, "nome": title})
            continue
        clients.append({"sigla": m.group(1).upper(), "nome": m.group(2).strip()})

    return JSONResponse({
        "clients":  clients,
        "list_id":  list_id,
        "error":    err,
        "raw_count": len(rows),
    })

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

def _load_granola_cache_docs() -> list:
    """Lê documentos do cache local do app Granola (só disponível em modo local)."""
    try:
        if not GRANOLA_CACHE_PATH.exists():
            return []
        data = json.loads(GRANOLA_CACHE_PATH.read_text())
        docs_dict = data.get("cache", {}).get("state", {}).get("documents", {})
        if not isinstance(docs_dict, dict):
            return []
        return [v for v in docs_dict.values() if v and isinstance(v, dict)]
    except Exception as e:
        print(f"[granola] Erro ao ler cache local: {e}")
        return []


@app.get("/api/granola/meetings")
async def get_granola_meetings(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    api_docs = await granola_post("get-documents")
    if not isinstance(api_docs, list):
        api_docs = []

    # Complementa com cache local (contém reuniões mais antigas não retornadas pela API)
    cache_docs = _load_granola_cache_docs()

    # Merge deduplificado por ID
    seen_ids = {d.get("id") for d in api_docs if d.get("id")}
    all_docs = list(api_docs)
    for doc in cache_docs:
        if doc.get("id") not in seen_ids:
            all_docs.append(doc)
            seen_ids.add(doc.get("id"))

    if not all_docs:
        return JSONResponse({"error": "Não foi possível conectar ao Granola."}, status_code=503)

    meetings = []
    for doc in all_docs:
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
    return JSONResponse(meetings)


@app.post("/api/ata/generate")
async def generate_ata(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    meeting_id = body.get("meeting_id")

    api_docs = await granola_post("get-documents")
    cache_docs = _load_granola_cache_docs()
    all_docs = list(api_docs) if isinstance(api_docs, list) else []
    seen_ids = {d.get("id") for d in all_docs if d.get("id")}
    for d in cache_docs:
        if d.get("id") not in seen_ids:
            all_docs.append(d)

    doc = next((d for d in all_docs if d.get("id") == meeting_id), None)
    if not doc:
        raise HTTPException(status_code=404, detail="Reunião não encontrada.")

    doc_title = doc.get("title") or ""
    date = extract_date_from_title(doc_title) or (doc.get("created_at") or "")[:10]

    # 1. Notas formatadas do Granola (já é um resumo estruturado)
    ata_text = sanitize_ata_content(doc.get("notes_markdown") or doc.get("notes_plain") or "")
    source = "notes" if ata_text else None

    # 2. Painéis estruturados (resumo das seções)
    if not ata_text:
        ata_text = await granola_get_panels_markdown(meeting_id)
        if ata_text:
            source = "panels"

    # 3. Fallback: só tem transcript bruto — passa pelo Claude pra estruturar
    if not ata_text:
        transcript = await granola_get_transcript(meeting_id)
        if not transcript:
            raise HTTPException(status_code=422, detail="Esta reunião não tem notas nem transcript disponível.")
        ata_text = _resumir_transcript_para_ata(transcript, doc_title)
        source = "transcript+claude"

    return JSONResponse({"ata": ata_text, "title": doc_title, "date": date, "source": source})


def _resumir_transcript_para_ata(transcript: str, meeting_title: str = "") -> str:
    """
    Quando o Granola não tem notes nem panels, transforma a transcrição
    bruta numa ata estruturada em markdown via Claude.
    """
    if not transcript or not transcript.strip():
        return ""

    # Limita transcript pra ficar dentro do budget de tokens
    max_chars = 80_000
    transcript = transcript[:max_chars]

    title_line = f"Título da reunião: {meeting_title}\n\n" if meeting_title else ""
    prompt = f"""{title_line}A reunião abaixo está em forma de transcrição bruta (linhas "Microfone: ..."). Transforme em uma ata de reunião estruturada em markdown, no formato exato:

## Resumo executivo
3-5 bullets curtos com os pontos centrais da conversa.

## Decisões tomadas
- Decisão 1
- Decisão 2
(omita esta seção se nenhuma decisão clara foi tomada)

## Próximos passos
- Ação responsável: prazo (se mencionado)
(omita esta seção se nenhuma ação ficou definida)

## Pontos de atenção
- Riscos, dependências, dúvidas em aberto
(omita esta seção se não houver)

## Detalhes da conversa
Resumo narrativo dividido por tópico, em parágrafos curtos. Use H3 (###) pra cada tópico discutido.

Diretrizes:
- Português brasileiro
- Tom profissional e direto
- NÃO copie a transcrição literal — sintetize
- NÃO invente nada que não está na transcrição
- Use nomes próprios quando aparecerem
- Valores monetários, datas, prazos: preserve com fidelidade

Transcrição:
\"\"\"
{transcript}
\"\"\""""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system="Você é um redator de atas de reunião. Produza atas estruturadas, objetivas e fiéis ao que foi discutido. Responda apenas com a ata em markdown, sem comentários antes ou depois.",
            messages=[{"role": "user", "content": prompt}],
        )
        out = next((b.text for b in response.content if hasattr(b, "text")), "").strip()
        # Limpa code fences acidentais
        if out.startswith("```"):
            out = re.sub(r"^```[a-z]*\n?", "", out)
            out = re.sub(r"\n?```$", "", out).strip()
        return out or transcript  # se o Claude falhar em retornar texto, devolve o transcript bruto como último recurso
    except Exception as e:
        # Não deixa o gerador quebrar — devolve transcript bruto com aviso
        return f"> ⚠️ Falha ao resumir transcript via IA ({e.__class__.__name__}). Texto bruto abaixo.\n\n{transcript}"


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
        status = CLICKUP_AREA_STATUS.get(area, "to do")

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
            "status": status,
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
        # Assignee: usa o selecionado pelo usuário, senão fallback por área
        assignee_name = (task.get("assignee") or "").lower().strip()
        assignee_id = CLICKUP_MEMBERS.get(assignee_name)
        if not assignee_id:
            assignee_id = JOSE_ID if area == "trafego" else VICTOR_ID
        if assignee_id:
            payload["assignees"] = [assignee_id]

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
        "nav_user": username or "",
        "nav_is_admin": (username or "") in _ADMIN_USERS,
        "nav_fin_pessoais": (username or "") in _FIN_PESSOAIS_USERS,
        "active_page": "trafego",
        "nav_clients": nav_clients,
        "nav_current_client": selected_client,
        "gads_clients": json.dumps(gads_configured),
    })

@app.get("/dashboards/clientes", response_class=HTMLResponse)
async def dashboards_clientes_page(request: Request):
    """Shell com seletor de cliente + iframe pros dashboards individuais."""
    username = verify_session(request)
    if not username:
        return RedirectResponse("/login")
    resp = templates.TemplateResponse("dashboards_clientes.html", {"request": request})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/dashboard/nf", response_class=HTMLResponse)
async def dashboard_nf_page(request: Request):
    """Dashboard de performance — Grupo NF (Gráfica NF). Standalone HTML embedado."""
    username = verify_session(request)
    if not username:
        return RedirectResponse("/login")
    resp = templates.TemplateResponse("dashboard_nf.html", {"request": request})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/dashboard/dft", response_class=HTMLResponse)
async def dashboard_dft_page(request: Request):
    """Dashboard de performance — DFT Logística. Standalone HTML embedado."""
    username = verify_session(request)
    if not username:
        return RedirectResponse("/login")
    resp = templates.TemplateResponse("dashboard_dft.html", {"request": request})
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
        "nav_user": username or "",
        "nav_is_admin": (username or "") in _ADMIN_USERS,
        "nav_fin_pessoais": (username or "") in _FIN_PESSOAIS_USERS,
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

def _wici2_fetch_trafego(date_start=None, date_end=None, profile: str = "") -> dict:
    """Retorna dados de tráfego por campanha, público (conjunto) e criativo (anúncio)."""
    creds = _sa_creds(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    sheets = gapi_build("sheets", "v4", credentials=creds).spreadsheets().values()

    def _toi(v):
        try: return int(v)
        except: return 0

    def _blank():
        return {"invest": 0.0, "imp": 0, "clicks": 0, "pv": 0, "checkout": 0}

    def _agg(d, key, cost, imp, clks, pv, co):
        if not key: return
        if key not in d: d[key] = _blank()
        d[key]["invest"]   += cost
        d[key]["imp"]      += imp
        d[key]["clicks"]   += clks
        d[key]["pv"]       += pv
        d[key]["checkout"] += co

    # ── Meta Ads: agrega por campanha, conjunto e anúncio ────────────────────
    meta_rows = sheets.get(spreadsheetId=WICI2_SHEET_ID, range=WICI2_ABA_META).execute().get("values", [])[1:]
    camp_meta:  dict = {}
    adset_meta: dict = {}
    ad_meta:    dict = {}
    ad_links:   dict = {}   # anúncio → primeiro link encontrado (col 9)
    ad_daily_invest:    dict = {}  # (anuncio, date) → invest
    camp_daily_invest:  dict = {}  # (campanha, date) → invest
    adset_daily_invest: dict = {}  # (conjunto, date) → invest

    for row in meta_rows:
        if len(row) < 4: continue
        row_date = _wici2_parse_date(row[0]) if row[0] else None
        if date_start and row_date and row_date < date_start: continue
        if date_end   and row_date and row_date > date_end:   continue
        camp = row[1].strip() if len(row) > 1 else ""
        if not camp: continue
        has_mo = "MO" in camp
        if profile == "malu" and not has_mo: continue
        if profile == "hire" and has_mo:     continue
        try:    cost = float(row[3].replace(",", "."))
        except: continue
        adset   = row[2].strip()  if len(row) > 2  else ""
        anuncio = row[10].strip() if len(row) > 10 else ""
        link    = row[9].strip()  if len(row) > 9  else ""
        imp  = _toi(row[4]) if len(row) > 4 else 0
        clks = _toi(row[6]) if len(row) > 6 else 0
        pv   = _toi(row[7]) if len(row) > 7 else 0
        co   = _toi(row[8]) if len(row) > 8 else 0
        _agg(camp_meta,  camp,    cost, imp, clks, pv, co)
        _agg(adset_meta, adset,   cost, imp, clks, pv, co)
        _agg(ad_meta,    anuncio, cost, imp, clks, pv, co)
        if anuncio and link and anuncio not in ad_links:
            ad_links[anuncio] = link
        if row_date:
            if anuncio:
                dk = (anuncio, row_date); ad_daily_invest[dk]    = ad_daily_invest.get(dk, 0.0)    + cost
            if camp:
                dk = (camp,    row_date); camp_daily_invest[dk]  = camp_daily_invest.get(dk, 0.0)  + cost
            if adset:
                dk = (adset,   row_date); adset_daily_invest[dk] = adset_daily_invest.get(dk, 0.0) + cost

    # ── Green: vendas por campanha (utm_campaign), público (utm_medium) e criativo (utm_content) ──
    green_rows = sheets.get(spreadsheetId=WICI2_SHEET_ID, range=WICI2_ABA_VENDAS).execute().get("values", [])[1:]
    camp_sales:    dict = {}
    medium_sales:  dict = {}
    content_sales: dict = {}
    ad_daily_sales:     dict = {}  # (utm_content, date) → vendas
    camp_daily_sales:   dict = {}  # (utm_campaign, date) → vendas
    medium_daily_sales: dict = {}  # (utm_medium, date) → vendas

    for row in green_rows:
        if len(row) < 7: continue
        row_date = _wici2_parse_date(row[0]) if row[0] else None
        if date_start and row_date and row_date < date_start: continue
        if date_end   and row_date and row_date > date_end:   continue
        prod = row[4].strip() if len(row) > 4 else ""
        if _wici2_product_category(prod) != "workshop": continue
        src  = row[5].strip().lower() if len(row) > 5 else ""
        if src not in ("ig", "facebook", "metaads", "meta"): continue
        camp    = row[6].strip() if len(row) > 6 else ""
        medium  = row[7].strip() if len(row) > 7 else ""
        content = row[8].strip() if len(row) > 8 else ""
        if camp:
            has_mo = "MO" in camp
            if profile == "malu" and not has_mo: continue
            if profile == "hire" and has_mo:     continue
        if camp:    camp_sales[camp]       = camp_sales.get(camp, 0)       + 1
        if medium:  medium_sales[medium]   = medium_sales.get(medium, 0)   + 1
        if content: content_sales[content] = content_sales.get(content, 0) + 1
        if row_date:
            if content: dk = (content, row_date); ad_daily_sales[dk]     = ad_daily_sales.get(dk, 0)     + 1
            if camp:    dk = (camp,    row_date); camp_daily_sales[dk]   = camp_daily_sales.get(dk, 0)   + 1
            if medium:  dk = (medium,  row_date); medium_daily_sales[dk] = medium_daily_sales.get(dk, 0) + 1

    # ── Ads RMKT: lê lista de nomes da aba "Ads RMKT" ────────────────────────
    rmkt_names: set = set()
    try:
        rmkt_rows = sheets.get(spreadsheetId=WICI2_SHEET_ID, range=WICI2_ABA_RMKT).execute().get("values", [])
        for row in rmkt_rows:
            if row and row[0].strip():
                rmkt_names.add(row[0].strip())
    except Exception:
        pass  # aba ausente ou erro → sem marcação RMKT

    # ── Monta tabela ─────────────────────────────────────────────────────────
    def _build(meta_dict, sales_dict, with_profile=False):
        rows = []
        for key, m in meta_dict.items():
            vendas  = sales_dict.get(key, 0)
            invest  = m["invest"]
            clicks  = m["clicks"]
            imp     = m["imp"]
            pv      = m["pv"]
            ctr          = round(clicks / imp    * 100, 2) if imp    else 0.0
            connect_rate = round(pv     / clicks * 100, 2) if clicks else 0.0
            tx_conv = round(vendas / clicks * 100, 2) if clicks else 0.0
            cpa     = round(invest / vendas, 2) if vendas else None
            row = {
                "nome":         key,
                "type":         _wici2_campaign_type(key) or "outro",
                "invest":       round(invest, 2),
                "imp":          imp,
                "ctr":          ctr,
                "clicks":       clicks,
                "pv":           pv,
                "connect_rate": connect_rate,
                "checkout":     m["checkout"],
                "vendas":       vendas,
                "tx_conv":      tx_conv,
                "cpa":          cpa,
            }
            if with_profile:
                row["profile"] = "malu" if "MO" in key else "hire"
            rows.append(row)

        rows.sort(key=lambda x: -x["invest"])

        # Alertas baseados na meta fixa de CPA = R$ 70
        # green = OK (≤ R$70) | yellow = CPA Alto (>R$70–R$105) | orange = CPA Crítico (>R$105) | red = Sem Venda
        CPA_META = 70.0
        for r in rows:
            if r["vendas"] == 0:            r["alerta"] = "red"
            elif r["cpa"] > CPA_META * 1.5: r["alerta"] = "orange"   # > R$105
            elif r["cpa"] > CPA_META:       r["alerta"] = "yellow"   # > R$70
            else:                           r["alerta"] = "green"
        return rows

    criativos = _build(ad_meta, content_sales)
    for r in criativos:
        r["rmkt"] = r["nome"] in rmkt_names

    # ── Tendência de CPA (últimos 3 dias do período selecionado) ─────────────
    ref_date   = date_end if date_end else datetime.today().date()
    trend_days = [(ref_date - timedelta(days=i)) for i in range(3)]  # [hoje, ontem, anteontem]

    def _cpa_trend(name, inv_dict, sales_dict):
        daily_cpas = []
        for d in trend_days:
            inv   = inv_dict.get((name, d), 0.0)
            sales = sales_dict.get((name, d), 0)
            if sales > 0:
                daily_cpas.append(inv / sales)
        if len(daily_cpas) < 2:
            return ""
        recent, old = daily_cpas[0], daily_cpas[-1]
        if recent <= old * 0.9:  return "improving"
        if recent >= old * 1.1:  return "worsening"
        return "stable"

    for r in criativos:
        r["cpa_trend"] = _cpa_trend(r["nome"], ad_daily_invest, ad_daily_sales)

    # Adiciona link por anúncio (col 9 da aba Meta Ads)
    for r in criativos:
        r["link"] = ad_links.get(r["nome"], "")

    # Status via Meta Graph API — campanhas, conjuntos e anúncios
    active_ads:    set = set()
    active_camps:  set = set()
    active_adsets: set = set()
    meta_api_ok = False
    try:
        import urllib.request as _ur, json as _json, urllib.parse as _up
        hire_cfg   = META_CLIENTS.get("HIRE", {})
        hire_token = hire_cfg.get("token", lambda: "")()
        hire_accts = hire_cfg.get("accounts", [])
        if hire_token and hire_accts:
            _filt = _up.quote('[{"field":"effective_status","operator":"IN","value":["ACTIVE"]}]')

            def _fetch_active(endpoint, target_set):
                for acct_id in hire_accts:
                    nxt = (f"{META_GRAPH_BASE}/{acct_id}/{endpoint}"
                           f"?fields=name&limit=500&filtering={_filt}&access_token={hire_token}")
                    while nxt:
                        with _ur.urlopen(nxt, timeout=10) as resp:
                            d = _json.loads(resp.read().decode())
                        if "error" in d:
                            print(f"[WICI2/Meta] {endpoint} error {acct_id}: {d['error']}", flush=True)
                            break
                        for obj in d.get("data", []):
                            n = obj.get("name", "").strip()
                            if n: target_set.add(n)
                        nxt = d.get("paging", {}).get("next")

            _fetch_active("ads",       active_ads)
            _fetch_active("campaigns", active_camps)
            _fetch_active("adsets",    active_adsets)
            meta_api_ok = True
        else:
            print(f"[WICI2/Meta] token={bool(hire_token)} accounts={hire_accts}", flush=True)
    except Exception as _meta_err:
        print(f"[WICI2/Meta] Erro ao buscar status: {_meta_err}", flush=True)

    for r in criativos:
        r["status"] = ("ACTIVE" if r["nome"] in active_ads else "PAUSED") if meta_api_ok else ""

    campanhas = _build(camp_meta,  camp_sales, with_profile=True)
    for r in campanhas:
        r["cpa_trend"] = _cpa_trend(r["nome"], camp_daily_invest, camp_daily_sales)
        r["status"] = ("ACTIVE" if r["nome"] in active_camps  else "PAUSED") if meta_api_ok else ""

    publicos = _build(adset_meta, medium_sales)
    for r in publicos:
        r["cpa_trend"] = _cpa_trend(r["nome"], adset_daily_invest, medium_daily_sales)
        r["status"] = ("ACTIVE" if r["nome"] in active_adsets else "PAUSED") if meta_api_ok else ""

    return {
        "campanhas": campanhas,
        "publicos":  publicos,
        "criativos": criativos,
    }


@app.get("/api/wici2/trafego")
async def get_wici2_trafego(
    request: Request,
    date_start: str = None,
    date_end:   str = None,
    profile:    str = "",
):
    try:
        ds = datetime.strptime(date_start, "%Y-%m-%d").date() if date_start else None
        de = datetime.strptime(date_end,   "%Y-%m-%d").date() if date_end   else None
        data = await asyncio.to_thread(_wici2_fetch_trafego, ds, de, profile)
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/wici2", response_class=HTMLResponse)
async def wici2_page(request: Request):
    if not verify_session(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse("wici2.html", {"request": request, **_nav_base(request, "wici2")})

@app.get("/api/wici2/metrics")
async def get_wici2_metrics(
    request: Request,
    date_start: str = None,
    date_end:   str = None,
    profile:    str = "",
):
    try:
        ds = datetime.strptime(date_start, "%Y-%m-%d").date() if date_start else None
        de = datetime.strptime(date_end,   "%Y-%m-%d").date() if date_end   else None
        data = await asyncio.to_thread(_wici2_fetch_metrics, ds, de, profile)
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

PESQUISA_SHEET_ID = "1anx4DPJxtLI7c_TefA8bn3aNTUrvWoIQBHkN4dE1ALg"
PESQUISA_ABA      = "Respostas ao formulário 1"

_PT_STOPS = {
    'a','e','o','as','os','de','do','da','dos','das','em','no','na','nos','nas',
    'que','para','por','com','um','uma','me','se','eu','te','não','nao','meu','minha',
    'mais','ja','já','mas','ou','é','e','sao','são','ter','ser','estar','ao','aos',
    'à','às','pelo','pela','pelos','pelas','entre','como','muito','bem','também',
    'tambem','só','so','ainda','ele','ela','eles','elas','isso','esse','essa',
    'este','esta','aqui','la','lá','quando','onde','quem','qual','quais','porque',
    'pois','então','entao','assim','hoje','sempre','nunca','todo','toda','todos',
    'todas','cada','tem','foi','ir','poder','quero','tenho','sou','estou','meus',
    'minhas','seus','suas','seu','sua','num','numa','nao','pra','pro','ta','to',
    'mim','voce','você','vc','nos','aquela','aquele','mesmo','mesma','muitos',
    'muitas','poucos','poucas','outros','outras','outro','outra','tanto','tanta',
    'quanto','quanta','agora','antes','depois','pouco','mal','ali','ai','aí',
    'talvez','porem','porém','logo','portanto','além','alem','disso','desde','até',
    'ate','sem','sob','sobre','ante','apos','após','durante','através','atraves',
    'mediante','conforme','segundo','fora','dentro','diante','acima','abaixo',
    'junto','contra','fazer','saber','haver','vir','ver','dar','ficar','dizer',
    'falar','tomar','parecer','deixar','passar','usar','dia','dias','anos','ano',
    'vez','vezes','parte','coisa','coisas','lugar','pessoas','pessoa','forma',
    'modo','tempo','area','área','caso','ponto','meio','mundo','pais','país',
    'brasil','trabalho','vaga','vagas','internacional','exterior','moeda','forte',
    'remoto','morar','conseguir','oportunidade','oportunidades','vida','novo',
    'nova','querer','poder','ter','algum','alguma','alguns','algumas','nenhum',
    'nenhuma','tudo','nada','algo','alguem','alguém','ninguem','ninguém',
}

def _pesquisa_word_cloud(texts: list, max_words: int = 70) -> list:
    from collections import Counter
    words = []
    for text in texts:
        if not text:
            continue
        for w in re.findall(r"[a-záéíóúàãõâêôüç]+", text.lower()):
            if len(w) > 3 and w not in _PT_STOPS:
                words.append(w)
    c = Counter(words)
    return [[w, cnt] for w, cnt in c.most_common(max_words)]

def _wici2_fetch_pesquisa() -> dict:
    creds = _sa_creds(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    from googleapiclient.discovery import build as _build_svc
    svc    = _build_svc("sheets", "v4", credentials=creds)
    rows_raw = svc.spreadsheets().values().get(
        spreadsheetId=PESQUISA_SHEET_ID, range=PESQUISA_ABA
    ).execute().get("values", [])

    if not rows_raw:
        return {}

    data  = rows_raw[1:]
    total = len(data)

    def _col(i):
        return [r[i].strip() if len(r) > i else "" for r in data]

    def _count(idx):
        from collections import Counter
        return dict(Counter(v for v in _col(idx) if v).most_common())

    # Profissão: OUTRO → usa texto livre do campo seguinte
    profissao_texts = []
    for r in data:
        p     = r[12].strip() if len(r) > 12 else ""
        outro = r[13].strip() if len(r) > 13 else ""
        if p == "OUTRO":
            if outro:
                profissao_texts.append(outro)
        elif p:
            profissao_texts.append(p)

    return {
        "total":          total,
        "genero":         _count(4),
        "idade":          _count(3),
        "faixa_salarial": _count(6),
        "formacao":       _count(7),
        "passaporte":     _count(8),
        "ingles":         _count(9),
        "outro_idioma":   _count(10),
        "tempo_insta":    _count(14),
        "morou_fora":     _count(15),
        "estado":         _count(16),
        "profissao_words": _pesquisa_word_cloud(profissao_texts),
        "porque_words":    _pesquisa_word_cloud(_col(17)),
    }

@app.get("/api/wici2/pesquisa")
async def get_wici2_pesquisa(request: Request):
    try:
        data = await asyncio.to_thread(_wici2_fetch_pesquisa)
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── LP (Landing Page) ─────────────────────────────────────────────────────────
WICI2_LP_A_NOME = "LP A"
WICI2_LP_A_URL  = "https://hirebrazil.com.br/workshop-intensivo-de-carreira-internacional-malu-ads/"
WICI2_LP_B_NOME = "Página B"
WICI2_LP_B_URL  = "https://hirebrazil.com.br/workshop-intensivo-de-carreira-internacional-malu-ads-b/"

def _lp_empty_agg():
    return {"invest": 0.0, "imp": 0, "clicks": 0, "pv": 0, "checkout": 0, "vendas": 0, "daily": {}}

def _lp_add_meta(agg, row, row_date):
    def _toi(v):
        try: return int(v)
        except: return 0
    try:    cost = float(row[3].replace(",", "."))
    except: return
    imp  = _toi(row[4]) if len(row) > 4 else 0
    clks = _toi(row[6]) if len(row) > 6 else 0
    pv   = _toi(row[7]) if len(row) > 7 else 0
    co   = _toi(row[8]) if len(row) > 8 else 0
    agg["invest"] += cost; agg["imp"] += imp; agg["clicks"] += clks
    agg["pv"] += pv; agg["checkout"] += co
    if row_date:
        dk = row_date.strftime("%Y-%m-%d")
        if dk not in agg["daily"]:
            agg["daily"][dk] = {"invest": 0.0, "imp": 0, "clicks": 0, "pv": 0, "checkout": 0, "vendas": 0}
        agg["daily"][dk]["invest"]   += cost; agg["daily"][dk]["imp"]      += imp
        agg["daily"][dk]["clicks"]   += clks; agg["daily"][dk]["pv"]       += pv
        agg["daily"][dk]["checkout"] += co

def _lp_build_result(agg, nome, url):
    invest = agg["invest"]; imp = agg["imp"]; clicks = agg["clicks"]
    pv = agg["pv"]; checkout = agg["checkout"]; vendas = agg["vendas"]
    ctr           = round(clicks   / imp      * 100, 2) if imp      else 0.0
    connect_rate  = round(pv       / clicks   * 100, 2) if clicks   else 0.0
    checkout_rate = round(checkout / pv       * 100, 2) if pv       else 0.0
    tx_conv       = round(vendas   / checkout * 100, 2) if checkout else 0.0
    tx_conv_pv    = round(vendas   / pv       * 100, 2) if pv       else 0.0
    cpa           = round(invest   / vendas,   2)        if vendas  else None
    sorted_days   = sorted(agg["daily"].items())
    return {
        "lp_nome": nome, "lp_url": url,
        "invest": round(invest, 2), "imp": imp, "clicks": clicks,
        "pv": pv, "checkout": checkout, "vendas": vendas, "cpa": cpa,
        "ctr": ctr, "connect_rate": connect_rate,
        "checkout_rate": checkout_rate, "tx_conv": tx_conv, "tx_conv_pv": tx_conv_pv,
        "daily": {
            "labels":        [d for d, _ in sorted_days],
            "connect_rate":  [round(v["pv"]/v["clicks"]*100,2)       if v["clicks"]   else None for _,v in sorted_days],
            "checkout_rate": [round(v["checkout"]/v["pv"]*100,2)     if v["pv"]       else None for _,v in sorted_days],
            "tx_conv":       [round(v["vendas"]/v["checkout"]*100,2)  if v["checkout"] else None for _,v in sorted_days],
            "tx_conv_pv":    [round(v["vendas"]/v["pv"]*100,2)        if v["pv"]       else None for _,v in sorted_days],
            "ctr":           [round(v["clicks"]/v["imp"]*100,2)       if v["imp"]      else None for _,v in sorted_days],
            "cpa":           [round(v["invest"]/v["vendas"],2)         if v["vendas"]   else None for _,v in sorted_days],
        }
    }

def _wici2_fetch_lp(date_start=None, date_end=None, profile: str = "") -> dict:
    """Retorna métricas de LP A e LP B separadas."""
    creds = _sa_creds(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    sheets = gapi_build("sheets", "v4", credentials=creds).spreadsheets().values()

    agg_a = _lp_empty_agg()
    agg_b = _lp_empty_agg()

    # ── Meta Ads ──────────────────────────────────────────────────────────────
    meta_rows = sheets.get(spreadsheetId=WICI2_SHEET_ID, range=WICI2_ABA_META).execute().get("values", [])[1:]
    for row in meta_rows:
        if len(row) < 4: continue
        row_date = _wici2_parse_date(row[0]) if row[0] else None
        if date_start and row_date and row_date < date_start: continue
        if date_end   and row_date and row_date > date_end:   continue
        camp = row[1].strip() if len(row) > 1 else ""
        if not camp: continue
        has_mo = "MO" in camp
        if profile == "malu" and not has_mo: continue
        if profile == "hire" and has_mo:     continue
        is_b = "pag_b" in camp.lower()
        _lp_add_meta(agg_b if is_b else agg_a, row, row_date)

    # ── Green (vendas) ────────────────────────────────────────────────────────
    green_rows = sheets.get(spreadsheetId=WICI2_SHEET_ID, range=WICI2_ABA_VENDAS).execute().get("values", [])[1:]
    for row in green_rows:
        if len(row) < 7: continue
        row_date = _wici2_parse_date(row[0]) if row[0] else None
        if date_start and row_date and row_date < date_start: continue
        if date_end   and row_date and row_date > date_end:   continue
        prod = row[4].strip() if len(row) > 4 else ""
        if _wici2_product_category(prod) != "workshop": continue
        src = row[5].strip().lower() if len(row) > 5 else ""
        if src not in ("ig", "facebook", "metaads", "meta"): continue
        camp = row[6].strip() if len(row) > 6 else ""
        if camp:
            has_mo = "MO" in camp
            if profile == "malu" and not has_mo: continue
            if profile == "hire" and has_mo:     continue
        is_b = ("pag_b" in camp.lower()) if camp else False
        agg = agg_b if is_b else agg_a
        agg["vendas"] += 1
        if row_date:
            dk = row_date.strftime("%Y-%m-%d")
            if dk not in agg["daily"]:
                agg["daily"][dk] = {"invest": 0.0, "imp": 0, "clicks": 0, "pv": 0, "checkout": 0, "vendas": 0}
            agg["daily"][dk].setdefault("checkout", 0)
            agg["daily"][dk]["vendas"] += 1

    return {
        "lp_a": _lp_build_result(agg_a, WICI2_LP_A_NOME, WICI2_LP_A_URL),
        "lp_b": _lp_build_result(agg_b, WICI2_LP_B_NOME, WICI2_LP_B_URL),
    }


@app.get("/api/wici2/cpa-alerts")
async def get_cpa_alerts(request: Request):
    return JSONResponse({"alerts": _cpa_alerts})

@app.post("/api/wici2/cpa-monitor/run")
async def run_cpa_monitor_now(request: Request):
    """Dispara o monitor de CPA manualmente (para teste)."""
    await asyncio.to_thread(_run_cpa_monitor)
    return JSONResponse({"alerts": _cpa_alerts})

@app.get("/api/wici2/lp")
async def get_wici2_lp(
    request: Request,
    date_start: str = None,
    date_end:   str = None,
    profile:    str = "",
):
    try:
        ds = datetime.strptime(date_start, "%Y-%m-%d").date() if date_start else None
        de = datetime.strptime(date_end,   "%Y-%m-%d").date() if date_end   else None
        data = await asyncio.to_thread(_wici2_fetch_lp, ds, de, profile)
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Greenn Webhook ────────────────────────────────────────────────────────────
_GREENN_PRODUCTS = {
    "Workshop Intensivo de Carreira Internacional",
    "Workshop Intensivo de Carreira Internacional [MAIO 26]",
    "Acesso à gravação do Workshop Intensivo de Carreira Internacional por 7 dias",
    "Análise de currículo e LinkedIn AO VIVO com mentora de carreira no Zoom",
    "Combo: Gravação + Análise de LinkedIn e CV",
}

@app.post("/webhook/greenn")
async def greenn_webhook(request: Request):
    # Validação do token
    token_header = request.headers.get("webhook-token", "") or request.headers.get("authorization", "").replace("Bearer ", "")
    body = await request.json()
    token_body = body.get("token", "") if isinstance(body, dict) else ""
    received_token = token_header or token_body
    if GREENN_WEBHOOK_TOKEN and received_token != GREENN_WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")

    try:
        data   = body.get("data", body)
        sale   = data.get("sale", {})
        prod   = data.get("product", {})
        cust   = data.get("customer", {})

        status = sale.get("status", "")
        if status not in ("approved", "paid", "complete", "completed"):
            return JSONResponse({"ok": True, "skipped": f"status={status}"})

        product_name = prod.get("name", "").strip()
        if product_name not in _GREENN_PRODUCTS:
            return JSONResponse({"ok": True, "skipped": f"produto não filtrado: {product_name}"})

        # Extrai UTMs dos saleMetas
        utms = {}
        for m in sale.get("saleMetas", []):
            utms[m.get("meta_key", "")] = m.get("meta_value", "")

        sale_date = (sale.get("dt_payment") or sale.get("created_at") or "")[:10]
        if not sale_date:
            sale_date = datetime.now().strftime("%Y-%m-%d")

        nome  = cust.get("name", "")
        email = cust.get("email", "")
        valor = str(sale.get("total", "0")).replace(".", ",")
        src   = utms.get("utm_source", "greenn")
        camp  = utms.get("utm_campaign", "")
        medium = utms.get("utm_medium", "")
        term  = utms.get("utm_term", "")

        # Grava na aba Green - Todas as Vendas
        row = [sale_date, nome, email, valor, product_name, src, camp, medium, "", term]
        creds = _sa_creds(["https://www.googleapis.com/auth/spreadsheets"])
        sheets = gapi_build("sheets", "v4", credentials=creds).spreadsheets().values()
        sheets.append(
            spreadsheetId=WICI2_SHEET_ID,
            range=f"{WICI2_ABA_VENDAS}!A:J",
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()

        print(f"[greenn] venda gravada: {product_name} | {email} | {camp}")
        return JSONResponse({"ok": True})

    except Exception as e:
        print(f"[greenn] erro: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook/dft-lead")
async def dft_lead(request: Request):
    try:
        body = await request.json()
        name  = body.get("name", "").strip()
        email = body.get("email", "").strip()
        phone = body.get("phone", "").strip()

        if not email:
            raise HTTPException(status_code=400, detail="email obrigatório")

        payload = {
            "contact": {
                "name": name,
                "emails": [{"email": email}],
                "phones": [{"phone": phone}] if phone else [],
            }
        }
        async with httpx.AsyncClient() as c:
            resp = await c.post(
                f"https://crm.rdstation.com/api/v1/contacts?token={RD_STATION_CRM_TOKEN}",
                json=payload,
                timeout=10,
            )
        print(f"[dft-lead] {email} → RD Station {resp.status_code}")
        return JSONResponse({"ok": True, "status": resp.status_code})

    except HTTPException:
        raise
    except Exception as e:
        print(f"[dft-lead] erro: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/dft/sheets")
async def dft_sheets():
    creds = _sa_creds(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = gapi_build("sheets", "v4", credentials=creds).spreadsheets()
    meta = svc.get(spreadsheetId=DFT_PERF_SHEET_ID).execute()
    return JSONResponse([
        {"title": s["properties"]["title"], "gid": s["properties"]["sheetId"]}
        for s in meta.get("sheets", [])
    ])


@app.get("/api/dft/performance")
async def dft_performance():
    try:
        creds = _sa_creds(["https://www.googleapis.com/auth/spreadsheets.readonly"])
        svc = gapi_build("sheets", "v4", credentials=creds).spreadsheets()
        meta = svc.get(spreadsheetId=DFT_PERF_SHEET_ID).execute()
        tab_name = next(
            (s["properties"]["title"] for s in meta.get("sheets", [])
             if s["properties"]["sheetId"] == DFT_PERF_SHEET_GID),
            None
        )
        if not tab_name:
            raise HTTPException(status_code=404, detail="Aba não encontrada")

        rows = svc.values().get(
            spreadsheetId=DFT_PERF_SHEET_ID,
            range=f"{tab_name}!A:N"
        ).execute().get("values", [])

        if len(rows) < 2:
            return JSONResponse({"data": []})

        # Mapeia índices por nome de coluna (normalizado sem acentos)
        import unicodedata
        def norm(s):
            return unicodedata.normalize("NFD", s).encode("ascii","ignore").decode().strip().lower()

        headers_raw = rows[0]
        hdrs = {norm(h): i for i, h in enumerate(headers_raw)}

        def ci(name, fallback=-1):
            return hdrs.get(norm(name), fallback)

        def get_col(row, name, fallback=""):
            idx = ci(name)
            if idx < 0 or idx >= len(row): return fallback
            return row[idx]

        def to_float(v):
            if not v: return 0.0
            try:
                return float(str(v).replace("R$","").replace(" ","").replace(".","").replace(",",".").strip() or 0)
            except ValueError:
                return 0.0

        def to_int(v):
            if not v: return 0
            try:
                return int(float(str(v).replace(".","").replace(",",".").strip() or 0))
            except ValueError:
                return 0

        def fmt_date(v):
            v = str(v).strip()
            if "/" in v:
                p = v.split("/")
                if len(p) == 3:
                    return f"{p[2]}-{p[1].zfill(2)}-{p[0].zfill(2)}"
            return v

        def map_camp(raw):
            n = norm(raw)
            if "branding" in n:     return "Branding"
            if "demanda" in n:      return "Demanda Ativa"
            if "segmento" in n:     return "Segmentos"
            return raw

        # Encontra coluna de investimento por prefixo (investido, investimento, etc.)
        invest_idx = next((i for k, i in hdrs.items() if k.startswith("invest")), -1)

        data = []
        for row in rows[1:]:
            if not row or not row[0]: continue
            try:
                camp_raw = get_col(row, "campanha") or (row[1] if len(row) > 1 else "")
                invest_val = row[invest_idx] if invest_idx >= 0 and invest_idx < len(row) else ""
                data.append({
                    "date":   fmt_date(row[0]),
                    "camp":   map_camp(camp_raw),
                    "invest": round(to_float(invest_val), 2),
                    "imp":    to_int(get_col(row, "impressoes")),
                    "clk":    to_int(get_col(row, "cliques")),
                    "leads":  to_int(get_col(row, "leads")),
                })
            except Exception:
                continue

        return JSONResponse({"data": data, "headers": list(hdrs.keys())})

    except HTTPException:
        raise
    except Exception as e:
        print(f"[dft-performance] erro: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/logs", response_class=HTMLResponse)
async def view_logs(request: Request):
    if not verify_session(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse("logs.html", {"request": request, "convs": list_conversations(), **_nav_base(request, "logs")})
