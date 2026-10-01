"""
Тестовий скрипт: ПОВНИЙ цикл формування звіту без завантаження документів і
без OCR — тільки за адресою й даними, зчитаними з реального прикладного
оголошення на OLX. Прогонить 2 приклади (будинок і квартиру), для кожного:

  1. читає ціну/площу/кімнати/поверх реального оголошення OLX (тим самим
     кодом, яким бойовий сайт читає будь-яке оголошення);
  2. шукає аналоги ТІЛЬКИ на OLX (DIM.RIA свідомо вимкнено нижче) — з тим
     самим фолбеком "спочатку район, якщо аналогів мало — по всьому місту",
     який вже є в бойовому app/services/olx_service.py::find_olx_analogs;
  3. відбирає до 5 найкращих аналогів тим самим алгоритмом ранжування, що й
     бойовий /find-analogs (app/services/valuation_analytics.py::analyse);
  4. знімає скріншот кожного фінального аналогу (той самий виклик, що робить
     бойовий /generate: _capture_selected_analog_screenshots);
  5. рахує вартість і діапазон — стартова/референсна ціна = ціна прикладного
     оголошення, робочий коридор = ±25% від неї (використано напряму той
     самий бойовий механізм "коридору е-довідки", без жодних змін логіки:
     app/api/routes.py::_search_price_band + _apply_report_valuation);
  6. формує повний Word-пакет (app/services/report_generator.py::
     generate_full_word_package) — той самий документ, що йде клієнту.

У БД нічого не пишеться і не читається: Report/User/Analog створюються як
звичайні python-об'єкти-параметри (генератор Word працює лише з переданими
йому об'єктами, БД йому не потрібна).

ЗАПУСК (на реальному сервері, де є робочі ключі ZENROWS_API_KEY /
SCRAPERAPI у .env — без них жодне звернення до OLX не спрацює):

    cd /opt/ocinka
    venv/bin/python scripts/test_full_pipeline_house_apartment.py

РЕЗУЛЬТАТ:
    ./test_output/budynok_odesa_gagarina.docx
    ./test_output/kvartyra_ostrozkoho.docx

ПЕРЕД ЗАПУСКОМ — обов'язково перевірте блок TEST_CASES нижче:
місто ("city") потрібне для пошуку і не завжди однозначно видно з URL
оголошення (OLX не показує місто на сторінці оголошення настільки явно,
щоб скрипт міг взяти його звідти автоматично). Для будинку з Гагаріна
місто підставлено як "Одеса" (видно зі слага URL — "odesskaya"); для
квартири на Острозького місто НЕ вказано — його треба вписати вручну.
"district" можна лишити порожнім (None) — тоді пошук одразу піде по
всьому місту; якщо вкажете район, спрацює саме той фолбек "район → місто",
який просили перевірити.
"""
import asyncio
import shutil
import sys
import uuid
import traceback
from pathlib import Path

sys.path.insert(0, ".")


# ============================ TEST CASES =============================
TEST_CASES = [
    {
        "label": "Будинок — Одеса, вул. Гагаріна",
        "output_file": "test_output/budynok_odesa_gagarina.docx",
        "listing_url": "https://www.olx.ua/d/uk/obyavlenie/prodam-dom-odesskaya-gagarina-IDYtkkn.html",
        "object_type": "house",
        "address": "м. Одеса, вул. Гагаріна",
        "city": "Одеса",
        "district": None,
    },
    {
        "label": "Квартира — вул. Князя Острозького",
        "output_file": "test_output/kvartyra_ostrozkoho.docx",
        "listing_url": "https://www.olx.ua/d/uk/obyavlenie/prodazh-odno-kmn-kvartiri-vul-knyazya-ostrozkogo-ID115fWc.html",
        "object_type": "apartment",
        "address": "вул. Князя Острозького",
        "city": "Харьков",  # <-- ОБОВ'ЯЗКОВО вкажіть місто перед запуском!
        "district": None,
    },
]

# Дані тестового оцінювача/СОД — лише для шапки тестового документа.
# Це не впливає на пошук/розрахунок, можна лишити як є.
TEST_EVALUATOR = {
    "full_name": "Тестовий Оцінювач Оцінювачович",
    "cert_number": "000000",
    "sod_name": "ФОП Тестовий (тестовий запуск скрипта)",
    "sod_address": "",
    "sod_cert_number": "",
}

MIN_ANALOGS = 5
# =======================================================================


