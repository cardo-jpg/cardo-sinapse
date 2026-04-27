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
from backend.financeiro import router as financeiro_router, init_db as financeiro_init_db
from backend.fin_pessoais import router as fp_router, init_db as fp_init_db

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
    start_sync_scheduler()
    _schedule_cpa_monitor()
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://lp.dftlogistica.com.br"],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)
app.include_router(gestao_router)
app.include_router(financeiro_router)
app.include_router(fp_router)
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

@app.get("/dashboard-hire", response_class=HTMLResponse)
async def dashboard_hire_page(request: Request):
    """Public alias for WICI2 — no login required, no sidebar."""
    return templates.TemplateResponse("wici2.html", {"request": request, "nav_username": "", "active_page": "wici2", "nav_clients": None, "nav_current_client": None, "public_mode": True})

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

# ── Hire Funis Contínuos ──────────────────────────────────────────────────────
# ── Google Ads credentials (env vars set in Railway) ─────────────────────────
GADS_DEVELOPER_TOKEN     = os.getenv("GADS_DEVELOPER_TOKEN", "")
GADS_CLIENT_ID           = os.getenv("GADS_CLIENT_ID", "")
GADS_CLIENT_SECRET       = os.getenv("GADS_CLIENT_SECRET", "")
HIRE_YT_CLIENT_ID        = os.getenv("HIRE_YT_CLIENT_ID", "") or GADS_CLIENT_ID
HIRE_YT_CLIENT_SECRET    = os.getenv("HIRE_YT_CLIENT_SECRET", "") or GADS_CLIENT_SECRET
GADS_REFRESH_TOKEN       = os.getenv("GADS_REFRESH_TOKEN", "")
GADS_HIRE_CUSTOMER_ID    = os.getenv("GADS_HIRE_CUSTOMER_ID", "1045573188")  # Hire / History Makers

# Overrides manuais: nome_normalizado (sem prefixo AD##) → youtube_video_id
_GADS_VIDEO_OVERRIDES: dict = {
    "trabalhe remoto": "2aO86hvDOZc",
}

HIRE_FUNIS_SHEET_ID   = "1l6_bsucWh3CZKhBZpqBykPJuYT3GQ5ZehAR5IAXd8kg"
HIRE_MALU_TRACKER_ID  = "1SVz6Eti4E6hkOpgVjOeYkQ3XvDuYuVYmSWOj_cWYvPM"
# Row 1 (index 1) of IG Malu tracker "dados" = 05/02/2024 (daily sequential; last data row 777 = 22/03/2026)
HIRE_MALU_TRACKER_BASE = datetime(2024, 2, 5).date()
HIRE_HIRE_TRACKER_ID  = "1XWIvqBx1TXjoFtiIViW_lW4L0EpbXAQsmU6vbvUMVIk"
# Row 1 (index 1) of IG Hire tracker "dados" = 01/01/2026 (daily sequential)
HIRE_HIRE_TRACKER_BASE = datetime(2026, 1, 1).date()
HIRE_FUNIS_SHEET2_ID   = "1VfZIM9f4-EixdQtQgrgaH-BUeIWUFEaxLJRerWCAuxw"  # resultado comercial

# ── Sheet cache (TTL 5 min) ───────────────────────────────────────────────────
import time as _time
_hf_sheet_cache: dict = {}
_HF_CACHE_TTL = 300  # segundos

def _hf_sheets_svc():
    creds = _sa_creds(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    return gapi_build("sheets", "v4", credentials=creds).spreadsheets().values()

def _hf_get(sheet_id: str, aba: str) -> list:
    key = f"{sheet_id}|{aba}"
    now = _time.time()
    cached = _hf_sheet_cache.get(key)
    if cached and now - cached[0] < _HF_CACHE_TTL:
        return cached[1]
    rows = _hf_sheets_svc().get(spreadsheetId=sheet_id, range=aba).execute().get("values", [])
    _hf_sheet_cache[key] = (now, rows)
    return rows

def _hf_cache_clear():
    _hf_sheet_cache.clear()

def _hf_budget_svc():
    creds = _sa_creds(["https://www.googleapis.com/auth/spreadsheets"])
    return gapi_build("sheets", "v4", credentials=creds).spreadsheets().values()

def _hf_load_budgets() -> dict:
    try:
        rows = _hf_budget_svc().get(
            spreadsheetId=HIRE_FUNIS_SHEET_ID, range="HF_Budgets!A2:C"
        ).execute().get("values", [])
        budgets: dict = {}
        for row in rows:
            if len(row) < 3:
                continue
            ch, mo, val = row[0], row[1], row[2]
            budgets.setdefault(ch, {})[mo] = float(val)
        return budgets
    except Exception:
        return {}

def _hf_save_budget(channel: str, month: str, budget: float):
    svc = _hf_budget_svc()
    rows = svc.get(
        spreadsheetId=HIRE_FUNIS_SHEET_ID, range="HF_Budgets!A2:C"
    ).execute().get("values", [])
    new_rows = [
        [r[0], r[1], r[2]] for r in rows
        if not (r[0] == channel and r[1] == month)
    ]
    new_rows.append([channel, month, budget])
    svc.clear(spreadsheetId=HIRE_FUNIS_SHEET_ID, range="HF_Budgets!A2:C").execute()
    svc.update(
        spreadsheetId=HIRE_FUNIS_SHEET_ID,
        range="HF_Budgets!A2:C",
        valueInputOption="RAW",
        body={"values": new_rows},
    ).execute()

def _hf_parse_num(s) -> float:
    if s is None:
        return 0.0
    s = str(s).strip().replace("R$", "").replace("\xa0", "").strip()
    if not s:
        return 0.0
    if "," in s and "." in s:
        # PT-BR: "1.234,56" → "." = thousands, "," = decimal
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        # Comma only: decimal separator → "1,5" → "1.5"
        s = s.replace(",", ".")
    elif "." in s:
        # Period only: if followed by exactly 3 digits (once or more) → PT-BR thousands
        # e.g. "1.174" → 1174, "1.174.256" → 1174256
        # e.g. "8.76" → 8.76 (decimal, 2 digits → no match)
        if re.search(r'\.\d{3}(?:\.|$)', s):
            s = s.replace(".", "")
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except Exception:
        return 0.0

def _hf_parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None

def _hf_fetch_yt_gads(date_start=None, date_end=None) -> dict:
    """Busca métricas YouTube direto do Google Ads API (Inscritos, Investido).

    GAQL não permite cost_micros + segments.conversion_action_name na mesma query,
    então usa 2 queries separadas:
      1. Inscritos: filtra por 'YouTube channel subscriptions' (sem cost_micros)
      2. Investido: cost_micros das campanhas YouTube (sem segmento de conversão)
    """
    if not all([GADS_DEVELOPER_TOKEN, GADS_CLIENT_ID, GADS_CLIENT_SECRET, GADS_REFRESH_TOKEN]):
        return {"investido": 0.0, "inscritos": 0, "custo_inscrito": 0.0}
    try:
        from google.ads.googleads.client import GoogleAdsClient
        gads_cfg = {
            "developer_token": GADS_DEVELOPER_TOKEN,
            "client_id":       GADS_CLIENT_ID,
            "client_secret":   GADS_CLIENT_SECRET,
            "refresh_token":   GADS_REFRESH_TOKEN,
            "use_proto_plus":  True,
        }
        client = GoogleAdsClient.load_from_dict(gads_cfg)
        ga_svc = client.get_service("GoogleAdsService")

        today = datetime.today().date()
        ds = date_start.isoformat() if date_start else "2020-01-01"
        de = date_end.isoformat()   if date_end   else today.isoformat()
        date_clause = f"segments.date BETWEEN '{ds}' AND '{de}'"
        yt_filter   = "campaign.name LIKE '%YOUTUBE%'"

        # Query 1: Inscritos — conversion_action_name deve aparecer no SELECT
        q_ins = f"""
            SELECT segments.conversion_action_name, metrics.conversions
            FROM campaign
            WHERE {date_clause}
              AND {yt_filter}
              AND segments.conversion_action_name = 'YouTube channel subscriptions'
        """
        inscritos = sum(
            r.metrics.conversions
            for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q_ins)
        )

        # Query 2: Investido — cost_micros sem segmento de conversão
        q_inv = f"""
            SELECT metrics.cost_micros
            FROM campaign
            WHERE {date_clause} AND {yt_filter}
        """
        investido = sum(
            r.metrics.cost_micros
            for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q_inv)
        ) / 1_000_000

        custo_inscrito = round(investido / inscritos, 2) if inscritos else 0.0
        return {
            "investido":      round(investido, 2),
            "inscritos":      int(inscritos),
            "custo_inscrito": custo_inscrito,
        }
    except Exception:
        return {"investido": 0.0, "inscritos": 0, "custo_inscrito": 0.0}


