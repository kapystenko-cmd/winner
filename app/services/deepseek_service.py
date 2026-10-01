"""DeepSeek V4 Flash — генерація тексту звіту про оцінку українською"""
import json
import httpx
from app.core.config import settings


REPORT_PROMPT = """Ти — помічник оцінювача нерухомості в Україні. Напиши текст розділів звіту про оцінку нерухомості українською мовою, використовуючи офіційний діловий стиль.

ДАНІ ОБ'ЄКТА:
{object_data}

РИНКОВІ АНАЛОГИ (5 штук):
{analogs_data}

РЕЖИМ ОЦІНКИ: {eval_mode}
ОЦІНОЧНА ВАРТІСТЬ: {estimated_value} грн
ДІАПАЗОН: від {range_min} до {range_max} грн

Напиши такі розділи:

1. ОПИС ОБ'ЄКТА ОЦІНКИ — коротко, 3-4 речення, з усіма технічними даними.

2. АНАЛІЗ РИНКУ НЕРУХОМОСТІ — 2-3 речення про стан ринку в цьому місті/районі.

3. ОБҐРУНТУВАННЯ ВИБОРУ АНАЛОГІВ — чому обрано саме ці 5 об'єктів, що їх об'єднує з об'єктом оцінки (площа, розташування, стан). Посилання на НС №2 п.30-31.

4. РОЗРАХУНОК ВАРТОСТІ — опис порівняльного підходу згідно з НС №1 п.3, НС №2 п.30. Яким чином з цін аналогів отримано оціночну вартість. Згадай коригувальні коефіцієнти якщо є різниця в площі/поверсі/стані.

5. ВИСНОВОК — підсумок з фінальною сумою.

Поверни JSON:
{
  "description": "текст розділу 1",
  "market_analysis": "текст розділу 2",
  "analogs_justification": "текст розділу 3",
  "calculation": "текст розділу 4",
  "conclusion": "текст розділу 5"
}

Тільки JSON, без пояснень. Кожен розділ — 2-5 речень. Офіційний діловий стиль. Посилання на Національні стандарти оцінки."""


async def generate_report_text(
    object_data: dict,
    analogs: list,
    eval_mode: str,
    estimated_value: float,
    range_min: float,
    range_max: float,
) -> dict:
    """Генерація тексту звіту через DeepSeek V4 Flash"""
    
    if not settings.deepseek_api_key:
        # Якщо DeepSeek не налаштовано — повертаємо шаблонний текст
        return _fallback_text(object_data, eval_mode, estimated_value)
    
    # Форматуємо дані для промпту
    obj_str = json.dumps(object_data, ensure_ascii=False, indent=2)
    
    analogs_str = ""
    for i, a in enumerate(analogs, 1):
        analogs_str += f"\n{i}. {a.get('address', 'Адреса невідома')}"
        analogs_str += f" — {a.get('price_uah', 0):,.0f} грн"
        analogs_str += f", {a.get('area_sqm', 0)} м²"
        analogs_str += f", {a.get('rooms', '?')} кім."
        analogs_str += f", поверх {a.get('floor', '?')}"
    
    mode_names = {
        "standard": "Стандартна (середній діапазон)",
        "conservative": "Консервативна (нижній діапазон, НС №1 п.3, НС №2 п.31)",
        "optimistic": "Оптимістична (верхній діапазон, НС №2 п.30)",
    }
    
    prompt = REPORT_PROMPT.format(
        object_data=obj_str,
        analogs_data=analogs_str,
        eval_mode=mode_names.get(eval_mode, eval_mode),
        estimated_value=f"{estimated_value:,.0f}" if estimated_value else "не визначено",
        range_min=f"{range_min:,.0f}" if range_min else "?",
        range_max=f"{range_max:,.0f}" if range_max else "?",
    )
    
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": "Ти — експерт з оцінки нерухомості в Україні. Відповідай тільки українською."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 2000,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        
        text = data["choices"][0]["message"]["content"].strip()
        
        # Очищення від markdown
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        
        return json.loads(text)
        
    except Exception as e:
        print(f"DeepSeek error: {e}")
        return _fallback_text(object_data, eval_mode, estimated_value)


