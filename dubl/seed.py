"""Разовий сид каталогу агентств по всій Україні.

Запуск (на сервері):
    cd /opt/dubl.top
    PYTHONPATH=/opt/dubl.top ./venv/bin/python -m dubl.seed

Бере office=estate_agent з OpenStreetMap по всій Україні (ті, що мають сайт),
складає в каталог (SQLite). Далі каталог зростає з кожним запитом по місту.
"""
from __future__ import annotations

import asyncio

from dubl import store
from dubl.catalog import find_agencies_osm_country


async def main() -> None:
    await store.init()
    print("Сид OSM (вся Україна) — зачекайте, Overpass може думати до кількох хвилин…", flush=True)
    items = await find_agencies_osm_country()
    with_site = [a for a in items if a.get("website")]
    saved = await store.upsert(with_site)
    s = await store.stats()
    print(f"OSM знайдено з сайтом: {len(with_site)}; записано/оновлено: {saved}", flush=True)
    print(f"Каталог тепер: total={s['total']}, with_site={s['with_site']}", flush=True)
    print(f"Топ міст: {s['top_cities']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
