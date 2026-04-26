import os
import psycopg
from psycopg.rows import dict_row


def get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    conn = psycopg.connect(url, row_factory=dict_row)
    conn.autocommit = False
    return conn


def dict_cursor(conn):
    return conn.cursor()