def _fallback_text(object_data: dict, eval_mode: str, estimated_value: float) -> dict:
    """Шаблонний текст якщо DeepSeek недоступний"""
    addr = object_data.get("address", "адреса не вказана")
    area = object_data.get("area_total", "?")
    rooms = object_data.get("rooms", "?")
    
    mode_text = {
        "standard": "стандартної методики оцінки",
        "conservative": "консервативної методики оцінки відповідно до НС №1 п.3, НС №2 п.31",
        "optimistic": "оптимістичної методики оцінки відповідно до НС №2 п.30",
    }
    
    return {
        "description": f"Об'єктом оцінки є квартира за адресою: {addr}. Загальна площа становить {area} м², кількість кімнат — {rooms}.",
        "market_analysis": "Аналіз ринку нерухомості проведено на основі актуальних пропозицій продажу аналогічних об'єктів у даному населеному пункті.",
        "analogs_justification": "Для порівняльного аналізу обрано 5 об'єктів-аналогів, які за основними характеристиками (місцезнаходження, площа, кількість кімнат, поверх) є найбільш подібними до об'єкта оцінки.",
        "calculation": f"Розрахунок ринкової вартості проведено із застосуванням {mode_text.get(eval_mode, 'стандартної методики')} на основі порівняльного підходу згідно з вимогами Національних стандартів оцінки.",
        "conclusion": f"На підставі проведеного аналізу ринкова вартість об'єкта оцінки становить {estimated_value:,.0f} грн." if estimated_value else "Оціночна вартість потребує уточнення.",
    }


# ===================== РОЗШИРЕНИЙ ЕКСПЕРТНИЙ ОПИС =========================

EXPERT_PROMPT = """Ти — сертифікований експерт-оцінювач нерухомості в Україні з 15-річним досвідом. Напиши РОЗГОРНУТИЙ ПРОФЕСІЙНИЙ опис об'єкта оцінки для офіційного звіту про оцінку майна.

ДАНІ ОБ'ЄКТА ОЦІНКИ:
{object_data}

РИНКОВІ АНАЛОГИ (порівняльні об'єкти):
{analogs_data}

ДАНІ З ФОТООБСТЕЖЕННЯ:
{evidence_data}
{price_guidance}
Напиши 4 розділи. Кожен розділ — МІНІМУМ 5-8 речень, розгорнуто, як професійний оцінювач. Загальний обсяг тексту — НЕ МЕНШЕ однієї повної сторінки А4.

1. ОБ'ЄКТ ОЦІНКИ ТА ЙОГО СТАН
Детально опиши:
- Тип об'єкта (квартира/будинок/земля), загальна площа, житлова площа, площа кухні, кількість кімнат, поверх і поверховість будинку
- Рік побудови будинку (якщо відомо), матеріал стін, тип перекриттів
- Поточний стан ремонту: вікна (металопластикові/дерев'яні), двері (вхідні/міжкімнатні), підлога (ламінат/лінолеум/плитка), стіни (шпалери/фарба/штукатурка), стеля (натяжна/побілка/фарба)
- Стан інженерних комунікацій: опалення (центральне/автономне), водопостачання, каналізація, газопостачання, електрика
- Санвузол (суміжний/роздільний), балкон/лоджія (засклені/незасклені)
- Меблювання (якщо залишається при продажу)
- Загальне враження: потребує капремонту / косметичного ремонту / в житловому стані / після євроремонту

2. ЛОКАЦІЯ ТА ІНФРАСТРУКТУРА
Детально опиши розташування:
- Точна адреса та район/мікрорайон міста
- Тип забудови району
- Найближчі зупинки транспорту — відстань у хвилинах пішки
- Школи, дитсадки — відстань
- Магазини, супермаркети, ринок — відстань
- Медичні заклади, аптеки
- Парки, зони відпочинку
- Відстань до центру міста / найближчого великого міста
- Екологічна ситуація
- Загальна привабливість локації для покупця

3. ОБҐРУНТУВАННЯ ПОРІВНЯЛЬНОГО ПІДХОДУ
- Чому обрано саме ці аналоги
- Діапазон цін аналогів та середня ціна за м²
- Коригувальні коефіцієнти (площа, поверх, стан)
- Посилання на НС №1 п.3, НС №2 п.30-31
- Обґрунтування достатності вибірки

4. ЕКСПЕРТНЕ РЕЗЮМЕ
- Характеристика об'єкта як типового/нетипового для ринку
- Фактори впливу на вартість (позитивні/негативні)
- Підсумкова ринкова вартість
- Рекомендації щодо збільшення вартості (якщо потребує ремонту)

Поверни JSON:
{{
  "object_and_condition": "текст розділу 1 (мінімум 8 речень)",
  "location": "текст розділу 2 (мінімум 8 речень)",
  "comparative_reasoning": "текст розділу 3 (мінімум 6 речень)",
  "expert_summary": "текст розділу 4 (мінімум 5 речень)"
}}

ТІЛЬКИ JSON, без пояснень, без markdown. Офіційний діловий стиль українською. Якщо параметр невідомий — НЕ вигадуй, напиши "за результатами огляду" або подібне."""


