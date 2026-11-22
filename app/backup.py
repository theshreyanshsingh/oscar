"""A copy of Oscar's database each day, in data/backups/, the last 14 kept.

The copy is made with SQLite's own backup, which is consistent even while calls
are writing. It is made under a temporary name and renamed once whole, so a
copy cut off half-way (the server stopped) is never mistaken for a backup.

To go back to one: stop Oscar (and its WhatsApp copy), delete oscar.db-wal and
oscar.db-shm, copy data/backups/oscar-YYYY-MM-DD.db over data/oscar.db, and
start it again (README.md, Backups).
"""

import os
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

BACKUPS_KEPT = 14
_BACKUP_NAME = re.compile(r"oscar-\d{4}-\d{2}-\d{2}\.db")


def backup_folder(db_path: Path) -> Path:
    return db_path.parent / "backups"


def backups(db_path: Path) -> list[Path]:
    """The backups there are, oldest first."""
    folder = backup_folder(db_path)
    return sorted(path for path in folder.glob("oscar-*.db") if _BACKUP_NAME.fullmatch(path.name))


def back_up(db_path: Path, *, today: date | None = None, keep: int = BACKUPS_KEPT) -> Path | None:
    """Today's backup, made now: None when there already is one. Older ones past `keep` go."""
    folder = backup_folder(db_path)
    folder.mkdir(mode=0o700, exist_ok=True)
    day = (today or datetime.now(timezone.utc).date()).isoformat()
    target = folder / f"oscar-{day}.db"
    if target.exists():
        return None
    partial = folder / f".oscar-{day}.db.partial"
    partial.unlink(missing_ok=True)
    source = sqlite3.connect(db_path)
    copy = sqlite3.connect(partial)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    os.chmod(partial, 0o600)  # transcripts and numbers: owner-only, as the database itself
    partial.rename(target)
    for old in backups(db_path)[:-keep]:
        old.unlink()
    return target
