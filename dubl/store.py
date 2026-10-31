"""Каталог посилань агентств — єдине, що зберігаємо на диск (SQLite).

Зберігаємо ЛИШЕ посилання/контакти агентств (домен, сайт, телефон, місто,
джерело). Жодних оголошень, фото чи їх даних тут немає. Каталог поповнюється
разовим сидом (OSM по всій Україні) і далі зростає з кожним запитом
(Serper по місту) — щоб не шукати щоразу з нуля.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from urllib.parse import urlparse

from dubl.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agencies (
    domain     TEXT PRIMARY KEY,
    name       TEXT DEFAULT '',
    website    TEXT DEFAULT '',
    phone      TEXT DEFAULT '',
    city       TEXT DEFAULT '',
    source     TEXT DEFAULT '',
    added_at   INTEGER,
    last_seen  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agencies_city ON agencies(city);
"""


def _domain(url: str | None) -> str:
    try:
        net = urlparse(url or "").netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.catalog_db, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _init_sync() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _upsert_sync(items: list[dict]) -> int:
    now = int(time.time())
    rows = []
    for a in items:
        dom = _domain(a.get("website"))
        if not dom:
            continue
        rows.append((
            dom, a.get("name") or "", a.get("website") or "", a.get("phone") or "",
            a.get("city") or "", a.get("source") or "", now, now,
        ))
    if not rows:
        return 0
    with _connect() as conn:
        conn.executemany(
            """INSERT INTO agencies(domain,name,website,phone,city,source,added_at,last_seen)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(domain) DO UPDATE SET
                 last_seen=excluded.last_seen,
                 name=CASE WHEN agencies.name='' THEN excluded.name ELSE agencies.name END,
                 phone=CASE WHEN agencies.phone='' THEN excluded.phone ELSE agencies.phone END,
                 city=CASE WHEN agencies.city='' THEN excluded.city ELSE agencies.city END""",
            rows,
        )
    return len(rows)


def _by_city_sync(city: str) -> list[dict]:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT domain,name,website,phone,city,source FROM agencies WHERE city=? ORDER BY name",
            (city,),
        )
        return [dict(r) for r in cur.fetchall()]


def _stats_sync() -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM agencies").fetchone()[0]
        with_site = conn.execute("SELECT COUNT(*) FROM agencies WHERE website!=''").fetchone()[0]
        top = conn.execute(
            "SELECT city, COUNT(*) n FROM agencies WHERE city!='' GROUP BY city ORDER BY n DESC LIMIT 20"
        ).fetchall()
        return {"total": total, "with_site": with_site, "top_cities": {r[0]: r[1] for r in top}}


async def init() -> None:
    await asyncio.to_thread(_init_sync)


async def upsert(items: list[dict]) -> int:
    return await asyncio.to_thread(_upsert_sync, items)


async def by_city(city: str) -> list[dict]:
    return await asyncio.to_thread(_by_city_sync, city)


async def stats() -> dict:
    return await asyncio.to_thread(_stats_sync)