def _hf_fetch_audiencia(date_start=None, date_end=None) -> dict:
    def _get(aba):
        return _hf_get(HIRE_FUNIS_SHEET_ID, aba)

    def _in_range(row):
        d = _hf_parse_date(row[0]) if row else None
        if date_start and d and d < date_start: return False
        if date_end   and d and d > date_end:   return False
        return True

    # ── Somatório: Date | Investido | Leads | Vendas | Faturamento | Roas ────
    som_invest = som_leads = som_vendas = som_fat = 0.0
    for row in _get("Somatório")[1:]:
        if len(row) < 2 or not _in_range(row): continue
        som_invest += _hf_parse_num(row[1]) if len(row) > 1 else 0.0
        som_leads  += _hf_parse_num(row[2]) if len(row) > 2 else 0.0
        som_vendas += _hf_parse_num(row[3]) if len(row) > 3 else 0.0
        som_fat    += _hf_parse_num(row[4]) if len(row) > 4 else 0.0

    # ── IG Malu ───────────────────────────────────────────────────────────────
    # Investido/Vendas/Faturamento: Sheet1 "IG Malu" (dd/mm/yyyy)
    # Novos Seguidores: Sheet2 "dados" (daily rows from 05/02/2023, dates as dd/mm)
    malu_invest = malu_seg = malu_vendas = malu_fat = 0.0
    for row in _get("IG Malu")[1:]:
        if len(row) < 2 or not _in_range(row): continue
        malu_invest += _hf_parse_num(row[1])
        # col 2 (Seguidores) is always empty in Sheet1 — read from tracker below
        malu_vendas += _hf_parse_num(row[3]) if len(row) > 3 else 0.0
        malu_fat    += _hf_parse_num(row[4]) if len(row) > 4 else 0.0

    # Read Novos Seguidores from dedicated tracker (sequential daily rows)
    try:
        tracker_rows = _hf_get(HIRE_MALU_TRACKER_ID, "dados")
        for i, row in enumerate(tracker_rows[1:]):
            row_date = HIRE_MALU_TRACKER_BASE + timedelta(days=i)
            if date_start and row_date < date_start: continue
            if date_end   and row_date > date_end:   continue
            if len(row) > 1:
                malu_seg += _hf_parse_num(row[1])
    except Exception:
        pass

    # ── IG Hire ───────────────────────────────────────────────────────────────
    hire_invest = hire_seg = hire_vendas = hire_fat = 0.0
    for row in _get("IG Hire")[1:]:
        if len(row) < 2 or not _in_range(row): continue
        hire_invest += _hf_parse_num(row[1])
        hire_vendas += _hf_parse_num(row[3]) if len(row) > 3 else 0.0
        hire_fat    += _hf_parse_num(row[4]) if len(row) > 4 else 0.0

    try:
        hire_tracker_rows = _hf_get(HIRE_HIRE_TRACKER_ID, "dados")
        for i, row in enumerate(hire_tracker_rows[1:]):
            row_date = HIRE_HIRE_TRACKER_BASE + timedelta(days=i)
            if date_start and row_date < date_start: continue
            if date_end   and row_date > date_end:   continue
            if len(row) > 1:
                hire_seg += _hf_parse_num(row[1])
    except Exception:
        pass

    # ── YouTube ───────────────────────────────────────────────────────────────
    # Inscritos + Investido: Google Ads API (conversion action "YouTube channel subscriptions")
    # Vendas + Faturamento:  Sheet tab "Youtube" (col 3 e 4)
    yt_gads = _hf_fetch_yt_gads(date_start, date_end)
    yt_invest    = yt_gads["investido"]
    yt_inscritos = yt_gads["inscritos"]
    yt_custo_ins = yt_gads["custo_inscrito"]
    yt_vendas = yt_fat = 0.0
    for row in _get("Youtube")[1:]:
        if len(row) < 3 or not _in_range(row): continue
        yt_vendas += _hf_parse_num(row[3]) if len(row) > 3 else 0.0
        yt_fat    += _hf_parse_num(row[4]) if len(row) > 4 else 0.0

    # ── Site: Date | Campanha | Investido | Leads | Vendas | Faturamento ─────
    site_invest = site_leads = site_vendas = site_fat = 0.0
    for row in _get("Site")[1:]:
        if len(row) < 3 or not _in_range(row): continue
        site_invest += _hf_parse_num(row[2])
        site_leads  += _hf_parse_num(row[3]) if len(row) > 3 else 0.0
        site_vendas += _hf_parse_num(row[4]) if len(row) > 4 else 0.0
        site_fat    += _hf_parse_num(row[5]) if len(row) > 5 else 0.0

    # ── Sheet2: Resultado Comercial (planilha alternativa de atribuição) ──────
    # Somatório v2: Date | Investido | Leads | Vendas | Faturamento | Roas
    # IG Malu/Hire v2: Date | Investido | Seguidores | Vendas | Faturamento
    # Youtube v2: Date | Campanha | Investido | Vendas | Faturamento
    # Site v2: Date | Campanha | Investido | Leads | Vendas | Faturamento
    def _get2(aba):
        return _hf_get(HIRE_FUNIS_SHEET2_ID, aba)

    s2_invest = s2_leads = s2_vendas = s2_fat = 0.0
    for row in _get2("Somatório")[1:]:
        if len(row) < 2 or not _in_range(row): continue
        s2_invest += _hf_parse_num(row[1])
        s2_leads  += _hf_parse_num(row[2]) if len(row) > 2 else 0.0
        s2_vendas += _hf_parse_num(row[3]) if len(row) > 3 else 0.0
        s2_fat    += _hf_parse_num(row[4]) if len(row) > 4 else 0.0

    def _agg2_ch(rows_data, inv_col, vend_col, fat_col):
        inv = vend = fat = 0.0
        for row in rows_data[1:]:
            if not _in_range(row): continue
            inv  += _hf_parse_num(row[inv_col])              if len(row) > inv_col  else 0.0
            vend += _hf_parse_num(row[vend_col])             if len(row) > vend_col else 0.0
            fat  += _hf_parse_num(row[fat_col])              if len(row) > fat_col  else 0.0
        return round(inv, 2), int(vend), round(fat, 2)

    v2_malu_inv,  v2_malu_vend,  v2_malu_fat  = _agg2_ch(_get2("IG Malu"),  1, 3, 4)
    v2_hire_inv,  v2_hire_vend,  v2_hire_fat  = _agg2_ch(_get2("IG Hire"),  1, 3, 4)
    v2_yt_inv,    v2_yt_vend,    v2_yt_fat    = _agg2_ch(_get2("Youtube"),  2, 3, 4)
    v2_site_inv,  v2_site_vend,  v2_site_fat  = _agg2_ch(_get2("Site"),     2, 4, 5)

    def _roas(fat, inv):  return round(fat / inv, 2)  if inv  else 0.0
    def _unit(inv, n):    return round(inv / n,   2)  if n    else 0.0

    som_ticket = som_fat / som_vendas if som_vendas else 0.0
    som_cac    = som_invest / som_vendas if som_vendas else 0.0
    som_roas   = _roas(som_fat, som_invest)

    return {
        "somatorio": {
            "investido":    round(som_invest, 2),
            "faturamento":  round(som_fat, 2),
            "vendas":       int(som_vendas),
            "leads":        int(som_leads),
            "ticket_medio": round(som_ticket, 2),
            "cac":          round(som_cac, 2),
            "roas":         som_roas,
        },
        "channels": {
            "ig_malu": {
                "investido":      round(malu_invest, 2),
                "seguidores":     int(malu_seg),
                "custo_seguidor": _unit(malu_invest, malu_seg),
                "vendas":         int(malu_vendas),
                "faturamento":    round(malu_fat, 2),
                "roas":           _roas(malu_fat, malu_invest),
            },
            "ig_hire": {
                "investido":      round(hire_invest, 2),
                "seguidores":     int(hire_seg),
                "custo_seguidor": _unit(hire_invest, hire_seg),
                "vendas":         int(hire_vendas),
                "faturamento":    round(hire_fat, 2),
                "roas":           _roas(hire_fat, hire_invest),
            },
            "youtube": {
                "investido":      round(yt_invest, 2),
                "inscritos":      yt_inscritos,
                "custo_inscrito": yt_custo_ins,
                "vendas":         int(yt_vendas),
                "faturamento":    round(yt_fat, 2),
                "roas":           _roas(yt_fat, yt_invest),
            },
            "site": {
                "investido":   round(site_invest, 2),
                "leads":       int(site_leads),
                "custo_lead":  _unit(site_invest, site_leads),
                "vendas":      int(site_vendas),
                "faturamento": round(site_fat, 2),
                "roas":        _roas(site_fat, site_invest),
            },
        },
        "somatorio_v2": {
            "investido":    s2_invest,
            "faturamento":  round(s2_fat, 2),
            "vendas":       int(s2_vendas),
            "leads":        int(s2_leads),
            "ticket_medio": round(s2_fat / s2_vendas, 2) if s2_vendas else 0.0,
            "cac":          round(s2_invest / s2_vendas, 2) if s2_vendas else 0.0,
            "roas":         _roas(s2_fat, s2_invest),
        },
        "channels_v2": {
            "ig_malu": {"investido": v2_malu_inv, "vendas": v2_malu_vend, "faturamento": v2_malu_fat, "roas": _roas(v2_malu_fat, v2_malu_inv)},
            "ig_hire": {"investido": v2_hire_inv, "vendas": v2_hire_vend, "faturamento": v2_hire_fat, "roas": _roas(v2_hire_fat, v2_hire_inv)},
            "youtube": {"investido": v2_yt_inv,   "vendas": v2_yt_vend,   "faturamento": v2_yt_fat,   "roas": _roas(v2_yt_fat,   v2_yt_inv)},
            "site":    {"investido": v2_site_inv,  "vendas": v2_site_vend, "faturamento": v2_site_fat,  "roas": _roas(v2_site_fat,  v2_site_inv)},
        },
    }

