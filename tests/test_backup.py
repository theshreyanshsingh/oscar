"""A copy of the database each day (app/backup.py)."""

import sqlite3
import stat
from datetime import date, timedelta

from app.backup import back_up, backups
from app.store import Store


def test_a_backup_is_a_whole_copy_made_once_a_day(tmp_path):
    db = tmp_path / "oscar.db"
    store = Store(db)
    store.create_call("browser", status="ended")
    made = back_up(db, today=date(2026, 11, 22))
    assert made.name == "oscar-2026-11-22.db" and made.parent.name == "backups"
    assert stat.S_IMODE(made.stat().st_mode) == 0o600
    with sqlite3.connect(made) as copy:
        assert copy.execute("SELECT COUNT(*) FROM calls").fetchone() == (1,)
    # Once a day: the same day's backup is not made again.
    assert back_up(db, today=date(2026, 11, 22)) is None
    store.close()


def test_only_the_latest_are_kept(tmp_path):
    db = tmp_path / "oscar.db"
    Store(db).close()
    first = date(2026, 11, 1)
    for day in range(5):
        back_up(db, today=first + timedelta(days=day), keep=3)
    assert [path.name for path in backups(db)] == ["oscar-2026-11-03.db", "oscar-2026-11-04.db", "oscar-2026-11-05.db"]
    # Anything else in the folder is left alone.
    (tmp_path / "backups" / "notes.txt").write_text("mine")
    back_up(db, today=first + timedelta(days=6), keep=3)
    assert (tmp_path / "backups" / "notes.txt").exists()
