#!/usr/bin/env python3
"""Migrate local audio files to Cloudflare R2.

Usage:
    # Dry run (default) — shows what would be migrated
    python scripts/migrate_to_r2.py

    # Actually migrate
    python scripts/migrate_to_r2.py --execute

    # Limit batch size
    python scripts/migrate_to_r2.py --execute --batch 50
"""

import argparse
import logging
import sys
from pathlib import Path

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.comment_audio import CommentAudio
from app.models.master_delivery import MasterDelivery
from app.models.track import Track
from app.models.track_source_version import TrackSourceVersion
from app.services.r2 import get_r2_client, object_exists

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("migrate_to_r2")


def upload_file(local_path: Path, object_key: str, dry_run: bool) -> bool:
    """Upload a single file to R2. Returns True on success."""
    if not local_path.exists():
        log.warning("  SKIP (file missing): %s", local_path)
        return False
    if dry_run:
        log.info("  DRY-RUN would upload: %s -> %s (%.1f MB)", local_path, object_key, local_path.stat().st_size / 1048576)
        return True

    client = get_r2_client()
    try:
        client.upload_file(str(local_path), settings.R2_BUCKET_NAME, object_key)
        if not object_exists(object_key):
            log.error("  FAIL (verify failed): %s", object_key)
            return False
        log.info("  OK: %s -> %s", local_path, object_key)
        return True
    except Exception:
        log.error("  FAIL: %s", object_key, exc_info=True)
        return False


def derive_object_key(model_name: str, record, local_path: Path) -> str:
    """Derive an R2 object key from a record and its local file path."""
    ext = local_path.suffix.lower()
    stem = local_path.stem

    if model_name == "Track":
        return f"tracks/{record.id}/source/{stem}{ext}"
    elif model_name == "TrackSourceVersion":
        return f"tracks/{record.track_id}/source/{stem}{ext}"
    elif model_name == "MasterDelivery":
        return f"tracks/{record.track_id}/master/{stem}{ext}"
    elif model_name == "CommentAudio":
        return f"comments/{record.comment_id}/{stem}{ext}"
    return f"misc/{stem}{ext}"


def migrate_table(db: Session, model, model_name: str, dry_run: bool, batch: int) -> tuple[int, int, int]:
    """Migrate all local records in a table. Returns (total, migrated, skipped)."""
    records = list(db.scalars(
        select(model).where(model.storage_backend == "local").limit(batch)
    ).all())

    total = len(records)
    migrated = 0
    skipped = 0

    for record in records:
        local_path = Path(record.file_path)
        if not local_path.is_absolute():
            # CommentAudio stores relative paths like "comment_audios/uuid.mp3"
            local_path = settings.get_upload_path() / local_path

        object_key = derive_object_key(model_name, record, local_path)

        if upload_file(local_path, object_key, dry_run):
            if not dry_run:
                record.file_path = object_key
                record.storage_backend = "r2"
                db.commit()
            migrated += 1
        else:
            skipped += 1

    return total, migrated, skipped


def main():
    parser = argparse.ArgumentParser(description="Migrate local audio files to R2")
    parser.add_argument("--execute", action="store_true", help="Actually perform the migration (default is dry-run)")
    parser.add_argument("--batch", type=int, default=1000, help="Maximum number of records to process per table")
    args = parser.parse_args()

    dry_run = not args.execute

    if not settings.R2_ENABLED:
        log.error("R2 is not enabled. Set AUDIO_MGMT_R2_ENABLED=true in .env")
        sys.exit(1)

    if dry_run:
        log.info("=== DRY RUN MODE (use --execute to actually migrate) ===")

    db = SessionLocal()
    try:
        tables = [
            (Track, "Track"),
            (TrackSourceVersion, "TrackSourceVersion"),
            (MasterDelivery, "MasterDelivery"),
            (CommentAudio, "CommentAudio"),
        ]

        grand_total = 0
        grand_migrated = 0
        grand_skipped = 0

        for model, name in tables:
            log.info("\n--- %s ---", name)
            total, migrated, skipped = migrate_table(db, model, name, dry_run, args.batch)
            log.info("  %s: %d total, %d migrated, %d skipped", name, total, migrated, skipped)
            grand_total += total
            grand_migrated += migrated
            grand_skipped += skipped

        log.info("\n=== SUMMARY ===")
        log.info("Total: %d, Migrated: %d, Skipped: %d", grand_total, grand_migrated, grand_skipped)
        if dry_run:
            log.info("This was a dry run. Use --execute to actually migrate.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
