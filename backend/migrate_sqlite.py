"""
One-off migration helper: exports data from existing SQLite databases and
inserts it into the PostgreSQL instance pointed to by DATABASE_URL.

Run once manually AFTER deploying the new code to Railway and BEFORE
discarding the SQLite files:

    DATABASE_URL=<postgres_url> python backend/migrate_sqlite.py

The script is idempotent for tables that use TEXT primary keys (spaces, folders,
lists, tasks, documents, features, document_versions); for SERIAL-pk tables it
checks for existing rows by a unique business key where possible, or skips if
the table already has data (safe for a fresh database).
"""

import os
import sys
import sqlite3
import json
from pathlib import Path

import psycopg2
import psycopg2.extras

BASE_DIR = Path(__file__).parent.parent

SQLITE_PATHS = {
    "users":      BASE_DIR / "data" / "users.db",
    "gestao":     BASE_DIR / "data" / "gestao.db",
    "financeiro": BASE_DIR / "data" / "financeiro.db",
    "fin_pessoais": BASE_DIR / "data" / "fin_pessoais.db",
}


def sqlite_rows(db_path: Path, table: str) -> list[dict]:
    """Return all rows from a SQLite table as a list of dicts."""
    if not db_path.exists():
        print(f"  [skip] {db_path} not found")
        return []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(f"SELECT * FROM {table}").fetchall()]
    except Exception as e:
        print(f"  [warn] could not read {table} from {db_path}: {e}")
        rows = []
    finally:
        con.close()
    return rows


def pg_insert_ignore(cur, table: str, rows: list[dict], conflict_col: str = "id"):
    """Insert rows into a PG table, ignoring conflicts on conflict_col."""
    if not rows:
        return 0
    sample = rows[0]
    cols = list(sample.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    col_str = ", ".join(cols)
    count = 0
    for row in rows:
        vals = [row[c] for c in cols]
        try:
            cur.execute(
                f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) "
                f"ON CONFLICT ({conflict_col}) DO NOTHING",
                vals,
            )
            count += 1
        except Exception as e:
            print(f"  [warn] row skipped in {table}: {e}")
    return count


def pg_insert_serial(cur, table: str, rows: list[dict], check_empty: bool = True):
    """Insert rows into a SERIAL-pk table, optionally skipping if table has data."""
    if not rows:
        return 0
    if check_empty:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        if cur.fetchone()[0] > 0:
            print(f"  [skip] {table} already has data — not overwriting")
            return 0
    sample = rows[0]
    # Strip 'id' column — let PG assign a new SERIAL id
    cols = [c for c in sample.keys() if c != "id"]
    if not cols:
        return 0
    placeholders = ", ".join(["%s"] * len(cols))
    col_str = ", ".join(cols)
    count = 0
    for row in rows:
        vals = [row[c] for c in cols]
        try:
            cur.execute(
                f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})",
                vals,
            )
            count += 1
        except Exception as e:
            print(f"  [warn] row skipped in {table}: {e}")
    return count


def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("=== Migrating users (users.db → users) ===")
    rows = sqlite_rows(SQLITE_PATHS["users"], "users")
    n = pg_insert_ignore(cur, "users", rows, conflict_col="username")
    print(f"  inserted {n} users")
    conn.commit()

    print("=== Migrating gestao (gestao.db) ===")
    for table in ("spaces", "folders", "lists", "tasks", "documents", "features"):
        rows = sqlite_rows(SQLITE_PATHS["gestao"], table)
        n = pg_insert_ignore(cur, table, rows, conflict_col="id")
        print(f"  {table}: {n} rows")
        conn.commit()

    # document_versions has a SERIAL id — map via doc_id+created_at
    rows = sqlite_rows(SQLITE_PATHS["gestao"], "document_versions")
    if rows:
        cur.execute("SELECT COUNT(*) FROM document_versions")
        if cur.fetchone()["count"] == 0:
            cols = [c for c in rows[0].keys() if c != "id"]
            col_str = ", ".join(cols)
            ph = ", ".join(["%s"] * len(cols))
            for row in rows:
                cur.execute(
                    f"INSERT INTO document_versions ({col_str}) VALUES ({ph})",
                    [row[c] for c in cols],
                )
            conn.commit()
            print(f"  document_versions: {len(rows)} rows")
        else:
            print("  document_versions: already has data — skipped")

    print("=== Migrating financeiro (financeiro.db) ===")
    serial_tables = [
        "fin_clientes", "fin_fornecedores", "fin_contas", "fin_categorias",
        "fin_centros_custo", "fin_lancamentos", "fin_transferencias",
        "fin_importacoes", "fin_importacao_itens",
    ]
    for table in serial_tables:
        rows = sqlite_rows(SQLITE_PATHS["financeiro"], table)
        n = pg_insert_serial(cur, table, rows)
        print(f"  {table}: {n} rows")
        conn.commit()

    print("=== Migrating fin_pessoais (fin_pessoais.db) ===")
    fp_tables = [
        "fp_contas", "fp_categorias", "fp_lancamentos",
        "fp_importacoes", "fp_importacao_itens",
    ]
    for table in fp_tables:
        rows = sqlite_rows(SQLITE_PATHS["fin_pessoais"], table)
        n = pg_insert_serial(cur, table, rows)
        print(f"  {table}: {n} rows")
        conn.commit()

    cur.close()
    conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    main()