def _hf_fetch_ebooks(date_start=None, date_end=None) -> dict:
    """Agrega dados das 4 abas de e-books (Ebook - 5 op., 7 erros, CR, 10 empresas)."""
    def _get(aba):
        return _hf_get(HIRE_FUNIS_SHEET_ID, f"'{aba}'")

    def _in_range(row):
        d = _hf_parse_date(row[0]) if row else None
        if date_start and d and d < date_start: return False
        if date_end   and d and d > date_end:   return False
        return True

    EBOOK_TABS = [
        ("eb_5op",    "Ebook - 5 op."),
        ("eb_7erros", "Ebook - 7 erros"),
        ("eb_cr",     "Ebook - CR"),
        ("eb_10emp",  "Ebook - 10 empresas"),
    ]

    def _get2(aba):
        return _hf_get(HIRE_FUNIS_SHEET2_ID, f"'{aba}'")

    def _agg(rows, inv_col, leads_col, vend_col, fat_col):
        inv = leads = vend = fat = 0.0
        for row in rows[1:]:
            if not _in_range(row): continue
            inv   += _hf_parse_num(row[inv_col])              if len(row) > inv_col   else 0.0
            leads += _hf_parse_num(row[leads_col])            if len(row) > leads_col else 0.0
            vend  += _hf_parse_num(row[vend_col])             if len(row) > vend_col  else 0.0
            fat   += _hf_parse_num(row[fat_col])              if len(row) > fat_col   else 0.0
        return round(inv,2), round(leads,0), round(vend,0), round(fat,2)

    ebooks: dict = {}
    som_invest = som_leads = som_vendas = som_fat = 0.0
    ebooks_v2: dict = {}
    s2_invest = s2_leads = s2_vendas = s2_fat = 0.0

    for key, aba in EBOOK_TABS:
        # Sheet1 — período filtrado
        inv, leads, vend, fat = _agg(_get(aba), 2, 3, 4, 5)
        som_invest += inv; som_leads += leads; som_vendas += vend; som_fat += fat
        ebooks[key] = {
            "investido":   inv,
            "leads":       int(leads),
            "vendas":      int(vend),
            "faturamento": fat,
            "cpl":         round(inv / leads, 2) if leads else 0.0,
            "roas":        round(fat / inv,   2) if inv   else 0.0,
        }
        # Sheet2 — todo o período (mesma aba, sem filtro de data)
        inv2, leads2, vend2, fat2 = _agg(_get2(aba), 2, 3, 4, 5)
        s2_invest += inv2; s2_leads += leads2; s2_vendas += vend2; s2_fat += fat2
        ebooks_v2[key] = {
            "investido":   inv2,
            "leads":       int(leads2),
            "vendas":      int(vend2),
            "faturamento": fat2,
            "cpl":         round(inv2 / leads2, 2) if leads2 else 0.0,
            "roas":        round(fat2 / inv2,   2) if inv2   else 0.0,
        }

    return {
        "somatorio": {
            "investido":    som_invest,
            "faturamento":  som_fat,
            "vendas":       int(som_vendas),
            "leads":        int(som_leads),
            "ticket_medio": round(som_fat / som_vendas, 2) if som_vendas else 0.0,
            "cac":          round(som_invest / som_vendas, 2) if som_vendas else 0.0,
            "roas":         round(som_fat / som_invest,   2) if som_invest else 0.0,
        },
        "ebooks": ebooks,
        "somatorio_v2": {
            "investido":    s2_invest,
            "faturamento":  s2_fat,
            "vendas":       int(s2_vendas),
            "leads":        int(s2_leads),
            "ticket_medio": round(s2_fat / s2_vendas, 2) if s2_vendas else 0.0,
            "cac":          round(s2_invest / s2_vendas, 2) if s2_vendas else 0.0,
            "roas":         round(s2_fat / s2_invest,   2) if s2_invest else 0.0,
        },
        "ebooks_v2": ebooks_v2,
    }

