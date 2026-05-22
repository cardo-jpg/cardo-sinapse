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
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backend.gestao import _verify, _require
from backend.db import get_conn, dict_cursor

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))

# Token opcional pra autenticar webhook
WEBHOOK_TOKEN = os.getenv("WHATSAPP_WEBHOOK_TOKEN", "")

# WAHA — controle administrativo (criar sessão, gerar QR/pairing)
WAHA_API_URL = (os.getenv("WAHA_API_URL") or "").rstrip("/")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")
WAHA_SESSION = os.getenv("WAHA_SESSION", "default")  # WAHA Core só aceita 'default'

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"

# Números do TIME (agência) no WhatsApp — separados por vírgula.
# Qualquer mensagem cujo sender é um desses números é tratada como
# from_me (do nosso lado), não do cliente.
# Exemplo: "554792222955,554791111111,554798888888"
# Backwards-compat: aceita também WHATSAPP_OWN_NUMBER (singular).
_raw = os.getenv("WHATSAPP_OWN_NUMBERS") or os.getenv("WHATSAPP_OWN_NUMBER") or ""
WHATSAPP_OWN_NUMBERS: set[str] = {
    re.sub(r"\D", "", n) for n in _raw.split(",") if n.strip()
}


def _waha_call(method: str, path: str, json_body: Optional[dict] = None) -> dict:
    if not WAHA_API_URL:
        raise HTTPException(503, "WAHA não configurado (WAHA_API_URL)")
    url = f"{WAHA_API_URL}{path}"
    headers = {}
    if WAHA_API_KEY:
        headers["X-Api-Key"] = WAHA_API_KEY
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.request(method, url, headers=headers, json=json_body)
            print(f"[WAHA] {method} {path} → {r.status_code}: {r.text[:500]}", flush=True)
            try:
                return {"status": r.status_code, "data": r.json()}
            except Exception:
                return {"status": r.status_code, "data": {"raw": r.text}}
    except httpx.HTTPError as e:
        print(f"[WAHA ERROR] {method} {path}: {e}", flush=True)
        raise HTTPException(502, f"Falha ao chamar WAHA: {e}")


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
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Endpoint público que recebe eventos do WAHA.
    Se WHATSAPP_WEBHOOK_TOKEN está setado, valida o header X-Api-Key.
    """
    if WEBHOOK_TOKEN:
        provided = request.headers.get("x-api-key") or request.headers.get("apikey") or ""
        if provided != WEBHOOK_TOKEN:
            raise HTTPException(401, "Token inválido")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Body inválido")

    event = (payload.get("event") or "").lower()

    # WAHA: event="message" → mensagem nova
    if event in ("message", "message.any"):
        result = _handle_waha_message(payload)
        # Auto-transcrição de áudio em background (não bloqueia o webhook)
        if result.get("tipo") == "audio" and result.get("msg_id") and result.get("media_url"):
            background_tasks.add_task(transcrever_mensagem_audio, result["msg_id"])
        return result

    # Evolution legacy (caso ainda usem antigos webhooks)
    if event in ("messages.upsert", "messages_upsert"):
        return _handle_message_upsert(payload.get("data") or {}, payload)
    if event in ("groups.upsert", "groups.update"):
        return _handle_group_upsert(payload.get("data") or {})

    return {"ok": True, "ignored": event}


def _baixar_arquivo_waha(media_url: str) -> tuple[bytes, str]:
    """Baixa arquivo de mídia do WAHA. Retorna (bytes, mime).
    Se a URL aponta pra localhost (WAHA retorna URL interna do container),
    substitui pelo host público (WAHA_API_URL).
    """
    if not media_url:
        raise ValueError("media_url vazio")
    # WAHA NOWEB retorna media URL como http://localhost:3000/... — troca pelo host público
    if "localhost" in media_url or "127.0.0.1" in media_url:
        if WAHA_API_URL:
            import re as _re
            media_url = _re.sub(r"^https?://[^/]+", WAHA_API_URL, media_url)
    headers = {}
    if WAHA_API_KEY:
        headers["X-Api-Key"] = WAHA_API_KEY
    with httpx.Client(timeout=60.0) as client:
        r = client.get(media_url, headers=headers, follow_redirects=True)
        if r.status_code != 200:
            raise RuntimeError(f"Falha download {r.status_code}: {r.text[:200]}")
        return r.content, r.headers.get("content-type", "audio/ogg")


def _whisper_transcrever(content: bytes, mime: str = "audio/ogg") -> str:
    """Manda áudio pro Whisper. Retorna texto."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurada")
    ext = "ogg"
    if "mp3" in mime: ext = "mp3"
    elif "wav" in mime: ext = "wav"
    elif "m4a" in mime or "mp4" in mime: ext = "m4a"
    elif "webm" in mime: ext = "webm"
    filename = f"audio.{ext}"
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            WHISPER_URL,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (filename, content, mime)},
            data={"model": "whisper-1", "language": "pt", "response_format": "json"},
        )
    if r.status_code != 200:
        raise RuntimeError(f"Whisper {r.status_code}: {r.text[:300]}")
    return r.json().get("text", "").strip()


