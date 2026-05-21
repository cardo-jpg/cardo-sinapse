"""
Núcleo de Clientes nativo do Sinapse.

Substitui a dependência do Painel de Clientes do ClickUp por uma tabela
nativa no Postgres. Schema cobre todos os campos do modal Win Midias
(Informações, Financeiro, Reunião de Venda, Plano de Ação) + upload de
arquivos (contrato assinado, briefings, etc).

Arquivos são armazenados como BYTEA no banco — pragmático para PDFs
de contrato que tipicamente ficam abaixo de 1MB e evita problemas com
filesystem efêmero do Railway.
"""

import json
import re
import io
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from backend.gestao import _verify, _require
from backend.db import get_conn, dict_cursor

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))


# ── DB ────────────────────────────────────────────────────────────────────────

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clientes (
                id                  SERIAL PRIMARY KEY,

                -- Identificação
                sigla               TEXT NOT NULL UNIQUE,
                razao_social        TEXT NOT NULL,
                nome_fantasia       TEXT,
                cnpj                TEXT,

                -- Atribuição & contrato
                account             TEXT,
                plano_pacote        TEXT,
                nicho               TEXT,
                status_contrato     TEXT,
                performance         TEXT,
                inadimplente        BOOLEAN DEFAULT FALSE,
                canais_ativos       JSONB DEFAULT '[]'::jsonb,
                observacoes         TEXT,

                -- Financeiro
                valor_mensal        NUMERIC(12,2) DEFAULT 0,
                vencimento_dia      INTEGER,
                verba_midia         NUMERIC(12,2) DEFAULT 0,
                forma_pagamento     TEXT,
                plataformas         JSONB DEFAULT '[]'::jsonb,

                -- Reunião de venda
                reuniao_venda_transcricao  TEXT,
                reuniao_venda_resumo       TEXT,

                -- Plano de ação
                plano_acao          TEXT,

                -- Saúde
                saude               INTEGER,
                saude_label         TEXT,
                em_risco            BOOLEAN DEFAULT FALSE,

                -- Status
                ativo               BOOLEAN DEFAULT TRUE,
                entrada_em          DATE,
                data_final_contrato DATE,

                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_clientes_ativo ON clientes(ativo)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_clientes_account ON clientes(account)")

        # Migrations idempotentes pra colunas adicionadas depois da criação inicial
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                     WHERE table_name='clientes' AND column_name='data_final_contrato'
                ) THEN
                    ALTER TABLE clientes ADD COLUMN data_final_contrato DATE;
                END IF;
            END $$;
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS clientes_arquivos (
                id              SERIAL PRIMARY KEY,
                cliente_id      INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
                tipo            TEXT NOT NULL DEFAULT 'documento',  -- contrato | briefing | documento
                titulo          TEXT NOT NULL,
                filename        TEXT NOT NULL,
                mime_type       TEXT,
                size_bytes      BIGINT DEFAULT 0,
                conteudo        BYTEA,
                uploaded_by     TEXT,
                uploaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_clientes_arquivos_cliente ON clientes_arquivos(cliente_id, tipo)")

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_int(v, default=None):
    if v is None or v == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def _parse_float(v, default=0.0):
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("R$", "").replace(" ", "")
    if "," in s and s.rfind(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return default


def _parse_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "t", "sim", "yes", "y")


def _parse_date(v):
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _to_date(v):
    """Aceita date, datetime ou string ISO. Retorna date ou None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _metricas_relacionamento(cliente: dict) -> dict:
    """
    Calcula métricas derivadas pra exibir no dossiê:
      - meses_como_cliente, dias_como_cliente
      - ltv_estimado (valor_mensal × meses)
      - dias_ate_vencimento (próximo mês corrente baseado em vencimento_dia)
      - dias_ate_fim_contrato (negativo = expirado)
    """
    import calendar
    today = date.today()
    entrada = _to_date(cliente.get("entrada_em"))
    fim = _to_date(cliente.get("data_final_contrato"))

    meses = 0
    dias_cliente = 0
    if entrada:
        dias_cliente = (today - entrada).days
        meses = (today.year - entrada.year) * 12 + (today.month - entrada.month)
        if today.day < entrada.day:
            meses -= 1
        meses = max(meses, 0)

    valor = float(cliente.get("valor_mensal") or 0)
    # LTV estimado considera fração de mês como mês completo se entrada faz >1 dia
    # (evita LTV=0 pra cliente novo). Conservador: usa meses inteiros + 1 mês corrente.
    meses_efetivos = meses + (1 if entrada and dias_cliente >= 1 else 0)
    ltv_estimado = round(valor * meses_efetivos, 2)

    dias_ate_venc = None
    venc_dia = cliente.get("vencimento_dia")
    if isinstance(venc_dia, int) and 1 <= venc_dia <= 31:
        try:
            last_this = calendar.monthrange(today.year, today.month)[1]
            real_day = min(venc_dia, last_this)
            this_month = date(today.year, today.month, real_day)
            if this_month >= today:
                dias_ate_venc = (this_month - today).days
            else:
                if today.month == 12:
                    nx_year, nx_month = today.year + 1, 1
                else:
                    nx_year, nx_month = today.year, today.month + 1
                last_nx = calendar.monthrange(nx_year, nx_month)[1]
                nx = date(nx_year, nx_month, min(venc_dia, last_nx))
                dias_ate_venc = (nx - today).days
        except Exception:
            pass

    dias_ate_fim = None
    if fim:
        dias_ate_fim = (fim - today).days

    return {
        "meses_como_cliente": meses,
        "dias_como_cliente": dias_cliente,
        "ltv_estimado": ltv_estimado,
        "dias_ate_vencimento": dias_ate_venc,
        "dias_ate_fim_contrato": dias_ate_fim,
    }


def _row_to_dict(row):
    if not row:
        return None
    d = dict(row)
    # JSONB já vem como list/dict pelo psycopg
    for k in ("canais_ativos", "plataformas"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = []
    # Datas → ISO
    for k in ("entrada_em", "data_final_contrato"):
        if isinstance(d.get(k), date):
            d[k] = d[k].isoformat()
    for k in ("created_at", "updated_at"):
        if isinstance(d.get(k), datetime):
            d[k] = d[k].isoformat()
    # Numéricos
    for k in ("valor_mensal", "verba_midia"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    return d


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/clientes", response_class=HTMLResponse)
async def page_clientes(request: Request):
    user = _verify(request)
    if not user:
        return RedirectResponse("/login")
    from backend.main import _nav_base
    return templates.TemplateResponse(
        "clientes.html",
        {"request": request, **_nav_base(request, "clientes")},
    )


@router.get("/clientes/{cliente_id}", response_class=HTMLResponse)
async def page_cliente_dossie(cliente_id: int, request: Request):
    user = _verify(request)
    if not user:
        return RedirectResponse("/login")
    from backend.main import _nav_base
    # Valida existência antes de renderizar
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT sigla, razao_social FROM clientes WHERE id=%s", (cliente_id,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        return RedirectResponse("/clientes")
    return templates.TemplateResponse(
        "cliente_dossie.html",
        {
            "request": request,
            "cliente_id": cliente_id,
            "cliente_sigla": row["sigla"],
            "cliente_nome": row["razao_social"],
            **_nav_base(request, "clientes"),
        },
    )


@router.get("/api/clientes/{cliente_id}/dossie")
async def cliente_dossie(cliente_id: int, request: Request):
    """
    Retorna o cliente + arquivos + atas/documentos vinculados pela sigla.
    Atas são documents cujo título contém [SIGLA] OU que estão em pasta
    cujo nome contém [SIGLA] (mesma lógica do _sinapse_client_context).
    """
    _require(request)

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM clientes WHERE id=%s", (cliente_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Cliente não encontrado")
        cliente = _row_to_dict(row)
        sigla = (cliente.get("sigla") or "").upper()

        # Arquivos uploadados
        cur.execute(
            "SELECT id, tipo, titulo, filename, mime_type, size_bytes, uploaded_by, uploaded_at "
            "FROM clientes_arquivos WHERE cliente_id=%s ORDER BY uploaded_at DESC",
            (cliente_id,),
        )
        arquivos = []
        for r in cur.fetchall():
            d = dict(r)
            if isinstance(d.get("uploaded_at"), datetime):
                d["uploaded_at"] = d["uploaded_at"].isoformat()
            arquivos.append(d)

        # Atas e documentos vinculados — navega recursivamente a árvore de
        # documents a partir de qualquer nó cujo título inicia com [SIGLA].
        # Cobre a estrutura atual do Sinapse:
        #   Documentação > [SIGLA] Cliente > Atas de Reunião > Ata X
        # e qualquer outra estrutura customizada que tenha o cliente como raiz.
        documentos = []
        try:
            cur.execute(
                """
                WITH RECURSIVE
                cliente_root AS (
                    SELECT id, title, parent_id, title AS path, 0 AS depth
                      FROM documents
                     WHERE title ILIKE %s
                ),
                arvore AS (
                    SELECT d.id, d.title, d.parent_id,
                           d.created_at, d.updated_at,
                           (cr.path || ' > ' || d.title) AS path,
                           1 AS depth
                      FROM documents d
                      JOIN cliente_root cr ON d.parent_id = cr.id
                    UNION ALL
                    SELECT d.id, d.title, d.parent_id,
                           d.created_at, d.updated_at,
                           (a.path || ' > ' || d.title) AS path,
                           a.depth + 1
                      FROM documents d
                      JOIN arvore a ON d.parent_id = a.id
                     WHERE a.depth < 6
                )
                SELECT id, title, parent_id, created_at, updated_at, path
                  FROM arvore
                 ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST
                 LIMIT 500
                """,
                (f"[{sigla}]%",),
            )
            for r in cur.fetchall():
                d = dict(r)
                for k in ("created_at", "updated_at"):
                    if isinstance(d.get(k), datetime):
                        d[k] = d[k].isoformat()
                documentos.append(d)
        except Exception:
            pass

        # Classifica: se o "path" passa por um nó com "Ata" (ou o título começa
        # com data DD/MM/AAAA), é uma ata; senão, é documento auxiliar.
        atas = []
        outros = []
        for d in documentos:
            path_lower = (d.get("path") or "").lower()
            title = d.get("title") or ""
            is_container = title.strip().lower() in (
                "ficha do cliente", "arquivos", "atas de reunião", "atas de reuniao",
                "onboarding", "contrato", "documentos",
            )
            if is_container:
                continue  # nós de organização, não conteúdo
            if "ata" in path_lower or re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}", title):
                atas.append(d)
            else:
                outros.append(d)
    finally:
        cur.close()
        conn.close()

    cliente["arquivos"] = arquivos
    cliente["atas"] = atas
    cliente["documentos_vinculados"] = outros
    cliente["metricas"] = _metricas_relacionamento(cliente)

    return cliente


@router.get("/api/clientes")
async def list_clientes(request: Request, q: str = "", status: str = "", account: str = ""):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        sql = ["SELECT * FROM clientes WHERE 1=1"]
        params = []
        if status == "ativo":
            sql.append("AND ativo = TRUE")
        elif status == "inativo":
            sql.append("AND ativo = FALSE")
        if account:
            sql.append("AND account ILIKE %s")
            params.append(f"%{account}%")
        if q:
            sql.append("AND (razao_social ILIKE %s OR nome_fantasia ILIKE %s OR sigla ILIKE %s)")
            term = f"%{q}%"
            params.extend([term, term, term])
        sql.append("ORDER BY ativo DESC, razao_social")
        cur.execute(" ".join(sql), params)
        rows = [_row_to_dict(r) for r in cur.fetchall()]

        # Arquivos por cliente (só count e tipos)
        if rows:
            ids = [r["id"] for r in rows]
            cur.execute(
                "SELECT cliente_id, tipo, COUNT(*) AS n FROM clientes_arquivos WHERE cliente_id = ANY(%s) GROUP BY cliente_id, tipo",
                (ids,),
            )
            arquivos_map: dict[int, dict[str, int]] = {}
            for r in cur.fetchall():
                arquivos_map.setdefault(r["cliente_id"], {})[r["tipo"]] = r["n"]
            for c in rows:
                c["arquivos"] = arquivos_map.get(c["id"], {})
    finally:
        cur.close()
        conn.close()

    return {"clientes": rows}


@router.get("/api/clientes/{cliente_id}")
async def get_cliente(cliente_id: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM clientes WHERE id=%s", (cliente_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Cliente não encontrado")
        cliente = _row_to_dict(row)

        cur.execute(
            "SELECT id, tipo, titulo, filename, mime_type, size_bytes, uploaded_by, uploaded_at "
            "FROM clientes_arquivos WHERE cliente_id=%s ORDER BY uploaded_at DESC",
            (cliente_id,),
        )
        arquivos = []
        for r in cur.fetchall():
            d = dict(r)
            if isinstance(d.get("uploaded_at"), datetime):
                d["uploaded_at"] = d["uploaded_at"].isoformat()
            arquivos.append(d)
        cliente["arquivos"] = arquivos
    finally:
        cur.close()
        conn.close()
    return cliente


@router.post("/api/clientes")
async def create_cliente(request: Request):
    user = _require(request)
    body = await request.json()

    sigla = (body.get("sigla") or "").strip().upper()
    razao = (body.get("razao_social") or "").strip()
    if not razao:
        raise HTTPException(400, "Razão social é obrigatória")
    if not sigla:
        # Gera sigla a partir das iniciais
        words = [w for w in re.split(r"\s+", razao) if w]
        sigla = "".join(w[0].upper() for w in words[:3]) if words else "X"

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        # Garante sigla única — sufixo numérico se preciso
        base = sigla
        n = 1
        while True:
            cur.execute("SELECT 1 FROM clientes WHERE sigla=%s", (sigla,))
            if not cur.fetchone():
                break
            n += 1
            sigla = f"{base}{n}"

        cur.execute(
            """
            INSERT INTO clientes (
                sigla, razao_social, nome_fantasia, cnpj,
                account, plano_pacote, nicho, status_contrato, performance,
                inadimplente, canais_ativos, observacoes,
                valor_mensal, vencimento_dia, verba_midia, forma_pagamento, plataformas,
                reuniao_venda_transcricao, reuniao_venda_resumo, plano_acao,
                saude, saude_label, em_risco, ativo, entrada_em
            ) VALUES (
                %s,%s,%s,%s,
                %s,%s,%s,%s,%s,
                %s,%s::jsonb,%s,
                %s,%s,%s,%s,%s::jsonb,
                %s,%s,%s,
                %s,%s,%s,%s,%s
            )
            RETURNING *
            """,
            (
                sigla,
                razao,
                (body.get("nome_fantasia") or "").strip() or None,
                (body.get("cnpj") or "").strip() or None,

                (body.get("account") or "").strip() or None,
                (body.get("plano_pacote") or "").strip() or None,
                (body.get("nicho") or "").strip() or None,
                (body.get("status_contrato") or "").strip() or None,
                (body.get("performance") or "").strip() or None,

                _parse_bool(body.get("inadimplente")),
                json.dumps(body.get("canais_ativos") or []),
                (body.get("observacoes") or "").strip() or None,

                _parse_float(body.get("valor_mensal")),
                _parse_int(body.get("vencimento_dia")),
                _parse_float(body.get("verba_midia")),
                (body.get("forma_pagamento") or "").strip() or None,
                json.dumps(body.get("plataformas") or []),

                (body.get("reuniao_venda_transcricao") or "").strip() or None,
                (body.get("reuniao_venda_resumo") or "").strip() or None,
                (body.get("plano_acao") or "").strip() or None,

                _parse_int(body.get("saude")),
                (body.get("saude_label") or "").strip() or None,
                _parse_bool(body.get("em_risco")),
                _parse_bool(body.get("ativo")) if body.get("ativo") is not None else True,
                _parse_date(body.get("entrada_em")) or date.today(),
            ),
        )
        cliente = _row_to_dict(cur.fetchone())
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"cliente": cliente}


@router.put("/api/clientes/{cliente_id}")
async def update_cliente(cliente_id: int, request: Request):
    _require(request)
    body = await request.json()

    # Whitelist de campos editáveis
    field_map = {
        "razao_social": ("razao_social", str),
        "nome_fantasia": ("nome_fantasia", str),
        "cnpj": ("cnpj", str),
        "account": ("account", str),
        "plano_pacote": ("plano_pacote", str),
        "nicho": ("nicho", str),
        "status_contrato": ("status_contrato", str),
        "performance": ("performance", str),
        "inadimplente": ("inadimplente", "bool"),
        "canais_ativos": ("canais_ativos", "jsonb"),
        "observacoes": ("observacoes", str),
        "valor_mensal": ("valor_mensal", "float"),
        "vencimento_dia": ("vencimento_dia", "int"),
        "verba_midia": ("verba_midia", "float"),
        "forma_pagamento": ("forma_pagamento", str),
        "plataformas": ("plataformas", "jsonb"),
        "reuniao_venda_transcricao": ("reuniao_venda_transcricao", str),
        "reuniao_venda_resumo": ("reuniao_venda_resumo", str),
        "plano_acao": ("plano_acao", str),
        "saude": ("saude", "int"),
        "saude_label": ("saude_label", str),
        "em_risco": ("em_risco", "bool"),
        "ativo": ("ativo", "bool"),
        "entrada_em": ("entrada_em", "date"),
        "data_final_contrato": ("data_final_contrato", "date"),
        "sigla": ("sigla", str),
    }

    sets = []
    params = []
    for k, v in body.items():
        if k not in field_map:
            continue
        col, kind = field_map[k]
        if kind is str:
            sets.append(f"{col}=%s")
            params.append((str(v).strip() or None) if v is not None else None)
        elif kind == "int":
            sets.append(f"{col}=%s")
            params.append(_parse_int(v))
        elif kind == "float":
            sets.append(f"{col}=%s")
            params.append(_parse_float(v))
        elif kind == "bool":
            sets.append(f"{col}=%s")
            params.append(_parse_bool(v))
        elif kind == "date":
            sets.append(f"{col}=%s")
            params.append(_parse_date(v))
        elif kind == "jsonb":
            sets.append(f"{col}=%s::jsonb")
            params.append(json.dumps(v or []))

    if not sets:
        raise HTTPException(400, "Nada para atualizar")

    sets.append("updated_at=CURRENT_TIMESTAMP")
    params.append(cliente_id)

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            f"UPDATE clientes SET {', '.join(sets)} WHERE id=%s RETURNING *",
            params,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Cliente não encontrado")
        conn.commit()
        return {"cliente": _row_to_dict(row)}
    finally:
        cur.close()
        conn.close()


@router.delete("/api/clientes/{cliente_id}")
async def delete_cliente(cliente_id: int, request: Request, hard: bool = False):
    """Soft delete por padrão (ativo=false). Use ?hard=true pra apagar."""
    _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        if hard:
            cur.execute("DELETE FROM clientes WHERE id=%s", (cliente_id,))
        else:
            cur.execute("UPDATE clientes SET ativo=FALSE, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (cliente_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


# ── Arquivos ──────────────────────────────────────────────────────────────────

@router.post("/api/clientes/{cliente_id}/arquivos")
async def upload_arquivo(
    cliente_id: int,
    request: Request,
    file: UploadFile = File(...),
    tipo: str = Form("documento"),
    titulo: str = Form(""),
):
    user = _require(request)
    if tipo not in ("contrato", "briefing", "documento"):
        tipo = "documento"

    conteudo = await file.read()
    if len(conteudo) > 10 * 1024 * 1024:
        raise HTTPException(413, "Arquivo > 10MB. Use storage externo (S3/R2) para arquivos grandes.")

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id FROM clientes WHERE id=%s", (cliente_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Cliente não encontrado")

        cur.execute(
            """
            INSERT INTO clientes_arquivos (cliente_id, tipo, titulo, filename, mime_type, size_bytes, conteudo, uploaded_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, tipo, titulo, filename, mime_type, size_bytes, uploaded_by, uploaded_at
            """,
            (
                cliente_id,
                tipo,
                (titulo or file.filename or "").strip(),
                file.filename or "arquivo",
                file.content_type or "application/octet-stream",
                len(conteudo),
                conteudo,
                user,
            ),
        )
        arq = dict(cur.fetchone())
        if isinstance(arq.get("uploaded_at"), datetime):
            arq["uploaded_at"] = arq["uploaded_at"].isoformat()
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"arquivo": arq}


@router.get("/api/clientes/{cliente_id}/arquivos/{arquivo_id}/download")
async def download_arquivo(cliente_id: int, arquivo_id: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT filename, mime_type, conteudo FROM clientes_arquivos WHERE id=%s AND cliente_id=%s",
            (arquivo_id, cliente_id),
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        raise HTTPException(404, "Arquivo não encontrado")
    return Response(
        content=bytes(row["conteudo"]),
        media_type=row["mime_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{row["filename"]}"'},
    )


@router.delete("/api/clientes/{cliente_id}/arquivos/{arquivo_id}")
async def delete_arquivo(cliente_id: int, arquivo_id: int, request: Request):
    _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM clientes_arquivos WHERE id=%s AND cliente_id=%s", (arquivo_id, cliente_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


# ── Briefing vinculado ────────────────────────────────────────────────────────

def _normaliza(s: str) -> str:
    """Remove acentos e lowercase pra fuzzy match."""
    import unicodedata
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


@router.get("/api/clientes/{cliente_id}/briefing")
async def get_briefing(cliente_id: int, request: Request):
    """
    Retorna o briefing vinculado ao cliente.
    Estratégia:
      1. Tenta match direto via briefing_responses.cliente_id (vínculo explícito)
      2. Fallback: fuzzy match por client_name/empresa contendo a razão social ou sigla
    """
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT razao_social, sigla FROM clientes WHERE id=%s", (cliente_id,))
        cli = cur.fetchone()
        if not cli:
            raise HTTPException(404, "Cliente não encontrado")

        # 1) Vínculo direto
        cur.execute(
            "SELECT id, client_name, client_email, responses, submitted_at "
            "FROM briefing_responses WHERE cliente_id=%s ORDER BY submitted_at DESC LIMIT 1",
            (cliente_id,),
        )
        row = cur.fetchone()

        # 2) Fuzzy match
        if not row:
            cur.execute(
                "SELECT id, client_name, client_email, responses, submitted_at FROM briefing_responses ORDER BY submitted_at DESC"
            )
            razao_n = _normaliza(cli["razao_social"])
            sigla_n = _normaliza(cli["sigla"])
            for candidate in cur.fetchall():
                try:
                    resp = json.loads(candidate.get("responses") or "{}")
                except Exception:
                    resp = {}
                empresa = _normaliza(resp.get("empresa_nome") or "")
                nome = _normaliza(candidate.get("client_name") or "")
                if not razao_n:
                    continue
                if razao_n in empresa or empresa in razao_n or razao_n in nome or nome in razao_n:
                    row = candidate
                    break
                if sigla_n and len(sigla_n) >= 2 and (sigla_n in empresa or sigla_n in nome):
                    row = candidate
                    break

        if not row:
            return {"briefing": None, "definition": _briefing_definition()}

        try:
            responses = json.loads(row.get("responses") or "{}")
        except Exception:
            responses = {}
        submitted = row.get("submitted_at")
        if isinstance(submitted, datetime):
            submitted = submitted.isoformat()

        return {
            "briefing": {
                "id": row["id"],
                "client_name": row.get("client_name") or "",
                "client_email": row.get("client_email") or "",
                "responses": responses,
                "submitted_at": submitted,
            },
            "definition": _briefing_definition(),
        }
    finally:
        cur.close()
        conn.close()


@router.post("/api/clientes/{cliente_id}/briefing/link/{response_id}")
async def link_briefing(cliente_id: int, response_id: str, request: Request):
    """Vincula explicitamente um briefing_response a um cliente."""
    _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE briefing_responses SET cliente_id=%s WHERE id=%s", (cliente_id, response_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Briefing não encontrado")
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


def _briefing_definition():
    """Importa a definição do módulo briefings sem ciclo."""
    try:
        from backend.briefings import BRIEFING_DEFINITION
        return BRIEFING_DEFINITION
    except Exception:
        return {}


# ── Conteúdo inline de documents (atas e ficha do espaço Documentação) ───────

@router.get("/api/clientes/{cliente_id}/documents/{doc_id}")
async def get_document_content(cliente_id: int, doc_id: str, request: Request):
    """
    Retorna o conteúdo HTML do documento — usado pelo dossiê pra preview
    inline de atas e ficha (legado do espaço Documentação).

    Validação: doc_id precisa estar na hierarquia do cliente (segurança).
    """
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT sigla FROM clientes WHERE id=%s", (cliente_id,))
        cli = cur.fetchone()
        if not cli:
            raise HTTPException(404, "Cliente não encontrado")
        sigla = (cli["sigla"] or "").upper()

        # Verifica se o doc pertence à árvore do cliente
        cur.execute(
            """
            WITH RECURSIVE
            cliente_root AS (
                SELECT id FROM documents WHERE title ILIKE %s
            ),
            arvore AS (
                SELECT d.id FROM documents d JOIN cliente_root cr ON d.parent_id = cr.id
                UNION ALL
                SELECT d.id FROM documents d JOIN arvore a ON d.parent_id = a.id
            )
            SELECT id FROM cliente_root WHERE id=%s
            UNION
            SELECT id FROM arvore WHERE id=%s
            """,
            (f"[{sigla}]%", doc_id, doc_id),
        )
        if not cur.fetchone():
            raise HTTPException(403, "Documento não pertence a este cliente")

        cur.execute("SELECT id, title, content FROM documents WHERE id=%s", (doc_id,))
        doc = cur.fetchone()
        if not doc:
            raise HTTPException(404, "Documento não encontrado")
        return {
            "id": doc["id"],
            "title": doc["title"],
            "content": doc["content"] or "",
        }
    finally:
        cur.close()
        conn.close()


# ── Arquivar histórico de Documentação ────────────────────────────────────────

@router.post("/api/clientes/{cliente_id}/arquivar-documentacao")
async def arquivar_documentacao(cliente_id: int, request: Request):
    """
    Move toda a subárvore [SIGLA] X do espaço Documentação para o estado
    "arquivado" — implementado via prefixo no título (não destrutivo).

    Reversão manual: editar título removendo o prefixo.
    """
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    archived = 0
    try:
        cur.execute("SELECT sigla FROM clientes WHERE id=%s", (cliente_id,))
        cli = cur.fetchone()
        if not cli:
            raise HTTPException(404, "Cliente não encontrado")
        sigla = (cli["sigla"] or "").upper()

        # Coleta todos os IDs da árvore
        cur.execute(
            """
            WITH RECURSIVE
            cliente_root AS (
                SELECT id FROM documents WHERE title ILIKE %s
            ),
            arvore AS (
                SELECT d.id FROM documents d JOIN cliente_root cr ON d.parent_id = cr.id
                UNION ALL
                SELECT d.id FROM documents d JOIN arvore a ON d.parent_id = a.id
            )
            SELECT id FROM cliente_root
            UNION
            SELECT id FROM arvore
            """,
            (f"[{sigla}]%",),
        )
        ids = [r["id"] for r in cur.fetchall()]

        if ids:
            # Estratégia não-destrutiva: move pra debaixo de um document raiz "[ARQUIVADO]"
            # Cria o nó raiz arquivado se não existir
            cur.execute(
                "SELECT id FROM documents WHERE title='[ARQUIVADO] Documentação legada' LIMIT 1"
            )
            arquiv = cur.fetchone()
            if not arquiv:
                # Acha o space_id da primeira raiz do cliente para herdar
                cur.execute(
                    "SELECT space_id, folder_id FROM documents WHERE id=%s",
                    (ids[0],),
                )
                home = cur.fetchone()
                if home:
                    new_id = str(uuid.uuid4())
                    now = datetime.now().isoformat()
                    cur.execute(
                        "INSERT INTO documents (id,title,content,space_id,folder_id,parent_id,position,created_at,updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,NULL,9999,%s,%s)",
                        (
                            new_id,
                            "[ARQUIVADO] Documentação legada",
                            "<p>Documentos legados do espaço Documentação. Visualize pelo dossiê do cliente.</p>",
                            home["space_id"],
                            home["folder_id"],
                            now, now,
                        ),
                    )
                    arquiv_id = new_id
                else:
                    raise HTTPException(500, "Não foi possível resolver o space_id pra arquivamento")
            else:
                arquiv_id = arquiv["id"]

            # Re-parent só a raiz [SIGLA] X (filhos seguem ela automaticamente)
            cur.execute(
                "UPDATE documents SET parent_id=%s, updated_at=NOW() WHERE title ILIKE %s",
                (arquiv_id, f"[{sigla}]%"),
            )
            archived = cur.rowcount

        conn.commit()
    finally:
        cur.close()
        conn.close()

    return {"archived": archived, "sigla": sigla}


# ── Migração do Painel de Clientes (ClickUp) ──────────────────────────────────

@router.post("/api/clientes/migrate-from-painel")
async def migrate_from_painel(request: Request):
    """
    Importa clientes do Painel de Clientes (ClickUp sync) pra tabela nativa.
    Idempotente: se cliente com mesma sigla já existe, pula.
    """
    _require(request)
    from backend.inicio import _find_painel  # reuso da lógica

    list_id, cf_by_name = _find_painel()
    if not list_id:
        return {"migrated": 0, "skipped": 0, "errors": ["Painel de Clientes não encontrado"]}

    fee_field_id = cf_by_name.get("fee mensal") or cf_by_name.get("fee")
    impl_field_id = cf_by_name.get("tx de implementação") or cf_by_name.get("tx de implementacao")

    conn = get_conn()
    cur = dict_cursor(conn)
    migrated = 0
    skipped = 0
    errors = []
    try:
        cur.execute(
            "SELECT title, status, cf_values FROM tasks WHERE list_id=%s ORDER BY title",
            (list_id,),
        )
        for r in cur.fetchall():
            title = (r.get("title") or "").strip()
            m = re.match(r"^\s*\[([A-Za-z0-9]+)\]\s*(.+?)\s*$", title)
            if m:
                sigla = m.group(1).upper()
                nome = m.group(2).strip()
            else:
                words = [w for w in re.split(r"\s+", title) if w]
                sigla = "".join(w[0].upper() for w in words[:3]) or "X"
                nome = title

            try:
                cfv = json.loads(r.get("cf_values") or "{}")
            except Exception:
                cfv = {}
            fee = _parse_float(cfv.get(fee_field_id)) if fee_field_id else 0
            impl = _parse_float(cfv.get(impl_field_id)) if impl_field_id else 0

            status = (r.get("status") or "").strip().lower()
            ativo = status == "ativo"

            cur.execute("SELECT id FROM clientes WHERE sigla=%s", (sigla,))
            if cur.fetchone():
                skipped += 1
                continue

            try:
                cur.execute(
                    """
                    INSERT INTO clientes (sigla, razao_social, valor_mensal, ativo, entrada_em, observacoes)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        sigla, nome, fee, ativo, date.today(),
                        f"Migrado do Painel de Clientes do ClickUp." + (f" Tx implementação: R$ {impl:.2f}" if impl else "")
                    ),
                )
                migrated += 1
            except Exception as e:
                errors.append(f"{sigla}: {e}")
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"migrated": migrated, "skipped": skipped, "errors": errors}
