"""Конфігурація dubl.top — читається з .env.dubl у робочій теці сервісу.

Сервіс навмисно НЕ імпортує app.core.config (ocinka), щоб залишатися
повністю ізольованим: власні ключі, власні ліміти, власні теки.
Ключі Gemini/ZenRows/DIM.RIA ті самі, що в ocinka, але вписуються окремо
в .env.dubl — зміна одного сервісу ніколи не зачіпає інший.
"""
from pydantic_settings import BaseSettings


class DublSettings(BaseSettings):
    # --- AI / дані (ті самі ключі, окрема копія) ---
    gemini_api_key: str = ""
    # Google періодично змінює ідентифікатори моделей — тримаємо в .env.
    gemini_model: str = "gemini-3.5-flash"
    zenrows_api_key: str = ""
    dimria_api_key: str = ""
    dimria_enabled: bool = False
    # Google Places API (New) — карта агентств по місту (M2).
    google_places_api_key: str = ""
    # Serper.dev — веб-пошук Google (агентства + розділи site:) (M2).
    serper_api_key: str = ""

    # --- Сервіс ---
    port: int = 8020
    # Кома-роздільний список дозволених origin для CORS.
    cors_origins: str = "https://dubl.top,https://www.dubl.top"

    # --- Параметри пошуку (ТЗ) ---
    # Площа: допуск ±10%. Кількість кімнат — строго. Поверх — строго, якщо є.
    area_tolerance: float = 0.10
    # Мінімум збігів фото для підтвердження дубля (2–3).
    min_photo_matches: int = 2

    # --- Сховище ---
    # Каталог посилань агентств (SQLite) — єдине, що зберігаємо на диск.
    catalog_db: str = "/var/lib/dubl/catalog.db"
    # ПРИНЦИП: нічого не зберігаємо на диск, крім каталогу посилань агентств
    # (сайти, шляхи розділів, підгрупи) — лише для швидкості повторних пошуків.
    # Скриншоти, фото кандидатів і дані оголошень обробляються В ПАМ'ЯТІ і
    # відкидаються одразу після відповіді. Тому тек для завантажень немає.

    # Таймаут одного виклику Gemini (до 10 зображень — більше даних).
    gemini_timeout_seconds: int = 45

    model_config = {
        "env_file": ".env.dubl",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = DublSettings()