def transcrever_mensagem_audio(msg_id: int) -> Optional[str]:
    """Baixa áudio + Whisper + salva. Idempotente. Best-effort: erros viram log."""
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            "SELECT id, tipo, media_url, message_id, raw, transcricao FROM wa_mensagens WHERE id=%s",
            (msg_id,),
        )
        msg = cur.fetchone()
        if not msg or msg["tipo"] != "audio":
            return None
        if (msg.get("transcricao") or "").strip():
            return msg["transcricao"]

        # Tenta múltiplas formas de baixar
        content = None
        mime = "audio/ogg"
        attempts = []

        # 1) media_url (com fix automático de localhost→público em _baixar_arquivo_waha)
        media_url = msg.get("media_url")
        if media_url:
            attempts.append(("media_url", media_url))

        for label, url in attempts:
            try:
                print(f"[transcrever] msg_id={msg_id} tentando {label}: {url[:100]}", flush=True)
                content, mime = _baixar_arquivo_waha(url)
                if content:
                    print(f"[transcrever] {label} OK: {len(content)} bytes mime={mime}", flush=True)
                    break
            except Exception as e:
                print(f"[transcrever] {label} falhou: {e}", flush=True)
                content = None

        if not content:
            print(f"[transcrever] msg_id={msg_id} nenhuma forma de download funcionou. raw keys: {list((msg.get('raw') or {}).keys()) if isinstance(msg.get('raw'), dict) else 'no-raw'}", flush=True)
            return None

        text = _whisper_transcrever(content, mime)
        if text:
            cur.execute("UPDATE wa_mensagens SET transcricao=%s WHERE id=%s", (text, msg_id))
            conn.commit()
            print(f"[transcrever] msg_id={msg_id} OK: {text[:80]}", flush=True)
        return text
    except Exception as e:
        print(f"[transcrever] msg_id={msg_id} ERRO: {e}", flush=True)
        return None
    finally:
        cur.close()
        conn.close()


def _try_enrich_group_name(cur, grupo_id: int, jid: str) -> None:
    """Best-effort: busca nome do grupo via WAHA e atualiza no banco. Silencioso em erros."""
    if not WAHA_API_URL:
        return
    try:
        r = _waha_call("GET", f"/api/{WAHA_SESSION}/groups/{jid}")
        if r["status"] != 200:
            return
        d = r["data"] or {}
        subject = (d.get("subject")
                   or (d.get("groupMetadata") or {}).get("subject")
                   or "")
        if subject:
            cur.execute("UPDATE wa_grupos SET nome=%s WHERE id=%s", (subject, grupo_id))
    except Exception as e:
        print(f"[WAHA] enrich falhou {jid}: {e}", flush=True)


