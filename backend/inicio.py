"""
Tela de Início do Sinapse — dashboard executivo.

Lê os clientes do Painel de Clientes (já sincronizado do ClickUp pelo
sync_clickup.py) e calcula KPIs em tempo real:
  - Clientes ativos
  - MRR total (soma de Fee Mensal de quem está com status='ativo')
  - Fee por cliente (pra lista de saúde)

MRR em risco e saúde do cliente ainda são placeholders — campos
manuais (hab_health-style) virão em versão futura quando definirmos
o critério.
"""

import json
import re
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backend.gestao import _verify
from backend.db import get_conn, dict_cursor

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))


def _parse_money(v) -> float:
    """Aceita formatos R$ 1.347,00, 1347, "1.347,00", 1347.0, etc."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    # Remove R$, espaços
    s = re.sub(r"[Rr]\$\s*", "", s)
    s = s.replace(" ", "")
    # Caso BR: "1.347,00" → tira ponto de milhar, troca vírgula por ponto
    if "," in s and s.rfind(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")
    else:
        # Caso EN ou inteiro: "1347.00" ou "1347" — só remove vírgulas
        s = s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return 0.0


def _find_painel():
    """Acha a lista 'Painel de Clientes' e retorna (id, custom_fields_dict)."""
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        for name in ("Painel de Clientes", "Painel de Cliente", "Painel de clientes"):
            cur.execute(
                "SELECT id, custom_fields FROM lists WHERE LOWER(name) = LOWER(%s) LIMIT 1",
                (name,),
            )
            row = cur.fetchone()
            if row:
                cf_raw = row.get("custom_fields") or "[]"
                try:
                    cf_defs = json.loads(cf_raw) if isinstance(cf_raw, str) else cf_raw
                except Exception:
                    cf_defs = []
                # Mapeia nome do campo → id
                cf_by_name = {}
                for cf in (cf_defs or []):
                    if isinstance(cf, dict):
                        nm = (cf.get("name") or "").strip().lower()
                        if nm:
                            cf_by_name[nm] = cf.get("id")
                return row["id"], cf_by_name
        return None, {}
    finally:
        cur.close()
        conn.close()


def _parse_sigla_nome(title: str):
    m = re.match(r"^\s*\[([A-Za-z0-9]+)\]\s*(.+?)\s*$", title or "")
    if m:
        return m.group(1).upper(), m.group(2).strip()
    return "?", (title or "").strip()


@router.get("/inicio", response_class=HTMLResponse)
async def page_inicio(request: Request):
    user = _verify(request)
    if not user:
        return RedirectResponse("/login")
    from backend.main import _nav_base
    return templates.TemplateResponse(
        "inicio.html",
        {"request": request, **_nav_base(request, "inicio")},
    )


@router.get("/api/inicio/overview")
async def overview(request: Request):
    user = _verify(request)
    if not user:
        raise HTTPException(401)

    list_id, cf_by_name = _find_painel()
    fee_field_id = cf_by_name.get("fee mensal") or cf_by_name.get("fee")

    clients = []
    if list_id:
        conn = get_conn()
        cur = dict_cursor(conn)
        try:
            cur.execute(
                "SELECT title, status, cf_values FROM tasks WHERE list_id=%s ORDER BY title",
                (list_id,),
            )
            rows = cur.fetchall() or []
        finally:
            cur.close()
            conn.close()

        for r in rows:
            status = (r.get("status") or "").strip().lower()
            if status != "ativo":
                continue
            sigla, nome = _parse_sigla_nome(r.get("title") or "")
            fee = 0.0
            if fee_field_id:
                try:
                    cfv = json.loads(r.get("cf_values") or "{}")
                except Exception:
                    cfv = {}
                fee = _parse_money(cfv.get(fee_field_id))
            clients.append({
                "sigla": sigla,
                "nome": nome,
                "fee_mensal": round(fee, 2),
                # Placeholders pra versão futura — colocar como flags manuais depois
                "saude": None,        # 0..100 ou None
                "saude_label": "—",   # "Boa" | "Média" | "Ruim" | "—"
                "responsavel": "",
                "em_risco": False,
            })

    mrr_total = round(sum(c["fee_mensal"] for c in clients), 2)
    mrr_risco = round(sum(c["fee_mensal"] for c in clients if c["em_risco"]), 2)

    # Tarefas — placeholder até integrar ClickUp real-time
    tarefas = {"hoje": 0, "atrasadas": 0, "sem_contato_7d": 0, "abertas": 0}

    return {
        "user": user,
        "kpis": {
            "clientes_ativos": len(clients),
            "mrr_total": mrr_total,
            "mrr_risco": mrr_risco,
            "tarefas_abertas": tarefas["abertas"],
        },
        "foco": tarefas,
        "clients": clients,
        "alertas": [],   # lista futura: clientes em risco, em vácuo, etc
    }
