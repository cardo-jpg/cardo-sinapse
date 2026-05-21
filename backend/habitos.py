"""
Módulo de Hábitos do Sinapse.

Cada usuário tem seus próprios hábitos privados. Schema multi-user desde o dia 1.

Schema:
- hab_habits: hábitos por usuário (nome, emoji, freq alvo semanal, cor, ordem, ativo)
- hab_marks: marcações diárias (usuario, habit_id, data, status)
    status: 'done' | 'skip' (skip = falho intencional, deixar pra falhar; ausência = neutro)
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
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
            CREATE TABLE IF NOT EXISTS hab_habits (
                id          SERIAL PRIMARY KEY,
                username    TEXT    NOT NULL,
                nome        TEXT    NOT NULL,
                emoji       TEXT    DEFAULT '🎯',
                cor         TEXT    DEFAULT '#4ade80',
                freq_alvo   INTEGER NOT NULL DEFAULT 7 CHECK(freq_alvo BETWEEN 1 AND 7),
                ordem       INTEGER NOT NULL DEFAULT 0,
                ativo       INTEGER NOT NULL DEFAULT 1,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hab_marks (
                id          SERIAL PRIMARY KEY,
                username    TEXT NOT NULL,
                habit_id    INTEGER NOT NULL REFERENCES hab_habits(id) ON DELETE CASCADE,
                data        DATE NOT NULL,
                status      TEXT NOT NULL CHECK(status IN ('done','skip')),
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, habit_id, data)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hab_habits_user ON hab_habits(username, ativo, ordem)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hab_marks_user_data ON hab_marks(username, data)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hab_marks_habit_data ON hab_marks(habit_id, data)")
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(s: str | None) -> date:
    if not s:
        return date.today()
    return datetime.strptime(s, "%Y-%m-%d").date()


def _week_start(d: date) -> date:
    """Segunda-feira da semana de d."""
    return d - timedelta(days=d.weekday())


def _compute_streak(marks_done: set[date], today: date) -> tuple[int, int]:
    """
    Retorna (streak_atual, recorde).

    Streak = dias consecutivos com mark 'done' terminando em today (ou ontem,
    se today ainda não foi marcado — assim não quebra a streak por estar no dia).
    Recorde = maior sequência já alcançada no histórico.
    """
    if not marks_done:
        return 0, 0

    # Streak atual: começa do dia mais recente possível
    streak = 0
    if today in marks_done:
        cursor = today
    elif (today - timedelta(days=1)) in marks_done:
        cursor = today - timedelta(days=1)
    else:
        cursor = None

    if cursor is not None:
        while cursor in marks_done:
            streak += 1
            cursor -= timedelta(days=1)

    # Recorde: percorre todas as datas ordenadas e mede maior run
    dates_sorted = sorted(marks_done)
    record = 1
    run = 1
    for i in range(1, len(dates_sorted)):
        if (dates_sorted[i] - dates_sorted[i - 1]).days == 1:
            run += 1
            if run > record:
                record = run
        else:
            run = 1

    return streak, max(record, streak)


# ── API ───────────────────────────────────────────────────────────────────────

@router.get("/habitos", response_class=HTMLResponse)
async def page_habitos(request: Request):
    user = _verify(request)
    if not user:
        return RedirectResponse("/login")
    # _nav_base é definido em main.py; aqui só passamos o necessário e o template usa o include
    from backend.main import _nav_base  # import local pra evitar ciclo
    return templates.TemplateResponse(
        "habitos.html",
        {"request": request, **_nav_base(request, "habitos")},
    )


@router.get("/api/habitos")
async def list_habits(request: Request):
    user = _require(request)
    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            """
            SELECT id, nome, emoji, cor, freq_alvo, ordem, ativo
              FROM hab_habits
             WHERE username=%s AND ativo=1
             ORDER BY ordem, id
            """,
            (user,),
        )
        habits = [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return {"habits": habits}


@router.post("/api/habitos")
async def create_habit(request: Request):
    user = _require(request)
    body = await request.json()
    nome = (body.get("nome") or "").strip()
    if not nome:
        raise HTTPException(400, "Nome obrigatório")
    emoji = (body.get("emoji") or "🎯").strip()[:8]
    cor = (body.get("cor") or "#4ade80").strip()[:16]
    freq_alvo = max(1, min(7, int(body.get("freq_alvo") or 7)))

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute("SELECT COALESCE(MAX(ordem),0)+1 AS o FROM hab_habits WHERE username=%s", (user,))
        ordem = cur.fetchone()["o"]
        cur.execute(
            """
            INSERT INTO hab_habits (username, nome, emoji, cor, freq_alvo, ordem)
            VALUES (%s,%s,%s,%s,%s,%s)
            RETURNING id, nome, emoji, cor, freq_alvo, ordem, ativo
            """,
            (user, nome, emoji, cor, freq_alvo, ordem),
        )
        habit = dict(cur.fetchone())
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"habit": habit}


@router.put("/api/habitos/{habit_id}")
async def update_habit(habit_id: int, request: Request):
    user = _require(request)
    body = await request.json()

    fields = []
    values = []
    for col, key, cast in [
        ("nome", "nome", str),
        ("emoji", "emoji", str),
        ("cor", "cor", str),
        ("freq_alvo", "freq_alvo", int),
        ("ordem", "ordem", int),
        ("ativo", "ativo", int),
    ]:
        if key in body:
            v = body[key]
            if col == "freq_alvo":
                v = max(1, min(7, int(v)))
            elif col == "ativo":
                v = 1 if v else 0
            else:
                v = cast(v)
            fields.append(f"{col}=%s")
            values.append(v)
    if not fields:
        return {"ok": True}
    values.extend([habit_id, user])

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE hab_habits SET {', '.join(fields)} WHERE id=%s AND username=%s",
            values,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Hábito não encontrado")
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.delete("/api/habitos/{habit_id}")
async def delete_habit(habit_id: int, request: Request):
    """Soft delete — marca como inativo. Mantém histórico de marks."""
    user = _require(request)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE hab_habits SET ativo=0 WHERE id=%s AND username=%s",
            (habit_id, user),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True}


@router.post("/api/habitos/{habit_id}/mark")
async def toggle_mark(habit_id: int, request: Request):
    """
    Toggle de marcação para um dia. Body: { data: 'YYYY-MM-DD', status: 'done'|'skip' }

    Lógica: se já existe mark igual, remove (toggle off → neutro).
            Se existe diferente, atualiza. Se não existe, insere.
    """
    user = _require(request)
    body = await request.json()
    d = _parse_date(body.get("data"))
    status_in = body.get("status")
    if status_in not in ("done", "skip"):
        raise HTTPException(400, "Status inválido")

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        # Verifica ownership do hábito
        cur.execute(
            "SELECT id FROM hab_habits WHERE id=%s AND username=%s",
            (habit_id, user),
        )
        if not cur.fetchone():
            raise HTTPException(404, "Hábito não encontrado")

        cur.execute(
            "SELECT status FROM hab_marks WHERE username=%s AND habit_id=%s AND data=%s",
            (user, habit_id, d),
        )
        existing = cur.fetchone()
        if existing and existing["status"] == status_in:
            cur.execute(
                "DELETE FROM hab_marks WHERE username=%s AND habit_id=%s AND data=%s",
                (user, habit_id, d),
            )
            new_status = None
        elif existing:
            cur.execute(
                "UPDATE hab_marks SET status=%s WHERE username=%s AND habit_id=%s AND data=%s",
                (status_in, user, habit_id, d),
            )
            new_status = status_in
        else:
            cur.execute(
                "INSERT INTO hab_marks (username, habit_id, data, status) VALUES (%s,%s,%s,%s)",
                (user, habit_id, d, status_in),
            )
            new_status = status_in
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"ok": True, "status": new_status}


@router.get("/api/habitos/week")
async def week_view(request: Request, data: str | None = None):
    """
    Retorna a semana (seg→dom) com todas as marcações dos hábitos ativos.
    `data` é qualquer dia da semana desejada. Default: semana atual.

    Response:
    {
      week_start: 'YYYY-MM-DD',
      days: ['YYYY-MM-DD' × 7],
      habits: [{ id, nome, emoji, cor, freq_alvo, marks: { 'YYYY-MM-DD': 'done'|'skip' }, realizado, aderencia }]
    }
    """
    user = _require(request)
    base = _parse_date(data)
    week_start = _week_start(base)
    days = [week_start + timedelta(days=i) for i in range(7)]

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            """
            SELECT id, nome, emoji, cor, freq_alvo, ordem
              FROM hab_habits
             WHERE username=%s AND ativo=1
             ORDER BY ordem, id
            """,
            (user,),
        )
        habits = [dict(r) for r in cur.fetchall()]
        if habits:
            ids = [h["id"] for h in habits]
            cur.execute(
                """
                SELECT habit_id, data, status
                  FROM hab_marks
                 WHERE username=%s AND habit_id = ANY(%s) AND data BETWEEN %s AND %s
                """,
                (user, ids, days[0], days[-1]),
            )
            marks_by_habit: dict[int, dict[str, str]] = {h["id"]: {} for h in habits}
            for r in cur.fetchall():
                marks_by_habit[r["habit_id"]][r["data"].isoformat()] = r["status"]
        else:
            marks_by_habit = {}
    finally:
        cur.close()
        conn.close()

    for h in habits:
        h_marks = marks_by_habit.get(h["id"], {})
        h["marks"] = h_marks
        realizado = sum(1 for s in h_marks.values() if s == "done")
        h["realizado"] = realizado
        h["aderencia"] = round(realizado / h["freq_alvo"] * 100, 1) if h["freq_alvo"] else 0.0

    return {
        "week_start": week_start.isoformat(),
        "days": [d.isoformat() for d in days],
        "habits": habits,
    }


@router.get("/api/habitos/stats")
async def stats(request: Request):
    """
    Estatísticas por hábito: streak atual, recorde, total de done, % do mês.

    Response:
    {
      today: 'YYYY-MM-DD',
      habits: [{ id, nome, emoji, cor, freq_alvo, streak, record, total, mes_done, mes_pct }]
    }
    """
    user = _require(request)
    today = date.today()
    month_start = today.replace(day=1)

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        cur.execute(
            """
            SELECT id, nome, emoji, cor, freq_alvo
              FROM hab_habits
             WHERE username=%s AND ativo=1
             ORDER BY ordem, id
            """,
            (user,),
        )
        habits = [dict(r) for r in cur.fetchall()]
        if not habits:
            return {"today": today.isoformat(), "habits": []}

        ids = [h["id"] for h in habits]
        cur.execute(
            """
            SELECT habit_id, data, status
              FROM hab_marks
             WHERE username=%s AND habit_id = ANY(%s) AND status='done'
            """,
            (user, ids),
        )
        done_by_habit: dict[int, set] = {h["id"]: set() for h in habits}
        for r in cur.fetchall():
            done_by_habit[r["habit_id"]].add(r["data"])
    finally:
        cur.close()
        conn.close()

    days_in_month_so_far = (today - month_start).days + 1
    for h in habits:
        done_set = done_by_habit.get(h["id"], set())
        streak, record = _compute_streak(done_set, today)
        h["streak"] = streak
        h["record"] = record
        h["total"] = len(done_set)
        h["mes_done"] = sum(1 for d in done_set if d >= month_start)
        # Meta proporcional do mês: freq_alvo dias por semana × (dias_decorridos / 7)
        meta_mes = h["freq_alvo"] * (days_in_month_so_far / 7)
        h["mes_pct"] = round(h["mes_done"] / meta_mes * 100, 1) if meta_mes > 0 else 0.0

    return {"today": today.isoformat(), "habits": habits}


@router.get("/api/habitos/heatmap")
async def heatmap(request: Request, year: int | None = None):
    """
    Heatmap anual estilo GitHub. Retorna contagem de 'done' por dia (somando todos os hábitos).

    Response:
    {
      year: 2026,
      days: { 'YYYY-MM-DD': { done: 3, total: 5, pct: 60 } }
    }

    Critério de intensidade: percentual de hábitos completados naquele dia.
    O denominador (total) é o número de hábitos ativos que já existiam até aquela data
    (created_at <= dia + 1). Hábitos desativados ainda contam pois não temos
    timestamp de desativação — soft delete simples.
    """
    user = _require(request)
    y = year or date.today().year
    start = date(y, 1, 1)
    end = date(y, 12, 31)

    conn = get_conn()
    cur = dict_cursor(conn)
    try:
        # Marcações done por dia
        cur.execute(
            """
            SELECT data, COUNT(*) AS c
              FROM hab_marks
             WHERE username=%s AND status='done' AND data BETWEEN %s AND %s
             GROUP BY data
            """,
            (user, start, end),
        )
        done_by_day = {r["data"]: int(r["c"]) for r in cur.fetchall()}

        # Datas de criação de todos os hábitos do usuário (ativos ou não)
        cur.execute(
            """
            SELECT DATE(created_at) AS d
              FROM hab_habits
             WHERE username=%s
            """,
            (user,),
        )
        created_dates = sorted(r["d"] for r in cur.fetchall() if r["d"] is not None)
    finally:
        cur.close()
        conn.close()

    def total_active_on(d: date) -> int:
        # Hábitos cuja data de criação é <= d
        n = 0
        for cd in created_dates:
            if cd <= d:
                n += 1
            else:
                break
        return n

    days_out: dict = {}
    for iso_str, done in done_by_day.items():
        d = iso_str if isinstance(iso_str, date) else datetime.strptime(iso_str, "%Y-%m-%d").date()
        total = total_active_on(d)
        pct = round(done / total * 100) if total > 0 else 0
        days_out[d.isoformat()] = {"done": done, "total": total, "pct": pct}

    return {"year": y, "days": days_out}
