import io
import os
import json
import re
import time as _time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import anthropic as _anthropic

from backend.gestao import _verify
from backend.db import get_conn, dict_cursor

router   = APIRouter()
BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))

_MESES = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']


# ── Auth ──────────────────────────────────────────────────────────────────────

def _has_fp_access(username: str) -> bool:
    try:
        conn = get_conn()
        cur = dict_cursor(conn)
        cur.execute("SELECT fin_pessoais FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return bool(row and row["fin_pessoais"])
    except Exception:
        return username in {"victor", "jadna"}


def _fp_verify(request: Request):
    user = _verify(request)
    return user if user and _has_fp_access(user) else None


def _fp_require(request: Request) -> str:
    user = _fp_verify(request)
    if not user:
        raise HTTPException(403, "Acesso restrito")
    return user


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    return get_conn()


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fp_contas (
                id            SERIAL PRIMARY KEY,
                nome          TEXT    NOT NULL,
                situacao      TEXT    DEFAULT 'ativa',
                saldo_inicial REAL    DEFAULT 0,
                created_at    TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fp_categorias (
                id         SERIAL PRIMARY KEY,
                nome       TEXT NOT NULL,
                tipo       TEXT DEFAULT 'despesa' CHECK(tipo IN ('receita','despesa')),
                situacao   TEXT DEFAULT 'ativa',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fp_lancamentos (
                id             SERIAL PRIMARY KEY,
                tipo           TEXT    NOT NULL CHECK(tipo IN ('receita','despesa')),
                emissao        TEXT    NOT NULL,
                descricao      TEXT    NOT NULL,
                vencimento     TEXT,
                valor          REAL    DEFAULT 0,
                valor_total    REAL    DEFAULT 0,
                conta_id       INTEGER REFERENCES fp_contas(id),
                categoria_id   INTEGER REFERENCES fp_categorias(id),
                meio_pagamento TEXT,
                situacao       TEXT    DEFAULT 'em_aberto' CHECK(situacao IN ('em_aberto','quitado')),
                created_at     TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fp_importacoes (
                id              SERIAL PRIMARY KEY,
                nome_arquivo    TEXT    NOT NULL,
                tipo_arquivo    TEXT    NOT NULL,
                conta_id        INTEGER REFERENCES fp_contas(id),
                total_itens     INTEGER DEFAULT 0,
                itens_aprovados INTEGER DEFAULT 0,
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fp_importacao_itens (
                id                 SERIAL PRIMARY KEY,
                importacao_id      INTEGER NOT NULL REFERENCES fp_importacoes(id) ON DELETE CASCADE,
                data_lancamento    TEXT,
                descricao          TEXT,
                valor              REAL,
                tipo               TEXT CHECK(tipo IN ('receita','despesa')),
                categoria_sugerida TEXT,
                categoria_id       INTEGER REFERENCES fp_categorias(id),
                conta_id           INTEGER REFERENCES fp_contas(id),
                status             TEXT DEFAULT 'pendente' CHECK(status IN ('pendente','aprovado','rejeitado')),
                lancamento_id      INTEGER REFERENCES fp_lancamentos(id),
                created_at         TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ── Page ──────────────────────────────────────────────────────────────────────

@router.get("/financas-pessoais", response_class=HTMLResponse)
async def page_fp(request: Request):
    user = _fp_verify(request)
    if not user:
        return RedirectResponse("/login")
    _FP_ALL = {"victor", "jadna"}
    _ADMIN  = {"victor"}
    resp = templates.TemplateResponse("financas_pessoais.html", {
        "request":          request,
        "nav_username":     user.capitalize(),
        "nav_user":         user,
        "nav_is_admin":     user in _ADMIN,
        "nav_fin_pessoais": True,
        "active_page":      "financas_pessoais",
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


# ── Contas ────────────────────────────────────────────────────────────────────

@router.get("/api/fp/contas")
async def fp_list_contas(request: Request):
    _fp_require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM fp_contas WHERE situacao='ativa' ORDER BY nome")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"contas": rows}


@router.post("/api/fp/contas")
async def fp_create_conta(request: Request):
    _fp_require(request)
    data = await request.json()
    nome = (data.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "INSERT INTO fp_contas (nome, situacao, saldo_inicial) VALUES (%s,%s,%s) RETURNING id",
            (nome, data.get("situacao", "ativa"), float(data.get("saldo_inicial") or 0)),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.execute("SELECT * FROM fp_contas WHERE id=%s", (new_id,))
        row = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"conta": row}


@router.put("/api/fp/contas/{cid}")
async def fp_update_conta(cid: int, request: Request):
    _fp_require(request)
    data   = await request.json()
    fields = {k: data[k] for k in ("nome", "situacao", "saldo_inicial") if k in data}
    if "nome" in fields:
        fields["nome"] = (fields["nome"] or "").strip()
        if not fields["nome"]:
            raise HTTPException(400, "nome não pode ser vazio")
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    clause = ", ".join(f"{k}=%s" for k in fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE fp_contas SET {clause} WHERE id=%s", list(fields.values()) + [cid])
        conn.commit()
        cur.execute("SELECT * FROM fp_contas WHERE id=%s", (cid,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Conta não encontrada")
    return {"conta": dict(row)}


@router.delete("/api/fp/contas/{cid}")
async def fp_delete_conta(cid: int, request: Request):
    _fp_require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT COUNT(*) FROM fp_lancamentos WHERE conta_id=%s", (cid,))
        in_use = cur.fetchone()["count"]
        if in_use:
            raise HTTPException(409, "Conta em uso em lançamentos")
        cur.execute("DELETE FROM fp_contas WHERE id=%s", (cid,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


# ── Categorias ────────────────────────────────────────────────────────────────

@router.get("/api/fp/categorias")
async def fp_list_categorias(request: Request, tipo: str = None):
    _fp_require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        sql, params = "SELECT * FROM fp_categorias WHERE 1=1", []
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


@router.post("/api/fp/categorias")
async def fp_create_categoria(request: Request):
    _fp_require(request)
    data = await request.json()
    nome = (data.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "nome é obrigatório")
    tipo = data.get("tipo", "despesa")
    if tipo not in ("receita", "despesa"):
        raise HTTPException(400, "tipo inválido")
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "INSERT INTO fp_categorias (nome, tipo, situacao) VALUES (%s,%s,%s) RETURNING id",
            (nome, tipo, data.get("situacao", "ativa")),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.execute("SELECT * FROM fp_categorias WHERE id=%s", (new_id,))
        row = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"categoria": row}


@router.put("/api/fp/categorias/{cid}")
async def fp_update_categoria(cid: int, request: Request):
    _fp_require(request)
    data   = await request.json()
    fields = {k: data[k] for k in ("nome", "tipo", "situacao") if k in data}
    if "nome" in fields:
        fields["nome"] = (fields["nome"] or "").strip()
        if not fields["nome"]:
            raise HTTPException(400, "nome inválido")
    if "tipo" in fields and fields["tipo"] not in ("receita", "despesa"):
        raise HTTPException(400, "tipo inválido")
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    clause = ", ".join(f"{k}=%s" for k in fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE fp_categorias SET {clause} WHERE id=%s", list(fields.values()) + [cid])
        conn.commit()
        cur.execute("SELECT * FROM fp_categorias WHERE id=%s", (cid,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Categoria não encontrada")
    return {"categoria": dict(row)}


@router.delete("/api/fp/categorias/{cid}")
async def fp_delete_categoria(cid: int, request: Request):
    _fp_require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM fp_categorias WHERE id=%s", (cid,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


# ── Lançamentos ───────────────────────────────────────────────────────────────

_FP_JOIN = """
    SELECT l.*, c.nome AS conta_nome, cat.nome AS categoria_nome
    FROM fp_lancamentos l
    LEFT JOIN fp_contas     c   ON c.id   = l.conta_id
    LEFT JOIN fp_categorias cat ON cat.id = l.categoria_id
"""


@router.get("/api/fp/lancamentos")
async def fp_list_lancamentos(request: Request, mes: str = None):
    _fp_require(request)
    today = date.today().isoformat()
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        sql = _FP_JOIN + " WHERE 1=1"
        params = []
        if mes:
            sql += " AND l.vencimento LIKE %s"
            params.append(f"{mes}%")
        sql += " ORDER BY l.vencimento, l.id"
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]

        like = f"{mes}%" if mes else None

        def _sum(q, p=None):
            cur.execute(q, p or [])
            return cur.fetchone()["coalesce"]

        em_aberto = _sum(
            "SELECT COALESCE(SUM(valor_total),0) FROM fp_lancamentos WHERE situacao='em_aberto' AND vencimento<=%s",
            (today,),
        )
        a_receber = _sum(
            "SELECT COALESCE(SUM(valor_total),0) FROM fp_lancamentos WHERE tipo='receita' AND situacao='em_aberto'" + (" AND vencimento LIKE %s" if like else ""),
            ([like] if like else None),
        )
        a_pagar = _sum(
            "SELECT COALESCE(SUM(valor_total),0) FROM fp_lancamentos WHERE tipo='despesa' AND situacao='em_aberto'" + (" AND vencimento LIKE %s" if like else ""),
            ([like] if like else None),
        )
        recebidos = _sum(
            "SELECT COALESCE(SUM(valor_total),0) FROM fp_lancamentos WHERE tipo='receita' AND situacao='quitado'" + (" AND vencimento LIKE %s" if like else ""),
            ([like] if like else None),
        )
        pagos = _sum(
            "SELECT COALESCE(SUM(valor_total),0) FROM fp_lancamentos WHERE tipo='despesa' AND situacao='quitado'" + (" AND vencimento LIKE %s" if like else ""),
            ([like] if like else None),
        )

        cur.execute("SELECT id, nome, saldo_inicial FROM fp_contas WHERE situacao='ativa'")
        contas = [dict(r) for r in cur.fetchall()]
        saldos = []
        for ct in contas:
            rec = _sum(
                "SELECT COALESCE(SUM(valor_total),0) FROM fp_lancamentos WHERE conta_id=%s AND tipo='receita' AND situacao='quitado'",
                (ct["id"],),
            )
            pag = _sum(
                "SELECT COALESCE(SUM(valor_total),0) FROM fp_lancamentos WHERE conta_id=%s AND tipo='despesa' AND situacao='quitado'",
                (ct["id"],),
            )
            saldos.append({"conta_id": ct["id"], "conta_nome": ct["nome"],
                           "saldo": round(ct["saldo_inicial"] + float(rec) - float(pag), 2)})
    finally:
        cur.close()
        conn.close()

    return {
        "lancamentos": rows,
        "saldos": saldos,
        "summary": {
            "em_aberto": round(float(em_aberto), 2),
            "a_receber":  round(float(a_receber), 2),
            "a_pagar":    round(float(a_pagar), 2),
            "recebidos":  round(float(recebidos), 2),
            "pagos":      round(float(pagos), 2),
        },
    }


@router.post("/api/fp/lancamentos")
async def fp_create_lancamento(request: Request):
    _fp_require(request)
    data = await request.json()
    tipo      = data.get("tipo")
    emissao   = (data.get("emissao") or "").strip()
    descricao = (data.get("descricao") or "").strip()
    if tipo not in ("receita", "despesa"):
        raise HTTPException(400, "tipo inválido")
    if not emissao or not descricao:
        raise HTTPException(400, "emissao e descricao são obrigatórios")
    valor       = float(data.get("valor") or 0)
    valor_total = float(data.get("valor_total") or valor)
    fields = {
        "tipo":           tipo,
        "emissao":        emissao,
        "descricao":      descricao,
        "vencimento":     data.get("vencimento") or emissao,
        "valor":          valor,
        "valor_total":    valor_total,
        "conta_id":       data.get("conta_id") or None,
        "categoria_id":   data.get("categoria_id") or None,
        "meio_pagamento": data.get("meio_pagamento") or None,
        "situacao":       data.get("situacao", "em_aberto"),
    }
    cols = ", ".join(fields.keys())
    phs  = ", ".join("%s" for _ in fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            f"INSERT INTO fp_lancamentos ({cols}) VALUES ({phs}) RETURNING id",
            list(fields.values()),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.execute(_FP_JOIN + " WHERE l.id=%s", (new_id,))
        row = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"lancamento": row}


@router.put("/api/fp/lancamentos/{lid}")
async def fp_update_lancamento(lid: int, request: Request):
    _fp_require(request)
    data    = await request.json()
    allowed = {"tipo","emissao","descricao","vencimento","valor","valor_total","conta_id","categoria_id","meio_pagamento","situacao"}
    fields  = {k: data[k] for k in allowed if k in data}
    if "tipo" in fields and fields["tipo"] not in ("receita","despesa"):
        raise HTTPException(400, "tipo inválido")
    if "situacao" in fields and fields["situacao"] not in ("em_aberto","quitado"):
        raise HTTPException(400, "situacao inválida")
    if not fields:
        raise HTTPException(400, "Nenhum campo válido")
    clause = ", ".join(f"{k}=%s" for k in fields)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE fp_lancamentos SET {clause} WHERE id=%s", list(fields.values()) + [lid])
        conn.commit()
        cur.execute(_FP_JOIN + " WHERE l.id=%s", (lid,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Lançamento não encontrado")
    return {"lancamento": dict(row)}


@router.delete("/api/fp/lancamentos/{lid}")
async def fp_delete_lancamento(lid: int, request: Request):
    _fp_require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM fp_lancamentos WHERE id=%s", (lid,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.patch("/api/fp/lancamentos/{lid}/quitar")
async def fp_quitar_lancamento(lid: int, request: Request):
    _fp_require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT situacao FROM fp_lancamentos WHERE id=%s", (lid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Lançamento não encontrado")
        nova = "quitado" if row["situacao"] == "em_aberto" else "em_aberto"
        cur.execute("UPDATE fp_lancamentos SET situacao=%s WHERE id=%s", (nova, lid))
        conn.commit()
        cur.execute(_FP_JOIN + " WHERE l.id=%s", (lid,))
        updated = dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()
    return {"lancamento": updated}


# ── Importação ────────────────────────────────────────────────────────────────

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
        raise HTTPException(500, "pdfplumber não instalado")


def _extract_text_csv(content: bytes) -> str:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return content.decode(enc)
        except Exception:
            continue
    return content.decode("utf-8", errors="replace")


_PROMPT = """Você é um assistente financeiro especializado em extrair lançamentos de extratos bancários e faturas de cartão de crédito brasileiros.

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
- Ignore saldos, totais e linhas de cabeçalho

Texto do extrato:
"""


def _call_ia(text: str) -> list:
    ai   = _anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": _PROMPT + text[:10000]}],
    )
    raw = resp.content[0].text.strip()
    m   = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise HTTPException(500, "IA não retornou JSON válido")
    return json.loads(m.group()).get("lancamentos", [])


@router.post("/api/fp/importar")
async def fp_importar(request: Request, arquivo: UploadFile = File(...), conta_id: str = Form(None)):
    _fp_require(request)
    content  = await arquivo.read()
    filename = arquivo.filename or "arquivo"
    ext      = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

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
            "INSERT INTO fp_importacoes (nome_arquivo, tipo_arquivo, conta_id, total_itens) VALUES (%s,%s,%s,%s) RETURNING id",
            (filename, tipo_arquivo, cid, len(lancamentos)),
        )
        imp_id = cur.fetchone()["id"]
        for l in lancamentos:
            cur.execute(
                "INSERT INTO fp_importacao_itens (importacao_id, data_lancamento, descricao, valor, tipo, categoria_sugerida, conta_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
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


@router.get("/api/fp/importacoes")
async def fp_list_importacoes(request: Request):
    _fp_require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM fp_importacoes ORDER BY created_at DESC LIMIT 20")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"importacoes": rows}


@router.get("/api/fp/importacoes/{imp_id}/itens")
async def fp_list_itens(imp_id: int, request: Request):
    _fp_require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT * FROM fp_importacao_itens WHERE importacao_id=%s ORDER BY data_lancamento, id",
            (imp_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"itens": rows}


@router.put("/api/fp/importacao-item/{item_id}")
async def fp_atualizar_item(item_id: int, request: Request):
    _fp_require(request)
    body   = await request.json()
    campos = ["data_lancamento", "descricao", "valor", "tipo", "categoria_id", "conta_id"]
    sets   = ", ".join(f"{c}=%s" for c in campos if c in body)
    vals   = [body[c] for c in campos if c in body] + [item_id]
    if not sets:
        return {}
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(f"UPDATE fp_importacao_itens SET {sets} WHERE id=%s", vals)
        cur.execute("SELECT status, lancamento_id FROM fp_importacao_itens WHERE id=%s", (item_id,))
        item = cur.fetchone()
        if item and item["status"] == "aprovado" and item["lancamento_id"]:
            lanc_map = {
                "data_lancamento": ["emissao","vencimento"],
                "descricao":       ["descricao"],
                "valor":           ["valor","valor_total"],
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
                lvals.append("despesa" if body["tipo"] == "despesa" else "receita")
            if lsets:
                lvals.append(item["lancamento_id"])
                cur.execute(f"UPDATE fp_lancamentos SET {', '.join(lsets)} WHERE id=%s", lvals)
        conn.commit()
        return {"ok": True}
    finally:
        cur.close()
        conn.close()


@router.post("/api/fp/importacoes/{imp_id}/aprovar")
async def fp_aprovar_itens(imp_id: int, request: Request):
    _fp_require(request)
    body     = await request.json()
    item_ids = body.get("item_ids", [])
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        aprovados = 0
        today     = date.today().isoformat()
        for iid in item_ids:
            cur.execute(
                "SELECT * FROM fp_importacao_itens WHERE id=%s AND importacao_id=%s AND status='pendente'",
                (iid, imp_id),
            )
            item = cur.fetchone()
            if not item:
                continue
            item = dict(item)
            dt   = item["data_lancamento"] or today
            tipo = item["tipo"] or "despesa"
            cur.execute(
                "INSERT INTO fp_lancamentos (tipo, emissao, descricao, vencimento, valor, valor_total, conta_id, categoria_id, situacao) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'quitado') RETURNING id",
                (tipo, dt, item["descricao"] or "—", dt,
                 item["valor"] or 0, item["valor"] or 0,
                 item["conta_id"], item["categoria_id"]),
            )
            lanc_id = cur.fetchone()["id"]
            cur.execute(
                "UPDATE fp_importacao_itens SET status='aprovado', lancamento_id=%s WHERE id=%s",
                (lanc_id, iid),
            )
            aprovados += 1
        cur.execute(
            "UPDATE fp_importacoes SET itens_aprovados=(SELECT COUNT(*) FROM fp_importacao_itens WHERE importacao_id=%s AND status='aprovado') WHERE id=%s",
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


@router.delete("/api/fp/importacao-item/{item_id}")
async def fp_rejeitar_item(item_id: int, request: Request):
    _fp_require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT status, lancamento_id, importacao_id FROM fp_importacao_itens WHERE id=%s",
            (item_id,),
        )
        item = cur.fetchone()
        if not item:
            raise HTTPException(404, "Item não encontrado")
        item = dict(item)
        lancamento_id = item["lancamento_id"]
        cur.execute(
            "UPDATE fp_importacao_itens SET status='rejeitado', lancamento_id=NULL WHERE id=%s",
            (item_id,),
        )
        if lancamento_id:
            cur.execute("DELETE FROM fp_lancamentos WHERE id=%s", (lancamento_id,))
        if item["importacao_id"]:
            cur.execute(
                "UPDATE fp_importacoes SET itens_aprovados=(SELECT COUNT(*) FROM fp_importacao_itens WHERE importacao_id=%s AND status='aprovado') WHERE id=%s",
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


# ── Análise CC ────────────────────────────────────────────────────────────────

@router.get("/api/fp/cartao-analise")
async def fp_cartao_analise(request: Request, de: str = None, ate: str = None):
    _fp_require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        filters = ["i.status='aprovado'", "i.tipo='despesa'"]
        params: list = []
        if de:
            filters.append("i.data_lancamento >= %s")
            params.append(de)
        if ate:
            filters.append("i.data_lancamento <= %s")
            params.append(ate + "-31")
        where = " AND ".join(filters)
        cur.execute(f"""
            SELECT i.data_lancamento, i.descricao, i.valor, i.tipo,
                   i.categoria_id, c.nome AS categoria_nome,
                   i.importacao_id, imp.nome_arquivo, imp.created_at AS imp_created_at
            FROM fp_importacao_itens i
            JOIN fp_importacoes imp ON imp.id = i.importacao_id
            LEFT JOIN fp_categorias c ON c.id = i.categoria_id
            WHERE {where}
            ORDER BY i.data_lancamento
        """, params)
        itens = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT imp.id, imp.nome_arquivo, imp.created_at,
                   imp.total_itens, imp.itens_aprovados,
                   COALESCE(SUM(CASE WHEN i.tipo='despesa' AND i.status='aprovado' THEN i.valor ELSE 0 END),0) AS total
            FROM fp_importacoes imp
            LEFT JOIN fp_importacao_itens i ON i.importacao_id = imp.id
            GROUP BY imp.id
            ORDER BY imp.created_at DESC
        """)
        faturas = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    def _ym_label(yyyymm):
        try:
            y, m = yyyymm.split('-')
            return f"{_MESES[int(m)-1]}/{y[2:]}"
        except Exception:
            return yyyymm

    by_month: dict = defaultdict(float)
    for it in itens:
        raw = (it['data_lancamento'] or '')[:7]
        if raw:
            by_month[raw] += it['valor'] or 0
    mensal = [{'mes': _ym_label(k), 'total': round(v, 2)} for k, v in sorted(by_month.items())]

    by_cat: dict = defaultdict(float)
    for it in itens:
        by_cat[it['categoria_nome'] or 'Sem categoria'] += it['valor'] or 0
    por_categoria = [{'categoria': k, 'total': round(v, 2)} for k, v in sorted(by_cat.items(), key=lambda x: -x[1])]

    by_merch: dict = defaultdict(float)
    for it in itens:
        by_merch[(it['descricao'] or 'Outros').strip()] += it['valor'] or 0
    top_merchants = [{'nome': k, 'total': round(v, 2)} for k, v in sorted(by_merch.items(), key=lambda x: -x[1])[:15]]

    total_gasto  = round(sum(by_month.values()), 2)
    media_mensal = round(total_gasto / len(by_month), 2) if by_month else 0
    melhor_mes   = max(mensal, key=lambda x: x['total']) if mensal else None

    return {
        'faturas': faturas, 'mensal': mensal, 'por_categoria': por_categoria,
        'top_merchants': top_merchants, 'total_gasto': total_gasto,
        'media_mensal': media_mensal, 'melhor_mes': melhor_mes,
    }


# ── Análise Geral ─────────────────────────────────────────────────────────────

@router.get("/api/fp/analise")
async def fp_analise(request: Request, de: str = None, ate: str = None):
    _fp_require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        filters, params = ["l.situacao='quitado'"], []
        if de:
            filters.append("l.vencimento >= %s")
            params.append(de)
        if ate:
            filters.append("l.vencimento <= %s")
            params.append(ate + "-31")
        where = " AND ".join(filters)
        cur.execute(f"""
            SELECT l.vencimento, l.tipo, l.valor_total, l.categoria_id, c.nome AS categoria_nome
            FROM fp_lancamentos l
            LEFT JOIN fp_categorias c ON c.id = l.categoria_id
            WHERE {where}
            ORDER BY l.vencimento
        """, params)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    def _ym_label(yyyymm):
        try:
            y, m = yyyymm.split('-')
            return f"{_MESES[int(m)-1]}/{y[2:]}"
        except Exception:
            return yyyymm

    by_month_rec: dict = defaultdict(float)
    by_month_dep: dict = defaultdict(float)
    for r in rows:
        raw = (r['vencimento'] or '')[:7]
        if not raw:
            continue
        if r['tipo'] == 'receita':
            by_month_rec[raw] += r['valor_total'] or 0
        else:
            by_month_dep[raw] += r['valor_total'] or 0

    all_months = sorted(set(list(by_month_rec.keys()) + list(by_month_dep.keys())))
    mensal = [{
        'mes':     _ym_label(k),
        'receita': round(by_month_rec.get(k, 0), 2),
        'despesa': round(by_month_dep.get(k, 0), 2),
        'saldo':   round(by_month_rec.get(k, 0) - by_month_dep.get(k, 0), 2),
    } for k in all_months]

    by_cat: dict = defaultdict(float)
    for r in rows:
        if r['tipo'] == 'despesa':
            by_cat[r['categoria_nome'] or 'Sem categoria'] += r['valor_total'] or 0
    por_categoria = [{'categoria': k, 'total': round(v, 2)} for k, v in sorted(by_cat.items(), key=lambda x: -x[1])]

    total_receita = round(sum(r['valor_total'] or 0 for r in rows if r['tipo'] == 'receita'), 2)
    total_despesa = round(sum(r['valor_total'] or 0 for r in rows if r['tipo'] == 'despesa'), 2)

    return {
        'mensal': mensal, 'por_categoria': por_categoria,
        'total_receita': total_receita, 'total_despesa': total_despesa,
        'saldo': round(total_receita - total_despesa, 2),
    }