async def build_one_report(case: dict):
    from app.core.config import settings
    from app.models.models import (
        Report, Analog, User, ReportStatus, ObjectType, DealType, EvalMode, ValueSelectionMode,
    )
    from app.services.olx_service import get_usd_rate, get_listing_details
    from app.services.analog_search import search_all
    from app.services.valuation_analytics import analyse, statistics_for_selected
    from app.services.report_generator import generate_full_word_package
    from app.api.routes import (
        _capture_selected_analog_screenshots,
        _apply_report_valuation,
        _search_price_band,
        _as_float,
    )

    label = case["label"]
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")

    if not case.get("city"):
        print("ПОМИЛКА: не вказано 'city' для цього прикладу в TEST_CASES — заповніть і запустіть знову.")
        return None

    # 1. Реальні дані прикладного оголошення — тим самим кодом, яким бойовий
    # сайт читає будь-яке оголошення OLX.
    usd_rate = await get_usd_rate()
    print(f"Курс USD: {usd_rate}")
    print(f"Читаю приклад: {case['listing_url']}")
    reference = await get_listing_details(case["listing_url"], usd_rate)
    if not reference or not reference.get("price_uah"):
        print("ПОМИЛКА: не вдалося прочитати приклад оголошення "
              "(сайт міг тимчасово заблокувати запит або змінити розмітку).")
        print(f"Відповідь: {reference}")
        return None
    print(
        f"Приклад: ціна={reference.get('price_uah')} грн, площа={reference.get('area_sqm')} м², "
        f"кімнат={reference.get('rooms')}, поверх={reference.get('floor')}/{reference.get('total_floors')}"
    )

    reference_price = float(reference["price_uah"])
    area_sqm = reference.get("area_sqm")
    # Точна кількість кімнат — жорсткий критерій пошуку тільки для квартир
    # (так само, як у бойовому /find-analogs). Для будинку кімнати з
    # прикладу не нав'язуються пошуку: приватні будинки надто різні за
    # плануванням, а саме "аналогів мало" і була скарга.
    rooms = reference.get("rooms") if case["object_type"] == "apartment" else None
    floor = reference.get("floor")
    total_floors = reference.get("total_floors")

    if case["object_type"] == "apartment" and not rooms:
        print("ПОМИЛКА: у прикладного оголошення не вдалося визначити кількість кімнат "
              "— без цього бойовий пошук для квартир також не запускається.")
        return None
    if not area_sqm:
        print("ПОМИЛКА: у прикладного оголошення не вдалося визначити площу.")
        return None

    # 2. Суб'єкт оцінки + робочий ціновий коридор. Стартова/референсна ціна —
    # ціна прикладного оголошення; коридор — рівно ±25% від неї. Це напряму
    # той самий бойовий механізм, яким сайт рахує коридор е-довідки
    # (routes.py::_search_price_band), застосований тут без жодних змін.
    subject = {
        "city": case["city"], "object_type": case["object_type"],
        "area_sqm": area_sqm, "rooms": rooms, "floor": floor,
        "total_floors": total_floors, "year_built": None,
        "district": case.get("district"), "region": None,
    }
    price_band = _search_price_band(reference_price)
    price_min_uah = price_band.get("corridor_minimum")
    price_max_uah = price_band.get("corridor_maximum")
    print(f"Робочий ціновий коридор (±25% від {round(reference_price)} грн): {price_min_uah}–{price_max_uah} грн")

    # 3. DIM.RIA явно вимкнено для цього тесту — шукаємо тільки на OLX.
    # (У .env й так стоїть DIMRIA_ENABLED=false; тут вимикається ще й
    # напряму в об'єкті settings, щоб цей тест не залежав від .env.)
    settings.dimria_enabled = False
    raw_analogs, search_journal = await search_all(
        subject, settings, include_olx=True,
        price_min_uah=price_min_uah, price_max_uah=price_max_uah,
    )
    print(f"Знайдено кандидатів на OLX (до фільтрів): {len(raw_analogs)}")
    if not raw_analogs:
        print("ПОМИЛКА: аналогів не знайдено взагалі. Журнал пошуку:")
        print(search_journal)
        return None

    # 4. Нормалізація ціни/площі і фільтр по ціновому коридору — точно як у
    # бойовому /find-analogs (app/api/routes.py).
    normalized = []
    for item in raw_analogs:
        candidate = dict(item or {})
        price = _as_float(candidate.get("price_uah"))
        area = _as_float(candidate.get("area_sqm"))
        if price < 5_000 or (area > 0 and price / area < 300):
            continue
        candidate["price_uah"] = round(price)
        if area > 0:
            candidate["price_per_sqm"] = round(price / area)
            candidate["area_sqm"] = area
        normalized.append(candidate)
    if price_min_uah is not None and price_max_uah is not None:
        normalized = [
            c for c in normalized
            if price_min_uah <= _as_float(c.get("price_uah")) <= price_max_uah
        ]
    print(f"Кандидатів у робочому коридорі: {len(normalized)}")

    if case["object_type"] == "apartment" and rooms:
        normalized = [
            c for c in normalized
            if int(_as_float(c.get("rooms"), -1)) == int(rooms) and _as_float(c.get("area_sqm")) > 0
        ]
        print(f"Кандидатів після точного фільтра по кімнатах: {len(normalized)}")

    if not normalized:
        print("ПОМИЛКА: після фільтрів не залишилось жодного кандидата.")
        return None

    # 5. Ранжування/відбір — та сама аналітика, що й у бойовому /find-analogs.
    analysis = analyse(normalized, subject, selection_limit=12, price_band=price_band)
    candidates_ranked = analysis.selected
    print(f"Пройшли комплексний аналіз (ранжування): {len(candidates_ranked)}")
    if len(candidates_ranked) < MIN_ANALOGS:
        print(
            f"УВАГА: знайдено лише {len(candidates_ranked)} аналогів (просили мінімум {MIN_ANALOGS}). "
            f"Це чесний результат наявного ринку/фільтрів, а не помилка скрипта — "
            f"продовжую з тим, що реально знайшлось."
        )
    final_items = candidates_ranked[:5] if len(candidates_ranked) >= 5 else candidates_ranked

    # 6. Report / User / Analog як звичайні python-об'єкти — без сесії БД.
    # generate_full_word_package працює лише з переданими йому параметрами.
    report_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user = User(
        id=user_id, email="test@ocinka.pro", password_hash="x",
        full_name=TEST_EVALUATOR["full_name"], cert_number=TEST_EVALUATOR["cert_number"],
        sod_name=TEST_EVALUATOR["sod_name"], sod_address=TEST_EVALUATOR["sod_address"],
        sod_cert_number=TEST_EVALUATOR["sod_cert_number"], sod_header_offset_mm=0,
    )
    report = Report(
        id=report_id, user_id=user_id,
        object_type=ObjectType(case["object_type"]), deal_type=DealType.CASH, eval_mode=EvalMode.STANDARD,
        address=case["address"], area_sqm=area_sqm, rooms=rooms, floor=floor, total_floors=total_floors,
        status=ReportStatus.CALCULATING, ocr_raw={}, report_options={"include_screenshots": True},
        search_journal=search_journal, upload_files=[], e_certificate_files=[],
    )

    analog_objs = []
    for idx, item in enumerate(final_items, start=1):
        price = _as_float(item.get("price_uah"))
        area = _as_float(item.get("area_sqm"))
        analog_objs.append(Analog(
            id=uuid.uuid4(), report_id=report_id,
            source=str(item.get("source", "olx")), url=str(item.get("url", "")),
            title=str(item.get("title", ""))[:500],
            price_uah=price, price_per_sqm=(price / area if area > 0 else 0), area_sqm=area,
            rooms=item.get("rooms"), floor=item.get("floor"),
            address=f'{item.get("city", "")} {item.get("address", "")}'.strip()[:500],
            rank=idx, is_selected=True, raw_data=item,
        ))

    # 7. Розрахунок вартості з фінальної вибірки — точно як /select-analogs,
    # з "е-довідкою" = ціна прикладу (той самий бойовий ±25% коридор).
    statistics = statistics_for_selected([dict(a.raw_data or {}) for a in analog_objs], subject)
    if not statistics.get("recommended_value"):
        print("ПОМИЛКА: недостатньо цінових даних у фінальних аналогах для розрахунку.")
        return None
    statistics = _apply_report_valuation(
        report, statistics,
        {"e_certificate_value": reference_price, "e_certificate_manual_confirmed": True},
        reference_price,
    )
    report.valuation_statistics = statistics
    report.recommended_value = statistics["recommended_value"]
    report.estimated_value = report.recommended_value
    report.selected_value = report.recommended_value
    report.range_min = statistics["range_min"]
    report.range_max = statistics["range_max"]
    report.benchmark_fdmu = reference_price
    report.value_selection_mode = ValueSelectionMode.AUTOMATIC
    print(f"Розрахована вартість: {report.recommended_value} грн (діапазон {report.range_min}–{report.range_max} грн)")

    # 8. Скріншоти фінальних аналогів — той самий виклик, що робить бойовий
    # /generate прямо перед формуванням Word-пакета.
    print("Знімаю скріншоти фінальних аналогів…")
    created = await _capture_selected_analog_screenshots(report, analog_objs)
    print(f"Скріншотів створено: {created} з {len(analog_objs)}")

    # 9. Повний Word-пакет.
    print("Формую Word-документ…")
    word_path = await generate_full_word_package(report, user, analog_objs, include_screenshots=True)
    out_path = Path(case["output_file"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(word_path, out_path)
    print(f"ГОТОВО: {out_path.resolve()}")
    return out_path


async def main():
    Path("test_output").mkdir(exist_ok=True)
    results = []
    for case in TEST_CASES:
        try:
            results.append(await build_one_report(case))
        except Exception as error:
            traceback.print_exc()
            print(f"ПОМИЛКА в прикладі '{case['label']}': {error}")
            results.append(None)

    print("\n" + "=" * 70)
    print("ПІДСУМОК:")
    for case, result in zip(TEST_CASES, results):
        status = f"OK -> {result}" if result else "НЕ ВДАЛОСЯ (див. лог вище)"
        print(f"  {case['label']}: {status}")


if __name__ == "__main__":
    asyncio.run(main())