def _handle_waha_message(payload: dict) -> dict:
    """
    Processa mensagem do WAHA. Formato:
    {
      "event": "message",
      "session": "sinapse",
      "payload": {
        "id": "false_551199@g.us_ABCD",
        "timestamp": 1683812345,
        "from": "551199@g.us",      // JID do grupo
        "fromMe": false,
        "body": "Olá",
        "hasMedia": false,
        "_data": {
          "notifyName": "João",
          ...
        }
      }
    }
    """
    p = payload.get("payload") or {}
    if not isinstance(p, dict):
        return {"ok": True, "skipped": "no payload"}

    from_jid = p.get("from") or ""
    if not from_jid.endswith("@g.us"):
        return {"ok": True, "skipped": "not group"}

    message_id = p.get("id") or ""
    from_me = bool(p.get("fromMe"))
    sender_jid = p.get("participant") or p.get("author") or from_jid
    push_name = (p.get("_data") or {}).get("notifyName") or p.get("notifyName") or ""
    # Fallback: NOWEB do WAHA às vezes marca fromMe errado. Confere pelo número.
    if not from_me and WHATSAPP_OWN_NUMBERS and sender_jid:
        sender_num = re.sub(r"\D", "", sender_jid.split("@")[0])
        if sender_num in WHATSAPP_OWN_NUMBERS:
            from_me = True
    print(f"[wa msg] tipo={'?' if not p.get('hasMedia') else 'media'} from_me={from_me} sender={sender_jid} push={push_name!r}", flush=True)

    body = p.get("body") or ""
    has_media = bool(p.get("hasMedia"))
    media_type = (p.get("media") or {}).get("mimetype") if isinstance(p.get("media"), dict) else None

    # Tipo derivado
    if has_media:
        if media_type and media_type.startswith("audio/"):
            tipo = "audio"
        elif media_type and media_type.startswith("image/"):
            tipo = "image"
        elif media_type and media_type.startswith("video/"):
            tipo = "video"
        else:
            tipo = "document"
    else:
        tipo = "text"

    media_url = (p.get("media") or {}).get("url") if isinstance(p.get("media"), dict) else None
    ts = _ts_to_dt(p.get("timestamp"))

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id, nome FROM wa_grupos WHERE jid=%s", (from_jid,))
        row = cur.fetchone()
        if row:
            grupo_id = row["id"]
            # Se ainda não tem nome, tenta buscar (best-effort, falha silenciosa)
            if not (row.get("nome") or "").strip():
                _try_enrich_group_name(cur, grupo_id, from_jid)
        else:
            cur.execute(
                "INSERT INTO wa_grupos (jid, nome) VALUES (%s, %s) RETURNING id",
                (from_jid, ""),
            )
            grupo_id = cur.fetchone()["id"]
            _try_enrich_group_name(cur, grupo_id, from_jid)

        if message_id:
            cur.execute("SELECT 1 FROM wa_mensagens WHERE message_id=%s", (message_id,))
            if cur.fetchone():
                return {"ok": True, "skipped": "duplicate"}

        cur.execute("""
            INSERT INTO wa_mensagens
                (message_id, grupo_id, from_me, sender_jid, sender_name,
                 tipo, body, media_url, message_timestamp, raw)
            VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s, %s::jsonb)
            RETURNING id
        """, (
            message_id or None, grupo_id, from_me, sender_jid, push_name,
            tipo, body, media_url, ts, json.dumps(p, ensure_ascii=False, default=str),
        ))
        new_msg_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE wa_grupos SET last_message_at=GREATEST(COALESCE(last_message_at, %s), %s) WHERE id=%s",
            (ts, ts, grupo_id),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

    return {"ok": True, "inserted": 1, "msg_id": new_msg_id, "tipo": tipo, "media_url": media_url}


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


# ── Connect/QR/Pairing (proxy pra WAHA) ──────────────────────────────────────

def _waha_session_status() -> str:
    """Retorna status: WORKING / STARTING / SCAN_QR_CODE / FAILED / STOPPED / NOT_FOUND."""
    r = _waha_call("GET", f"/api/sessions/{WAHA_SESSION}")
    if r["status"] == 404:
        return "NOT_FOUND"
    return (r["data"] or {}).get("status") or "UNKNOWN"


@router.get("/api/whatsapp/instance/state")
async def instance_state(request: Request):
    _require(request)
    try:
        status = _waha_session_status()
    except HTTPException:
        raise
    # Mapeia status do WAHA pra padrão usado pelo frontend
    state_map = {
        "WORKING": "open",
        "STARTING": "connecting",
        "SCAN_QR_CODE": "connecting",
        "FAILED": "close",
        "STOPPED": "close",
        "NOT_FOUND": "close",
    }
    return {
        "http_status": 200,
        "data": {
            "instance": {
                "instanceName": WAHA_SESSION,
                "state": state_map.get(status, "close"),
                "wahaStatus": status,
            }
        },
    }