def _hf_fetch_corredor(date_start=None, date_end=None) -> dict:
    """Agrega dados do Corredor Polonês por KPI, série diária, campanha e criativo."""
    rows = _hf_get(HIRE_FUNIS_SHEET_ID, "'Corredor'")

    # col: 0=Date,1=Camp,2=Pub,3=Cri,4=URL,5=Inv,6=Imp,7=Vis,8=Alc,9=Vis3s,10=Inter,11=25%,12=75%,13=Cli,14=CPM
    def _in_range(d):
        if date_start and d and d < date_start: return False
        if date_end   and d and d > date_end:   return False
        return True

    # Aggregation buckets
    tot_inv = tot_imp = tot_vis = tot_alc = tot_vis3s = tot_p25 = tot_p75 = tot_cli = 0.0
    serie: dict  = {}    # date_str → {vis, inv}
    by_camp: dict = {}   # campanha → {inv,imp,alc,vis3s,p25,p75,cli}
    by_cri: dict  = {}   # criativo → {inv,imp,alc,vis3s,p25,p75,cli,url}

    for row in rows[1:]:
        if len(row) < 10: continue
        d = _hf_parse_date(row[0])
        if not _in_range(d): continue

        inv   = _hf_parse_num(row[5])
        imp   = _hf_parse_num(row[6])
        vis   = _hf_parse_num(row[7])
        alc   = _hf_parse_num(row[8])
        vis3s = _hf_parse_num(row[9])
        p25   = _hf_parse_num(row[11]) if len(row) > 11 else 0.0
        p75   = _hf_parse_num(row[12]) if len(row) > 12 else 0.0
        cli   = _hf_parse_num(row[13]) if len(row) > 13 else 0.0
        camp  = row[1].strip()
        cri   = row[3].strip()
        url   = row[4].strip() if len(row) > 4 else ""

        tot_inv   += inv;  tot_imp   += imp;  tot_vis   += vis
        tot_alc   += alc;  tot_vis3s += vis3s; tot_p25   += p25
        tot_p75   += p75;  tot_cli   += cli

        # Daily series
        ds = d.isoformat() if d else None
        if ds:
            if ds not in serie: serie[ds] = {"vis": 0.0, "inv": 0.0}
            serie[ds]["vis"] += vis
            serie[ds]["inv"] += inv

        # By campaign
        if camp not in by_camp:
            by_camp[camp] = {"inv":0.0,"imp":0.0,"alc":0.0,"vis3s":0.0,"p25":0.0,"p75":0.0,"cli":0.0}
        bc = by_camp[camp]
        bc["inv"]+=inv; bc["imp"]+=imp; bc["alc"]+=alc
        bc["vis3s"]+=vis3s; bc["p25"]+=p25; bc["p75"]+=p75; bc["cli"]+=cli

        # By creative
        if cri not in by_cri:
            by_cri[cri] = {"inv":0.0,"imp":0.0,"alc":0.0,"vis3s":0.0,"p25":0.0,"p75":0.0,"cli":0.0,"url":"","last_date":None}
        bc2 = by_cri[cri]
        bc2["inv"]+=inv; bc2["imp"]+=imp; bc2["alc"]+=alc
        bc2["vis3s"]+=vis3s; bc2["p25"]+=p25; bc2["p75"]+=p75; bc2["cli"]+=cli
        if url and not bc2["url"]: bc2["url"] = url
        if d and (bc2["last_date"] is None or d > bc2["last_date"]): bc2["last_date"] = d

    def _hooke(vis3s, imp):  return round(vis3s / imp * 100, 1)  if imp   else 0.0
    def _ret(p75, p25):      return round(p75   / p25 * 100, 1)  if p25   else 0.0
    def _cpm(inv, imp):      return round(inv   / imp * 1000, 2) if imp   else 0.0

    def _acao(vis3s, hooke, ret):
        if vis3s < 400:  return "Amostra básica"
        if vis3s < 1500: return "Aguardar dados"
        if hooke < 30 or ret < 25: return "Ruim"
        return "Escalar"

    serie_sorted = [{"date": k, "vis": int(v["vis"]), "inv": round(v["inv"],2)}
                    for k, v in sorted(serie.items())]

    campanha_list = sorted(
        [{"campanha": k,
          "investido": round(v["inv"],2), "alcance": int(v["alc"]),
          "vis3s": int(v["vis3s"]), "cliques": int(v["cli"]),
          "cpm": _cpm(v["inv"],v["imp"]),
          "retencao": _ret(v["p75"],v["p25"])}
         for k, v in by_camp.items()],
        key=lambda x: x["investido"], reverse=True
    )

    ref_date = date_end or datetime.today().date()
    from datetime import timedelta as _td
    def _status_cri(last_date):
        if last_date is None: return "Pausado"
        return "Ativo" if (ref_date - last_date).days <= 3 else "Pausado"

    criativo_list = sorted(
        [{"criativo": k,
          "url": v["url"],
          "status": _status_cri(v["last_date"]),
          "investido": round(v["inv"],2), "alcance": int(v["alc"]),
          "vis3s": int(v["vis3s"]), "cliques": int(v["cli"]),
          "cpm": _cpm(v["inv"],v["imp"]),
          "hooke": _hooke(v["vis3s"],v["imp"]),
          "retencao": _ret(v["p75"],v["p25"]),
          "acao": _acao(v["vis3s"], _hooke(v["vis3s"],v["imp"]), _ret(v["p75"],v["p25"]))}
         for k, v in by_cri.items()],
        key=lambda x: x["investido"], reverse=True
    )

    return {
        "kpis": {
            "investido":    round(tot_inv, 2),
            "visualizacoes": int(tot_vis),
            "hooke_rate":   _hooke(tot_vis3s, tot_imp),
            "retencao":     _ret(tot_p75, tot_p25),
            "impressoes":   int(tot_imp),
            "cliques":      int(tot_cli),
            "cpm":          _cpm(tot_inv, tot_imp),
        },
        "serie_diaria":  serie_sorted,
        "por_campanha":  campanha_list,
        "por_criativo":  criativo_list,
    }

@app.get("/dashboard-hire-funis", response_class=HTMLResponse)
async def dashboard_hire_funis_page(request: Request):
    """Public — Funis Contínuos da Hire, no login required."""
    return templates.TemplateResponse("hire_funis.html", {
        "request": request,
        "nav_username": "", "active_page": "hire_funis",
        "nav_clients": None, "nav_current_client": None, "public_mode": True,
    })

