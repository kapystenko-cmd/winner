"""
Скрипт для скидання пароля напряму в базі даних — без листа очікування,
без "forgot password", напряму на сервері.

Запуск з сервера:
    cd /opt/ocinka
    venv/bin/python scripts/reset_password.py your@email.com "НовийПароль123"
"""
import asyncio
import sys
sys.path.insert(0, ".")


async def main():
    if len(sys.argv) != 3:
        print("Використання: venv/bin/python scripts/reset_password.py <email> <новий_пароль>")
        return

    email = sys.argv[1].strip().lower()
    new_password = sys.argv[2]

    from sqlalchemy import select
    from app.core.database import async_session
    from app.core.auth import hash_password
    from app.models.models import User

    async with async_session() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if not user:
            print(f"Користувача з email={email} не знайдено.")
            return
        user.password_hash = hash_password(new_password)
        await db.commit()
        print(f"Пароль оновлено для {email} (id={user.id}).")


if __name__ == "__main__":
    asyncio.run(main())
