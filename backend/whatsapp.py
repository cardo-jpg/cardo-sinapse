"""
Integração WhatsApp via Evolution API self-hosted.

Fluxo:
  WhatsApp (Evolution) → POST /api/whatsapp/webhook → Postgres (wa_*)
                                                   → Dossiê do cliente

Eventos suportados (Evolution v2):
  - messages.upsert (mensagem nova em grupo @g.us)
  - groups.upsert (info de grupo atualizada)

Grupos têm que ser vinculados manualmente a um cliente em /whatsapp.
Mensagens de grupos sem cliente vinculado ainda são salvas (pra
permitir descobrimento). Áudios ficam pendentes pra transcrição futura.
"""

import json
import os
import re
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backend.gestao import _verify, _require
from backend.db import get_conn, dict_cursor

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))

# Token opcional pra autenticar webhook (Evolution envia no header
# `apikey` se vc setar AUTHENTICATION_API_KEY).
WEBHOOK_TOKEN = os.getenv("WHATSAPP_WEBHOOK_TOKEN", "")

# Evolution API — pra controle administrativo (criar instância, gerar QR/pairing)
EVOLUTION_API_URL = (os.getenv("EVOLUTION_API_URL") or "").rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "sinapse")


def _evolution_call(method: str, path: str, json_body: Optional[dict] = None) -> dict:
    if not EVOLUTION_API_URL or not EVOLUTION_API_KEY:
        raise HTTPException(503, "Evolution API não configurada (EVOLUTION_API_URL/EVOLUTION_API_KEY)")
    url = f"{EVOLUTION_API_URL}{path}"
    headers = {"apikey": EVOLUTION_API_KEY}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.request(method, url, headers=headers, json=json_body)
            try:
                return {"status": r.status_code, "data": r.json()}
            except Exception:
                return {"status": r.status_code, "data": {"raw": r.text}}
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Falha ao chamar Evolution: {e}")


