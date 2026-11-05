"""Optional Postgres logging. Every function is a no-op (and never raises)
when DATABASE_URL is unset or the database is unreachable, so the bot keeps running."""
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("mercari-bot")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    cycle       INT,
    keyword     TEXT NOT NULL,
    results     INT NOT NULL,
    new_hits    INT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS listings (
    id               TEXT PRIMARY KEY,
    name             TEXT,
    price_jpy        INT,
    price_czk        NUMERIC(12,2),
    condition_id     TEXT,
    category_id      TEXT,
    seller_id        TEXT,
    item_type        TEXT,
    url              TEXT,
    thumbnail        TEXT,
    keyword          TEXT,
    score            INT,
    listed_at        TIMESTAMPTZ,
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_seen       INT NOT NULL DEFAULT 1,
    alerted          BOOLEAN NOT NULL DEFAULT FALSE,
    alerted_at       TIMESTAMPTZ,
    raw              JSONB
);
CREATE INDEX IF NOT EXISTS listings_price_idx ON listings (price_jpy);
CREATE INDEX IF NOT EXISTS listings_first_seen_idx ON listings (first_seen DESC);
"""


def _get():
    global _conn
    if not DATABASE_URL:
        return None
    if _conn is None or _conn.closed:
        import psycopg2
        _conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        _conn.autocommit = True
        with _conn.cursor() as cur:
            cur.execute(SCHEMA)
        log.info("Database connected, schema ready")
    return _conn


def _safe(fn):
    def wrapper(*a, **kw):
        global _conn
        try:
            return fn(*a, **kw)
        except Exception as e:
            log.warning(f"DB error in {fn.__name__}: {e}")
            try:
                if _conn:
                    _conn.close()
            except Exception:
                pass
            _conn = None
            return None
    return wrapper


def _ts(value):
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc) if value else None
    except (ValueError, TypeError, OSError):
        return None


@_safe
def init() -> bool:
    return _get() is not None


@_safe
def load_alerted_ids() -> set:
    conn = _get()
    if not conn:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM listings WHERE alerted")
        return {r[0] for r in cur.fetchall()}


@_safe
def log_listings(items: list, keyword: str, czk_rate: float) -> None:
    """Upsert every listing seen in a search (not only hits)."""
    conn = _get()
    if not conn or not items:
        return
    from psycopg2.extras import execute_values
    rows = [(
        i["id"], i.get("name"), i.get("price"), round(i.get("price", 0) * czk_rate, 2),
        i.get("itemConditionId"), i.get("categoryId"), i.get("sellerId"), i.get("itemType"),
        f"https://jp.mercari.com/item/{i['id']}", (i.get("thumbnails") or [""])[0],
        keyword, i.get("score"), _ts(i.get("created")), json.dumps(i.get("raw", {}), ensure_ascii=False),
    ) for i in items]
    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO listings (id, name, price_jpy, price_czk, condition_id, category_id, seller_id,
                                  item_type, url, thumbnail, keyword, score, listed_at, raw)
            VALUES %s
            ON CONFLICT (id) DO UPDATE SET
                price_jpy = EXCLUDED.price_jpy, price_czk = EXCLUDED.price_czk,
                score = EXCLUDED.score, last_seen = now(),
                times_seen = listings.times_seen + 1, raw = EXCLUDED.raw
        """, rows, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)")


@_safe
def mark_alerted(ids: list) -> None:
    conn = _get()
    if not conn or not ids:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE listings SET alerted = TRUE, alerted_at = now() WHERE id = ANY(%s)", (ids,))


@_safe
def log_search(cycle: int, keyword: str, results: int, new_hits: int) -> None:
    conn = _get()
    if not conn:
        return
    with conn.cursor() as cur:
        cur.execute("INSERT INTO searches (cycle, keyword, results, new_hits) VALUES (%s,%s,%s,%s)",
                    (cycle, keyword, results, new_hits))