def _price_guidance_block(price_position: dict | None) -> str:
    """Optional prompt insert tying the narrative to the appraiser's chosen
    price level, strictly within evidenced facts.

    Returns an empty string when no position is supplied, so the prompt is
    byte-for-byte the previous one and behaviour is unchanged. When supplied,
    the block instructs DeepSeek to keep the object-condition description and
    the expert summary internally consistent with where inside the
    substantiated range the appraiser placed the value -- but ONLY through
    factors actually present in the photo survey, the object's own technical
    data, or its address. Inventing a defect or an upgrade that is not in the
    evidence is explicitly forbidden; if nothing in the evidence supports the
    direction, the object is described neutrally and the level is attributed
    to professional judgement, never to a fabricated reason.
    """
    if not price_position:
        return ""

    def _money(value) -> str:
        try:
            return f"{float(value):,.0f}".replace(",", " ")
        except (TypeError, ValueError):
            return "?"

    position = str(price_position.get("position") or "").lower()
    labels = {
        "lower": "нижньої частини діапазону (вартість нижча за ринкову медіану)",
        "middle": "середини діапазону (вартість близька до ринкової медіани)",
        "upper": "верхньої частини діапазону (вартість вища за ринкову медіану)",
    }
    label = labels.get(position, "обґрунтованого діапазону")

    direction_hint = ""
    if position == "lower":
        direction_hint = (
            "- Оскільки вартість у НИЖНІЙ частині діапазону — природно наголоси саме ті "
            "наявні у доказах чинники, що об'єктивно знижують вартість (наприклад "
            "зношені опоряджувальні матеріали, старі вікна чи двері, потреба в ремонті, "
            "менш вдалий поверх, застаріле планування) — але ЛИШЕ якщо вони справді "
            "присутні у фотоогляді або даних об'єкта.\n"
        )
    elif position == "upper":
        direction_hint = (
            "- Оскільки вартість у ВЕРХНІЙ частині діапазону — природно наголоси саме ті "
            "наявні у доказах чинники, що об'єктивно підвищують вартість (наприклад "
            "свіжий чи сучасний ремонт, якісні опоряджувальні матеріали, вдалий поверх, "
            "розвинена інфраструктура поряд) — але ЛИШЕ якщо вони справді присутні у "
            "фотоогляді або даних об'єкта.\n"
        )
    else:
        direction_hint = (
            "- Оскільки вартість близька до медіани — опиши стан збалансовано, "
            "відповідно до того, що зафіксовано у доказах.\n"
        )

    return (
        "\nОРІЄНТИР ПО ОБРАНІЙ ВАРТОСТІ:\n"
        "У межах обґрунтованого діапазону "
        f"{_money(price_position.get('range_min'))}–{_money(price_position.get('range_max'))} грн "
        f"(ринкова медіана — {_money(price_position.get('recommended_value'))} грн) "
        f"оцінювач професійно визначив підсумкову вартість {_money(price_position.get('selected_value'))} грн, "
        f"що відповідає {label}.\n"
        "Виклади розділ 1 (стан об'єкта) та розділ 4 (експертне резюме) так, щоб вони були "
        "внутрішньо УЗГОДЖЕНІ з цим рівнем вартості, СУВОРО дотримуючись правил:\n"
        + direction_hint +
        "- Пояснюй рівень вартості ВИКЛЮЧНО через факти, що реально зафіксовані у розділі "
        "«ДАНІ З ФОТООБСТЕЖЕННЯ», у технічних даних об'єкта або в його місцезнаходженні/адресі.\n"
        "- КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати будь-який дефект, перевагу чи факт, якого немає "
        "у доказах або технічних даних об'єкта. Не додавай нічого, що не стосується саме "
        "цього об'єкта.\n"
        "- Якщо докази не містять чинників у потрібному напрямі — опиши об'єкт нейтрально й "
        "віднеси рівень вартості до професійного судження оцінювача та ринкової позиції, "
        "не вигадуючи причин.\n"
    )


