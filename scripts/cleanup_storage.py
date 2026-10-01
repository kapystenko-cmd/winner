"""Retention cleanup for Ocinka.pro.

Default mode only shows what would be removed.  Run with --apply from the
server timer after verifying output.  Screenshots are removed after 24 hours;
report source documents, object photos and generated files stay until a report
reaches its configured expiry date (180 days by default).
"""
import argparse
import asyncio
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

# When called as `python scripts/cleanup_storage.py`, Python otherwise puts
# only /opt/ocinka/scripts on sys.path and cannot import the application.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete, select

from app.core.config import settings
from app.core.database import async_session
from app.models.models import Report, ValueChangeLog


def old_files(root: Path, cutoff: datetime):
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime) < cutoff]


async def main(apply: bool):
    screenshot_cutoff = datetime.now() - timedelta(hours=settings.screenshot_retention_hours)
    screenshots = old_files(Path(settings.screenshots_dir), screenshot_cutoff)
    print(f"Screenshots older than {settings.screenshot_retention_hours} hours: {len(screenshots)}")

    # Input scans/object photos are working material for OCR and report
    # drafting, not the deliverable itself. Per product policy they are kept
    # only 24 hours, independently of how long the finished report record
    # stays around (180 days). Previously these two directories were only
    # cleared when the whole report expired, so raw uploads accumulated on
    # disk for months instead of one day.
    upload_cutoff = datetime.now() - timedelta(hours=settings.screenshot_retention_hours)
    stale_uploads = old_files(Path(settings.upload_dir), upload_cutoff)
    stale_object_photos = old_files(Path(settings.object_photos_dir), upload_cutoff)
    print(f"Uploaded source scans older than {settings.screenshot_retention_hours} hours: {len(stale_uploads)}")
    print(f"Object photos older than {settings.screenshot_retention_hours} hours: {len(stale_object_photos)}")

    async with async_session() as db:
        result = await db.execute(select(Report).where(Report.expires_at.is_not(None), Report.expires_at <= datetime.utcnow()))
        expired = result.scalars().all()
        print(f"Reports expired after {settings.report_retention_days} days: {len(expired)}")
        if not apply:
            print("Dry run only. Re-run with --apply to delete.")
            return

        for path in screenshots:
            path.unlink(missing_ok=True)
        for path in stale_uploads:
            path.unlink(missing_ok=True)
        for path in stale_object_photos:
            path.unlink(missing_ok=True)
        for report in expired:
            await db.execute(delete(ValueChangeLog).where(ValueChangeLog.report_id == report.id))
            await db.delete(report)
            for base in (settings.upload_dir, settings.object_photos_dir, settings.generated_dir, settings.screenshots_dir):
                target = Path(base) / str(report.user_id) / str(report.id)
                # Screenshots use report ID directly, without a user directory.
                if base == settings.screenshots_dir:
                    target = Path(base) / str(report.id)
                shutil.rmtree(target, ignore_errors=True)
        await db.commit()
    print("Retention cleanup completed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="delete files and expired reports")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