@app.get("/api/hire/funis/audiencia")
async def get_hire_funis_audiencia(
    request: Request,
    date_start: str = None,
    date_end:   str = None,
):
    try:
        ds = datetime.strptime(date_start, "%Y-%m-%d").date() if date_start else None
        de = datetime.strptime(date_end,   "%Y-%m-%d").date() if date_end   else None
        data = await asyncio.to_thread(_hf_fetch_audiencia, ds, de)
        data["budgets"] = _hf_load_budgets()
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/hire/funis/ebooks")
async def get_hire_funis_ebooks(
    request: Request,
    date_start: str = None,
    date_end:   str = None,
):
    try:
        ds = datetime.strptime(date_start, "%Y-%m-%d").date() if date_start else None
        de = datetime.strptime(date_end,   "%Y-%m-%d").date() if date_end   else None
        data = await asyncio.to_thread(_hf_fetch_ebooks, ds, de)
        data["budgets"] = _hf_load_budgets()
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/hire/funis/corredor")
async def get_hire_funis_corredor(
    request: Request,
    date_start: str = None,
    date_end:   str = None,
):
    try:
        ds = datetime.strptime(date_start, "%Y-%m-%d").date() if date_start else None
        de = datetime.strptime(date_end,   "%Y-%m-%d").date() if date_end   else None
        data = await asyncio.to_thread(_hf_fetch_corredor, ds, de)
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/hire/funis/budget")
async def put_hire_funis_budget(request: Request):
    try:
        body    = await request.json()
        channel = str(body.get("channel", ""))
        month   = str(body.get("month", ""))
        budget  = float(body.get("budget", 0))
        if not channel or not month:
            raise HTTPException(status_code=400, detail="channel e month são obrigatórios")
        _hf_save_budget(channel, month, budget)
        return JSONResponse({"ok": True})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── YouTube Tab ───────────────────────────────────────────────────────────────

# Armazena refresh token em memória (persiste até restart; também salvo no Sheets)
_hf_yt_refresh_token: str = os.getenv("HIRE_YT_REFRESH_TOKEN", "")
_hf_malu_channel_id: str = ""  # cached after first resolve

_HF_CONFIG_TAB = "HF_Config"


def _hf_config_write(key: str, value: str):
    """Persiste chave-valor na aba HF_Config do sheet WICI2."""
    try:
        creds = _sa_creds(["https://www.googleapis.com/auth/spreadsheets"])
        svc = gapi_build("sheets", "v4", credentials=creds).spreadsheets()
        # Garante que a aba existe
        try:
            svc.values().get(spreadsheetId=WICI2_SHEET_ID,
                             range=f"{_HF_CONFIG_TAB}!A1").execute()
        except Exception:
            svc.batchUpdate(
                spreadsheetId=WICI2_SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": _HF_CONFIG_TAB}}}]},
            ).execute()
        # Acha linha existente ou appenda
        rows = svc.values().get(spreadsheetId=WICI2_SHEET_ID,
                                range=f"{_HF_CONFIG_TAB}!A:B").execute().get("values", [])
        row_idx = next((i + 1 for i, r in enumerate(rows) if r and r[0] == key), None)
        if row_idx:
            svc.values().update(
                spreadsheetId=WICI2_SHEET_ID,
                range=f"{_HF_CONFIG_TAB}!A{row_idx}:B{row_idx}",
                valueInputOption="RAW",
                body={"values": [[key, value]]},
            ).execute()
        else:
            svc.values().append(
                spreadsheetId=WICI2_SHEET_ID,
                range=f"{_HF_CONFIG_TAB}!A:B",
                valueInputOption="RAW",
                body={"values": [[key, value]]},
            ).execute()
    except Exception as e:
        print(f"[hf_config_write] {e}", flush=True)


def _hf_config_read(key: str) -> str:
    """Lê valor da aba HF_Config do sheet WICI2."""
    try:
        creds = _sa_creds(["https://www.googleapis.com/auth/spreadsheets.readonly"])
        svc = gapi_build("sheets", "v4", credentials=creds).spreadsheets()
        rows = svc.values().get(spreadsheetId=WICI2_SHEET_ID,
                                range=f"{_HF_CONFIG_TAB}!A:B").execute().get("values", [])
        for row in rows:
            if len(row) >= 2 and row[0] == key:
                return row[1]
    except Exception:
        pass
    return ""


def _resolve_malu_channel_id(creds) -> str:
    """Resolve Malu Osowski's YouTube channel ID via handle lookup; falls back to MINE."""
    global _hf_malu_channel_id
    if _hf_malu_channel_id:
        return _hf_malu_channel_id
    try:
        yt_data = gapi_build("youtube", "v3", credentials=creds)
        r = yt_data.channels().list(part="id,snippet", forHandle="MaluOsowski").execute()
        items = r.get("items", [])
        if items:
            _hf_malu_channel_id = items[0]["id"]
            return _hf_malu_channel_id
    except Exception:
        pass
    return "MINE"

def _hf_yt_creds():
    """Load YouTube OAuth2 credentials — memória → env var → Google Sheets."""
    global _hf_yt_refresh_token
    rt = _hf_yt_refresh_token or os.getenv("HIRE_YT_REFRESH_TOKEN", "")
    if not rt:
        rt = _hf_config_read("HIRE_YT_REFRESH_TOKEN")
        if rt:
            _hf_yt_refresh_token = rt  # cache em memória
    if not rt:
        return None
    import google.oauth2.credentials
    return google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=rt,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=HIRE_YT_CLIENT_ID,
        client_secret=HIRE_YT_CLIENT_SECRET,
    )


def _hf_yt_save_token(refresh_token: str):
    """Salva refresh token em memória + Google Sheets (persistente)."""
    global _hf_yt_refresh_token
    _hf_yt_refresh_token = refresh_token
    _hf_config_write("HIRE_YT_REFRESH_TOKEN", refresh_token)


