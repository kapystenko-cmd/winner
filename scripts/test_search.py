"""
Универсальный диагностический скрипт — прогоняет пошук ОБОХ джерел
(DIM.RIA + OLX) НАПРЯМУЮ, без сайту, без бази даних, без відкриття звіту.

Запуск з сервера:
    cd /opt/ocinka
    venv/bin/python scripts/test_search.py

Щоб перевірити інший випадок — просто змініть параметри в блоці нижче
("=== ПАРАМЕТРИ ТЕСТУ ===") і запустіть знову.
"""
import asyncio
import sys
sys.path.insert(0, ".")

from app.services.olx_service import find_olx_analogs
from app.services.dimria_service import search_analogs as dimria_search


# =========================== ПАРАМЕТРИ ТЕСТУ ===========================
CITY = "Київ"
DISTRICT = "Борщагівка"          # можна залишити "" якщо район не важливий
PROPERTY_TYPE = "apartment"      # apartment / house / land / commercial
ROOMS = 2
AREA_SQM = 56.0
E_CERTIFICATE_VALUE = 3_000_000.0
PRICE_TOLERANCE = 0.25           # ±25%, як у реальному звіті
# =========================================================================


async def main():
    price_min = E_CERTIFICATE_VALUE * (1 - PRICE_TOLERANCE)
    price_max = E_CERTIFICATE_VALUE * (1 + PRICE_TOLERANCE)

    print("=" * 70)
    print(f"Місто: {CITY}" + (f", район: {DISTRICT}" if DISTRICT else ""))
    print(f"Тип об'єкта: {PROPERTY_TYPE}, кімнат: {ROOMS}, площа: ~{AREA_SQM} м²")
    print(f"Сума е-довідки: {E_CERTIFICATE_VALUE:,.0f} грн")
    print(f"Ціновий коридор ±{int(PRICE_TOLERANCE*100)}%: {price_min:,.0f} — {price_max:,.0f} грн")
    print("=" * 70)

    # --- DIM.RIA ---
    print("\n--- DIM.RIA ---")
    dimria_results = await dimria_search(
        city=CITY,
        property_type=PROPERTY_TYPE,
        rooms=ROOMS,
        area_sqm=AREA_SQM,
        district=DISTRICT or None,
        max_results=30,
    )
    dimria_in_corridor = [
        item for item in dimria_results
        if item.get("price_uah") and price_min <= item["price_uah"] <= price_max
    ]
    print(f"DIM.RIA знайшов усього: {len(dimria_results)}")
    print(f"DIM.RIA у ціновому коридорі: {len(dimria_in_corridor)}")
    for i, item in enumerate(sorted(dimria_results, key=lambda x: x.get("price_uah") or 0), 1):
        price = item.get("price_uah") or 0
        in_range = "✓ У КОРИДОРІ" if price_min <= price <= price_max else "  поза коридором"
        print(f"  {i:2}. {price:>12,.0f} грн | {item.get('area_sqm','?')} м² | "
              f"{item.get('rooms','?')} кімн | {in_range} | {item.get('url','')}")

    # --- OLX ---
    print("\n--- OLX (з ціновим коридором) ---")
    olx_results = await find_olx_analogs(
        city=CITY,
        property_type=PROPERTY_TYPE,
        rooms=ROOMS,
        area_sqm=AREA_SQM,
        report_id="diagnostic-test",
        screenshots=False,
        district=DISTRICT or None,
        price_min_uah=price_min,
        price_max_uah=price_max,
    )
    print(f"\nOLX у ціновому коридорі: {len(olx_results)}")
    for i, item in enumerate(sorted(olx_results, key=lambda x: x.get("price_uah") or 0), 1):
        price = item.get("price_uah") or 0
        print(f"  {i:2}. {price:>12,.0f} грн | {item.get('area_sqm','?')} м² | "
              f"{item.get('rooms','?')} кімн | {item.get('price_per_sqm','?')} грн/м² | {item.get('url','')}")

    # --- Підсумок ---
    total = len(dimria_in_corridor) + len(olx_results)
    print("\n" + "=" * 70)
    print(f"РАЗОМ у ціновому коридорі: {total} (DIM.RIA: {len(dimria_in_corridor)} + OLX: {len(olx_results)})")
    if total < 3:
        print("⚠ Менше 3 — на сайті це покаже попередження про тонкий ринок.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
