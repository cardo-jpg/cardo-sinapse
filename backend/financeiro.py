import io
import csv
import os
import json
import re
import calendar
import time as _time
from datetime import date, datetime
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import anthropic as _anthropic

from backend.gestao import _require, _verify
from backend.db import get_conn, dict_cursor

router = APIRouter()

BASE_DIR  = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    return get_conn()


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_clientes (
                id            SERIAL PRIMARY KEY,
                tipo          TEXT    DEFAULT 'empresa',
                nome_fantasia TEXT    NOT NULL,
                razao_social  TEXT,
                cnpj          TEXT,
                cpf           TEXT,
                ie            TEXT,
                im            TEXT,
                email         TEXT,
                telefone      TEXT,
                endereco      TEXT,
                situacao      TEXT    DEFAULT 'ativo',
                created_at    TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_fornecedores (
                id            SERIAL PRIMARY KEY,
                tipo          TEXT    DEFAULT 'empresa',
                nome_fantasia TEXT    NOT NULL,
                razao_social  TEXT,
                cnpj          TEXT,
                cpf           TEXT,
                ie            TEXT,
                im            TEXT,
                email         TEXT,
                telefone      TEXT,
                endereco      TEXT,
                situacao      TEXT    DEFAULT 'ativo',
                created_at    TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_contas (
                id            SERIAL PRIMARY KEY,
                nome          TEXT    NOT NULL,
                situacao      TEXT    DEFAULT 'ativa',
                saldo_inicial REAL    DEFAULT 0,
                created_at    TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_categorias (
                id         SERIAL PRIMARY KEY,
                nome       TEXT NOT NULL,
                tipo       TEXT DEFAULT 'despesa' CHECK(tipo IN ('receita','despesa')),
                situacao   TEXT DEFAULT 'ativo',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_centros_custo (
                id         SERIAL PRIMARY KEY,
                nome       TEXT NOT NULL,
                situacao   TEXT DEFAULT 'ativo',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_lancamentos (
                id              SERIAL PRIMARY KEY,
                tipo            TEXT    NOT NULL CHECK(tipo IN ('receber','pagar')),
                emissao         TEXT    NOT NULL,
                contato_tipo    TEXT,
                contato_id      INTEGER,
                contato_nome    TEXT,
                descricao       TEXT    NOT NULL,
                vencimento      TEXT,
                valor           REAL    DEFAULT 0,
                acrescimo       REAL    DEFAULT 0,
                acrescimo_tipo  TEXT    DEFAULT 'R$',
                desconto        REAL    DEFAULT 0,
                desconto_tipo   TEXT    DEFAULT 'R$',
                valor_total     REAL    DEFAULT 0,
                conta_id        INTEGER REFERENCES fin_contas(id),
                categoria_id    INTEGER REFERENCES fin_categorias(id),
                centro_custo_id INTEGER REFERENCES fin_centros_custo(id),
                documento       TEXT,
                meio_pagamento  TEXT,
                situacao        TEXT    DEFAULT 'em_aberto' CHECK(situacao IN ('em_aberto','quitado')),
                origem          TEXT    DEFAULT 'manual',
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_transferencias (
                id            SERIAL PRIMARY KEY,
                emissao       TEXT    NOT NULL,
                valor         REAL    DEFAULT 0,
                de_conta_id   INTEGER NOT NULL REFERENCES fin_contas(id),
                para_conta_id INTEGER NOT NULL REFERENCES fin_contas(id),
                comentario    TEXT,
                created_at    TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_importacoes (
                id              SERIAL PRIMARY KEY,
                nome_arquivo    TEXT    NOT NULL,
                tipo_arquivo    TEXT    NOT NULL,
                conta_id        INTEGER REFERENCES fin_contas(id),
                total_itens     INTEGER DEFAULT 0,
                itens_aprovados INTEGER DEFAULT 0,
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_importacao_itens (
                id                 SERIAL PRIMARY KEY,
                importacao_id      INTEGER NOT NULL REFERENCES fin_importacoes(id) ON DELETE CASCADE,
                data_lancamento    TEXT,
                descricao          TEXT,
                valor              REAL,
                tipo               TEXT CHECK(tipo IN ('receita','despesa')),
                categoria_sugerida TEXT,
                categoria_id       INTEGER REFERENCES fin_categorias(id),
                conta_id           INTEGER REFERENCES fin_contas(id),
                status             TEXT DEFAULT 'pendente' CHECK(status IN ('pendente','aprovado','rejeitado')),
                lancamento_id      INTEGER REFERENCES fin_lancamentos(id),
                created_at         TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_colaboradores (
                id            SERIAL PRIMARY KEY,
                tipo          TEXT    DEFAULT 'pessoa',
                nome_fantasia TEXT    NOT NULL,
                cargo         TEXT,
                cpf           TEXT,
                email         TEXT,
                telefone      TEXT,
                situacao      TEXT    DEFAULT 'ativo',
                created_at    TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fin_cc_regras (
                id           SERIAL PRIMARY KEY,
                merchant     TEXT    NOT NULL UNIQUE,
                categoria_id INTEGER REFERENCES fin_categorias(id) ON DELETE SET NULL
            )
        """)
        conn.commit()

        cur.execute("ALTER TABLE fin_importacoes ADD COLUMN IF NOT EXISTS mes_referencia TEXT")

        # Seed clientes/fornecedores if tables are empty (first deploy)
        dcur = dict_cursor(conn)
        dcur.execute("SELECT COUNT(*) FROM fin_clientes")
        if dcur.fetchone()["count"] == 0:
            clientes = [
                ('empresa','Conexão Cirurgica','Risebook Ensino em Saúde LTDA','51.911.424/0001-49',None,'brunolsramos@hotmail.com','Comercial: (21) 98768-2006'),
                ('empresa','DFT Logística','D Freire transportes e Logística Eireli Me','22.225.052.0001-07',None,None,'Comercial: (11) 99282-9484'),
                ('empresa','Gráfica NF','F. Floriani Gráfica Edt. LTDA','2087884000199',None,'marketing@graficanf.com.br','Comercial: (47) 3350-0382'),
                ('empresa','HISTORY MAKERS LTDA','HISTORY MAKERS LTDA','40.012.266/0001-79',None,'contato@historymakersgroup.com','Comercial: +351 915 710 135'),
                ('empresa','Ledo Mazzei Massoni Neto','Ledo Mazzei Massoni Neto',None,'223.971.228-71','ledo.massoni@gmail.com',None),
                ('empresa','Outros','Outros',None,None,None,None),
                ('empresa','Patrícia Vogtt','ABROADER COMPANY LTDA','62.192.333/0001-17',None,None,None),
                ('empresa','Scale Army','Scale Army',None,None,None,None),
                ('empresa','Sobral Ensino','Sobral Ensino','43.668.790/0001-90',None,None,None),
                ('empresa','Speedrack west','Speedrack west',None,None,None,None),
            ]
            for row in clientes:
                cur.execute(
                    "INSERT INTO fin_clientes (tipo,nome_fantasia,razao_social,cnpj,cpf,email,telefone) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    row,
                )

        dcur.execute("SELECT COUNT(*) FROM fin_fornecedores")
        if dcur.fetchone()["count"] == 0:
            fornecedores = [
                ('empresa','ARCHER','ARCHER',None,None,None,None),
                ('empresa','Arthur Manrchi','Perfil',None,None,None,None),
                ('empresa','Auto Giro','AutoGiro','317690000180',None,None,None),
                ('empresa','C6 Bank','C6 Bank',None,None,None,None),
                ('empresa','Cadu Nunes','Cadu Nunes',None,None,None,None),
                ('empresa','Celesc','Celesc',None,None,None,None),
                ('empresa','Conta Agil contabilidade','Conta Agil contabilidade',None,None,None,None),
                ('empresa','Debora Martins (Head Hunter)','Debora Martins (Head Hunter)','52.723.349/0001-55',None,None,None),
                ('empresa','Delapel Comercio de Material para Escritório LTDA','Delapel Comercio de Material para Escritório LTDA','27.141.743/0001-38',None,None,None),
                ('empresa','Imobiliária Teresinha Baron','Imobiliária Teresinha Baron',None,None,None,None),
                ('pessoa','Jadna da Rosa','Jadna da Rosa',None,'096.349.039-75',None,None),
                ('empresa','JN Administradora de Bens e Imoveis Próprios LTDA','JN Administradora de Bens e Imoveis Próprios LTDA','30.784.710/0001-10',None,'ivandro.jnadm@gmail.com','Comercial: (47) 99935-7246'),
                ('pessoa','José Luiz Dutra se Souza','Postmark','7797836973',None,None,'Comercial: (47) 99191-2539'),
                ('empresa','Larissa Schneider Keiber','Larissa Schneider Keiber','62.485.855/0001-07',None,None,None),
                ('empresa','Loja 10','Loja 10',None,None,None,None),
                ('empresa','LUAN MATHEUS','LUAN MATHEUS',None,None,None,None),
                ('empresa','Lujoe Uniformes','Lujoe Modas LTDA','08.051.645/0001-65',None,None,None),
                ('empresa','Mix Conect Telecon','Mix Conect Telecon',None,None,None,None),
                ('empresa','Nubank','Nubank',None,None,None,None),
                ('empresa','Obras','Obras',None,None,None,None),
                ('empresa','Outros','Outros',None,None,None,None),
                ('empresa','Ramper Desenvolvimento de Software ltda','Ramper Desenvolvimento de Software ltda','29138927000174',None,None,None),
                ('empresa','Receita Federal','Receita Federal',None,None,None,None),
                ('empresa','Renke Studio','Renke Studio','37079656000151',None,None,None),
                ('empresa','Sicoob Trento Credi','Sicoob Trento Credi',None,None,None,None),
                ('empresa','SIGA SISTEMA','SIGA SISTEMA',None,None,None,None),
                ('empresa','Site','Site',None,None,None,None),
                ('empresa','Unifique','Unifique',None,None,None,None),
                ('empresa','ValeSat Segurança Eletronica','ValeSat Segurança Eletronica',None,None,None,None),
                ('pessoa','Victor Leopoldo Dognini Cardoso','Victor Leopoldo Dognini Cardoso',None,'097.734.979-92',None,None),
                ('empresa','Vidraçaria Azaleia','Vidraçaria Azaleia',None,None,None,None),
                ('empresa','VIVO CELULAR','VIVO',None,None,None,None),
                ('empresa','Yes brindes','Yes brindes',None,None,None,None),
            ]
            for row in fornecedores:
                cur.execute(
                    "INSERT INTO fin_fornecedores (tipo,nome_fantasia,razao_social,cnpj,cpf,email,telefone) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    row,
                )
        # Seed centros de custo SIGA (idempotente — insere só se nome não existe)
        _SIGA_CENTROS = [
            'Despesa Comissão', 'Despesa de Infraestrutura', 'Despesa de Treinamento',
            'Despesa Financeira', 'Despesa Geral e Insumos', 'Despesa Operacional',
            'Despesa Tributária', 'Despesas Equipe', 'Despesas Fixas',
            'Remuneração Administrativa', 'Sem Centro de Custos',
        ]
        dcur.execute("SELECT nome FROM fin_centros_custo")
        existing = {r['nome'] for r in dcur.fetchall()}
        for nome in _SIGA_CENTROS:
            if nome not in existing:
                dcur.execute("INSERT INTO fin_centros_custo (nome) VALUES (%s)", (nome,))

        # Seed categorias padrão (idempotente — insere só se nome não existe)
        _DEFAULT_CATS = [
            ('Software / Ferramentas',  'despesa'),
            ('IA e Automação',          'despesa'),
            ('Serviços e Taxas',        'despesa'),
            ('Hospedagem / Infra',      'despesa'),
            ('Honorários',              'despesa'),
            ('Publicidade / Mídia',     'despesa'),
            ('Folha de Pagamento',      'despesa'),
            ('Impostos e Taxas',        'despesa'),
            ('Alimentação',             'despesa'),
            ('Transporte',              'despesa'),
            ('Outros',                  'despesa'),
            ('Receita de Clientes',     'receita'),
        ]
        dcur.execute("SELECT nome FROM fin_categorias")
        existing_cats = {r['nome'] for r in dcur.fetchall()}
        for nome, tipo in _DEFAULT_CATS:
            if nome not in existing_cats:
                dcur.execute(
                    "INSERT INTO fin_categorias (nome, tipo, situacao) VALUES (%s,%s,'ativa')",
                    (nome, tipo),
                )

        dcur.close()
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ── Internal helpers ──────────────────────────────────────────────────────────

_CONTATO_FIELDS = (
    "tipo", "nome_fantasia", "razao_social", "cnpj", "cpf",
    "ie", "im", "email", "telefone", "endereco", "situacao",
)

_LANCAMENTO_FIELDS = (
    "tipo", "emissao", "contato_tipo", "contato_id", "contato_nome",
    "descricao", "vencimento", "valor", "acrescimo", "acrescimo_tipo",
    "desconto", "desconto_tipo", "valor_total", "conta_id", "categoria_id",
    "centro_custo_id", "meio_pagamento", "situacao",
)


def _compute_valor_total(valor, acrescimo, acrescimo_tipo, desconto, desconto_tipo):
    base = float(valor or 0)
    ac   = float(acrescimo or 0)
    dc   = float(desconto or 0)
    if acrescimo_tipo == "%":
        ac = base * ac / 100
    if desconto_tipo == "%":
        dc = base * dc / 100
    return round(base + ac - dc, 2)


def _extract_contato(data: dict) -> dict:
    return {k: data.get(k) for k in _CONTATO_FIELDS if k in data}


def _extract_lancamento(data: dict) -> dict:
    fields = {k: data.get(k) for k in _LANCAMENTO_FIELDS if k in data}
    if any(k in data for k in ("valor", "acrescimo", "acrescimo_tipo", "desconto", "desconto_tipo")):
        fields["valor_total"] = _compute_valor_total(
            fields.get("valor", data.get("valor", 0)),
            fields.get("acrescimo", data.get("acrescimo", 0)),
            fields.get("acrescimo_tipo", data.get("acrescimo_tipo", "R$")),
            fields.get("desconto", data.get("desconto", 0)),
            fields.get("desconto_tipo", data.get("desconto_tipo", "R$")),
        )
    return fields


def _build_set(fields: dict):
    set_clause = ", ".join(f"{k}=%s" for k in fields)
    return set_clause, list(fields.values())


# ── Parcelamento ──────────────────────────────────────────────────────────────

_PARCELA_SUFFIX_RE = re.compile(r"\s+\d+/\d+\s*$")


def _strip_parcela_suffix(desc: str) -> str:
    """Remove um sufixo ' x/N' do fim da descrição, para não duplicar (ex.: 'Teste 1/3 1/3')."""
    return _PARCELA_SUFFIX_RE.sub("", desc or "").strip()


def _add_months(iso_date: str, months: int) -> str:
    """Soma `months` meses a uma data ISO 'YYYY-MM-DD', preservando o dia
    (com clamp para o último dia do mês quando o dia não existir, ex.: 31 → 30)."""
    y, m, d = (int(x) for x in str(iso_date)[:10].split("-"))
    total = (m - 1) + months
    y2 = y + total // 12
    m2 = total % 12 + 1
    d2 = min(d, calendar.monthrange(y2, m2)[1])
    return date(y2, m2, d2).isoformat()


def _parse_parcelas(data: dict):
    """Retorna o nº de parcelas (>=2) quando o lançamento é parcelado válido; senão None."""
    if (data.get("vencimento_tipo") or "uma_vez") != "parcelado":
        return None
    try:
        n = int(data.get("num_parcelas") or 0)
    except (TypeError, ValueError):
        return None
    return n if 2 <= n <= 420 else None


# ── Generic CRUD for contato tables (clientes / fornecedores) ─────────────────

def _list_contatos(table: str, situacao=None, q=None):
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        sql    = f"SELECT * FROM {table} WHERE 1=1"
        params = []
        if situacao:
            sql += " AND situacao=%s"
            params.append(situacao)
        if q:
            sql += " AND nome_fantasia LIKE %s"
            params.append(f"%{q}%")
        sql += " ORDER BY nome_fantasia"
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return rows


def _get_contato(table: str, cid: int):
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"SELECT * FROM {table} WHERE id=%s", (cid,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Registro não encontrado")
    return dict(row)


def _create_contato(table: str, data: dict):
    nome = (data.get("nome_fantasia") or "").strip()
    if not nome:
        raise HTTPException(400, "nome_fantasia é obrigatório")
    fields = _extract_contato(data)
    fields["nome_fantasia"] = nome
    cols         = ", ".join(fields.keys())
    placeholders = ", ".join("%s" for _ in fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) RETURNING id",
            list(fields.values()),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.execute(f"SELECT * FROM {table} WHERE id=%s", (new_id,))
        row = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return row


def _update_contato(table: str, cid: int, data: dict):
    fields = _extract_contato(data)
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    if "nome_fantasia" in fields:
        fields["nome_fantasia"] = (fields["nome_fantasia"] or "").strip()
        if not fields["nome_fantasia"]:
            raise HTTPException(400, "nome_fantasia não pode ser vazio")
    set_clause, vals = _build_set(fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE {table} SET {set_clause} WHERE id=%s", vals + [cid])
        conn.commit()
        cur.execute(f"SELECT * FROM {table} WHERE id=%s", (cid,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Registro não encontrado")
    return dict(row)


def _delete_contato(table: str, cid: int):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(f"DELETE FROM {table} WHERE id=%s", (cid,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


# ── Page route ────────────────────────────────────────────────────────────────

@router.get("/financeiro", response_class=HTMLResponse)
async def page_financeiro(request: Request):
    user = _verify(request)
    if not user:
        return RedirectResponse("/login")
    resp = templates.TemplateResponse("financeiro.html", {
        "request": request,
        "active_page": "financeiro",
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


# ── Clientes ──────────────────────────────────────────────────────────────────

@router.get("/api/fin/clientes")
async def api_list_clientes(request: Request, situacao: str = None, q: str = None):
    _require(request)
    return {"clientes": _list_contatos("fin_clientes", situacao, q)}


@router.post("/api/fin/clientes")
async def api_create_cliente(request: Request):
    _require(request)
    data = await request.json()
    return {"cliente": _create_contato("fin_clientes", data)}


@router.get("/api/fin/clientes/{cid}")
async def api_get_cliente(cid: int, request: Request):
    _require(request)
    return {"cliente": _get_contato("fin_clientes", cid)}


@router.put("/api/fin/clientes/{cid}")
async def api_update_cliente(cid: int, request: Request):
    _require(request)
    data = await request.json()
    return {"cliente": _update_contato("fin_clientes", cid, data)}


@router.delete("/api/fin/clientes/{cid}")
async def api_delete_cliente(cid: int, request: Request):
    _require(request)
    return _delete_contato("fin_clientes", cid)


# ── Fornecedores ──────────────────────────────────────────────────────────────

@router.get("/api/fin/fornecedores")
async def api_list_fornecedores(request: Request, situacao: str = None, q: str = None):
    _require(request)
    return {"fornecedores": _list_contatos("fin_fornecedores", situacao, q)}


@router.post("/api/fin/fornecedores")
async def api_create_fornecedor(request: Request):
    _require(request)
    data = await request.json()
    return {"fornecedor": _create_contato("fin_fornecedores", data)}


@router.get("/api/fin/fornecedores/{fid}")
async def api_get_fornecedor(fid: int, request: Request):
    _require(request)
    return {"fornecedor": _get_contato("fin_fornecedores", fid)}


@router.put("/api/fin/fornecedores/{fid}")
async def api_update_fornecedor(fid: int, request: Request):
    _require(request)
    data = await request.json()
    return {"fornecedor": _update_contato("fin_fornecedores", fid, data)}


@router.delete("/api/fin/fornecedores/{fid}")
async def api_delete_fornecedor(fid: int, request: Request):
    _require(request)
    return _delete_contato("fin_fornecedores", fid)


# ── Colaboradores ─────────────────────────────────────────────────────────────

_COLABORADOR_FIELDS = ("tipo", "nome_fantasia", "cargo", "cpf", "email", "telefone", "situacao")

@router.get("/api/fin/colaboradores")
async def api_list_colaboradores(request: Request, situacao: str = None, q: str = None):
    _require(request)
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        sql = "SELECT * FROM fin_colaboradores WHERE 1=1"
        params = []
        if situacao:
            sql += " AND situacao=%s"; params.append(situacao)
        if q:
            sql += " AND (nome_fantasia ILIKE %s OR cargo ILIKE %s)"
            params += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY nome_fantasia"
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()
    return {"colaboradores": rows}

@router.post("/api/fin/colaboradores")
async def api_create_colaborador(request: Request):
    _require(request)
    data = await request.json()
    nome = (data.get("nome_fantasia") or "").strip()
    if not nome:
        raise HTTPException(400, "nome_fantasia é obrigatório")
    fields = {k: data.get(k) for k in _COLABORADOR_FIELDS if k in data}
    fields["nome_fantasia"] = nome
    cols = ", ".join(fields.keys())
    phs  = ", ".join("%s" for _ in fields)
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute(f"INSERT INTO fin_colaboradores ({cols}) VALUES ({phs}) RETURNING id", list(fields.values()))
        new_id = cur.fetchone()["id"]; conn.commit()
        cur.execute("SELECT * FROM fin_colaboradores WHERE id=%s", (new_id,))
        row = dict(cur.fetchone())
    finally:
        cur.close(); conn.close()
    return {"colaborador": row}

@router.get("/api/fin/colaboradores/{cid}")
async def api_get_colaborador(cid: int, request: Request):
    _require(request)
    return {"colaborador": _get_contato("fin_colaboradores", cid)}

@router.put("/api/fin/colaboradores/{cid}")
async def api_update_colaborador(cid: int, request: Request):
    _require(request)
    data = await request.json()
    fields = {k: data[k] for k in _COLABORADOR_FIELDS if k in data}
    if not fields: raise HTTPException(400, "Nenhum campo válido")
    set_clause, vals = _build_set(fields)
    conn = get_conn(); cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE fin_colaboradores SET {set_clause} WHERE id=%s", vals + [cid])
        conn.commit()
        cur.execute("SELECT * FROM fin_colaboradores WHERE id=%s", (cid,))
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not row: raise HTTPException(404, "Colaborador não encontrado")
    return {"colaborador": dict(row)}

@router.delete("/api/fin/colaboradores/{cid}")
async def api_delete_colaborador(cid: int, request: Request):
    _require(request)
    return _delete_contato("fin_colaboradores", cid)


# ── Contas ────────────────────────────────────────────────────────────────────

@router.get("/api/fin/contas")
async def api_list_contas(request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM fin_contas WHERE situacao='ativa' ORDER BY nome")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"contas": rows}


@router.post("/api/fin/contas")
async def api_create_conta(request: Request):
    _require(request)
    data = await request.json()
    nome = (data.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "INSERT INTO fin_contas (nome, situacao, saldo_inicial) VALUES (%s,%s,%s) RETURNING id",
            (nome, data.get("situacao", "ativa"), float(data.get("saldo_inicial") or 0)),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.execute("SELECT * FROM fin_contas WHERE id=%s", (new_id,))
        row = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"conta": row}


@router.put("/api/fin/contas/{cid}")
async def api_update_conta(cid: int, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"nome", "situacao", "saldo_inicial"}
    fields  = {k: data[k] for k in allowed if k in data}
    if "nome" in fields:
        fields["nome"] = (fields["nome"] or "").strip()
        if not fields["nome"]:
            raise HTTPException(400, "nome não pode ser vazio")
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    set_clause, vals = _build_set(fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE fin_contas SET {set_clause} WHERE id=%s", vals + [cid])
        conn.commit()
        cur.execute("SELECT * FROM fin_contas WHERE id=%s", (cid,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Conta não encontrada")
    return {"conta": dict(row)}


@router.delete("/api/fin/contas/{cid}")
async def api_delete_conta(cid: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT COUNT(*) FROM fin_lancamentos WHERE conta_id=%s", (cid,))
        in_use = cur.fetchone()["count"]
        if in_use:
            raise HTTPException(409, "Conta em uso em lançamentos — não é possível excluir")
        cur.execute("DELETE FROM fin_contas WHERE id=%s", (cid,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


# ── Categorias ────────────────────────────────────────────────────────────────

@router.get("/api/fin/categorias")
async def api_list_categorias(request: Request, tipo: str = None):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        sql    = "SELECT * FROM fin_categorias WHERE 1=1"
        params = []
        if tipo in ("receita", "despesa"):
            sql += " AND tipo=%s"
            params.append(tipo)
        sql += " ORDER BY nome"
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"categorias": rows}


@router.post("/api/fin/categorias")
async def api_create_categoria(request: Request):
    _require(request)
    data = await request.json()
    nome = (data.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    tipo = data.get("tipo", "despesa")
    if tipo not in ("receita", "despesa"):
        raise HTTPException(400, "tipo deve ser 'receita' ou 'despesa'")
    situacao = data.get("situacao", "ativo")
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "INSERT INTO fin_categorias (nome, tipo, situacao) VALUES (%s,%s,%s) RETURNING id",
            (nome, tipo, situacao),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.execute("SELECT * FROM fin_categorias WHERE id=%s", (new_id,))
        row = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"categoria": row}


@router.put("/api/fin/categorias/{cid}")
async def api_update_categoria(cid: int, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"nome", "tipo", "situacao"}
    fields  = {k: data[k] for k in allowed if k in data}
    if "nome" in fields:
        fields["nome"] = (fields["nome"] or "").strip()
        if not fields["nome"]:
            raise HTTPException(400, "nome não pode ser vazio")
    if "tipo" in fields and fields["tipo"] not in ("receita", "despesa"):
        raise HTTPException(400, "tipo deve ser 'receita' ou 'despesa'")
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    set_clause, vals = _build_set(fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE fin_categorias SET {set_clause} WHERE id=%s", vals + [cid])
        conn.commit()
        cur.execute("SELECT * FROM fin_categorias WHERE id=%s", (cid,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Categoria não encontrada")
    return {"categoria": dict(row)}


@router.delete("/api/fin/categorias/{cid}")
async def api_delete_categoria(cid: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM fin_categorias WHERE id=%s", (cid,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


# ── Centros de Custo ──────────────────────────────────────────────────────────

@router.get("/api/fin/centros")
async def api_list_centros(request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM fin_centros_custo ORDER BY nome")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"centros": rows}


@router.post("/api/fin/centros")
async def api_create_centro(request: Request):
    _require(request)
    data = await request.json()
    nome = (data.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    situacao = data.get("situacao", "ativo")
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "INSERT INTO fin_centros_custo (nome, situacao) VALUES (%s,%s) RETURNING id",
            (nome, situacao),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.execute("SELECT * FROM fin_centros_custo WHERE id=%s", (new_id,))
        row = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"centro": row}


@router.put("/api/fin/centros/{cid}")
async def api_update_centro(cid: int, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"nome", "situacao"}
    fields  = {k: data[k] for k in allowed if k in data}
    if "nome" in fields:
        fields["nome"] = (fields["nome"] or "").strip()
        if not fields["nome"]:
            raise HTTPException(400, "nome não pode ser vazio")
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    set_clause, vals = _build_set(fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE fin_centros_custo SET {set_clause} WHERE id=%s", vals + [cid])
        conn.commit()
        cur.execute("SELECT * FROM fin_centros_custo WHERE id=%s", (cid,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Centro de custo não encontrado")
    return {"centro": dict(row)}


@router.delete("/api/fin/centros/{cid}")
async def api_delete_centro(cid: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM fin_centros_custo WHERE id=%s", (cid,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


# ── Lançamentos ───────────────────────────────────────────────────────────────

_LANCAMENTO_JOIN = """
    SELECT
        l.*,
        c.nome   AS conta_nome,
        cat.nome AS categoria_nome,
        cc.nome  AS centro_nome
    FROM fin_lancamentos l
    LEFT JOIN fin_contas        c   ON c.id   = l.conta_id
    LEFT JOIN fin_categorias    cat ON cat.id  = l.categoria_id
    LEFT JOIN fin_centros_custo cc  ON cc.id   = l.centro_custo_id
"""

_MESES_ABBR_LIST = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']

# ── SIGA Google Sheets — fonte de verdade ─────────────────────────────────────

SIGA_SHEET_ID        = os.getenv("SIGA_SHEET_ID", "1mcZiIOsI2jLC_A-rSUuVFCEkXIdihyqj2qMRjBiCzjU")
SIGA_MIGRATION_VER   = "v11"  # bump para forçar reimportação
_SIGA_ROWS: list      = []
_SIGA_ROWS_TS: float  = 0.0
_SIGA_ROWS_TTL: int   = 300


def _sheets_svc():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if raw.strip().startswith("{"):
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        path = raw or str(BASE_DIR / "service_account.json")
        creds = Credentials.from_service_account_file(path, scopes=scopes)
    return build("sheets", "v4", credentials=creds)


def _br_date(v) -> str | None:
    if not v or not str(v).strip():
        return None
    try:
        return datetime.strptime(str(v).strip(), "%d/%m/%Y").date().isoformat()
    except Exception:
        return None


def _br_val(v) -> float:
    if not v or not str(v).strip():
        return 0.0
    try:
        return float(str(v).strip().replace(".", "").replace(",", "."))
    except Exception:
        return 0.0


_SIGA_COL_ALIASES: dict = {
    'emissao':              'emissao',
    'emissão':              'emissao',
    'vencimento':           'vencimento',
    'quitado em':           'quitado_em',
    'situacao':             'situacao',
    'situação':             'situacao',
    'contato':              'contato_nome',
    'nome fantasia':        'contato_nome',
    'nome fantasia/apelido':'contato_nome',
    'descricao':            'descricao',
    'descrição':            'descricao',
    'categoria':            'categoria_nome',
    'centro de custos':     'centro_nome',
    'conta corrente':       'conta_nome',
    'receber':              'receber',
    'pagar':                'pagar',
}
# Fallback por índice fixo (formato antigo) se a coluna não for encontrada no header
_SIGA_COL_DEFAULTS: dict = {
    'emissao': 0, 'vencimento': 1, 'quitado_em': 2,
    'contato_nome': 5, 'descricao': 6, 'categoria_nome': 7,
    'centro_nome': 8, 'conta_nome': 9,
    'receber': 13, 'pagar': 14,
}


def _siga_build_col_map(header_row: list) -> dict:
    def _n(s): return (s or '').lower().strip().replace('ã','a').replace('é','e').replace('ê','e').replace('ç','c').replace('ó','o').replace('á','a').replace('â','a')
    col_map = {}
    for i, cell in enumerate(header_row):
        field = _SIGA_COL_ALIASES.get(_n(cell))
        if field and field not in col_map:
            col_map[field] = i
    for field, idx in _SIGA_COL_DEFAULTS.items():
        col_map.setdefault(field, idx)
    return col_map


def _fetch_siga_rows() -> list[dict]:
    global _SIGA_ROWS, _SIGA_ROWS_TS
    now = _time.time()
    if _SIGA_ROWS and now - _SIGA_ROWS_TS < _SIGA_ROWS_TTL:
        return _SIGA_ROWS

    svc = _sheets_svc()

    meta = svc.spreadsheets().get(spreadsheetId=SIGA_SHEET_ID).execute()
    sheet_names = [s['properties']['title'] for s in meta.get('sheets', [])]

    def _g(r, idx): return str(r[idx]).strip() if idx < len(r) else ""
    def _gf(r, cm, f): return _g(r, cm.get(f, -1)) if cm.get(f, -1) >= 0 else ""

    rows = []
    seen: set = set()

    for sheet_name in sheet_names:
        result = svc.spreadsheets().values().get(
            spreadsheetId=SIGA_SHEET_ID,
            range=f"'{sheet_name}'!A:T",
        ).execute()
        raw = result.get("values", [])

        header_idx = 0
        col_map: dict = {}
        for i, row in enumerate(raw):
            n = _g(row, 0).lower().replace('ã','a').replace('ê','e')
            if n in ('emissao', 'emissão') or _g(row, 1).lower() == 'vencimento':
                header_idx = i
                col_map = _siga_build_col_map(row)
                break
        if not col_map:
            col_map = _siga_build_col_map([])

        for r in raw[header_idx + 1:]:
            receber = _br_val(_gf(r, col_map, 'receber'))
            pagar   = _br_val(_gf(r, col_map, 'pagar'))
            if receber == 0.0 and pagar == 0.0:
                continue
            emissao    = _br_date(_gf(r, col_map, 'emissao'))
            vencimento = _br_date(_gf(r, col_map, 'vencimento'))
            quitado_em = _br_date(_gf(r, col_map, 'quitado_em'))
            if not emissao and not vencimento and not quitado_em:
                continue

            # Usa coluna Situação explícita se disponível
            sit_raw = _gf(r, col_map, 'situacao').lower()
            if 'aberto' in sit_raw:
                situacao = 'em_aberto'
            elif sit_raw == 'quitado':
                situacao = 'quitado'
            else:
                situacao = 'quitado' if quitado_em else 'em_aberto'

            tipo  = "receber" if receber > 0 else "pagar"
            valor = receber if receber > 0 else pagar
            key   = (vencimento or emissao, _gf(r, col_map, 'contato_nome')[:30],
                     _gf(r, col_map, 'descricao')[:30], round(valor, 2))
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "tipo":           tipo,
                "emissao":        emissao,
                "vencimento":     vencimento or emissao,
                "quitado_em":     quitado_em,
                "contato_nome":   _gf(r, col_map, 'contato_nome'),
                "descricao":      _gf(r, col_map, 'descricao'),
                "categoria_nome": _gf(r, col_map, 'categoria_nome'),
                "centro_nome":    _gf(r, col_map, 'centro_nome'),
                "conta_nome":     _gf(r, col_map, 'conta_nome'),
                "situacao":       situacao,
                "valor":          valor,
                "valor_total":    valor,
            })

    _SIGA_ROWS    = rows
    _SIGA_ROWS_TS = now
    return rows



@router.get("/api/fin/lancamentos")
async def api_list_lancamentos(request: Request, mes: str = None):
    _require(request)
    today = date.today().isoformat()

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        sql = _LANCAMENTO_JOIN + " WHERE COALESCE(l.origem,'manual') != 'cc'"
        params = []
        if mes:
            sql += " AND l.vencimento LIKE %s"
            params.append(f"{mes}%")
        sql += " ORDER BY l.vencimento, l.id"
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]

        if mes:
            like_mes = f"{mes}%"
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='receber' AND situacao='em_aberto' AND vencimento LIKE %s", (like_mes,))
            a_receber = cur.fetchone()["coalesce"]
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='pagar' AND situacao='em_aberto' AND vencimento LIKE %s", (like_mes,))
            a_pagar = cur.fetchone()["coalesce"]
            row_emabert = float(a_receber) - float(a_pagar)
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='receber' AND situacao='quitado' AND vencimento LIKE %s AND COALESCE(origem,'manual')!='cc'", (like_mes,))
            recebidos = cur.fetchone()["coalesce"]
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='pagar' AND situacao='quitado' AND vencimento LIKE %s AND COALESCE(origem,'manual')!='cc'", (like_mes,))
            pagos = cur.fetchone()["coalesce"]
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='receber' AND situacao='em_aberto' AND vencimento < %s AND vencimento LIKE %s", (today, like_mes))
            atrasados_receber = cur.fetchone()["coalesce"]
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='pagar' AND situacao='em_aberto' AND vencimento < %s AND vencimento LIKE %s", (today, like_mes))
            atrasados_pagar = cur.fetchone()["coalesce"]
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='receber' AND situacao='em_aberto' AND vencimento >= %s AND vencimento LIKE %s", (today, like_mes))
            mes_receber = cur.fetchone()["coalesce"]
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='pagar' AND situacao='em_aberto' AND vencimento >= %s AND vencimento LIKE %s", (today, like_mes))
            mes_pagar = cur.fetchone()["coalesce"]
        else:
            cur.execute(
                "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos "
                "WHERE situacao='em_aberto' AND vencimento <= %s",
                (today,),
            )
            row_emabert = cur.fetchone()["coalesce"]
            a_receber = a_pagar = recebidos = pagos = 0.0
            atrasados_receber = atrasados_pagar = mes_receber = mes_pagar = 0.0
    finally:
        cur.close()
        conn.close()

    summary = {
        "em_aberto": round(float(row_emabert), 2),
        "a_receber": round(float(a_receber), 2),
        "a_pagar":   round(float(a_pagar), 2),
        "recebidos": round(float(recebidos), 2),
        "pagos":     round(float(pagos), 2),
    }
    bottom_totals = {
        "atrasados_receber": round(float(atrasados_receber), 2),
        "mes_receber":       round(float(mes_receber), 2),
        "atrasados_pagar":   round(float(atrasados_pagar), 2),
        "mes_pagar":         round(float(mes_pagar), 2),
        "total":             round(float(recebidos) - float(pagos), 2),
    }
    return {"lancamentos": rows, "summary": summary, "bottom_totals": bottom_totals}


@router.post("/api/fin/lancamentos")
async def api_create_lancamento(request: Request):
    _require(request)
    data = await request.json()

    tipo = data.get("tipo")
    if tipo not in ("receber", "pagar"):
        raise HTTPException(400, "tipo deve ser 'receber' ou 'pagar'")
    emissao   = (data.get("emissao") or "").strip()
    descricao = (data.get("descricao") or "").strip()
    if not emissao:
        raise HTTPException(400, "emissao é obrigatório")
    if not descricao:
        raise HTTPException(400, "descricao é obrigatório")

    fields = _extract_lancamento(data)
    fields["tipo"]     = tipo
    fields["emissao"]  = emissao
    fields["descricao"] = descricao
    fields.setdefault("situacao", "em_aberto")

    n_parcelas = _parse_parcelas(data)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        if n_parcelas:
            # Gera N lançamentos, um por mês, com o vencimento avançando 1 mês a cada parcela.
            base_venc = fields.get("vencimento") or emissao
            base_desc = _strip_parcela_suffix(descricao)
            first_id = None
            for i in range(1, n_parcelas + 1):
                row_fields = dict(fields)
                row_fields["descricao"]  = f"{base_desc} {i}/{n_parcelas}"
                row_fields["vencimento"] = _add_months(base_venc, i - 1)
                p_cols = ", ".join(row_fields.keys())
                p_phs  = ", ".join("%s" for _ in row_fields)
                cur.execute(
                    f"INSERT INTO fin_lancamentos ({p_cols}) VALUES ({p_phs}) RETURNING id",
                    list(row_fields.values()),
                )
                rid = cur.fetchone()["id"]
                if first_id is None:
                    first_id = rid
            conn.commit()
            cur.execute(_LANCAMENTO_JOIN + " WHERE l.id=%s", (first_id,))
            return {"lancamento": dict(cur.fetchone()), "parcelas": n_parcelas}

        cols         = ", ".join(fields.keys())
        placeholders = ", ".join("%s" for _ in fields)
        cur.execute(
            f"INSERT INTO fin_lancamentos ({cols}) VALUES ({placeholders}) RETURNING id",
            list(fields.values()),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.execute(_LANCAMENTO_JOIN + " WHERE l.id=%s", (new_id,))
        row = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"lancamento": row}


@router.get("/api/fin/lancamentos/{lid}")
async def api_get_lancamento(lid: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(_LANCAMENTO_JOIN + " WHERE l.id=%s", (lid,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Lançamento não encontrado")
    return {"lancamento": dict(row)}


@router.put("/api/fin/lancamentos/{lid}")
async def api_update_lancamento(lid: int, request: Request):
    _require(request)
    data = await request.json()

    if "tipo" in data and data["tipo"] not in ("receber", "pagar"):
        raise HTTPException(400, "tipo deve ser 'receber' ou 'pagar'")
    if "situacao" in data and data["situacao"] not in ("em_aberto", "quitado"):
        raise HTTPException(400, "situacao deve ser 'em_aberto' ou 'quitado'")

    fields = _extract_lancamento(data)
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")

    n_parcelas = _parse_parcelas(data)
    set_clause, vals = _build_set(fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        if n_parcelas:
            # Edição → parcelado: este lançamento vira a 1ª parcela e as demais
            # são criadas (clonadas) nos meses seguintes.
            cur.execute("SELECT * FROM fin_lancamentos WHERE id=%s", (lid,))
            atual = cur.fetchone()
            if not atual:
                raise HTTPException(404, "Lançamento não encontrado")
            atual = dict(atual)
            base_venc = fields.get("vencimento") or atual.get("vencimento") or atual.get("emissao")
            base_desc = _strip_parcela_suffix(fields.get("descricao") or atual.get("descricao") or "")
            # Atualiza a linha atual como 1/N
            f1 = dict(fields)
            f1["descricao"]  = f"{base_desc} 1/{n_parcelas}"
            f1["vencimento"] = _add_months(base_venc, 0)
            sc1, v1 = _build_set(f1)
            cur.execute(f"UPDATE fin_lancamentos SET {sc1} WHERE id=%s", v1 + [lid])
            # Recarrega a linha já atualizada para clonar as parcelas seguintes
            cur.execute("SELECT * FROM fin_lancamentos WHERE id=%s", (lid,))
            full = dict(cur.fetchone())
            clone_cols = [c for c in full.keys() if c not in ("id", "created_at")]
            for i in range(2, n_parcelas + 1):
                newrow = {c: full[c] for c in clone_cols}
                newrow["descricao"]  = f"{base_desc} {i}/{n_parcelas}"
                newrow["vencimento"] = _add_months(base_venc, i - 1)
                p_cols = ", ".join(newrow.keys())
                p_phs  = ", ".join("%s" for _ in newrow)
                cur.execute(f"INSERT INTO fin_lancamentos ({p_cols}) VALUES ({p_phs})", list(newrow.values()))
            conn.commit()
            cur.execute(_LANCAMENTO_JOIN + " WHERE l.id=%s", (lid,))
            return {"lancamento": dict(cur.fetchone()), "parcelas": n_parcelas}

        cur.execute(f"UPDATE fin_lancamentos SET {set_clause} WHERE id=%s", vals + [lid])
        conn.commit()
        cur.execute(_LANCAMENTO_JOIN + " WHERE l.id=%s", (lid,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Lançamento não encontrado")
    return {"lancamento": dict(row)}


@router.delete("/api/fin/lancamentos/{lid}")
async def api_delete_lancamento(lid: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM fin_lancamentos WHERE id=%s", (lid,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.patch("/api/fin/lancamentos/{lid}/quitar")
async def api_quitar_lancamento(lid: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT situacao FROM fin_lancamentos WHERE id=%s", (lid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Lançamento não encontrado")
        nova_situacao = "quitado" if row["situacao"] == "em_aberto" else "em_aberto"
        cur.execute("UPDATE fin_lancamentos SET situacao=%s WHERE id=%s", (nova_situacao, lid))
        conn.commit()
        cur.execute(_LANCAMENTO_JOIN + " WHERE l.id=%s", (lid,))
        updated = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"lancamento": updated}


@router.patch("/api/fin/lancamentos/quitar-lote")
async def api_quitar_lote(request: Request):
    _require(request)
    data = await request.json()
    ids = [int(i) for i in (data.get("ids") or [])]
    acao = data.get("acao")
    if not ids:
        raise HTTPException(400, "Nenhum id informado")
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        for lid in ids:
            cur.execute("SELECT situacao FROM fin_lancamentos WHERE id=%s", (lid,))
            row = cur.fetchone()
            if not row:
                continue
            if acao == "quitar":
                nova = "quitado"
            elif acao == "reabrir":
                nova = "em_aberto"
            else:
                nova = "quitado" if row["situacao"] == "em_aberto" else "em_aberto"
            cur.execute("UPDATE fin_lancamentos SET situacao=%s WHERE id=%s", (nova, lid))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True, "count": len(ids)}


# ── Transferências ────────────────────────────────────────────────────────────

@router.post("/api/fin/transferencias")
async def api_create_transferencia(request: Request):
    _require(request)
    data = await request.json()

    emissao       = (data.get("emissao") or "").strip()
    valor         = float(data.get("valor") or 0)
    de_conta_id   = data.get("de_conta_id")
    para_conta_id = data.get("para_conta_id")
    comentario    = data.get("comentario") or None

    if not emissao:
        raise HTTPException(400, "emissao é obrigatório")
    if not de_conta_id or not para_conta_id:
        raise HTTPException(400, "de_conta_id e para_conta_id são obrigatórios")
    if de_conta_id == para_conta_id:
        raise HTTPException(400, "Conta de origem e destino devem ser diferentes")
    if valor <= 0:
        raise HTTPException(400, "valor deve ser maior que zero")

    now = datetime.now().isoformat()
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        for conta_id in (de_conta_id, para_conta_id):
            cur.execute("SELECT id FROM fin_contas WHERE id=%s", (conta_id,))
            if not cur.fetchone():
                raise HTTPException(404, f"Conta {conta_id} não encontrada")

        cur.execute(
            "INSERT INTO fin_transferencias (emissao, valor, de_conta_id, para_conta_id, comentario, created_at) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (emissao, valor, de_conta_id, para_conta_id, comentario, now),
        )
        transferencia_id = cur.fetchone()["id"]
        descricao = "Transferência entre contas"

        cur.execute(
            "INSERT INTO fin_lancamentos (tipo, emissao, descricao, vencimento, valor, valor_total, conta_id, situacao, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'quitado',%s)",
            ("pagar", emissao, descricao, emissao, valor, valor, de_conta_id, now),
        )
        cur.execute(
            "INSERT INTO fin_lancamentos (tipo, emissao, descricao, vencimento, valor, valor_total, conta_id, situacao, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'quitado',%s)",
            ("receber", emissao, descricao, emissao, valor, valor, para_conta_id, now),
        )

        conn.commit()
        cur.execute("SELECT * FROM fin_transferencias WHERE id=%s", (transferencia_id,))
        transferencia = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"transferencia": transferencia}


@router.get("/api/fin/transferencias")
async def api_list_transferencias(request: Request, mes: str = None):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        sql = """
            SELECT t.*,
                   dc.nome AS de_conta_nome,
                   pc.nome AS para_conta_nome
            FROM fin_transferencias t
            LEFT JOIN fin_contas dc ON dc.id = t.de_conta_id
            LEFT JOIN fin_contas pc ON pc.id = t.para_conta_id
            WHERE 1=1
        """
        params = []
        if mes:
            sql += " AND t.emissao LIKE %s"
            params.append(f"{mes}%")
        sql += " ORDER BY t.emissao DESC, t.id DESC"
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"transferencias": rows}


# ── Saldos por conta ──────────────────────────────────────────────────────────

@router.get("/api/fin/saldos")
async def api_saldos(request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id, nome, saldo_inicial FROM fin_contas WHERE situacao='ativa' ORDER BY nome")
        contas = [dict(r) for r in cur.fetchall()]
        saldos = []
        for conta in contas:
            cid = conta["id"]
            cur.execute(
                "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE conta_id=%s AND tipo='receber' AND situacao='quitado'",
                (cid,),
            )
            total_receber = cur.fetchone()["coalesce"]
            cur.execute(
                "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE conta_id=%s AND tipo='pagar' AND situacao='quitado'",
                (cid,),
            )
            total_pagar = cur.fetchone()["coalesce"]
            saldo = round(float(conta["saldo_inicial"]) + float(total_receber) - float(total_pagar), 2)
            saldos.append({"conta_id": cid, "conta_nome": conta["nome"], "saldo": saldo})
    finally:
        cur.close()
        conn.close()
    return {"saldos": saldos}


@router.get("/api/fin/fluxo-caixa")
async def api_fluxo_caixa(request: Request, ano: int = None):
    _require(request)
    if not ano:
        ano = date.today().year
    today_str = date.today().isoformat()
    labels = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
    resultado = []

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        for m in range(1, 13):
            like = f"{ano}-{m:02d}%"
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='receber' AND vencimento LIKE %s AND COALESCE(origem,'manual')!='cc'", (like,))
            receber = float(cur.fetchone()["coalesce"])
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='pagar' AND vencimento LIKE %s AND COALESCE(origem,'manual')!='cc'", (like,))
            pagar = float(cur.fetchone()["coalesce"])
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='receber' AND situacao='quitado' AND vencimento LIKE %s AND COALESCE(origem,'manual')!='cc'", (like,))
            recebidos = float(cur.fetchone()["coalesce"])
            cur.execute("SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos WHERE tipo='pagar' AND situacao='quitado' AND vencimento LIKE %s AND COALESCE(origem,'manual')!='cc'", (like,))
            pagos = float(cur.fetchone()["coalesce"])
            mes_str   = f"{ano}-{m:02d}"
            realizado = mes_str < today_str[:7]
            saldo_op  = (recebidos - pagos) if realizado else (receber - pagar)
            resultado.append({
                'mes': m, 'label': labels[m-1],
                'receber': receber, 'pagar': pagar,
                'recebidos': recebidos, 'pagos': pagos,
                'saldo_op': round(saldo_op, 2), 'realizado': realizado,
            })
    finally:
        cur.close()
        conn.close()

    saldo_acum = 0.0
    for r in resultado:
        saldo_acum += r['saldo_op']
        r['saldo_acum'] = round(saldo_acum, 2)

    return {'fluxo': resultado, 'ano': ano}


# ── Importação de extratos ────────────────────────────────────────────────────

def _extract_text_pdf(content: bytes) -> str:
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    parts.append(text)
                for table in (page.extract_tables() or []):
                    for row in table:
                        if row:
                            parts.append("\t".join(str(c or "").strip() for c in row))
        return "\n".join(parts)
    except ImportError:
        raise HTTPException(500, "pdfplumber não instalado. Execute: pip install pdfplumber")


def _extract_text_csv(content: bytes) -> str:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return content.decode(enc)
        except Exception:
            continue
    return content.decode("utf-8", errors="replace")


_PROMPT_EXTRATOR = """Você é um assistente financeiro especializado em extrair lançamentos de extratos bancários e faturas de cartão de crédito brasileiros.

Analise o texto abaixo e extraia TODOS os lançamentos financeiros encontrados.
Retorne APENAS um JSON válido, sem texto adicional, no formato:

{
  "lancamentos": [
    {
      "data": "YYYY-MM-DD",
      "descricao": "descrição limpa e legível",
      "valor": 123.45,
      "tipo": "despesa",
      "categoria_sugerida": "Alimentação"
    }
  ]
}

Regras importantes:
- "tipo": use "despesa" para débitos/saídas/pagamentos/compras; "receita" para créditos/entradas/depósitos/PIX recebido/estornos
- "valor": sempre número positivo, sem símbolo de moeda
- "data": formato YYYY-MM-DD obrigatório; se ano não aparecer use o ano mais provável
- "descricao": texto limpo, remova códigos internos desnecessários
- "categoria_sugerida": escolha entre: Alimentação, Transporte, Saúde, Educação, Moradia, Lazer, Serviços, Salário, Transferência, Investimento, Impostos, Outros
- Ignore saldos, totais e linhas de cabeçalho — extraia apenas transações individuais

Texto do extrato:
"""


def _call_ia(text: str) -> list[dict]:
    ai = _anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": _PROMPT_EXTRATOR + text[:10000]}],
    )
    raw = resp.content[0].text.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise HTTPException(500, "IA não retornou JSON válido")
    data = json.loads(m.group())
    return data.get("lancamentos", [])


@router.post("/api/fin/importar")
async def importar_extrato(
    request: Request,
    arquivo: UploadFile = File(...),
    conta_id: str = Form(None),
    mes_referencia: str = Form(None),
):
    _require(request)
    content  = await arquivo.read()
    filename = arquivo.filename or "arquivo"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext == "pdf":
        text = _extract_text_pdf(content); tipo_arquivo = "pdf"
    elif ext in ("csv", "txt", "ofx", "ret"):
        text = _extract_text_csv(content); tipo_arquivo = ext
    else:
        raise HTTPException(400, "Formato não suportado. Use PDF ou CSV.")

    if not text.strip():
        raise HTTPException(400, "Não foi possível extrair texto do arquivo.")

    lancamentos = _call_ia(text)
    if not lancamentos:
        raise HTTPException(422, "Nenhum lançamento encontrado no arquivo.")

    cid = int(conta_id) if conta_id and conta_id.isdigit() else None
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "INSERT INTO fin_importacoes (nome_arquivo, tipo_arquivo, conta_id, total_itens, mes_referencia) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (filename, tipo_arquivo, cid, len(lancamentos), mes_referencia or None),
        )
        imp_id = cur.fetchone()["id"]
        for l in lancamentos:
            cur.execute(
                "INSERT INTO fin_importacao_itens (importacao_id, data_lancamento, descricao, valor, tipo, categoria_sugerida, conta_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (imp_id, l.get("data"), l.get("descricao"), l.get("valor"), l.get("tipo"), l.get("categoria_sugerida"), cid),
            )
        conn.commit()
        return {"id": imp_id, "total": len(lancamentos)}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


@router.get("/api/fin/importacoes")
async def listar_importacoes(request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM fin_importacoes ORDER BY created_at DESC LIMIT 20")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"importacoes": rows}


@router.patch("/api/fin/importacoes/{imp_id}")
async def atualizar_importacao(imp_id: int, request: Request):
    _require(request)
    data = await request.json()
    allowed = {"mes_referencia"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        sets = ", ".join(f"{k}=%s" for k in fields)
        cur.execute(f"UPDATE fin_importacoes SET {sets} WHERE id=%s", list(fields.values()) + [imp_id])
        conn.commit()
        return {"ok": True}
    finally:
        cur.close()
        conn.close()


@router.get("/api/fin/importacoes/{imp_id}/itens")
async def listar_itens(imp_id: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT * FROM fin_importacao_itens WHERE importacao_id=%s ORDER BY data_lancamento, id",
            (imp_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"itens": rows}


@router.put("/api/fin/importacao-item/{item_id}")
async def atualizar_item(item_id: int, request: Request):
    _require(request)
    body   = await request.json()
    campos = ["data_lancamento", "descricao", "valor", "tipo", "categoria_id", "conta_id"]
    sets   = ", ".join(f"{c}=%s" for c in campos if c in body)
    vals   = [body[c] for c in campos if c in body] + [item_id]
    if not sets:
        return {}
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE fin_importacao_itens SET {sets} WHERE id=%s", vals)
        cur.execute("SELECT status, lancamento_id FROM fin_importacao_itens WHERE id=%s", (item_id,))
        item = cur.fetchone()
        if item and item["status"] == "aprovado" and item["lancamento_id"]:
            lanc_map = {
                "data_lancamento": ["emissao", "vencimento"],
                "descricao":       ["descricao"],
                "valor":           ["valor", "valor_total"],
                "categoria_id":    ["categoria_id"],
            }
            lsets, lvals = [], []
            for src, dsts in lanc_map.items():
                if src in body:
                    for dst in dsts:
                        lsets.append(f"{dst}=%s")
                        lvals.append(body[src])
            if "tipo" in body:
                lsets.append("tipo=%s")
                lvals.append("pagar" if body["tipo"] == "despesa" else "receber")
            if lsets:
                lvals.append(item["lancamento_id"])
                cur.execute(f"UPDATE fin_lancamentos SET {', '.join(lsets)} WHERE id=%s", lvals)
        conn.commit()
        return {"ok": True}
    finally:
        cur.close()
        conn.close()


@router.post("/api/fin/importacoes/{imp_id}/aprovar")
async def aprovar_itens(imp_id: int, request: Request):
    _require(request)
    body     = await request.json()
    item_ids = body.get("item_ids", [])
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        aprovados = 0
        today = date.today().isoformat()
        for iid in item_ids:
            cur.execute(
                "SELECT * FROM fin_importacao_itens WHERE id=%s AND importacao_id=%s AND status='pendente'",
                (iid, imp_id),
            )
            item = cur.fetchone()
            if not item:
                continue
            item = dict(item)
            dt        = item["data_lancamento"] or today
            tipo_lanc = "pagar" if item["tipo"] == "despesa" else "receber"
            cur.execute(
                "INSERT INTO fin_lancamentos (tipo, emissao, descricao, vencimento, valor, valor_total, conta_id, categoria_id, situacao, origem) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'quitado','cc') RETURNING id",
                (tipo_lanc, dt, item["descricao"] or "—", dt,
                 item["valor"] or 0, item["valor"] or 0,
                 item["conta_id"], item["categoria_id"]),
            )
            lanc_id = cur.fetchone()["id"]
            cur.execute(
                "UPDATE fin_importacao_itens SET status='aprovado', lancamento_id=%s WHERE id=%s",
                (lanc_id, iid),
            )
            aprovados += 1

        cur.execute(
            "UPDATE fin_importacoes SET itens_aprovados=(SELECT COUNT(*) FROM fin_importacao_itens WHERE importacao_id=%s AND status='aprovado') WHERE id=%s",
            (imp_id, imp_id),
        )
        conn.commit()
        return {"aprovados": aprovados}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


@router.delete("/api/fin/importacao-item/{item_id}")
async def rejeitar_item(item_id: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT status, lancamento_id, importacao_id FROM fin_importacao_itens WHERE id=%s",
            (item_id,),
        )
        item = cur.fetchone()
        if not item:
            raise HTTPException(404, "Item não encontrado")
        item = dict(item)
        lancamento_id = item["lancamento_id"]
        cur.execute(
            "UPDATE fin_importacao_itens SET status='rejeitado', lancamento_id=NULL WHERE id=%s",
            (item_id,),
        )
        if lancamento_id:
            cur.execute("DELETE FROM fin_lancamentos WHERE id=%s", (lancamento_id,))
        if item["importacao_id"]:
            cur.execute(
                "UPDATE fin_importacoes SET itens_aprovados=(SELECT COUNT(*) FROM fin_importacao_itens WHERE importacao_id=%s AND status='aprovado') WHERE id=%s",
                (item["importacao_id"], item["importacao_id"]),
            )
        conn.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ── Dashboard Analytics ────────────────────────────────────────────────────────

@router.get("/api/fin/cartao-analise")
async def cartao_analise(request: Request, de: str = None, ate: str = None):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"""
            SELECT i.data_lancamento, i.descricao, i.valor, i.tipo,
                   i.categoria_id, c.nome AS categoria_nome,
                   i.importacao_id,
                   imp.nome_arquivo, imp.mes_referencia,
                   imp.created_at AS imp_created_at
            FROM fin_importacao_itens i
            JOIN fin_importacoes imp ON imp.id = i.importacao_id
            LEFT JOIN fin_categorias c ON c.id = i.categoria_id
            WHERE i.status = 'aprovado' AND i.tipo = 'despesa'
            ORDER BY COALESCE(imp.mes_referencia, LEFT(i.data_lancamento,7)), i.data_lancamento
        """)
        itens = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT imp.id, imp.nome_arquivo, imp.created_at, imp.mes_referencia,
                   imp.total_itens, imp.itens_aprovados,
                   COALESCE(SUM(CASE WHEN i.tipo='despesa' AND i.status='aprovado' THEN i.valor ELSE 0 END),0) AS total
            FROM fin_importacoes imp
            LEFT JOIN fin_importacao_itens i ON i.importacao_id = imp.id
            GROUP BY imp.id
            ORDER BY COALESCE(imp.mes_referencia, imp.created_at::text) DESC
        """)
        faturas = [dict(r) for r in cur.fetchall()]

        # Fetch rules and categories
        cur.execute("SELECT merchant, categoria_id FROM fin_cc_regras")
        regras = {r['merchant']: r['categoria_id'] for r in cur.fetchall()}

        cur.execute("SELECT id, nome FROM fin_categorias WHERE situacao='ativa' ORDER BY nome")
        cat_map = {r['id']: r['nome'] for r in cur.fetchall()}
        categorias = [{'id': r_id, 'nome': nome} for r_id, nome in sorted(cat_map.items(), key=lambda x: x[1])]
    finally:
        cur.close()
        conn.close()

    from collections import defaultdict

    def _ym_label(yyyymm):
        try:
            y, m = yyyymm.split('-')
            return f"{_MESES_ABBR_LIST[int(m)-1]}/{y[2:]}"
        except Exception:
            return yyyymm

    def _item_mes(it):
        return it.get('mes_referencia') or (it['data_lancamento'] or '')[:7]

    # Apply de/ate filter based on mes_referencia
    if de or ate:
        itens = [it for it in itens if
                 (not de or _item_mes(it) >= de) and
                 (not ate or _item_mes(it) <= ate)]

    by_month: dict = defaultdict(float)
    for it in itens:
        raw = _item_mes(it)
        if raw:
            by_month[raw] += it['valor'] or 0
    mensal = [{'mes': _ym_label(k), 'total': round(v, 2)} for k, v in sorted(by_month.items())]

    def _item_cat_nome(it):
        if it.get('categoria_id'):
            return it.get('categoria_nome') or 'Sem categoria'
        cid = regras.get((it.get('descricao') or '').strip())
        return cat_map.get(cid, 'Sem categoria') if cid else 'Sem categoria'

    by_cat: dict = defaultdict(float)
    for it in itens:
        by_cat[_item_cat_nome(it)] += it['valor'] or 0
    por_categoria = [{'categoria': k, 'total': round(v, 2)}
                     for k, v in sorted(by_cat.items(), key=lambda x: -x[1])]

    by_merchant: dict = defaultdict(float)
    for it in itens:
        desc = (it['descricao'] or 'Outros').strip()
        by_merchant[desc] += it['valor'] or 0

    top_merchants = []
    for k, v in sorted(by_merchant.items(), key=lambda x: -x[1])[:15]:
        cid = regras.get(k)
        top_merchants.append({
            'nome': k,
            'total': round(v, 2),
            'categoria_id': cid,
            'categoria_nome': cat_map.get(cid, '') if cid else '',
        })

    total_gasto  = round(sum(v for v in by_month.values()), 2)
    media_mensal = round(total_gasto / len(by_month), 2) if by_month else 0
    melhor_mes   = max(mensal, key=lambda x: x['total']) if mensal else None

    return {
        'faturas': faturas, 'mensal': mensal,
        'por_categoria': por_categoria, 'top_merchants': top_merchants,
        'total_gasto': total_gasto, 'media_mensal': media_mensal, 'melhor_mes': melhor_mes,
        'categorias': categorias,
    }


@router.post("/api/fin/cc/regra")
async def api_cc_regra_upsert(request: Request):
    _require(request)
    body = await request.json()
    merchant = (body.get('merchant') or '').strip()
    categoria_id = body.get('categoria_id')
    if not merchant:
        raise HTTPException(400, "merchant required")
    conn = get_conn()
    cur = conn.cursor()
    try:
        if categoria_id:
            cur.execute("""
                INSERT INTO fin_cc_regras (merchant, categoria_id) VALUES (%s, %s)
                ON CONFLICT (merchant) DO UPDATE SET categoria_id = EXCLUDED.categoria_id
            """, (merchant, int(categoria_id)))
        else:
            cur.execute("DELETE FROM fin_cc_regras WHERE merchant = %s", (merchant,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.get("/api/fin/dashboard-sheets")
async def dashboard_sheets(request: Request, de: str = None, ate: str = None):
    _require(request)
    from collections import defaultdict

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        filters = ["l.situacao = 'quitado'", "COALESCE(l.origem,'manual') != 'cc'"]
        params: list = []
        if de:
            y, m = (de.split('-') + ['01'])[:2]
            filters.append("l.vencimento >= %s"); params.append(f"{y}-{m}-01")
        if ate:
            y, m = (ate.split('-') + ['01'])[:2]
            filters.append("l.vencimento <= %s"); params.append(f"{y}-{m}-31")
        cur.execute(f"""
            SELECT l.vencimento, l.tipo, l.valor_total, l.contato_nome,
                   cc.nome AS centro_nome
            FROM fin_lancamentos l
            LEFT JOIN fin_centros_custo cc ON cc.id = l.centro_custo_id
            WHERE {' AND '.join(filters)}
            ORDER BY l.vencimento
        """, params)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    def _label(iso):
        if not iso or len(iso) < 7: return None
        try:
            y, m = iso[:7].split('-')
            return f"{_MESES_ABBR_LIST[int(m)-1]}/{y[2:]}"
        except Exception: return None

    def _sk(mes):
        try: abbr, yy = mes.split('/'); return (int(yy), _MESES_ABBR_LIST.index(abbr))
        except Exception: return (0, 0)

    SOCIO_NAMES = {'victor cardô', 'victor leopoldo dognini cardoso', 'victor cardo'}

    by_month: dict = defaultdict(lambda: {'receber': 0.0, 'pagar': 0.0, 'rem_socio': 0.0, 'n': 0})
    cli:  dict = defaultdict(float)
    forn: dict = defaultdict(float)
    cc:   dict = defaultdict(float)

    for r in rows:
        k    = _label(r['vencimento'])
        nome = r['contato_nome'] or '—'
        val  = r['valor_total'] or 0
        is_socio = r['tipo'] == 'pagar' and nome.strip().lower() in SOCIO_NAMES
        if k:
            if r['tipo'] == 'receber': by_month[k]['receber'] += val
            else:                       by_month[k]['pagar']   += val
            if is_socio:               by_month[k]['rem_socio'] += val
            by_month[k]['n'] += 1
        if r['tipo'] == 'receber':
            cli[nome] += val
        else:
            forn[nome] += val
            cn = r.get('centro_nome') or ''
            if cn and cn not in ('Sem Centro de Custos', ''):
                cc[cn] += val

    siga_mensal = []
    for k, v in sorted(by_month.items(), key=lambda x: _sk(x[0])):
        lucro_op = round(v['receber'] - (v['pagar'] - v['rem_socio']), 2)
        siga_mensal.append({
            'mes':        k,
            'receber':    round(v['receber'], 2),
            'pagar':      round(v['pagar'], 2),
            'lucro':      round(v['receber'] - v['pagar'], 2),
            'rem_socio':  round(v['rem_socio'], 2),
            'lucro_op':   lucro_op,
            'n':          v['n'],
        })

    return {
        'siga_mensal':      siga_mensal,
        'top_clientes':     [{'nome': k, 'total': round(v,2)} for k,v in sorted(cli.items(),  key=lambda x: -x[1])[:12]],
        'top_fornecedores': [{'nome': k, 'total': round(v,2)} for k,v in sorted(forn.items(), key=lambda x: -x[1])[:12]],
        'centros':          [{'nome': k, 'total': round(v,2)} for k,v in sorted(cc.items(),   key=lambda x: -x[1])],
    }


def _executar_migracao_siga() -> int:
    """Limpa todos os lançamentos e reimporta do Google Sheets SIGA. Retorna qtd importada."""
    import logging
    log = logging.getLogger(__name__)
    try:
        siga_rows = _fetch_siga_rows()
    except Exception as e:
        log.error(f"[migrar-siga] erro ao ler Google Sheets: {e}")
        return 0

    # Proteção: aborta se o Sheet não tiver dados dos últimos 2 meses
    today = date.today()
    if today.month <= 2:
        cutoff = f"{today.year - 1}-{today.month + 10:02d}"
    else:
        cutoff = f"{today.year}-{today.month - 2:02d}"
    recent = [r for r in siga_rows
              if (r.get('quitado_em') or r.get('vencimento') or '')[:7] >= cutoff]
    if not recent:
        log.warning(
            "[migrar-siga] Sheet sem dados a partir de %s (%d linhas total) "
            "— migração abortada para preservar dados existentes no banco",
            cutoff, len(siga_rows)
        )
        return -1

    conn = get_conn()
    cur  = dict_cursor(conn)
    try:
        cur.execute("SELECT id, nome FROM fin_categorias")
        cat_map = {r['nome'].lower(): r['id'] for r in cur.fetchall()}
        cur.execute("SELECT id, nome FROM fin_contas")
        conta_map = {r['nome'].lower(): r['id'] for r in cur.fetchall()}
        cur.execute("SELECT id, nome FROM fin_centros_custo")
        centro_map = {r['nome'].lower(): r['id'] for r in cur.fetchall()}

        # Limpa tudo — dá uma base limpa antes de reimportar
        cur.execute("DELETE FROM fin_lancamentos WHERE COALESCE(origem,'manual') NOT IN ('cc')")

        today_iso = date.today().isoformat()
        now = datetime.now().isoformat()
        for r in siga_rows:
            # Para itens quitados, usar quitado_em como vencimento (alinha com o que o SIGA mostra)
            venc_db = r['quitado_em'] if r['situacao'] == 'quitado' and r['quitado_em'] else r['vencimento']
            emissao_db = r['emissao'] or r['vencimento'] or today_iso
            venc_db    = venc_db or emissao_db
            cur.execute(
                """INSERT INTO fin_lancamentos
                   (tipo, emissao, contato_nome, descricao, vencimento,
                    valor, valor_total, conta_id, categoria_id, centro_custo_id,
                    situacao, origem, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'siga',%s)""",
                (
                    r['tipo'], emissao_db,
                    r['contato_nome'] or '—', r['descricao'] or '—',
                    venc_db, r['valor'], r['valor_total'],
                    conta_map.get((r['conta_nome'] or '').lower()),
                    cat_map.get((r['categoria_nome'] or '').lower()),
                    centro_map.get((r['centro_nome'] or '').lower()),
                    r['situacao'], now,
                )
            )

        # Grava versão da migração para controle de re-runs
        cur.execute(
            "INSERT INTO fin_meta (key, value) VALUES ('siga_migration_version', %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (SIGA_MIGRATION_VER,)
        )
        # Limpa erro anterior se existia
        cur.execute("DELETE FROM fin_meta WHERE key='siga_migration_error'")
        conn.commit()
        log.info(f"[migrar-siga] {len(siga_rows)} lançamentos importados (versão {SIGA_MIGRATION_VER})")
        return len(siga_rows)
    except Exception as e:
        conn.rollback()
        log.error(f"[migrar-siga] erro na inserção: {e}")
        # Grava o erro no banco para diagnóstico
        try:
            err_conn = get_conn()
            err_cur  = err_conn.cursor()
            err_cur.execute(
                "INSERT INTO fin_meta (key, value) VALUES ('siga_migration_error', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (str(e)[:500],)
            )
            err_conn.commit()
            err_cur.close(); err_conn.close()
        except Exception:
            pass
        return 0
    finally:
        cur.close()
        conn.close()


async def migrar_siga_startup():
    """Roda no startup: só executa se a versão da migração no banco for diferente da atual."""
    import asyncio
    import logging
    log = logging.getLogger(__name__)
    try:
        conn = get_conn()
        cur  = dict_cursor(conn)
        try:
            cur.execute("SELECT value FROM fin_meta WHERE key = 'siga_migration_version'")
            row = cur.fetchone()
            current_ver = row["value"] if row else None
        finally:
            cur.close(); conn.close()

        if current_ver == SIGA_MIGRATION_VER:
            log.info(f"[migrar-siga] versão {SIGA_MIGRATION_VER} já aplicada — OK")
            return

        log.info(f"[migrar-siga] versão no banco={current_ver}, esperada={SIGA_MIGRATION_VER} — reimportando...")
        loop = asyncio.get_event_loop()
        n = await loop.run_in_executor(None, _executar_migracao_siga)
        log.info(f"[migrar-siga] startup concluído: {n} lançamentos")
    except Exception as e:
        logging.getLogger(__name__).error(f"[migrar-siga] startup falhou: {e}")


@router.post("/api/fin/migrar-siga")
async def migrar_siga(request: Request):
    """Reimporta todo o histórico SIGA (apaga lançamentos manuais/siga e reimporta)."""
    _require(request)
    import asyncio
    loop = asyncio.get_event_loop()
    n = await loop.run_in_executor(None, _executar_migracao_siga)
    global _SIGA_ROWS, _SIGA_ROWS_TS
    _SIGA_ROWS = []; _SIGA_ROWS_TS = 0.0
    return {"ok": True, "importados": n}


@router.get("/api/fin/debug-siga")
async def debug_siga_mes(request: Request, mes: str = "2026-04"):
    """Debug: compara Sheet vs DB para um mês (quitados). Ex: ?mes=2026-04"""
    _require(request)

    # Raw Sheet data (sem cache) para inspecionar colunas
    try:
        svc = _sheets_svc()
        result = svc.spreadsheets().values().get(
            spreadsheetId=SIGA_SHEET_ID, range="A:P"
        ).execute()
        raw = result.get("values", [])
    except Exception as e:
        return {"error": str(e)}

    header = raw[0] if raw else []
    sample_raw = raw[1:6]  # primeiras 5 linhas de dados

    try:
        sheet_rows = _fetch_siga_rows()
    except Exception as e:
        return {"error_fetch": str(e), "header": header}

    sheet_pagar_mes   = [r for r in sheet_rows if r['tipo'] == 'pagar'   and (r.get('quitado_em') or '')[:7] == mes]
    sheet_receber_mes = [r for r in sheet_rows if r['tipo'] == 'receber' and (r.get('quitado_em') or '')[:7] == mes]
    # também por vencimento (sem filtrar quitado_em)
    sheet_pagar_venc   = [r for r in sheet_rows if r['tipo'] == 'pagar'   and (r.get('vencimento') or '')[:7] == mes]
    sheet_receber_venc = [r for r in sheet_rows if r['tipo'] == 'receber' and (r.get('vencimento') or '')[:7] == mes]

    # 21º item: quitado_em no mês mas vencimento fora do mês
    sheet_pagar_fora_venc = [
        r for r in sheet_pagar_mes
        if (r.get('vencimento') or '')[:7] != mes
    ]

    conn = get_conn()
    cur  = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT tipo, vencimento, situacao, contato_nome, descricao, valor_total "
            "FROM fin_lancamentos "
            "WHERE vencimento LIKE %s AND COALESCE(origem,'manual') != 'cc' "
            "ORDER BY tipo, situacao, vencimento",
            (f"{mes}%",),
        )
        db_rows = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT key, value FROM fin_meta")
        meta_rows = {r['key']: r['value'] for r in cur.fetchall()}
    finally:
        cur.close(); conn.close()

    db_pagar_quit   = [r for r in db_rows if r['tipo'] == 'pagar'   and r['situacao'] == 'quitado']
    db_receber_quit = [r for r in db_rows if r['tipo'] == 'receber' and r['situacao'] == 'quitado']

    return {
        "mes": mes,
        "migration_ver_codigo": SIGA_MIGRATION_VER,
        "migration_ver_banco":  meta_rows.get('siga_migration_version'),
        "migration_error":      meta_rows.get('siga_migration_error'),
        "sheet_stats": {
            "total_rows":          len(sheet_rows),
            "com_quitado_em":      sum(1 for r in sheet_rows if r.get('quitado_em')),
            "pagar_quitado_em_mes":        len(sheet_pagar_mes),
            "pagar_vencimento_mes":        len(sheet_pagar_venc),
            "pagar_quitado_em_mes_total":  round(sum(r['valor'] for r in sheet_pagar_mes), 2),
            "receber_quitado_em_mes":      len(sheet_receber_mes),
            "receber_quitado_em_mes_total":round(sum(r['valor'] for r in sheet_receber_mes), 2),
        },
        "item_extra_quitado_em_abril_vencimento_outro_mes": [
            {"contato": r['contato_nome'], "descricao": r['descricao'],
             "vencimento": r['vencimento'], "quitado_em": r['quitado_em'], "valor": r['valor']}
            for r in sheet_pagar_fora_venc
        ],
        "db": {
            "pagar_quitado_total":   round(sum(r['valor_total'] for r in db_pagar_quit),   2),
            "pagar_quitado_count":   len(db_pagar_quit),
            "receber_quitado_total": round(sum(r['valor_total'] for r in db_receber_quit), 2),
            "receber_quitado_count": len(db_receber_quit),
        },
    }