async def generate_expert_report_text(
    object_data: dict,
    analogs: list,
    evidence: dict,
    price_position: dict | None = None,
) -> dict:
    """Генерація розширеного експертного опису через DeepSeek.

    price_position (необов'язковий) описує, у якій частині обґрунтованого
    діапазону оцінювач професійно визначив підсумкову вартість (нижня /
    середина / верхня). Коли він переданий, у промпт додається орієнтир, що
    просить викласти опис стану та експертне резюме УЗГОДЖЕНО з цим рівнем
    вартості — але строго в межах фактів, зафіксованих у фотоогляді, даних
    об'єкта та його адресі, без вигадування дефектів чи переваг. Коли він
    відсутній (None), поведінка не змінюється порівняно з попередньою версією.
    """

    if not settings.deepseek_api_key:
        return _expert_fallback(object_data)

    obj_str = json.dumps(object_data, ensure_ascii=False, indent=2)

    analogs_str = ""
    for i, a in enumerate(analogs, 1):
        analogs_str += f"\n{i}. Джерело: {a.get('source', '?')}"
        analogs_str += f", Площа: {a.get('area_sqm', a.get('area', '?'))} м²"
        analogs_str += f", Кімнат: {a.get('rooms', '?')}"
        analogs_str += f", Поверх: {a.get('floor', '?')}"
        analogs_str += f", Ціна: {a.get('price_uah', a.get('price', 0)):,.0f} грн"

    evidence_str = json.dumps(evidence, ensure_ascii=False, indent=2) if evidence else "{}"

    prompt = EXPERT_PROMPT.format(
        object_data=obj_str,
        analogs_data=analogs_str if analogs_str.strip() else "Дані аналогів відсутні",
        evidence_data=evidence_str,
        price_guidance=_price_guidance_block(price_position),
    )

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": "Ти — сертифікований експерт-оцінювач нерухомості в Україні. Пишеш офіційні звіти. Відповідай ТІЛЬКИ українською. Обсяг — не менше сторінки А4."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.35,
                    "max_tokens": 4000,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        text = data["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]

        return json.loads(text)

    except Exception as e:
        print(f"DeepSeek expert report error: {e}")
        return _expert_fallback(object_data)


_OBJECT_TYPE_NAMES = {"apartment": "квартира", "house": "житловий будинок", "land": "земельна ділянка", "commercial": "комерційний об'єкт"}


def _expert_fallback(object_data: dict) -> dict:
    addr = object_data.get("address", "адреса не вказана")
    area = object_data.get("area_total", "?")
    rooms = object_data.get("rooms", "?")
    object_name = _OBJECT_TYPE_NAMES.get(str(object_data.get("object_type") or "").lower(), "об'єкт нерухомості")
    return {
        "object_and_condition": f"Об'єкт оцінки — {object_name} за адресою: {addr}. Загальна площа {area} кв. м, кімнат — {rooms}. Стан визначається за результатами натурного огляду.",
        "location": f"Об'єкт розташований за адресою: {addr}. Характеристика інфраструктури визначається за результатами огляду.",
        "comparative_reasoning": "Для визначення ринкової вартості застосовано порівняльний підхід відповідно до НС №1 та НС №2.",
        "expert_summary": "Ринкова вартість визначена на підставі порівняльного аналізу актуальних пропозицій на ринку нерухомості.",
    }
