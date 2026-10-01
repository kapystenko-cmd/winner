"""
Диагностический скрипт — прогоняет OLX-поиск НАПРЯМУЮ, без сайта и без отчёта.
Запуск с сервера:
    cd /opt/ocinka
    venv/bin/python scripts/test_olx_search.py

Печатает ВСЕ найденные объявления (без ценового коридора) — чтобы своими
глазами увидеть, что реально есть на OLX прямо сейчас, и убедиться, что
задеплоенный код действительно новый (аренда должна быть уже отсеяна).
"""
import asyncio
import sys
sys.path.insert(0, ".")

from app.services.olx_service import find_olx_analogs


async def main():
    city = "Слов'янськ"
    rooms = 2
    area_sqm = 45.2

    print(f"=== Пошук OLX: {city}, {rooms}-кімн., ~{area_sqm} м², БЕЗ цінового коридору ===\n")

    results = await find_olx_analogs(
        city=city,
        property_type="apartment",
        rooms=rooms,
        area_sqm=area_sqm,
        report_id="diagnostic-test",
        screenshots=False,
        price_min_uah=None,   # <-- намеренно без ограничения снизу
        price_max_uah=None,   # <-- намеренно без ограничения сверху
    )

    print(f"\n=== Знайдено кандидатів: {len(results)} ===\n")
    for i, item in enumerate(sorted(results, key=lambda x: x.get("price_uah") or 0), 1):
        price = item.get("price_uah") or 0
        area = item.get("area_sqm") or "?"
        rooms_found = item.get("rooms") or "?"
        ppsm = item.get("price_per_sqm") or "?"
        url = item.get("url") or ""
        print(f"{i:2}. {price:>12,.0f} грн | {area} м² | {rooms_found} кімн | {ppsm} грн/м² | {url}")

    if results:
        prices = [item.get("price_uah") or 0 for item in results if item.get("price_uah")]
        if prices:
            print(f"\nМін. ціна: {min(prices):,.0f} грн")
            print(f"Макс. ціна: {max(prices):,.0f} грн")
            print(f"Медіана (приблизно): {sorted(prices)[len(prices)//2]:,.0f} грн")


if __name__ == "__main__":
    asyncio.run(main())