# ── DB ────────────────────────────────────────────────────────────────────────

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_grupos (
                id              SERIAL PRIMARY KEY,
                jid             TEXT NOT NULL UNIQUE,
                nome            TEXT,
                cliente_id      INTEGER REFERENCES clientes(id) ON DELETE SET NULL,
                ativo           BOOLEAN NOT NULL DEFAULT TRUE,
                first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_message_at TIMESTAMPTZ
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wa_grupos_cliente ON wa_grupos(cliente_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wa_mensagens (
                id                SERIAL PRIMARY KEY,
                message_id        TEXT UNIQUE,
                grupo_id          INTEGER NOT NULL REFERENCES wa_grupos(id) ON DELETE CASCADE,
                from_me           BOOLEAN NOT NULL DEFAULT FALSE,
                sender_jid        TEXT,
                sender_name       TEXT,
                tipo              TEXT NOT NULL DEFAULT 'text',
                body              TEXT,
                transcricao       TEXT,
                media_url         TEXT,
                message_timestamp TIMESTAMPTZ,
                raw               JSONB,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wa_msg_grupo_ts ON wa_mensagens(grupo_id, message_timestamp DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wa_msg_ts ON wa_mensagens(message_timestamp DESC)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts_to_dt(ts) -> Optional[datetime]:
    """Aceita unix timestamp em segundos ou milissegundos. Retorna UTC."""
    if ts is None:
        return None
    try:
        n = int(ts)
    except Exception:
        return None
    if n > 10_000_000_000:  # milissegundos
        n = n / 1000
    return datetime.fromtimestamp(n, tz=timezone.utc)


def _extract_text_and_type(message: dict) -> tuple[str, str, Optional[str]]:
    """
    Dado o objeto 'message' do webhook do Evolution, retorna (body, tipo, media_url).
    Cobre os tipos mais comuns. Tipos não cobertos viram tipo='outro' com body vazio.
    """
    if not isinstance(message, dict):
        return "", "outro", None

    # Texto simples
    if "conversation" in message and message["conversation"]:
        return message["conversation"], "text", None

    # Texto com formatação / reply
    ext = message.get("extendedTextMessage")
    if isinstance(ext, dict) and ext.get("text"):
        return ext["text"], "text", None

    # Áudio
    audio = message.get("audioMessage")
    if isinstance(audio, dict):
        return "", "audio", audio.get("url") or audio.get("mediaUrl")

    # Imagem
    img = message.get("imageMessage")
    if isinstance(img, dict):
        return img.get("caption") or "", "image", img.get("url") or img.get("mediaUrl")

    # Vídeo
    vid = message.get("videoMessage")
    if isinstance(vid, dict):
        return vid.get("caption") or "", "video", vid.get("url") or vid.get("mediaUrl")

    # Documento
    doc = message.get("documentMessage")
    if isinstance(doc, dict):
        return doc.get("fileName") or doc.get("title") or "", "document", doc.get("url") or doc.get("mediaUrl")

    # Sticker
    if "stickerMessage" in message:
        return "", "sticker", None

    # Localização
    if "locationMessage" in message:
        loc = message["locationMessage"]
        return f"{loc.get('name', '')} {loc.get('address', '')}".strip(), "location", None

    # Contato
    if "contactMessage" in message:
        return message["contactMessage"].get("displayName") or "", "contact", None

    return "", "outro", None


# ── Webhook ───────────────────────────────────────────────────────────────────

@router.post("/api/whatsapp/webhook")
async def webhook(request: Request):
    """
    Endpoint público (sem login) que recebe eventos da Evolution API.
    Se WHATSAPP_WEBHOOK_TOKEN está setado, valida o header 'apikey'.
    """
    if WEBHOOK_TOKEN:
        provided = request.headers.get("apikey") or request.headers.get("authorization", "").replace("Bearer ", "")
        if provided != WEBHOOK_TOKEN:
            raise HTTPException(401, "Token inválido")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Body inválido")

    event = payload.get("event") or ""
    data = payload.get("data") or {}

    # Aceita variações do formato (Evolution v1 e v2)
    if event in ("messages.upsert", "MESSAGES_UPSERT"):
        return _handle_message_upsert(data, payload)

    if event in ("groups.upsert", "GROUPS_UPSERT", "groups.update", "GROUPS_UPDATE"):
        return _handle_group_upsert(data)

    # Eventos ignorados (mas retornamos 200 pra Evolution não retry)
    return {"ok": True, "ignored": event}


def _handle_group_upsert(data) -> dict:
    """Atualiza nome do grupo (ou cria stub) quando Evolution avisa."""
    groups = data if isinstance(data, list) else [data]
    conn = get_conn()
    cur = conn.cursor()
    updated = 0
    try:
        for g in groups:
            if not isinstance(g, dict):
                continue
            jid = g.get("id") or g.get("jid") or g.get("remoteJid")
            if not jid or not jid.endswith("@g.us"):
                continue
            nome = g.get("subject") or g.get("name") or g.get("groupSubject") or ""
            cur.execute("""
                INSERT INTO wa_grupos (jid, nome, last_message_at)
                VALUES (%s, %s, NULL)
                ON CONFLICT (jid) DO UPDATE
                  SET nome = COALESCE(NULLIF(EXCLUDED.nome, ''), wa_grupos.nome)
            """, (jid, nome or None))
            updated += 1
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True, "groups_updated": updated}


def _handle_message_upsert(data, full_payload) -> dict:
    """Salva mensagem nova em grupo. Ignora não-grupo e duplicatas."""
    # Evolution às vezes manda data como dict (1 msg) ou lista (várias)
    messages = data if isinstance(data, list) else [data]
    inserted = 0
    skipped = 0

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        for m in messages:
            if not isinstance(m, dict):
                skipped += 1
                continue

            key = m.get("key") or {}
            jid = key.get("remoteJid") or ""
            # Só grupos
            if not jid.endswith("@g.us"):
                skipped += 1
                continue

            message_id = key.get("id") or ""
            from_me = bool(key.get("fromMe"))
            sender_jid = key.get("participant") or jid
            push_name = m.get("pushName") or m.get("verifiedBizName") or ""

            message_obj = m.get("message") or {}
            body, tipo, media_url = _extract_text_and_type(message_obj)

            ts = _ts_to_dt(m.get("messageTimestamp") or m.get("messageTimestampMs"))

            # Garante grupo existente
            cur.execute("SELECT id FROM wa_grupos WHERE jid=%s", (jid,))
            row = cur.fetchone()
            if row:
                grupo_id = row["id"]
            else:
                # Cria stub — nome vem depois via groups.upsert ou edição manual
                cur.execute(
                    "INSERT INTO wa_grupos (jid, nome) VALUES (%s, %s) RETURNING id",
                    (jid, ""),
                )
                grupo_id = cur.fetchone()["id"]

            # Skip duplicata
            if message_id:
                cur.execute("SELECT 1 FROM wa_mensagens WHERE message_id=%s", (message_id,))
                if cur.fetchone():
                    skipped += 1
                    continue

            cur.execute("""
                INSERT INTO wa_mensagens
                    (message_id, grupo_id, from_me, sender_jid, sender_name,
                     tipo, body, media_url, message_timestamp, raw)
                VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s, %s::jsonb)
            """, (
                message_id or None, grupo_id, from_me, sender_jid, push_name,
                tipo, body, media_url, ts, json.dumps(m, ensure_ascii=False, default=str),
            ))

            # Atualiza last_message_at
            cur.execute(
                "UPDATE wa_grupos SET last_message_at=GREATEST(COALESCE(last_message_at, %s), %s) WHERE id=%s",
                (ts, ts, grupo_id),
            )
            inserted += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    return {"ok": True, "inserted": inserted, "skipped": skipped}


# ── Connect/QR/Pairing (proxy pra Evolution API) ─────────────────────────────

@router.get("/api/whatsapp/instance/state")
async def instance_state(request: Request):
    _require(request)
    r = _evolution_call("GET", f"/instance/connectionState/{EVOLUTION_INSTANCE}")
    return {"http_status": r["status"], "data": r["data"]}


@router.post("/api/whatsapp/instance/connect")
async def instance_connect(request: Request):
    """
    Cria/recria a instância e retorna pairing code ou QR.
    Body opcional: { "number": "5547999999999" } pra pairing code via número.
                   Sem number, gera QR code.
    """
    _require(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    number = (body.get("number") or "").strip()

    # 1) Deleta instância existente (idempotente — ignora 404)
    _evolution_call("DELETE", f"/instance/delete/{EVOLUTION_INSTANCE}")

    # 2) Cria de novo
    payload = {
        "instanceName": EVOLUTION_INSTANCE,
        "integration": "WHATSAPP-BAILEYS",
    }
    if number:
        payload["number"] = number
    else:
        payload["qrcode"] = True

    r = _evolution_call("POST", "/instance/create", payload)
    data = r.get("data") or {}

    # Normaliza retorno pro frontend
    qr_base64 = None
    qr_code = None
    pairing_code = None

    qrcode_obj = data.get("qrcode") or {}
    if isinstance(qrcode_obj, dict):
        qr_base64 = qrcode_obj.get("base64")
        qr_code = qrcode_obj.get("code")
        pairing_code = qrcode_obj.get("pairingCode")

    # Algumas versões da Evolution colocam pairingCode no nível raiz
    if not pairing_code:
        pairing_code = data.get("pairingCode") or data.get("code")

    return {
        "ok": True,
        "instance": data.get("instance") or {},
        "qr_base64": qr_base64,
        "qr_code": qr_code,
        "pairing_code": pairing_code,
        "raw": data,
    }


@router.post("/api/whatsapp/instance/refresh-qr")
async def instance_refresh_qr(request: Request):
    """Tenta pegar um novo QR sem recriar a instância (caso QR tenha expirado)."""
    _require(request)
    r = _evolution_call("GET", f"/instance/connect/{EVOLUTION_INSTANCE}")
    data = r.get("data") or {}
    qr = data.get("qrcode") or {}
    return {
        "ok": True,
        "qr_base64": qr.get("base64") if isinstance(qr, dict) else None,
        "qr_code": qr.get("code") if isinstance(qr, dict) else None,
        "pairing_code": data.get("pairingCode") or data.get("code"),
        "raw": data,
    }


@router.post("/api/whatsapp/instance/disconnect")
async def instance_disconnect(request: Request):
    _require(request)
    r = _evolution_call("DELETE", f"/instance/logout/{EVOLUTION_INSTANCE}")
    return {"ok": True, "data": r["data"]}


# ── Grupos: CRUD pra vincular a clientes ──────────────────────────────────────

@router.get("/whatsapp", response_class=HTMLResponse)
async def page_whatsapp(request: Request):
    user = _verify(request)
    if not user:
        return RedirectResponse("/login")
    from backend.main import _nav_base
    return templates.TemplateResponse(
        "whatsapp.html",
        {"request": request, **_nav_base(request, "whatsapp")},
    )


@router.get("/api/whatsapp/grupos")
async def list_grupos(request: Request):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("""
            SELECT g.id, g.jid, g.nome, g.cliente_id, g.ativo,
                   g.first_seen_at, g.last_message_at,
                   c.razao_social AS cliente_nome, c.sigla AS cliente_sigla,
                   (SELECT COUNT(*) FROM wa_mensagens m WHERE m.grupo_id=g.id) AS total_mensagens
              FROM wa_grupos g
              LEFT JOIN clientes c ON c.id = g.cliente_id
             ORDER BY (g.cliente_id IS NULL) DESC, g.last_message_at DESC NULLS LAST, g.nome
        """)
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            for k in ("first_seen_at", "last_message_at"):
                if isinstance(d.get(k), datetime):
                    d[k] = d[k].isoformat()
            rows.append(d)
    finally:
        cur.close()
        conn.close()

    # Lista de clientes pro dropdown
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id, sigla, razao_social FROM clientes WHERE ativo=TRUE ORDER BY razao_social")
        clientes = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    return {"grupos": rows, "clientes": clientes}


@router.put("/api/whatsapp/grupos/{grupo_id}")
async def update_grupo(grupo_id: int, request: Request):
    _require(request)
    body = await request.json()
    sets, params = [], []
    if "cliente_id" in body:
        sets.append("cliente_id=%s")
        params.append(body["cliente_id"] if body["cliente_id"] else None)
    if "ativo" in body:
        sets.append("ativo=%s")
        params.append(bool(body["ativo"]))
    if "nome" in body:
        sets.append("nome=%s")
        params.append((body["nome"] or "").strip() or None)
    if not sets:
        raise HTTPException(400, "Nada para atualizar")
    params.append(grupo_id)

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE wa_grupos SET {', '.join(sets)} WHERE id=%s", params)
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


# ── Cliente: histórico, saúde e resumo ────────────────────────────────────────

@router.get("/api/clientes/{cliente_id}/whatsapp")
async def cliente_whatsapp(cliente_id: int, request: Request, limit: int = 50):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id, nome, jid, last_message_at FROM wa_grupos WHERE cliente_id=%s AND ativo=TRUE", (cliente_id,))
        grupos = [dict(r) for r in cur.fetchall()]
        if not grupos:
            return {"grupos": [], "mensagens": [], "saude": _empty_saude()}

        grupo_ids = [g["id"] for g in grupos]
        cur.execute("""
            SELECT id, grupo_id, from_me, sender_name, tipo, body, transcricao,
                   message_timestamp, media_url
              FROM wa_mensagens
             WHERE grupo_id = ANY(%s)
             ORDER BY message_timestamp DESC NULLS LAST
             LIMIT %s
        """, (grupo_ids, limit))
        mensagens = []
        for r in cur.fetchall():
            d = dict(r)
            if isinstance(d.get("message_timestamp"), datetime):
                d["message_timestamp"] = d["message_timestamp"].isoformat()
            mensagens.append(d)

        # Métricas de saúde
        saude = _calcular_saude(cur, grupo_ids)
    finally:
        cur.close()
        conn.close()

    for g in grupos:
        if isinstance(g.get("last_message_at"), datetime):
            g["last_message_at"] = g["last_message_at"].isoformat()

    return {"grupos": grupos, "mensagens": mensagens, "saude": saude}


def _empty_saude() -> dict:
    return {
        "ultimo_contato": None,
        "dias_sem_contato": None,
        "mensagens_7d": 0,
        "mensagens_30d": 0,
        "media_diaria_30d": 0,
    }


def _calcular_saude(cur, grupo_ids: list) -> dict:
    cur.execute("""
        SELECT MAX(message_timestamp) AS ultimo,
               COUNT(*) FILTER (WHERE message_timestamp >= NOW() - INTERVAL '7 days')  AS m7,
               COUNT(*) FILTER (WHERE message_timestamp >= NOW() - INTERVAL '30 days') AS m30
          FROM wa_mensagens
         WHERE grupo_id = ANY(%s)
    """, (grupo_ids,))
    r = cur.fetchone() or {}
    ultimo = r.get("ultimo")
    dias = None
    if isinstance(ultimo, datetime):
        agora = datetime.now(tz=timezone.utc)
        dias = (agora - ultimo).days
    return {
        "ultimo_contato": ultimo.isoformat() if isinstance(ultimo, datetime) else None,
        "dias_sem_contato": dias,
        "mensagens_7d": int(r.get("m7") or 0),
        "mensagens_30d": int(r.get("m30") or 0),
        "media_diaria_30d": round((int(r.get("m30") or 0)) / 30, 1),
    }


@router.post("/api/clientes/{cliente_id}/whatsapp/resumo")
async def resumir_whatsapp(cliente_id: int, request: Request):
    """Gera resumo via Claude das últimas 24h de mensagens do cliente."""
    _require(request)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    horas = int(body.get("horas") or 24)
    horas = max(1, min(horas, 24 * 14))  # 1h a 14 dias

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id FROM wa_grupos WHERE cliente_id=%s AND ativo=TRUE", (cliente_id,))
        grupo_ids = [r["id"] for r in cur.fetchall()]
        if not grupo_ids:
            return {"resumo": "", "n_mensagens": 0}

        cur.execute("""
            SELECT from_me, sender_name, tipo, body, transcricao, message_timestamp
              FROM wa_mensagens
             WHERE grupo_id = ANY(%s) AND message_timestamp >= NOW() - (%s || ' hours')::interval
             ORDER BY message_timestamp
        """, (grupo_ids, str(horas)))
        msgs = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not msgs:
        return {"resumo": f"Nenhuma mensagem nas últimas {horas} horas.", "n_mensagens": 0}

    # Monta transcript pro Claude
    linhas = []
    for m in msgs:
        ts = m.get("message_timestamp")
        ts_str = ts.strftime("%d/%m %H:%M") if isinstance(ts, datetime) else "?"
        autor = "Agência" if m.get("from_me") else (m.get("sender_name") or "Cliente")
        tipo = m.get("tipo") or "text"
        if tipo == "text":
            corpo = m.get("body") or ""
        elif tipo == "audio":
            corpo = f"[ÁUDIO] {m.get('transcricao') or '(não transcrito)'}"
        else:
            corpo = f"[{tipo.upper()}] {m.get('body') or ''}"
        linhas.append(f"[{ts_str}] {autor}: {corpo}")

    transcript = "\n".join(linhas)
    transcript = transcript[:60_000]  # cap

    try:
        from backend.main import client as anthropic_client
        prompt = f"""Você é um analista de relacionamento com cliente de agência de marketing.

Abaixo está a conversa de WhatsApp do grupo do cliente das últimas {horas} horas. Gere um resumo executivo em português brasileiro com:

## Resumo
2-3 frases sobre o tom geral e o que aconteceu

## Pontos importantes
- Bullets curtos com decisões, pedidos, dúvidas relevantes

## Pendências/ações
- O que cliente espera | quem deveria fazer
- (omita se nada)

## Alertas
- Sinais de insatisfação, atrasos, ruídos
- (omita se nada)

Diretrizes: NÃO invente nada que não está na conversa. Seja objetivo. Use markdown.

Conversa:
\"\"\"
{transcript}
\"\"\""""
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system="Você produz resumos executivos de conversas de WhatsApp entre agência e cliente.",
            messages=[{"role": "user", "content": prompt}],
        )
        out = next((b.text for b in response.content if hasattr(b, "text")), "").strip()
        return {"resumo": out, "n_mensagens": len(msgs)}
    except Exception as e:
        return {"resumo": f"⚠️ Erro ao gerar resumo: {e.__class__.__name__}", "n_mensagens": len(msgs)}