@router.post("/api/whatsapp/instance/connect")
async def instance_connect(request: Request):
    """
    Inicia/reinicia a sessão WAHA. Retorna QR base64 ou pairing code.
    Body opcional: { "number": "5547999999999" } pra pairing via número.
    """
    import time as _t
    _require(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    number = (body.get("number") or "").strip()

    public_base = (os.getenv("PUBLIC_BASE_URL") or "https://sinapse.cardomarketing.com.br").rstrip("/")
    webhook_url = f"{public_base}/api/whatsapp/webhook"

    # 1) Limpa sessão existente (idempotente)
    for path in (f"/api/sessions/{WAHA_SESSION}/logout", f"/api/sessions/{WAHA_SESSION}/stop"):
        try: _waha_call("POST", path)
        except Exception: pass
    try: _waha_call("DELETE", f"/api/sessions/{WAHA_SESSION}")
    except Exception: pass
    _t.sleep(1)

    # 2) Cria nova sessão com webhook
    _waha_call("POST", "/api/sessions", {
        "name": WAHA_SESSION,
        "start": True,
        "config": {
            "webhooks": [{
                "url": webhook_url,
                "events": ["message", "session.status"],
            }]
        },
    })

    # 3) Aguarda WAHA chegar em SCAN_QR_CODE e pega credencial
    qr_base64 = None
    pairing_code = None
    for _ in range(15):  # ~30s max
        _t.sleep(2)
        status = _waha_session_status()
        if status == "WORKING":
            break
        if status == "SCAN_QR_CODE":
            if number:
                r = _waha_call("POST", f"/api/{WAHA_SESSION}/auth/request-code", {"phoneNumber": number})
                d = r.get("data") or {}
                pairing_code = d.get("code") or d.get("pairingCode")
                if pairing_code:
                    break
            else:
                r = _waha_call("GET", f"/api/{WAHA_SESSION}/auth/qr?format=image")
                d = r.get("data") or {}
                if isinstance(d, dict):
                    qr_base64 = d.get("data") or d.get("base64") or d.get("qr")
                    if qr_base64 and not qr_base64.startswith("data:"):
                        qr_base64 = f"data:image/png;base64,{qr_base64}"
                if qr_base64:
                    break

    return {
        "ok": True,
        "qr_base64": qr_base64,
        "pairing_code": pairing_code,
        "session_status": _waha_session_status(),
    }


@router.post("/api/whatsapp/instance/disconnect")
async def instance_disconnect(request: Request):
    _require(request)
    try: _waha_call("POST", f"/api/sessions/{WAHA_SESSION}/logout")
    except Exception: pass
    return {"ok": True}


# ── Grupos: CRUD pra vincular a clientes ──────────────────────────────────────

@router.get("/whatsapp", response_class=HTMLResponse)
async def page_whatsapp(request: Request):
    user = _verify(request)
    if not user:
        return RedirectResponse("/login")
    from backend.main import _nav_base
    resp = templates.TemplateResponse(
        "whatsapp.html",
        {"request": request, **_nav_base(request, "whatsapp")},
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


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


@router.post("/api/whatsapp/grupos/refresh-nomes")
async def refresh_nomes(request: Request):
    """Para cada grupo sem nome, busca o subject via WAHA."""
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id, jid FROM wa_grupos WHERE COALESCE(nome,'') = ''")
        sem_nome = cur.fetchall()
        updated = 0
        for row in sem_nome:
            jid = row["jid"]
            try:
                r = _waha_call("GET", f"/api/{WAHA_SESSION}/groups/{jid}")
                if r["status"] == 200:
                    d = r["data"] or {}
                    subject = (d.get("subject")
                               or (d.get("groupMetadata") or {}).get("subject")
                               or "")
                    if subject:
                        cur.execute("UPDATE wa_grupos SET nome=%s WHERE id=%s", (subject, row["id"]))
                        updated += 1
            except Exception as e:
                print(f"[WAHA] falhou pra {jid}: {e}", flush=True)
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True, "updated": updated, "total_sem_nome": len(sem_nome)}


@router.put("/api/whatsapp/grupos/{grupo_id}")
async def update_grupo(grupo_id: int, request: Request):
    _require(request)
    body = await request.json()
    print(f"[wa update_grupo] grupo_id={grupo_id} body={body}", flush=True)

    sets, params = [], []
    if "cliente_id" in body:
        v = body["cliente_id"]
        if v in (None, "", "null"):
            cid = None
        else:
            try:
                cid = int(v)
            except (TypeError, ValueError):
                cid = None
        sets.append("cliente_id=%s")
        params.append(cid)
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
        rowcount = cur.rowcount
        conn.commit()
        print(f"[wa update_grupo] rowcount={rowcount}", flush=True)
    finally:
        cur.close()
        conn.close()
    return {"ok": True, "rowcount": rowcount}


# ── Reclassificar from_me das mensagens antigas ──────────────────────────────

@router.post("/api/whatsapp/mensagens/reclassificar")
async def reclassificar_remetentes(request: Request):
    """
    Reprocessa from_me em todas as mensagens com base no WHATSAPP_OWN_NUMBERS atual.
    Útil quando se cadastra novos números do time depois que as mensagens já entraram.
    """
    _require(request)
    if not WHATSAPP_OWN_NUMBERS:
        return {"ok": False, "error": "WHATSAPP_OWN_NUMBERS não configurada", "updated": 0}

    conn = get_conn()
    cur = dict_cursor(conn)
    updated = 0
    try:
        cur.execute("SELECT id, sender_jid, from_me FROM wa_mensagens WHERE sender_jid IS NOT NULL")
        rows = cur.fetchall()
        ids_to_true = []
        ids_to_false = []
        for r in rows:
            sender_num = re.sub(r"\D", "", (r["sender_jid"] or "").split("@")[0])
            should_be_me = sender_num in WHATSAPP_OWN_NUMBERS
            if should_be_me and not r["from_me"]:
                ids_to_true.append(r["id"])
            elif not should_be_me and r["from_me"]:
                ids_to_false.append(r["id"])
        if ids_to_true:
            cur.execute("UPDATE wa_mensagens SET from_me=TRUE WHERE id = ANY(%s)", (ids_to_true,))
            updated += cur.rowcount
        if ids_to_false:
            cur.execute("UPDATE wa_mensagens SET from_me=FALSE WHERE id = ANY(%s)", (ids_to_false,))
            updated += cur.rowcount
        conn.commit()
    finally:
        cur.close(); conn.close()
    return {"ok": True, "updated": updated}


# ── Transcrição manual (retry) ───────────────────────────────────────────────

@router.post("/api/whatsapp/mensagens/{msg_id}/transcrever")
async def transcrever_msg(msg_id: int, request: Request):
    _require(request)
    # Coleta info do que vai tentar transcrever
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id, tipo, media_url, raw FROM wa_mensagens WHERE id=%s", (msg_id,))
        msg = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not msg:
        raise HTTPException(404, "Mensagem não encontrada")
    print(f"[transcrever_msg endpoint] msg_id={msg_id} tipo={msg['tipo']} media_url={msg.get('media_url')}", flush=True)
    text = transcrever_mensagem_audio(msg_id)
    return {
        "ok": True,
        "transcricao": text,
        "debug": {
            "tipo": msg["tipo"],
            "tem_media_url": bool(msg.get("media_url")),
            "openai_key_set": bool(OPENAI_API_KEY),
            "waha_url_set": bool(WAHA_API_URL),
        },
    }


# ── Cliente: histórico, saúde e resumo ────────────────────────────────────────

@router.get("/api/clientes/{cliente_id}/whatsapp")
async def cliente_whatsapp(cliente_id: int, request: Request, limit: int = 500):
    _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT id, nome, jid, last_message_at, ativo, cliente_id FROM wa_grupos WHERE cliente_id=%s", (cliente_id,))
        all_grupos = cur.fetchall()
        print(f"[wa cliente_whatsapp] cliente_id={cliente_id} grupos_encontrados={[dict(g) for g in all_grupos]}", flush=True)
        grupos = [dict(r) for r in all_grupos if r["ativo"]]
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