def _hf_fetch_youtube_tab(date_start=None, date_end=None) -> dict:
    """Full YouTube tab: GADS (KPIs + ads table + daily series) + YouTube Analytics."""
    today = datetime.today().date()
    ds = date_start.isoformat() if date_start else "2020-01-01"
    de = date_end.isoformat()   if date_end   else today.isoformat()

    # ── Google Ads portion ────────────────────────────────────────────────────
    kpis     = {"investido": 0.0, "inscritos": 0, "cliques": 0,
                "custo_inscrito": 0.0, "impressoes": 0, "cpm": 0.0}
    anuncios: list = []
    serie_gads_map: dict = {}

    if all([GADS_DEVELOPER_TOKEN, GADS_CLIENT_ID, GADS_CLIENT_SECRET, GADS_REFRESH_TOKEN]):
        from google.ads.googleads.client import GoogleAdsClient
        gads_cfg = {
            "developer_token": GADS_DEVELOPER_TOKEN,
            "client_id":       GADS_CLIENT_ID,
            "client_secret":   GADS_CLIENT_SECRET,
            "refresh_token":   GADS_REFRESH_TOKEN,
            "use_proto_plus":  True,
        }
        client = GoogleAdsClient.load_from_dict(gads_cfg)
        ga_svc = client.get_service("GoogleAdsService")
        dc = f"segments.date BETWEEN '{ds}' AND '{de}'"
        yf = "campaign.name LIKE '%YOUTUBE%'"

        # ── Q1: Daily cost + impressions + clicks (independent try) ─────────────
        try:
            q1 = f"""
                SELECT segments.date, metrics.cost_micros, metrics.impressions, metrics.clicks
                FROM campaign
                WHERE {dc} AND {yf}
            """
            tot_cost = tot_imp = tot_cli = 0
            for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q1):
                day = r.segments.date
                tot_cost += r.metrics.cost_micros
                tot_imp  += r.metrics.impressions
                tot_cli  += r.metrics.clicks
                sg = serie_gads_map.setdefault(day, {"inv": 0.0, "ins": 0})
                sg["inv"] += r.metrics.cost_micros / 1_000_000

            investido = tot_cost / 1_000_000
            kpis.update({
                "investido":  round(investido, 2),
                "cliques":    int(tot_cli),
                "impressoes": int(tot_imp),
                "cpm":        round(investido / tot_imp * 1000, 2) if tot_imp else 0.0,
            })
        except Exception as e:
            kpis["q1_error"] = str(e)

        # ── Q2: Subscription conversions por dia
        try:
            q2 = f"""
                SELECT segments.date, segments.conversion_action_name, metrics.conversions
                FROM campaign
                WHERE {dc} AND {yf}
                  AND segments.conversion_action_name = 'YouTube channel subscriptions'
            """
            daily_ins: dict = {}
            for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q2):
                d_key = r.segments.date
                daily_ins[d_key] = daily_ins.get(d_key, 0) + int(r.metrics.conversions)
            tot_ins = sum(daily_ins.values())
            kpis["inscritos"]      = tot_ins
            kpis["custo_inscrito"] = round(kpis["investido"] / tot_ins, 2) if tot_ins else 0.0
            # preenche série diária com dados reais por dia
            for d_key, ins in daily_ins.items():
                if d_key not in serie_gads_map:
                    serie_gads_map[d_key] = {"inv": 0.0, "ins": 0}
                serie_gads_map[d_key]["ins"] = ins
        except Exception as e:
            kpis["q2_error"] = str(e)

        # ── Q3: Per-ad table ─────────────────────────────────────────────────────
        try:
            # Q3a: métricas (sem campos de tipo específico para evitar erro GAQL)
            q3a = f"""
                SELECT
                    ad_group_ad.ad.id,
                    ad_group_ad.ad.name,
                    ad_group_ad.status,
                    metrics.cost_micros,
                    metrics.impressions,
                    metrics.clicks,
                    metrics.conversions
                FROM ad_group_ad
                WHERE {dc} AND {yf} AND metrics.impressions > 0
            """
            STATUS_PT = {"ENABLED": "Ativo", "PAUSED": "Pausado", "REMOVED": "Removido"}
            ads_map: dict = {}
            for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q3a):
                aid = str(r.ad_group_ad.ad.id)
                if aid not in ads_map:
                    ads_map[aid] = {
                        "nome":       r.ad_group_ad.ad.name,
                        "status":     STATUS_PT.get(r.ad_group_ad.status.name, "—"),
                        "asset_name": None,
                        "cost": 0, "imp": 0, "cli": 0, "conv": 0.0,
                    }
                ads_map[aid]["cost"] += r.metrics.cost_micros
                ads_map[aid]["imp"]  += r.metrics.impressions
                ads_map[aid]["cli"]  += r.metrics.clicks
                ads_map[aid]["conv"] += r.metrics.conversions

            # Q3b: tenta buscar assets de video_responsive_ad
            asset_names: set = set()
            yt_id_map:   dict = {}   # resource_name / marker → youtube_video_id
            try:
                q3b = f"""
                    SELECT ad_group_ad.ad.id, ad_group_ad.ad.video_responsive_ad.videos
                    FROM ad_group_ad
                    WHERE {dc} AND {yf} AND metrics.impressions > 0
                """
                for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q3b):
                    aid = str(r.ad_group_ad.ad.id)
                    if aid in ads_map and ads_map[aid]["asset_name"] is None:
                        videos = r.ad_group_ad.ad.video_responsive_ad.videos
                        if videos:
                            ads_map[aid]["asset_name"] = videos[0].asset
                            asset_names.add(videos[0].asset)
            except Exception:
                pass

            # Q3c: in-stream / bumper — tenta campos por tipo de anúncio
            for _q3c_field in [
                "ad_group_ad.ad.in_stream_ad.video",
                "ad_group_ad.ad.bumper_ad.video",
                "ad_group_ad.ad.non_skippable_in_stream_ad.video",
            ]:
                try:
                    q3c = f"""
                        SELECT ad_group_ad.ad.id, {_q3c_field}
                        FROM ad_group_ad
                        WHERE {dc} AND {yf} AND metrics.impressions > 0
                    """
                    for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q3c):
                        aid = str(r.ad_group_ad.ad.id)
                        if aid not in ads_map or ads_map[aid]["asset_name"] is not None:
                            continue
                        # navega o proto dinamicamente: "ad_group_ad.ad.in_stream_ad.video"
                        obj = r
                        try:
                            for part in _q3c_field.split("."):
                                obj = getattr(obj, part)
                            val = str(obj) if obj else ""
                        except Exception:
                            val = ""
                        if val and val.startswith("customers/"):
                            ads_map[aid]["asset_name"] = val
                            asset_names.add(val)
                except Exception:
                    pass

            # Q3d: resource 'video' da conta → title.lower → youtube_video_id
            title_to_vid: dict = {}
            try:
                q3d = "SELECT video.id, video.title FROM video"
                for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q3d):
                    if r.video.id and r.video.title:
                        title_to_vid[r.video.title.lower().strip()] = r.video.id
                print(f"[hf_yt q3d] {len(title_to_vid)} vídeos encontrados", flush=True)
            except Exception as e:
                print(f"[hf_yt q3d err] {e}", flush=True)

            # Para anúncios sem vídeo, match por título (remove prefixo "AD13 - ")
            import re as _re, unicodedata as _ud
            def _norm(s):
                return _ud.normalize("NFD", s).encode("ascii", "ignore").decode().lower().strip()
            for aid, v in ads_map.items():
                if v["asset_name"] is not None or not title_to_vid:
                    continue
                ad_norm = _norm(_re.sub(r'^AD\d+\s*[-–]\s*', '', v["nome"], flags=_re.I))
                for title_raw, vid_id in title_to_vid.items():
                    title_norm = _norm(title_raw)
                    if title_norm in ad_norm or ad_norm in title_norm:
                        marker = f"__yt__{vid_id}"
                        v["asset_name"] = marker
                        yt_id_map[marker] = vid_id
                        break

            # Q4: busca youtube_video_id dos assets coletados via Q3b/Q3c
            if asset_names:
                try:
                    names_filter = ", ".join(f"'{n}'" for n in asset_names)
                    q4 = f"""
                        SELECT asset.resource_name, asset.youtube_video_asset.youtube_video_id
                        FROM asset
                        WHERE asset.resource_name IN ({names_filter})
                    """
                    for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q4):
                        vid_id = r.asset.youtube_video_asset.youtube_video_id
                        if vid_id:
                            yt_id_map[r.asset.resource_name] = vid_id
                except Exception:
                    pass

            for v in ads_map.values():
                valor = round(v["cost"] / 1_000_000, 2)
                conv  = int(v["conv"])
                ctr   = round(v["cli"] / v["imp"] * 100, 2) if v["imp"] else 0.0
                yt_id = yt_id_map.get(v["asset_name"], "") if v["asset_name"] else ""
                if not yt_id:
                    ad_norm = _norm(_re.sub(r'^AD\d+\s*[-–]\s*', '', v["nome"], flags=_re.I))
                    yt_id = _GADS_VIDEO_OVERRIDES.get(ad_norm, "")
                anuncios.append({
                    "nome":       v["nome"],
                    "status":     v["status"],
                    "video_url":  f"https://www.youtube.com/watch?v={yt_id}" if yt_id else "",
                    "conversoes": conv,
                    "impressoes": int(v["imp"]),
                    "valor":      valor,
                    "custo_conv": round(valor / conv, 2) if conv else 0.0,
                    "taxa":       ctr,
                })
            anuncios.sort(key=lambda x: x["valor"], reverse=True)
        except Exception:
            pass  # ads table stays empty; KPIs unaffected

    serie_gads = [
        {"date": k, "inv": round(v["inv"], 2), "ins": v["ins"]}
        for k, v in sorted(serie_gads_map.items())
    ]

    # ── YouTube Analytics portion ─────────────────────────────────────────────
    analytics: dict = {"authorized": False, "serie_diaria": [], "videos": []}
    creds = _hf_yt_creds()
    if creds is not None:
        try:
            from google.auth.transport.requests import Request as GRequest
            if not creds.valid:
                creds.refresh(GRequest())

            yt_an = gapi_build("youtubeAnalytics", "v2", credentials=creds)

            resp = yt_an.reports().query(
                ids="channel==MINE",
                startDate=ds,
                endDate=de,
                metrics="subscribersGained,subscribersLost",
                dimensions="day",
                sort="day",
            ).execute()
            serie_yt = [
                {"date": row[0], "gained": int(row[1]), "lost": int(row[2])}
                for row in resp.get("rows", [])
            ]

            resp_v = yt_an.reports().query(
                ids="channel==MINE",
                startDate=ds,
                endDate=de,
                metrics="views,likes,shares,averageViewDuration,subscribersGained",
                dimensions="video",
                sort="-subscribersGained",
                maxResults=20,
            ).execute()

            video_rows = resp_v.get("rows", [])
            id_to_title: dict = {}
            if video_rows:
                yt_data = gapi_build("youtube", "v3", credentials=creds)
                vid_ids = ",".join(row[0] for row in video_rows[:50])
                vid_resp = yt_data.videos().list(part="snippet", id=vid_ids).execute()
                id_to_title = {v["id"]: v["snippet"]["title"] for v in vid_resp.get("items", [])}

            videos = []
            for row in video_rows:
                vid_id = row[0]
                avg_sec = int(float(row[4]))
                avg_dur = f"{avg_sec//60}:{avg_sec%60:02d}"
                videos.append({
                    "id":       vid_id,
                    "titulo":   id_to_title.get(vid_id, vid_id),
                    "link":     f"https://youtube.com/watch?v={vid_id}",
                    "inscritos": int(row[5]),
                    "views":    int(row[1]),
                    "likes":    int(row[2]),
                    "shares":   int(row[3]),
                    "avg_dur":  avg_dur,
                })
            analytics = {"authorized": True, "serie_diaria": serie_yt, "videos": videos}
        except Exception as e:
            analytics = {"authorized": True, "serie_diaria": [], "videos": [], "yt_error": str(e)}

    return {
        "kpis":       kpis,
        "anuncios":   anuncios,
        "serie_gads": serie_gads,
        "analytics":  analytics,
    }


