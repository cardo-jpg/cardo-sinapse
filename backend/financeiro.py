import sqlite3
import io
import csv
import os
import json
import re
import time as _time
from datetime import date, datetime
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import anthropic as _anthropic

from backend.gestao import _require, _verify

router = APIRouter()

BASE_DIR  = Path(__file__).parent.parent
DB_PATH   = BASE_DIR / "data" / "financeiro.db"
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fin_clientes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
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
            );

            CREATE TABLE IF NOT EXISTS fin_fornecedores (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
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
            );

            CREATE TABLE IF NOT EXISTS fin_contas (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                nome          TEXT    NOT NULL,
                situacao      TEXT    DEFAULT 'ativa',
                saldo_inicial REAL    DEFAULT 0,
                created_at    TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS fin_categorias (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                nome       TEXT NOT NULL,
                tipo       TEXT DEFAULT 'despesa' CHECK(tipo IN ('receita','despesa')),
                situacao   TEXT DEFAULT 'ativo',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS fin_centros_custo (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                nome       TEXT NOT NULL,
                situacao   TEXT DEFAULT 'ativo',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS fin_lancamentos (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS fin_transferencias (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                emissao      TEXT    NOT NULL,
                valor        REAL    DEFAULT 0,
                de_conta_id  INTEGER NOT NULL REFERENCES fin_contas(id),
                para_conta_id INTEGER NOT NULL REFERENCES fin_contas(id),
                comentario   TEXT,
                created_at   TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS fin_importacoes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                nome_arquivo    TEXT    NOT NULL,
                tipo_arquivo    TEXT    NOT NULL,
                conta_id        INTEGER REFERENCES fin_contas(id),
                total_itens     INTEGER DEFAULT 0,
                itens_aprovados INTEGER DEFAULT 0,
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS fin_importacao_itens (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                importacao_id    INTEGER NOT NULL REFERENCES fin_importacoes(id) ON DELETE CASCADE,
                data_lancamento  TEXT,
                descricao        TEXT,
                valor            REAL,
                tipo             TEXT CHECK(tipo IN ('receita','despesa')),
                categoria_sugerida TEXT,
                categoria_id     INTEGER REFERENCES fin_categorias(id),
                conta_id         INTEGER REFERENCES fin_contas(id),
                status           TEXT DEFAULT 'pendente' CHECK(status IN ('pendente','aprovado','rejeitado')),
                lancamento_id    INTEGER REFERENCES fin_lancamentos(id),
                created_at       TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Migration: add origem column if missing
        cols = [r[1] for r in conn.execute("PRAGMA table_info(fin_lancamentos)").fetchall()]
        if "origem" not in cols:
            conn.execute("ALTER TABLE fin_lancamentos ADD COLUMN origem TEXT DEFAULT 'manual'")
        # Mark existing CC-imported lancamentos that predate the origem column
        conn.execute("""
            UPDATE fin_lancamentos SET origem='cc'
            WHERE (origem IS NULL OR origem='manual')
            AND id IN (
                SELECT lancamento_id FROM fin_importacao_itens
                WHERE lancamento_id IS NOT NULL AND status='aprovado'
            )
        """)
        conn.commit()


# ── Internal helpers ──────────────────────────────────────────────────────────

_CONTATO_FIELDS = (
    "tipo", "nome_fantasia", "razao_social", "cnpj", "cpf",
    "ie", "im", "email", "telefone", "endereco", "situacao",
)

_LANCAMENTO_FIELDS = (
    "tipo", "emissao", "contato_tipo", "contato_id", "contato_nome",
    "descricao", "vencimento", "valor", "acrescimo", "acrescimo_tipo",
    "desconto", "desconto_tipo", "valor_total", "conta_id", "categoria_id",
    "centro_custo_id", "documento", "meio_pagamento", "situacao",
)


def _compute_valor_total(valor, acrescimo, acrescimo_tipo, desconto, desconto_tipo):
    """Calculate final value after additions and discounts."""
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
    # auto-compute valor_total if component fields present
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
    """Return (set_clause, values_list) for UPDATE statements."""
    set_clause = ", ".join(f"{k}=?" for k in fields)
    return set_clause, list(fields.values())


# ── Generic CRUD for contato tables (clientes / fornecedores) ─────────────────

def _list_contatos(table: str, situacao=None, q=None):
    with get_db() as conn:
        sql    = f"SELECT * FROM {table} WHERE 1=1"
        params = []
        if situacao:
            sql += " AND situacao=?"
            params.append(situacao)
        if q:
            sql += " AND nome_fantasia LIKE ?"
            params.append(f"%{q}%")
        sql += " ORDER BY nome_fantasia"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return rows


def _get_contato(table: str, cid: int):
    with get_db() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Registro não encontrado")
    return dict(row)


def _create_contato(table: str, data: dict):
    nome = (data.get("nome_fantasia") or "").strip()
    if not nome:
        raise HTTPException(400, "nome_fantasia é obrigatório")
    fields = _extract_contato(data)
    fields["nome_fantasia"] = nome
    cols   = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    with get_db() as conn:
        cur = conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        conn.commit()
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def _update_contato(table: str, cid: int, data: dict):
    fields = _extract_contato(data)
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    if "nome_fantasia" in fields:
        fields["nome_fantasia"] = (fields["nome_fantasia"] or "").strip()
        if not fields["nome_fantasia"]:
            raise HTTPException(400, "nome_fantasia não pode ser vazio")
    set_clause, vals = _build_set(fields)
    with get_db() as conn:
        conn.execute(f"UPDATE {table} SET {set_clause} WHERE id=?", vals + [cid])
        conn.commit()
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Registro não encontrado")
    return dict(row)


def _delete_contato(table: str, cid: int):
    with get_db() as conn:
        conn.execute(f"DELETE FROM {table} WHERE id=?", (cid,))
        conn.commit()
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


# ── Contas ────────────────────────────────────────────────────────────────────

@router.get("/api/fin/contas")
async def api_list_contas(request: Request):
    _require(request)
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM fin_contas WHERE situacao='ativa' ORDER BY nome"
        ).fetchall()]
    return {"contas": rows}


@router.post("/api/fin/contas")
async def api_create_conta(request: Request):
    _require(request)
    data = await request.json()
    nome = (data.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    fields = {
        "nome":          nome,
        "situacao":      data.get("situacao", "ativa"),
        "saldo_inicial": float(data.get("saldo_inicial") or 0),
    }
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO fin_contas (nome, situacao, saldo_inicial) VALUES (?,?,?)",
            [fields["nome"], fields["situacao"], fields["saldo_inicial"]],
        )
        conn.commit()
        row = dict(conn.execute("SELECT * FROM fin_contas WHERE id=?", (cur.lastrowid,)).fetchone())
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
    with get_db() as conn:
        conn.execute(f"UPDATE fin_contas SET {set_clause} WHERE id=?", vals + [cid])
        conn.commit()
        row = conn.execute("SELECT * FROM fin_contas WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Conta não encontrada")
    return {"conta": dict(row)}


@router.delete("/api/fin/contas/{cid}")
async def api_delete_conta(cid: int, request: Request):
    _require(request)
    with get_db() as conn:
        # Guard: do not delete if lancamentos reference this conta
        in_use = conn.execute(
            "SELECT COUNT(*) FROM fin_lancamentos WHERE conta_id=?", (cid,)
        ).fetchone()[0]
        if in_use:
            raise HTTPException(409, "Conta em uso em lançamentos — não é possível excluir")
        conn.execute("DELETE FROM fin_contas WHERE id=?", (cid,))
        conn.commit()
    return {"ok": True}


# ── Categorias ────────────────────────────────────────────────────────────────

@router.get("/api/fin/categorias")
async def api_list_categorias(request: Request, tipo: str = None):
    _require(request)
    with get_db() as conn:
        sql    = "SELECT * FROM fin_categorias WHERE 1=1"
        params = []
        if tipo in ("receita", "despesa"):
            sql += " AND tipo=?"
            params.append(tipo)
        sql += " ORDER BY nome"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
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
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO fin_categorias (nome, tipo, situacao) VALUES (?,?,?)",
            (nome, tipo, situacao),
        )
        conn.commit()
        row = dict(conn.execute("SELECT * FROM fin_categorias WHERE id=?", (cur.lastrowid,)).fetchone())
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
    with get_db() as conn:
        conn.execute(f"UPDATE fin_categorias SET {set_clause} WHERE id=?", vals + [cid])
        conn.commit()
        row = conn.execute("SELECT * FROM fin_categorias WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Categoria não encontrada")
    return {"categoria": dict(row)}


@router.delete("/api/fin/categorias/{cid}")
async def api_delete_categoria(cid: int, request: Request):
    _require(request)
    with get_db() as conn:
        conn.execute("DELETE FROM fin_categorias WHERE id=?", (cid,))
        conn.commit()
    return {"ok": True}


# ── Centros de Custo ──────────────────────────────────────────────────────────

@router.get("/api/fin/centros")
async def api_list_centros(request: Request):
    _require(request)
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM fin_centros_custo ORDER BY nome"
        ).fetchall()]
    return {"centros": rows}


@router.post("/api/fin/centros")
async def api_create_centro(request: Request):
    _require(request)
    data = await request.json()
    nome = (data.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    situacao = data.get("situacao", "ativo")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO fin_centros_custo (nome, situacao) VALUES (?,?)",
            (nome, situacao),
        )
        conn.commit()
        row = dict(conn.execute("SELECT * FROM fin_centros_custo WHERE id=?", (cur.lastrowid,)).fetchone())
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
    with get_db() as conn:
        conn.execute(f"UPDATE fin_centros_custo SET {set_clause} WHERE id=?", vals + [cid])
        conn.commit()
        row = conn.execute("SELECT * FROM fin_centros_custo WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Centro de custo não encontrado")
    return {"centro": dict(row)}


@router.delete("/api/fin/centros/{cid}")
async def api_delete_centro(cid: int, request: Request):
    _require(request)
    with get_db() as conn:
        conn.execute("DELETE FROM fin_centros_custo WHERE id=?", (cid,))
        conn.commit()
    return {"ok": True}


# ── Lançamentos ───────────────────────────────────────────────────────────────

def _siga_summary_for_mes(mes_yyyymm: str) -> dict:
    """Return {receber, pagar} from SiGA data for a YYYY-MM month string."""
    try:
        year, month = mes_yyyymm.split('-')
        mes_label = f"{_MESES_ABBR_LIST[int(month)-1]}/{year[2:]}"
    except Exception:
        return {'receber': 0.0, 'pagar': 0.0}
    siga_mensal = _DASH_CACHE.get(('', ''), {}).get('siga_mensal') or []
    if not siga_mensal:
        try:
            siga_mensal = _parse_siga().get('siga_mensal', [])
        except Exception:
            return {'receber': 0.0, 'pagar': 0.0}
    for e in siga_mensal:
        if e['mes'] == mes_label:
            return {'receber': e['receber'], 'pagar': e['pagar']}
    return {'receber': 0.0, 'pagar': 0.0}

def _lancamento_row(row: dict) -> dict:
    """Attach joined names to a lancamento dict."""
    return row


@router.get("/api/fin/lancamentos")
async def api_list_lancamentos(request: Request, mes: str = None):
    _require(request)
    today = date.today().isoformat()

    with get_db() as conn:
        sql = """
            SELECT
                l.*,
                c.nome  AS conta_nome,
                cat.nome AS categoria_nome,
                cc.nome  AS centro_nome
            FROM fin_lancamentos l
            LEFT JOIN fin_contas        c   ON c.id   = l.conta_id
            LEFT JOIN fin_categorias    cat ON cat.id  = l.categoria_id
            LEFT JOIN fin_centros_custo cc  ON cc.id   = l.centro_custo_id
            WHERE COALESCE(l.origem,'manual') != 'cc'
        """
        params = []
        if mes:
            # mes format: YYYY-MM
            sql += " AND l.vencimento LIKE ?"
            params.append(f"{mes}%")
        sql += " ORDER BY l.vencimento, l.id"

        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        # ── Summary ───────────────────────────────────────────────────────────
        # em_aberto (overdue): vencimento <= today AND situacao = 'em_aberto'
        row_emabert = conn.execute(
            "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos "
            "WHERE situacao='em_aberto' AND vencimento <= ?",
            (today,),
        ).fetchone()[0]

        if mes:
            like_mes = f"{mes}%"
            a_receber = conn.execute(
                "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos "
                "WHERE tipo='receber' AND situacao='em_aberto' AND vencimento LIKE ?",
                (like_mes,),
            ).fetchone()[0]

            a_pagar = conn.execute(
                "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos "
                "WHERE tipo='pagar' AND situacao='em_aberto' AND vencimento LIKE ?",
                (like_mes,),
            ).fetchone()[0]

            _siga_mes = _siga_summary_for_mes(mes)
            recebidos = _siga_mes['receber']
            pagos     = _siga_mes['pagar']

            # bottom totals: overdue per type for selected month
            atrasados_receber = conn.execute(
                "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos "
                "WHERE tipo='receber' AND situacao='em_aberto' AND vencimento < ? AND vencimento LIKE ?",
                (today, like_mes),
            ).fetchone()[0]

            atrasados_pagar = conn.execute(
                "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos "
                "WHERE tipo='pagar' AND situacao='em_aberto' AND vencimento < ? AND vencimento LIKE ?",
                (today, like_mes),
            ).fetchone()[0]

            mes_receber = conn.execute(
                "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos "
                "WHERE tipo='receber' AND situacao='em_aberto' AND vencimento >= ? AND vencimento LIKE ?",
                (today, like_mes),
            ).fetchone()[0]

            mes_pagar = conn.execute(
                "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos "
                "WHERE tipo='pagar' AND situacao='em_aberto' AND vencimento >= ? AND vencimento LIKE ?",
                (today, like_mes),
            ).fetchone()[0]
        else:
            a_receber = a_pagar = recebidos = pagos = 0.0
            atrasados_receber = atrasados_pagar = mes_receber = mes_pagar = 0.0

    summary = {
        "em_aberto":  round(float(row_emabert), 2),
        "a_receber":  round(float(a_receber), 2),
        "a_pagar":    round(float(a_pagar), 2),
        "recebidos":  round(float(recebidos), 2),
        "pagos":      round(float(pagos), 2),
    }

    bottom_totals = {
        "atrasados_receber": round(float(atrasados_receber), 2),
        "mes_receber":       round(float(mes_receber), 2),
        "atrasados_pagar":   round(float(atrasados_pagar), 2),
        "mes_pagar":         round(float(mes_pagar), 2),
    }

    return {
        "lancamentos":   rows,
        "summary":       summary,
        "bottom_totals": bottom_totals,
    }


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

    cols         = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    with get_db() as conn:
        cur = conn.execute(
            f"INSERT INTO fin_lancamentos ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        conn.commit()
        row = dict(conn.execute("""
            SELECT l.*, c.nome AS conta_nome, cat.nome AS categoria_nome, cc.nome AS centro_nome
            FROM fin_lancamentos l
            LEFT JOIN fin_contas        c   ON c.id  = l.conta_id
            LEFT JOIN fin_categorias    cat ON cat.id = l.categoria_id
            LEFT JOIN fin_centros_custo cc  ON cc.id  = l.centro_custo_id
            WHERE l.id=?
        """, (cur.lastrowid,)).fetchone())
    return {"lancamento": row}


@router.get("/api/fin/lancamentos/{lid}")
async def api_get_lancamento(lid: int, request: Request):
    _require(request)
    with get_db() as conn:
        row = conn.execute("""
            SELECT l.*, c.nome AS conta_nome, cat.nome AS categoria_nome, cc.nome AS centro_nome
            FROM fin_lancamentos l
            LEFT JOIN fin_contas        c   ON c.id  = l.conta_id
            LEFT JOIN fin_categorias    cat ON cat.id = l.categoria_id
            LEFT JOIN fin_centros_custo cc  ON cc.id  = l.centro_custo_id
            WHERE l.id=?
        """, (lid,)).fetchone()
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

    set_clause, vals = _build_set(fields)
    with get_db() as conn:
        conn.execute(f"UPDATE fin_lancamentos SET {set_clause} WHERE id=?", vals + [lid])
        conn.commit()
        row = conn.execute("""
            SELECT l.*, c.nome AS conta_nome, cat.nome AS categoria_nome, cc.nome AS centro_nome
            FROM fin_lancamentos l
            LEFT JOIN fin_contas        c   ON c.id  = l.conta_id
            LEFT JOIN fin_categorias    cat ON cat.id = l.categoria_id
            LEFT JOIN fin_centros_custo cc  ON cc.id  = l.centro_custo_id
            WHERE l.id=?
        """, (lid,)).fetchone()
    if not row:
        raise HTTPException(404, "Lançamento não encontrado")
    return {"lancamento": dict(row)}


@router.delete("/api/fin/lancamentos/{lid}")
async def api_delete_lancamento(lid: int, request: Request):
    _require(request)
    with get_db() as conn:
        conn.execute("DELETE FROM fin_lancamentos WHERE id=?", (lid,))
        conn.commit()
    return {"ok": True}


@router.patch("/api/fin/lancamentos/{lid}/quitar")
async def api_quitar_lancamento(lid: int, request: Request):
    _require(request)
    with get_db() as conn:
        row = conn.execute(
            "SELECT situacao FROM fin_lancamentos WHERE id=?", (lid,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Lançamento não encontrado")
        nova_situacao = "quitado" if row["situacao"] == "em_aberto" else "em_aberto"
        conn.execute(
            "UPDATE fin_lancamentos SET situacao=? WHERE id=?", (nova_situacao, lid)
        )
        conn.commit()
        updated = dict(conn.execute("""
            SELECT l.*, c.nome AS conta_nome, cat.nome AS categoria_nome, cc.nome AS centro_nome
            FROM fin_lancamentos l
            LEFT JOIN fin_contas        c   ON c.id  = l.conta_id
            LEFT JOIN fin_categorias    cat ON cat.id = l.categoria_id
            LEFT JOIN fin_centros_custo cc  ON cc.id  = l.centro_custo_id
            WHERE l.id=?
        """, (lid,)).fetchone())
    return {"lancamento": updated}


@router.patch("/api/fin/lancamentos/quitar-lote")
async def api_quitar_lote(request: Request):
    _require(request)
    data = await request.json()
    ids = [int(i) for i in (data.get("ids") or [])]
    acao = data.get("acao")  # "quitar" | "reabrir" | None (toggle)
    if not ids:
        raise HTTPException(400, "Nenhum id informado")
    with get_db() as conn:
        for lid in ids:
            row = conn.execute("SELECT situacao FROM fin_lancamentos WHERE id=?", (lid,)).fetchone()
            if not row:
                continue
            if acao == "quitar":
                nova = "quitado"
            elif acao == "reabrir":
                nova = "em_aberto"
            else:
                nova = "quitado" if row["situacao"] == "em_aberto" else "em_aberto"
            conn.execute("UPDATE fin_lancamentos SET situacao=? WHERE id=?", (nova, lid))
        conn.commit()
    return {"ok": True, "count": len(ids)}


# ── Transferências ────────────────────────────────────────────────────────────

@router.post("/api/fin/transferencias")
async def api_create_transferencia(request: Request):
    _require(request)
    data = await request.json()

    emissao     = (data.get("emissao") or "").strip()
    valor       = float(data.get("valor") or 0)
    de_conta_id = data.get("de_conta_id")
    para_conta_id = data.get("para_conta_id")
    comentario  = data.get("comentario") or None

    if not emissao:
        raise HTTPException(400, "emissao é obrigatório")
    if not de_conta_id or not para_conta_id:
        raise HTTPException(400, "de_conta_id e para_conta_id são obrigatórios")
    if de_conta_id == para_conta_id:
        raise HTTPException(400, "Conta de origem e destino devem ser diferentes")
    if valor <= 0:
        raise HTTPException(400, "valor deve ser maior que zero")

    now = datetime.now().isoformat()

    with get_db() as conn:
        # Verify both accounts exist
        for conta_id in (de_conta_id, para_conta_id):
            exists = conn.execute(
                "SELECT id FROM fin_contas WHERE id=?", (conta_id,)
            ).fetchone()
            if not exists:
                raise HTTPException(404, f"Conta {conta_id} não encontrada")

        # Create the transfer record
        cur = conn.execute(
            "INSERT INTO fin_transferencias (emissao, valor, de_conta_id, para_conta_id, comentario, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (emissao, valor, de_conta_id, para_conta_id, comentario, now),
        )
        transferencia_id = cur.lastrowid
        descricao = "Transferência entre contas"

        # Create lancamento PAGAR (debit from de_conta)
        conn.execute(
            """INSERT INTO fin_lancamentos
               (tipo, emissao, descricao, vencimento, valor, valor_total, conta_id, situacao, created_at)
               VALUES (?,?,?,?,?,?,?,'quitado',?)""",
            ("pagar", emissao, descricao, emissao, valor, valor, de_conta_id, now),
        )

        # Create lancamento RECEBER (credit to para_conta)
        conn.execute(
            """INSERT INTO fin_lancamentos
               (tipo, emissao, descricao, vencimento, valor, valor_total, conta_id, situacao, created_at)
               VALUES (?,?,?,?,?,?,?,'quitado',?)""",
            ("receber", emissao, descricao, emissao, valor, valor, para_conta_id, now),
        )

        conn.commit()
        transferencia = dict(conn.execute(
            "SELECT * FROM fin_transferencias WHERE id=?", (transferencia_id,)
        ).fetchone())

    return {"transferencia": transferencia}


@router.get("/api/fin/transferencias")
async def api_list_transferencias(request: Request, mes: str = None):
    _require(request)
    with get_db() as conn:
        sql    = """
            SELECT t.*,
                   dc.nome  AS de_conta_nome,
                   pc.nome  AS para_conta_nome
            FROM fin_transferencias t
            LEFT JOIN fin_contas dc ON dc.id = t.de_conta_id
            LEFT JOIN fin_contas pc ON pc.id = t.para_conta_id
            WHERE 1=1
        """
        params = []
        if mes:
            sql += " AND t.emissao LIKE ?"
            params.append(f"{mes}%")
        sql += " ORDER BY t.emissao DESC, t.id DESC"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return {"transferencias": rows}


# ── Saldos por conta ──────────────────────────────────────────────────────────

@router.get("/api/fin/saldos")
async def api_saldos(request: Request):
    _require(request)
    with get_db() as conn:
        contas = [dict(r) for r in conn.execute(
            "SELECT id, nome, saldo_inicial FROM fin_contas WHERE situacao='ativa' ORDER BY nome"
        ).fetchall()]

        saldos = []
        for conta in contas:
            cid = conta["id"]

            total_receber = conn.execute(
                "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos "
                "WHERE conta_id=? AND tipo='receber' AND situacao='quitado'",
                (cid,),
            ).fetchone()[0]

            total_pagar = conn.execute(
                "SELECT COALESCE(SUM(valor_total),0) FROM fin_lancamentos "
                "WHERE conta_id=? AND tipo='pagar' AND situacao='quitado'",
                (cid,),
            ).fetchone()[0]

            saldo = round(
                float(conta["saldo_inicial"]) + float(total_receber) - float(total_pagar),
                2,
            )
            saldos.append({
                "conta_id":   cid,
                "conta_nome": conta["nome"],
                "saldo":      saldo,
            })

    return {"saldos": saldos}


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
):
    _require(request)
    content = await arquivo.read()
    filename = arquivo.filename or "arquivo"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext == "pdf":
        text = _extract_text_pdf(content)
        tipo_arquivo = "pdf"
    elif ext in ("csv", "txt", "ofx", "ret"):
        text = _extract_text_csv(content)
        tipo_arquivo = ext
    else:
        raise HTTPException(400, "Formato não suportado. Use PDF ou CSV.")

    if not text.strip():
        raise HTTPException(400, "Não foi possível extrair texto do arquivo.")

    lancamentos = _call_ia(text)
    if not lancamentos:
        raise HTTPException(422, "Nenhum lançamento encontrado no arquivo.")

    cid = int(conta_id) if conta_id and conta_id.isdigit() else None

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO fin_importacoes (nome_arquivo, tipo_arquivo, conta_id, total_itens) VALUES (?,?,?,?)",
            (filename, tipo_arquivo, cid, len(lancamentos)),
        )
        imp_id = cur.lastrowid
        for l in lancamentos:
            db.execute(
                """INSERT INTO fin_importacao_itens
                   (importacao_id, data_lancamento, descricao, valor, tipo, categoria_sugerida, conta_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (imp_id, l.get("data"), l.get("descricao"), l.get("valor"), l.get("tipo"), l.get("categoria_sugerida"), cid),
            )
        db.commit()
        return {"id": imp_id, "total": len(lancamentos)}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.get("/api/fin/importacoes")
async def listar_importacoes(request: Request):
    _require(request)
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM fin_importacoes ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        return {"importacoes": [dict(r) for r in rows]}
    finally:
        db.close()


@router.get("/api/fin/importacoes/{imp_id}/itens")
async def listar_itens(imp_id: int, request: Request):
    _require(request)
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM fin_importacao_itens WHERE importacao_id=? ORDER BY data_lancamento, id",
            (imp_id,),
        ).fetchall()
        return {"itens": [dict(r) for r in rows]}
    finally:
        db.close()


@router.put("/api/fin/importacao-item/{item_id}")
async def atualizar_item(item_id: int, request: Request):
    _require(request)
    body = await request.json()
    campos = ["data_lancamento", "descricao", "valor", "tipo", "categoria_id", "conta_id"]
    sets = ", ".join(f"{c}=?" for c in campos if c in body)
    vals = [body[c] for c in campos if c in body] + [item_id]
    if not sets:
        return {}
    db = get_db()
    try:
        db.execute(f"UPDATE fin_importacao_itens SET {sets} WHERE id=?", vals)
        # For approved items, mirror changes to the associated lancamento
        item = db.execute(
            "SELECT status, lancamento_id FROM fin_importacao_itens WHERE id=?", (item_id,)
        ).fetchone()
        if item and item["status"] == "aprovado" and item["lancamento_id"]:
            lanc_map = {
                "data_lancamento": ["emissao", "vencimento"],
                "descricao": ["descricao"],
                "valor": ["valor", "valor_total"],
                "categoria_id": ["categoria_id"],
            }
            lsets, lvals = [], []
            for src, dsts in lanc_map.items():
                if src in body:
                    for dst in dsts:
                        lsets.append(f"{dst}=?")
                        lvals.append(body[src])
            if "tipo" in body:
                lsets.append("tipo=?")
                lvals.append("pagar" if body["tipo"] == "despesa" else "receber")
            if lsets:
                lvals.append(item["lancamento_id"])
                db.execute(f"UPDATE fin_lancamentos SET {', '.join(lsets)} WHERE id=?", lvals)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.post("/api/fin/importacoes/{imp_id}/aprovar")
async def aprovar_itens(imp_id: int, request: Request):
    _require(request)
    body = await request.json()
    item_ids = body.get("item_ids", [])
    db = get_db()
    try:
        aprovados = 0
        today = date.today().isoformat()
        for iid in item_ids:
            item = db.execute(
                "SELECT * FROM fin_importacao_itens WHERE id=? AND importacao_id=? AND status='pendente'",
                (iid, imp_id),
            ).fetchone()
            if not item:
                continue
            dt = item["data_lancamento"] or today
            tipo_lanc = "pagar" if item["tipo"] == "despesa" else "receber"
            cur = db.execute(
                """INSERT INTO fin_lancamentos
                   (tipo, emissao, descricao, vencimento, valor, valor_total, conta_id, categoria_id, situacao, origem)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (tipo_lanc, dt, item["descricao"] or "—", dt,
                 item["valor"] or 0, item["valor"] or 0,
                 item["conta_id"], item["categoria_id"], "quitado", "cc"),
            )
            db.execute(
                "UPDATE fin_importacao_itens SET status='aprovado', lancamento_id=? WHERE id=?",
                (cur.lastrowid, iid),
            )
            aprovados += 1
        db.execute(
            """UPDATE fin_importacoes
               SET itens_aprovados=(SELECT COUNT(*) FROM fin_importacao_itens WHERE importacao_id=? AND status='aprovado')
               WHERE id=?""",
            (imp_id, imp_id),
        )
        db.commit()
        return {"aprovados": aprovados}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.delete("/api/fin/importacao-item/{item_id}")
async def rejeitar_item(item_id: int, request: Request):
    _require(request)
    db = get_db()
    try:
        item = db.execute(
            "SELECT status, lancamento_id, importacao_id FROM fin_importacao_itens WHERE id=?",
            (item_id,),
        ).fetchone()
        if not item:
            raise HTTPException(404, "Item não encontrado")
        lancamento_id = item["lancamento_id"]
        # NULL out the FK first so the DELETE doesn't trip the FK constraint
        db.execute(
            "UPDATE fin_importacao_itens SET status='rejeitado', lancamento_id=NULL WHERE id=?",
            (item_id,),
        )
        if lancamento_id:
            db.execute("DELETE FROM fin_lancamentos WHERE id=?", (lancamento_id,))
        if item["importacao_id"]:
            db.execute(
                """UPDATE fin_importacoes
                   SET itens_aprovados=(SELECT COUNT(*) FROM fin_importacao_itens WHERE importacao_id=? AND status='aprovado')
                   WHERE id=?""",
                (item["importacao_id"], item["importacao_id"]),
            )
        db.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


# ── Dashboard Analytics ────────────────────────────────────────────────────────

_DASH_CACHE: dict = {}   # keyed by (de, ate)
_DASH_CACHE_TS: dict = {}
_DASH_CACHE_TTL = 300
_MESES_ABBR_LIST = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']


_SIGA_DIR = BASE_DIR / 'Financeiro'
_MES_PT_MAP = {'Jan':'Jan','Feb':'Fev','Mar':'Mar','Apr':'Abr','May':'Mai',
               'Jun':'Jun','Jul':'Jul','Aug':'Ago','Sep':'Set','Oct':'Out',
               'Nov':'Nov','Dec':'Dez'}

def _mes_to_ym(mes: str):
    if '-' in mes:  # "2024-09" → (2024, 9)
        y, m = mes.split('-')
        return (int(y), int(m))
    abbr, yy = mes.split('/')  # "Set/24" → (2024, 9)
    return (2000 + int(yy), _MESES_ABBR_LIST.index(abbr) + 1)


def _parse_siga(de: str = None, ate: str = None):
    import openpyxl as _xl
    from collections import defaultdict
    from datetime import datetime as _dt

    siga_files = sorted(p for p in _SIGA_DIR.glob('*.xlsx') if 'Financeiro' in p.name)
    if not siga_files:
        return {}

    def _pd(v):
        if not v: return None
        try: return _dt.strptime(str(v).strip(), '%d/%m/%Y').date()
        except: return None
    def _pv(v):
        if v is None or v == '': return 0.0
        try: return float(v)
        except: return 0.0
    def _mes(d):
        return f"{_MES_PT_MAP.get(d.strftime('%b'), d.strftime('%b'))}/{d.strftime('%y')}"
    def _sort_key(mes):
        abbr, yy = mes.split('/')
        meses = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
        return (int(yy), meses.index(abbr) if abbr in meses else 0)

    # Collect rows from all files; deduplicate by (emissao, quitado, contato, descricao, receber, pagar)
    seen = set()
    rows = []
    for path in siga_files:
        wb = _xl.load_workbook(str(path), data_only=True, read_only=True)
        ws = wb.active
        for row in ws.iter_rows(min_row=6, values_only=True):
            if not row[0] or str(row[0]).strip() in ('Total', ''): continue
            emissao   = _pd(row[0])
            quitado   = _pd(row[2])
            contato   = str(row[5] or '').strip()
            descricao = str(row[6] or '').strip()
            centro    = str(row[8] or '').strip()
            conta     = str(row[9] or '').strip()
            receber   = _pv(row[13])
            pagar     = _pv(row[14])
            key = (emissao, quitado, contato, descricao, receber, pagar)
            if key in seen:
                continue
            seen.add(key)
            rows.append({'emissao': emissao, 'quitado': quitado, 'contato': contato,
                         'descricao': descricao, 'centro': centro, 'conta': conta,
                         'receber': receber, 'pagar': pagar})
        wb.close()

    # Date range filter (by quitado month)
    ym_de  = _mes_to_ym(de)  if de  else None
    ym_ate = _mes_to_ym(ate) if ate else None

    def _in_range(d):
        ym = (d.year, d.month)
        if ym_de  and ym < ym_de:  return False
        if ym_ate and ym > ym_ate: return False
        return True

    # Monthly by quitado date (caixa) — matches SiGA UI criterion
    quitadas = [r for r in rows if r['quitado'] and _in_range(r['quitado'])]
    by_month: dict = defaultdict(lambda: {'receber': 0.0, 'pagar': 0.0, 'n': 0})
    for r in quitadas:
        k = _mes(r['quitado'])
        by_month[k]['receber'] += r['receber']
        by_month[k]['pagar']   += r['pagar']
        by_month[k]['n']       += 1

    siga_mensal = [{'mes': k, 'receber': round(v['receber'],2), 'pagar': round(v['pagar'],2),
                    'lucro': round(v['receber'] - v['pagar'], 2), 'n': v['n']}
                   for k, v in sorted(by_month.items(), key=lambda x: _sort_key(x[0]))]

    # Top clientes (by receber)
    cli: dict = defaultdict(float)
    for r in quitadas:
        if r['receber'] > 0: cli[r['contato']] += r['receber']
    top_clientes = [{'nome': k, 'total': round(v,2)}
                    for k, v in sorted(cli.items(), key=lambda x: -x[1])[:12]]

    # Top fornecedores (by pagar)
    forn: dict = defaultdict(float)
    for r in quitadas:
        if r['pagar'] > 0: forn[r['contato']] += r['pagar']
    top_fornecedores = [{'nome': k, 'total': round(v,2)}
                        for k, v in sorted(forn.items(), key=lambda x: -x[1])[:12]]

    # Centro de custos breakdown
    cc: dict = defaultdict(float)
    for r in quitadas:
        if r['pagar'] > 0 and r['centro'] and r['centro'] not in ('Sem Centro de Custos',''):
            cc[r['centro']] += r['pagar']
    centros = [{'nome': k, 'total': round(v,2)}
               for k, v in sorted(cc.items(), key=lambda x: -x[1])]

    return {'siga_mensal': siga_mensal, 'top_clientes': top_clientes,
            'top_fornecedores': top_fornecedores, 'centros': centros}


def _fetch_dashboard(de: str = None, ate: str = None):
    return _parse_siga(de=de, ate=ate)


@router.get("/api/fin/cartao-analise")
async def cartao_analise(request: Request, de: str = None, ate: str = None):
    _require(request)
    with get_db() as conn:
        filters = ["i.status = 'aprovado'", "i.tipo = 'despesa'"]
        params: list = []
        if de:
            filters.append("i.data_lancamento >= ?")
            params.append(de)
        if ate:
            filters.append("i.data_lancamento <= ?")
            params.append(ate + "-31")
        where = " AND ".join(filters)
        itens = conn.execute(f"""
            SELECT i.data_lancamento, i.descricao, i.valor, i.tipo,
                   i.categoria_id, c.nome AS categoria_nome,
                   i.importacao_id,
                   imp.nome_arquivo, imp.created_at AS imp_created_at
            FROM fin_importacao_itens i
            JOIN fin_importacoes imp ON imp.id = i.importacao_id
            LEFT JOIN fin_categorias c ON c.id = i.categoria_id
            WHERE {where}
            ORDER BY i.data_lancamento
        """, params).fetchall()
        itens = [dict(r) for r in itens]

        # Faturas summary (per importacao)
        faturas_raw = conn.execute("""
            SELECT imp.id, imp.nome_arquivo, imp.created_at,
                   imp.total_itens, imp.itens_aprovados,
                   COALESCE(SUM(CASE WHEN i.tipo='despesa' AND i.status='aprovado' THEN i.valor ELSE 0 END),0) AS total
            FROM fin_importacoes imp
            LEFT JOIN fin_importacao_itens i ON i.importacao_id = imp.id
            GROUP BY imp.id
            ORDER BY imp.created_at DESC
        """).fetchall()
        faturas = [dict(r) for r in faturas_raw]

    from collections import defaultdict

    # Monthly totals — convert YYYY-MM to "Abr/26" label, sort by raw key
    def _ym_label(yyyymm):
        try:
            y, m = yyyymm.split('-')
            return f"{_MESES_ABBR_LIST[int(m)-1]}/{y[2:]}"
        except Exception:
            return yyyymm

    by_month: dict = defaultdict(float)
    for it in itens:
        raw = (it['data_lancamento'] or '')[:7]
        if raw:
            by_month[raw] += it['valor'] or 0
    mensal = [{'mes': _ym_label(k), 'total': round(v, 2)} for k, v in sorted(by_month.items())]

    # By category
    by_cat = defaultdict(float)
    for it in itens:
        cat = it['categoria_nome'] or 'Sem categoria'
        by_cat[cat] += it['valor'] or 0
    por_categoria = [{'categoria': k, 'total': round(v, 2)}
                     for k, v in sorted(by_cat.items(), key=lambda x: -x[1])]

    # Top merchants
    by_merchant = defaultdict(float)
    for it in itens:
        desc = (it['descricao'] or 'Outros').strip()
        by_merchant[desc] += it['valor'] or 0
    top_merchants = [{'nome': k, 'total': round(v, 2)}
                     for k, v in sorted(by_merchant.items(), key=lambda x: -x[1])[:15]]

    total_gasto = round(sum(v for v in by_month.values()), 2)
    media_mensal = round(total_gasto / len(by_month), 2) if by_month else 0
    melhor_mes = max(mensal, key=lambda x: x['total']) if mensal else None

    return {
        'faturas': faturas,
        'mensal': mensal,
        'por_categoria': por_categoria,
        'top_merchants': top_merchants,
        'total_gasto': total_gasto,
        'media_mensal': media_mensal,
        'melhor_mes': melhor_mes,
    }


@router.get("/api/fin/dashboard-sheets")
async def dashboard_sheets(request: Request, de: str = None, ate: str = None):
    _require(request)
    import asyncio
    global _DASH_CACHE, _DASH_CACHE_TS
    key = (de or '', ate or '')
    now = _time.time()
    if key in _DASH_CACHE and now - _DASH_CACHE_TS.get(key, 0) < _DASH_CACHE_TTL:
        return _DASH_CACHE[key]
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: _fetch_dashboard(de, ate))
    except Exception as e:
        raise HTTPException(500, f"Erro ao ler planilhas: {e}")
    _DASH_CACHE[key] = data
    _DASH_CACHE_TS[key] = now
    return data
