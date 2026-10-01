"""Store text-orientation metadata for already uploaded evaluator documents.

Run once after deploying the orientation update.  It does not alter source
scans or report files; it only adds ``page_orientations`` to a user's private
profile-document metadata, so future PDF annexes can be displayed upright.
"""
import asyncio
from pathlib import Path

from sqlalchemy import select

from app.core.database import async_session
from app.models.models import User
from app.services.ocr_service import process_document


async def main() -> None:
    updated = 0
    async with async_session() as session:
        users = list((await session.scalars(select(User))).all())
        for user in users:
            documents = list(user.profile_document_files or [])
            changed = False
            for item in documents:
                if not isinstance(item, dict) or item.get("kind") == "sod_logo":
                    continue
                path = Path(str(item.get("path") or ""))
                if not path.is_file():
                    continue
                result = await process_document(str(path))
                orientations = list(result.get("page_orientations") or [])
                item["page_orientations"] = orientations
                changed = True
                updated += 1
                print(f"Orientation: {user.email} | {path.name} | pages={len(orientations)}", flush=True)
            if changed:
                user.profile_document_files = documents
        await session.commit()
    print(f"Done. Profile documents checked: {updated}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