@app.get("/api/hire/funis/youtube")
async def get_hire_funis_youtube(
    request: Request,
    date_start: str = None,
    date_end:   str = None,
):
    try:
        ds = datetime.strptime(date_start, "%Y-%m-%d").date() if date_start else None
        de = datetime.strptime(date_end,   "%Y-%m-%d").date() if date_end   else None
        data = await asyncio.to_thread(_hf_fetch_youtube_tab, ds, de)
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Armazena flow temporariamente para o callback (keyed by state)
_yt_flow_store: dict = {}


@app.get("/api/hire/funis/youtube/disconnect")
async def hire_yt_disconnect(request: Request):
    """Limpa token do YouTube Analytics (memória + Sheets)."""
    global _hf_yt_refresh_token, _hf_malu_channel_id
    _hf_yt_refresh_token = ""
    _hf_malu_channel_id  = ""
    await asyncio.to_thread(_hf_config_write, "HIRE_YT_REFRESH_TOKEN", "")
    return JSONResponse({"ok": True})


@app.get("/api/hire/funis/youtube/auth")
async def hire_yt_auth(request: Request):
    try:
        from google_auth_oauthlib.flow import Flow
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host  = request.headers.get("x-forwarded-host",  request.url.netloc)
        base  = f"{proto}://{host}"
        redirect_uri = f"{base}/api/hire/funis/youtube/callback"
        flow = Flow.from_client_config(
            {"web": {
                "client_id":     HIRE_YT_CLIENT_ID,
                "client_secret": HIRE_YT_CLIENT_SECRET,
                "redirect_uris": [redirect_uri],
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
            }},
            scopes=[
                "https://www.googleapis.com/auth/yt-analytics.readonly",
                "https://www.googleapis.com/auth/youtube.readonly",
            ],
            redirect_uri=redirect_uri,
        )
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="false",
            prompt="consent",
        )
        _yt_flow_store[state] = flow
        return JSONResponse({"auth_url": auth_url, "redirect_uri": redirect_uri})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hire/funis/youtube/callback")
async def hire_yt_callback(request: Request, code: str = None, error: str = None):
    """Handle YouTube OAuth2 callback — saves refresh_token to HF_Config sheet."""
    if error:
        return HTMLResponse(f"<html><body style='font-family:sans-serif;padding:40px'>"
                            f"<h3>Erro: {error}</h3></body></html>")
    if not code:
        return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px'>"
                            "<h3>Código de autorização não recebido.</h3></body></html>")
    try:
        import os as _os
        _os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
        state = request.query_params.get("state", "")
        flow = _yt_flow_store.pop(state, None)
        if flow is None:
            return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px'>"
                                "<h3>Sessão expirada. Tente autorizar novamente.</h3></body></html>")
        flow.fetch_token(code=code)
        creds = flow.credentials
        if creds.refresh_token:
            await asyncio.to_thread(_hf_yt_save_token, creds.refresh_token)
        return HTMLResponse("""
            <html><body style="font-family:sans-serif;text-align:center;padding:60px;
                               background:#1a0a28;color:#fff">
              <h2 style="color:#4ade80">✓ YouTube Analytics autorizado!</h2>
              <p style="color:rgba(255,255,255,.7)">Pode fechar esta aba e recarregar o dashboard.</p>
              <script>setTimeout(()=>window.close(),3000)</script>
            </body></html>
        """)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Site Tab ──────────────────────────────────────────────────────────────────

