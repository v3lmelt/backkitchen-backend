import runpy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import database
from app.config import settings


@pytest.fixture
def application_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'application.db'}")
    # Execute the application's engine/session setup without replacing modules used by other tests.
    isolated = runpy.run_path(database.__file__)
    engine = isolated["engine"]
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE account (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"))
            connection.execute(text("INSERT INTO account VALUES (1, 'original')"))
            connection.execute(text("CREATE TABLE entry (account_id INTEGER REFERENCES account(id))"))
        yield isolated
    finally:
        engine.dispose()


@pytest.mark.parametrize("reader_action", ["close", "commit"])
def test_concurrent_request_cleanup_preserves_other_transactions(application_database, reader_action):
    session_factory = application_database["SessionLocal"]
    writer_flushed = Event()
    reader_finished = Event()

    def writer():
        with session_factory() as session:
            session.execute(text("UPDATE account SET value='pending' WHERE id=1"))
            writer_flushed.set()
            assert reader_finished.wait(timeout=10), "Reader did not complete"
            if reader_action == "commit":
                session.rollback()
            else:
                session.commit()

    def reader():
        assert writer_flushed.wait(timeout=10), "Writer did not flush"
        request = application_database["get_db"]()
        try:
            session = next(request)
            observed = session.scalar(text("SELECT value FROM account WHERE id=1"))
            if reader_action == "commit":
                session.commit()
            return observed
        finally:
            request.close()
            reader_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer_result = executor.submit(writer)
        reader_result = executor.submit(reader)
        writer_result.result(timeout=15)
        observed = reader_result.result(timeout=15)

    with session_factory() as session:
        persisted = session.scalar(text("SELECT value FROM account WHERE id=1"))
    assert observed == "original"
    assert persisted == ("original" if reader_action == "commit" else "pending")


def test_foreign_keys_are_enforced_on_concurrently_checked_out_connections(application_database):
    session_factory = application_database["SessionLocal"]
    with session_factory() as first, session_factory() as second:
        # Keep both connections checked out before exercising either constraint.
        first.execute(text("SELECT id FROM account")).all()
        second.execute(text("SELECT id FROM account")).all()
        for session in (first, second):
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                session.execute(text("INSERT INTO entry VALUES (999)"))
            session.rollback()
        assert first.scalar(text("SELECT count(*) FROM entry")) == 0