def _hf_fetch_site_tab(date_start=None, date_end=None) -> dict:
    """Site tab: GADS Search campaigns + site-palavras sheet fallback."""
    today = datetime.today().date()
    ds = date_start.isoformat() if date_start else "2020-01-01"
    de = date_end.isoformat()   if date_end   else today.isoformat()

    kpis = {"investido": 0.0, "conversoes": 0, "cliques": 0,
            "cpl": 0.0, "ctr": 0.0, "cpm": 0.0}
    serie_diaria: list = []
    keywords:     list = []
    campanhas:    list = []

    # ── Google Ads: Search campaigns ──────────────────────────────────────────
    if all([GADS_DEVELOPER_TOKEN, GADS_CLIENT_ID, GADS_CLIENT_SECRET, GADS_REFRESH_TOKEN]):
        from google.ads.googleads.client import GoogleAdsClient
        gads_cfg = {
            "developer_token": GADS_DEVELOPER_TOKEN,
            "client_id":       GADS_CLIENT_ID,
            "client_secret":   GADS_CLIENT_SECRET,
            "refresh_token":   GADS_REFRESH_TOKEN,
            "use_proto_plus":  True,
        }
        client = GoogleAdsClient.load_from_dict(gads_cfg)
        ga_svc = client.get_service("GoogleAdsService")
        dc = f"segments.date BETWEEN '{ds}' AND '{de}'"
        sf = "campaign.advertising_channel_type = 'SEARCH'"

        # Q1: Daily KPIs
        try:
            q1 = f"""
                SELECT segments.date, metrics.cost_micros, metrics.impressions,
                       metrics.clicks, metrics.conversions
                FROM campaign
                WHERE {dc} AND {sf}
            """
            daily: dict = {}
            for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q1):
                day = r.segments.date
                d = daily.setdefault(day, {"cost": 0, "imp": 0, "cli": 0, "conv": 0.0})
                d["cost"] += r.metrics.cost_micros
                d["imp"]  += r.metrics.impressions
                d["cli"]  += r.metrics.clicks
                d["conv"] += r.metrics.conversions

            tot_cost = sum(d["cost"] for d in daily.values())
            tot_imp  = sum(d["imp"]  for d in daily.values())
            tot_cli  = sum(d["cli"]  for d in daily.values())
            tot_conv = sum(d["conv"] for d in daily.values())
            investido = tot_cost / 1_000_000
            kpis.update({
                "investido":   round(investido, 2),
                "impressoes":  int(tot_imp),
                "cliques":     int(tot_cli),
                "ctr":         round(tot_cli / tot_imp * 100, 2) if tot_imp else 0.0,
                "conversoes":  int(tot_conv),
                "cpl":         round(investido / tot_conv, 2) if tot_conv else 0.0,
            })
            serie_diaria = [
                {
                    "date": k,
                    "conv": round(v["conv"], 1),
                    "cpl":  round(v["cost"] / 1_000_000 / v["conv"], 2) if v["conv"] else 0.0,
                }
                for k, v in sorted(daily.items())
            ]
        except Exception as e:
            kpis["q1_error"] = str(e)

        # Q2: Campaign breakdown
        try:
            q2 = f"""
                SELECT campaign.name, metrics.cost_micros, metrics.impressions,
                       metrics.clicks, metrics.conversions
                FROM campaign
                WHERE {dc} AND {sf} AND metrics.impressions > 0
            """
            camp_map: dict = {}
            for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q2):
                name = r.campaign.name
                cm = camp_map.setdefault(name, {"cost": 0, "imp": 0, "cli": 0, "conv": 0.0})
                cm["cost"] += r.metrics.cost_micros
                cm["imp"]  += r.metrics.impressions
                cm["cli"]  += r.metrics.clicks
                cm["conv"] += r.metrics.conversions
            for name, v in camp_map.items():
                valor = round(v["cost"] / 1_000_000, 2)
                conv  = int(v["conv"])
                campanhas.append({
                    "campanha":   name,
                    "investido":  valor,
                    "cliques":    int(v["cli"]),
                    "impressoes": int(v["imp"]),
                    "conversoes": conv,
                    "cpl": round(valor / conv, 2) if conv else None,
                    "ctr": round(v["cli"] / v["imp"] * 100, 2) if v["imp"] else 0.0,
                })
            campanhas.sort(key=lambda x: x["investido"], reverse=True)
        except Exception:
            pass

        # Q3: Keyword table
        try:
            q3 = f"""
                SELECT
                    ad_group_criterion.keyword.text,
                    metrics.cost_micros,
                    metrics.impressions,
                    metrics.clicks,
                    metrics.conversions,
                    metrics.average_cpc
                FROM keyword_view
                WHERE {dc} AND {sf} AND metrics.impressions > 0
            """
            kw_map: dict = {}
            for r in ga_svc.search(customer_id=GADS_HIRE_CUSTOMER_ID, query=q3):
                kw = r.ad_group_criterion.keyword.text
                kd = kw_map.setdefault(kw, {"cost": 0, "imp": 0, "cli": 0, "conv": 0.0})
                kd["cost"] += r.metrics.cost_micros
                kd["imp"]  += r.metrics.impressions
                kd["cli"]  += r.metrics.clicks
                kd["conv"] += r.metrics.conversions
            for kw, v in kw_map.items():
                valor = round(v["cost"] / 1_000_000, 2)
                conv  = int(v["conv"])
                keywords.append({
                    "keyword":    kw,
                    "investido":  valor,
                    "impressoes": int(v["imp"]),
                    "cliques":    int(v["cli"]),
                    "cpc":        round(v["cost"] / 1_000_000 / v["cli"], 2) if v["cli"] else 0.0,
                    "ctr":        round(v["cli"] / v["imp"] * 100, 2) if v["imp"] else 0.0,
                    "leads":      conv,
                    "cpl":        round(valor / conv, 2) if conv else None,
                })
            keywords.sort(key=lambda x: x["investido"], reverse=True)
        except Exception:
            pass

    # ── Sheet fallback: site-palavras (used when GADS returns no keywords) ────
    if not keywords:
        try:
            rows = _hf_get(HIRE_FUNIS_SHEET_ID, "site-palavras")

            if len(rows) > 1:
                header = [h.strip().lower() for h in rows[0]]
                def _ci(*names):
                    for n in names:
                        for i, h in enumerate(header):
                            if n in h: return i
                    return -1
                i_kw  = _ci("palavra", "keyword", "chave")
                i_inv = _ci("investido", "custo", "cost", "gasto")
                i_imp = _ci("impressao", "impression")
                i_cli = _ci("clique", "click")
                i_cpc = _ci("cpc")
                i_ctr = _ci("ctr")
                i_ld  = _ci("lead", "conv")
                i_cpl = _ci("cpl", "custo por lead")
                # default positional fallback if header not found
                if i_kw  < 0: i_kw  = 0
                if i_inv < 0: i_inv = 1
                if i_imp < 0: i_imp = 2
                if i_cli < 0: i_cli = 3
                if i_cpc < 0: i_cpc = 4
                if i_ctr < 0: i_ctr = 6
                if i_ld  < 0: i_ld  = 7
                if i_cpl < 0: i_cpl = 8

                def _v(row, idx):
                    return row[idx] if idx >= 0 and idx < len(row) else ""

                for row in rows[1:]:
                    kw = _v(row, i_kw)
                    if not kw: continue
                    inv  = _hf_parse_num(_v(row, i_inv))
                    imp  = int(_hf_parse_num(_v(row, i_imp)))
                    cli  = int(_hf_parse_num(_v(row, i_cli)))
                    cpc  = _hf_parse_num(_v(row, i_cpc))
                    ctr  = _hf_parse_num(_v(row, i_ctr))
                    ld   = int(_hf_parse_num(_v(row, i_ld)))
                    cpl_v = _hf_parse_num(_v(row, i_cpl))
                    keywords.append({
                        "keyword":    kw,
                        "investido":  inv,
                        "impressoes": imp,
                        "cliques":    cli,
                        "cpc":        cpc,
                        "ctr":        ctr,
                        "leads":      ld,
                        "cpl":        cpl_v if cpl_v else None,
                    })
        except Exception:
            pass

    return {
        "kpis":      kpis,
        "serie":     serie_diaria,
        "keywords":  keywords,
        "campanhas": campanhas,
    }


@app.post("/api/hire/funis/cache/clear")
async def clear_hire_funis_cache(request: Request):
    _hf_cache_clear()
    return JSONResponse({"ok": True, "cleared": True})

@app.get("/api/hire/funis/site")
async def get_hire_funis_site(
    request: Request,
    date_start: str = None,
    date_end:   str = None,
):
    try:
        ds = datetime.strptime(date_start, "%Y-%m-%d").date() if date_start else None
        de = datetime.strptime(date_end,   "%Y-%m-%d").date() if date_end   else None
        data = await asyncio.to_thread(_hf_fetch_site_tab, ds, de)
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


@app.get("/logs", response_class=HTMLResponse)
async def view_logs(request: Request):
    if not verify_session(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse("logs.html", {"request": request, "convs": list_conversations(), **_nav_base(request, "logs")})
