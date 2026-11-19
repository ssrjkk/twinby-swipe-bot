import asyncio
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import make_url

import main as main_module
from config import settings
from core.backup_service import DatabaseBackupService
from core.base_client import CandidateProfile
from core.ui_driver import MobileUIDriverFallback
from core.worker import SwipeWorker
from database.models import SwipeDecision, SwipeRecord
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine

REAL_SLEEP = asyncio.sleep


def profile(user_id="candidate", **overrides):
    values = dict(
        platform="twinby",
        user_id=user_id,
        name="Test",
        age=25,
        compatibility_score=90,
        photo_urls=["https://invalid.example/photo.jpg"],
    )
    values.update(overrides)
    return CandidateProfile(**values)


async def records(db):
    async with db.session_factory() as session:
        return list(
            (await session.scalars(select(SwipeRecord).order_by(SwipeRecord.id))).all()
        )


@pytest_asyncio.fixture
async def runtime_db(tmp_path):
    path = tmp_path / "custom store # %" / "configured.db"
    db = DatabaseManager(f"sqlite+aiosqlite:///{path.as_posix()}")
    await db.init_db()
    try:
        yield db
    finally:
        await db.engine.dispose()


@pytest.fixture
def worker_factory(monkeypatch, runtime_db):
    def create(db=None):
        client = SimpleNamespace(
            swipe=AsyncMock(return_value={"status": "ok", "is_match": False}),
            get_recommendations=AsyncMock(return_value=[]),
        )
        worker = SwipeWorker(
            client, db or runtime_db, ProfileFilterEngine(), on_match=AsyncMock()
        )
        monkeypatch.setattr(worker.humanizer, "sleep_between_swipes", AsyncMock())
        monkeypatch.setattr(
            worker.synthetic_scorer,
            "compute_compatibility",
            AsyncMock(side_effect=AssertionError("Unexpected AI/network scoring")),
        )
        return worker

    return create


@pytest.mark.asyncio
@pytest.mark.parametrize("uri", [False, True])
async def test_init_creates_fresh_nested_sqlite_parent(tmp_path, monkeypatch, uri):
    monkeypatch.chdir(tmp_path)
    expected = tmp_path / "fresh" / "nested" / "configured.db"
    database = (
        "file:fresh/nested/configured.db?uri=true"
        if uri
        else "./fresh/nested/configured.db"
    )
    db = DatabaseManager(f"sqlite+aiosqlite:///{database}")
    try:
        assert db.sqlite_path == expected
        assert not expected.parent.exists()
        await db.init_db()
        assert expected.is_file()
        assert await records(db) == []
        assert not (tmp_path / "data").exists()
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite://",
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///file:?uri=true",
        "sqlite+aiosqlite:///file::memory:?uri=true",
        "sqlite+aiosqlite:///file:runtime?mode=memory&cache=shared&uri=true",
    ],
)
async def test_memory_database_has_no_backup_path_or_directories(
    tmp_path, monkeypatch, url
):
    monkeypatch.chdir(tmp_path)
    db = DatabaseManager(url)
    try:
        assert db.sqlite_path is None
        await db.init_db()
        assert await records(db) == []
        assert list(tmp_path.iterdir()) == []
    finally:
        await db.engine.dispose()


def test_non_sqlite_database_has_no_sqlite_path(monkeypatch):
    import database.repository as repository_module

    engine = SimpleNamespace(url=make_url("postgresql+asyncpg://localhost/runtime"))
    monkeypatch.setattr(
        repository_module, "create_async_engine", Mock(return_value=engine)
    )
    db = DatabaseManager("postgresql+asyncpg://localhost/runtime")
    assert db.sqlite_path is None


@pytest.mark.asyncio
async def test_backup_copies_configured_database(runtime_db):
    await runtime_db.save_swipe(
        profile("configured-source"), SwipeDecision.LIKE, "saved", 77.0
    )
    service = DatabaseBackupService(bot=Mock(), db_path=runtime_db.sqlite_path)
    backup = service._create_safe_backup_sync()
    assert backup.parent == runtime_db.sqlite_path.parent / "backups"
    assert backup != runtime_db.sqlite_path
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute(
            "SELECT target_user_id, reason FROM swipe_history"
        ).fetchall() == [("configured-source", "saved")]


@pytest.mark.parametrize("nested", [False, True])
def test_missing_backup_source_never_creates_database(tmp_path, nested):
    parent = tmp_path / "missing" if nested else tmp_path
    source = parent / "missing.db"
    service = DatabaseBackupService(bot=Mock(), db_path=source)
    with pytest.raises(FileNotFoundError, match="Backup source does not exist"):
        service._create_safe_backup_sync()
    assert not source.exists()
    assert not (parent / "backups").exists()
    assert list(tmp_path.iterdir()) == []


def test_backup_opens_source_read_only_even_if_it_disappears(tmp_path, monkeypatch):
    source = tmp_path / "source # %.db"
    source.touch()
    connect = sqlite3.connect
    calls = []

    def remove_then_connect(database, **kwargs):
        calls.append((database, kwargs))
        source.unlink()
        return connect(database, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", remove_then_connect)
    service = DatabaseBackupService(bot=Mock(), db_path=source)
    with pytest.raises(sqlite3.OperationalError):
        service._create_safe_backup_sync()
    assert calls == [(f"{source.as_uri()}?mode=ro", {"uri": True})]
    assert not source.exists()
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("fail_open", [False, True])
def test_backup_closes_connections_on_failure(tmp_path, monkeypatch, fail_open):
    source = tmp_path / "source.db"
    source.touch()
    src = Mock()
    dst = sqlite3.connect(":memory:")
    src.backup.side_effect = RuntimeError("copy failed")
    connect = Mock(side_effect=[src, RuntimeError("open failed") if fail_open else dst])
    monkeypatch.setattr(sqlite3, "connect", connect)
    service = DatabaseBackupService(bot=Mock(), db_path=source)
    try:
        with pytest.raises(
            RuntimeError, match="open failed" if fail_open else "copy failed"
        ):
            service._create_safe_backup_sync()
        src.close.assert_called_once_with()
        if not fail_open:
            src.backup.assert_called_once_with(dst)
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                dst.execute("SELECT 1")
    finally:
        dst.close()


@pytest.mark.asyncio
async def test_backup_stop_awaits_cancellation(tmp_path, monkeypatch):
    service = DatabaseBackupService(bot=Mock(), db_path=tmp_path / "unused.db")
    started = asyncio.Event()
    finished = asyncio.Event()

    async def loop():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await REAL_SLEEP(0)
            finished.set()

    monkeypatch.setattr(service, "_loop", loop)
    service.start()
    task = service._task
    await started.wait()
    await service.stop()
    assert finished.is_set()
    assert task.done()
    assert service._task is None
    assert service._is_running is False
    await service.stop()


@pytest.mark.asyncio
async def test_backup_stop_waits_for_inflight_thread(tmp_path, monkeypatch):
    service = DatabaseBackupService(
        bot=SimpleNamespace(send_document=AsyncMock()), db_path=tmp_path / "unused.db"
    )
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def backup():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "Test did not release backup thread"
        finished.set()
        return tmp_path / "unused-backup.db"

    async def skip_initial_sleep(seconds):
        assert seconds == 60

    monkeypatch.setattr(service, "_create_safe_backup_sync", backup)
    monkeypatch.setattr("core.backup_service.asyncio.sleep", skip_initial_sleep)
    service.start()
    stop_task = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        stop_task = asyncio.create_task(service.stop())
        await REAL_SLEEP(0)
        await REAL_SLEEP(0)
        assert not stop_task.done()
        assert not finished.is_set()
    finally:
        release.set()
        if stop_task is not None:
            await asyncio.wait_for(stop_task, 2)
        else:
            await service.stop()
    assert finished.is_set()
    assert service._task is None
    service.bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_candidate_commits_intent_updates_same_row_and_dedups(
    runtime_db, worker_factory, monkeypatch
):
    worker = worker_factory()
    candidate = profile()
    verdict = worker.filter_engine.evaluate(candidate)
    pending = []

    async def swipe(user_id, is_like):
        assert worker.db.swipe_lock.locked()
        assert (user_id, is_like) == (candidate.user_id, True)
        rows = await records(runtime_db)
        assert len(rows) == 1
        row = rows[0]
        assert row.target_user_id == candidate.user_id
        assert row.decision == "like"
        assert row.is_match is False
        assert row.reason.startswith("Pending/uncertain external swipe; do not replay.")
        assert await runtime_db.get_today_likes_count() == 1
        pending.append((row.id, row.created_at))
        return {"status": "ok", "is_match": True}

    worker.client.swipe.side_effect = swipe
    dedup = AsyncMock(wraps=runtime_db.is_already_swiped)
    save = AsyncMock(wraps=runtime_db.save_swipe)
    update = AsyncMock(wraps=runtime_db.update_swipe_result)
    monkeypatch.setattr(runtime_db, "is_already_swiped", dedup)
    monkeypatch.setattr(runtime_db, "save_swipe", save)
    monkeypatch.setattr(runtime_db, "update_swipe_result", update)
    assert await worker._process_candidate(candidate) is True
    assert await worker._process_candidate(candidate) is True
    assert dedup.await_count == 2
    save.assert_awaited_once()
    update.assert_awaited_once()
    worker.client.swipe.assert_awaited_once_with(candidate.user_id, is_like=True)
    worker.humanizer.sleep_between_swipes.assert_awaited_once()
    worker.on_match.assert_awaited_once_with(candidate, verdict.score, verdict.reason)
    rows = await records(runtime_db)
    assert len(rows) == 1
    assert (rows[0].id, rows[0].created_at) == pending[0]
    assert rows[0].reason == verdict.reason
    assert rows[0].match_score == verdict.score
    assert rows[0].is_match is True
    assert await runtime_db.get_stats() == {
        "total_processed": 1,
        "total_likes": 1,
        "total_passes": 0,
        "total_matches": 1,
        "today_likes": 1,
        "match_rate": 100.0,
    }


@pytest.mark.asyncio
async def test_batch_stops_with_only_one_like_remaining(
    runtime_db, worker_factory, monkeypatch
):
    await runtime_db.save_swipe(profile("earlier"), SwipeDecision.LIKE, "earlier", 77.0)
    monkeypatch.setattr(settings, "daily_like_limit", 2)
    worker = worker_factory()
    candidates = [profile(str(i)) for i in range(15)]
    worker.client.get_recommendations.return_value = candidates
    process = AsyncMock(wraps=worker._process_candidate)
    monkeypatch.setattr(worker, "_process_candidate", process)
    sleeps = []

    async def stop_at_quota(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr("core.worker.asyncio.sleep", stop_at_quota)
    worker._is_running = True
    await asyncio.wait_for(worker._run_loop(), 2)
    worker._is_running = False
    assert sleeps == [1800]
    worker.client.get_recommendations.assert_awaited_once_with(limit=15)
    assert [call.args[0].user_id for call in process.await_args_list] == ["0", "1"]
    worker.client.swipe.assert_awaited_once_with("0", is_like=True)
    assert await runtime_db.get_today_likes_count() == 2
    assert [row.target_user_id for row in await records(runtime_db)] == ["earlier", "0"]


@pytest.mark.asyncio
async def test_workers_share_quota_lock(runtime_db, worker_factory, monkeypatch):
    await runtime_db.save_swipe(profile("earlier"), SwipeDecision.LIKE, "earlier", 77.0)
    monkeypatch.setattr(settings, "daily_like_limit", 2)
    first, second = worker_factory(), worker_factory()
    results = await asyncio.gather(
        first._process_candidate(profile("first")),
        second._process_candidate(profile("second")),
    )
    assert results == [True, False]
    first.client.swipe.assert_awaited_once_with("first", is_like=True)
    second.client.swipe.assert_not_awaited()
    assert await runtime_db.get_today_likes_count() == 2
    assert [row.target_user_id for row in await records(runtime_db)] == [
        "earlier",
        "first",
    ]


@pytest.mark.asyncio
async def test_workers_dedup_same_profile_concurrently(runtime_db, worker_factory):
    first, second = worker_factory(), worker_factory()
    assert await asyncio.gather(
        first._process_candidate(profile()), second._process_candidate(profile())
    ) == [True, True]
    first.client.swipe.assert_awaited_once()
    second.client.swipe.assert_not_awaited()
    second.humanizer.sleep_between_swipes.assert_not_awaited()
    assert len(await records(runtime_db)) == 1


@pytest.mark.asyncio
async def test_quota_rechecked_after_humanizer(runtime_db, worker_factory, monkeypatch):
    await runtime_db.save_swipe(profile("earlier"), SwipeDecision.LIKE, "earlier", 77.0)
    monkeypatch.setattr(settings, "daily_like_limit", 2)
    worker = worker_factory()

    async def lower_quota(**kwargs):
        monkeypatch.setattr(settings, "daily_like_limit", 1)

    worker.humanizer.sleep_between_swipes.side_effect = lower_quota
    assert await worker._process_candidate(profile()) is False
    worker.client.swipe.assert_not_awaited()
    assert len(await records(runtime_db)) == 1


@pytest.mark.asyncio
async def test_pass_does_not_consume_like_quota(
    runtime_db, worker_factory, monkeypatch
):
    await runtime_db.save_swipe(profile("earlier"), SwipeDecision.LIKE, "earlier", 77.0)
    monkeypatch.setattr(settings, "daily_like_limit", 1)
    worker = worker_factory()
    assert await worker._process_candidate(profile(compatibility_score=50)) is True
    worker.client.swipe.assert_awaited_once_with("candidate", is_like=False)
    rows = await records(runtime_db)
    assert len(rows) == 2
    assert rows[1].decision == "pass"
    assert not rows[1].reason.startswith("Pending/uncertain")
    assert await runtime_db.get_today_likes_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_failed_external_swipe_retains_intent_and_cannot_replay(
    runtime_db, worker_factory, failure
):
    worker = worker_factory()
    worker.client.swipe.side_effect = failure("Unknown external outcome")
    with pytest.raises(failure):
        await worker._process_candidate(profile())
    before = await records(runtime_db)
    assert len(before) == 1
    assert before[0].reason.startswith(
        "Pending/uncertain external swipe; do not replay."
    )
    assert before[0].decision == "like"
    assert before[0].is_match is False
    assert await runtime_db.get_today_likes_count() == 1
    assert await worker._process_candidate(profile()) is True
    worker.client.swipe.assert_awaited_once()
    worker.on_match.assert_not_awaited()

    reopened = DatabaseManager(str(runtime_db.engine.url))
    try:
        replacement = worker_factory(db=reopened)
        assert await replacement._process_candidate(profile()) is True
        replacement.client.swipe.assert_not_awaited()
        after = await records(reopened)
        assert len(after) == 1
        assert (after[0].id, after[0].reason) == (before[0].id, before[0].reason)
        assert await reopened.get_today_likes_count() == 1
    finally:
        await reopened.engine.dispose()


@pytest.mark.asyncio
async def test_failed_intent_commit_prevents_external_action(
    runtime_db, worker_factory, monkeypatch
):
    worker = worker_factory()
    monkeypatch.setattr(
        runtime_db, "save_swipe", AsyncMock(side_effect=RuntimeError("commit failed"))
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        await worker._process_candidate(profile())
    worker.client.swipe.assert_not_awaited()
    assert await records(runtime_db) == []


@pytest.mark.asyncio
async def test_failed_acknowledgement_never_replays_swipe(
    runtime_db, worker_factory, monkeypatch
):
    worker = worker_factory()
    worker.client.swipe.return_value = {"status": "ok", "is_match": True}
    monkeypatch.setattr(
        runtime_db,
        "update_swipe_result",
        AsyncMock(side_effect=RuntimeError("ack failed")),
    )
    with pytest.raises(RuntimeError, match="ack failed"):
        await worker._process_candidate(profile())
    assert await worker._process_candidate(profile()) is True
    worker.client.swipe.assert_awaited_once()
    worker.on_match.assert_not_awaited()
    rows = await records(runtime_db)
    assert len(rows) == 1
    assert rows[0].reason.startswith("Pending/uncertain")
    assert rows[0].is_match is False
    assert await runtime_db.get_today_likes_count() == 1


@pytest.mark.asyncio
async def test_result_update_cannot_insert_missing_intent(runtime_db):
    with pytest.raises(LookupError, match="does not exist"):
        await runtime_db.update_swipe_result(999, "ack", 77.0, True)
    assert await records(runtime_db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_driver", [False, True])
async def test_ui_only_swipe_fails_closed_without_connected_driver(
    worker_factory, with_driver
):
    worker = worker_factory()
    if with_driver:
        worker.ui_driver = MobileUIDriverFallback("test-device", "co.hinge.app")
    with pytest.raises(RuntimeError, match="connected"):
        await worker._execute_swipe(profile(platform="hinge"), is_like=True)
    worker.client.swipe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("file_database", [False, True])
@pytest.mark.parametrize("polling_fails", [False, True])
async def test_main_uses_configured_backup_and_awaits_cleanup(
    tmp_path, monkeypatch, file_database, polling_fails
):
    events = []
    started = asyncio.Event()

    def cleanup_callback(name):
        async def cleanup():
            await REAL_SLEEP(0)
            events.append(name)

        return AsyncMock(side_effect=cleanup)

    path = tmp_path / "configured" / "account.db" if file_database else None
    url = (
        f"sqlite+aiosqlite:///{path.as_posix()}"
        if file_database
        else "sqlite+aiosqlite:///:memory:"
    )
    monkeypatch.setattr(settings, "database_url", url)
    db = SimpleNamespace(
        sqlite_path=path,
        init_db=AsyncMock(),
        engine=SimpleNamespace(dispose=cleanup_callback("db disposed")),
    )
    db_factory = Mock(return_value=db)
    monkeypatch.setattr(main_module, "DatabaseManager", db_factory)
    bot = SimpleNamespace(
        delete_webhook=AsyncMock(),
        session=SimpleNamespace(close=cleanup_callback("bot closed")),
    )
    monkeypatch.setattr(main_module, "Bot", Mock(return_value=bot))
    backup = SimpleNamespace(start=Mock(), stop=cleanup_callback("backup stopped"))
    backup_factory = Mock(return_value=backup)
    monkeypatch.setattr(main_module, "DatabaseBackupService", backup_factory)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            events.append("client closed")

    monkeypatch.setattr(main_module, "TwinbyStealthClient", Client)
    worker = SimpleNamespace(stop=cleanup_callback("worker stopped"))
    monkeypatch.setattr(main_module, "SwipeWorker", Mock(return_value=worker))
    app = object()
    monkeypatch.setattr(main_module, "create_web_app", Mock(return_value=app))
    config_factory = Mock()
    monkeypatch.setattr(main_module.uvicorn, "Config", config_factory)

    async def serve():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await REAL_SLEEP(0)
            events.append("web stopped")

    monkeypatch.setattr(
        main_module.uvicorn, "Server", Mock(return_value=SimpleNamespace(serve=serve))
    )

    async def poll(*args, **kwargs):
        await started.wait()
        if polling_fails:
            raise RuntimeError("polling failed")

    dispatcher = SimpleNamespace(start_polling=AsyncMock(side_effect=poll))
    monkeypatch.setattr(main_module, "create_dispatcher", Mock(return_value=dispatcher))
    from contextlib import nullcontext

    outcome = (
        pytest.raises(RuntimeError, match="polling failed")
        if polling_fails
        else nullcontext()
    )
    with outcome:
        await asyncio.wait_for(main_module.main(), 2)
    db_factory.assert_called_once_with(url)
    db.init_db.assert_awaited_once()
    config_factory.assert_called_once_with(
        app=app, host="127.0.0.1", port=8080, log_level="warning"
    )
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    dispatcher.start_polling.assert_awaited_once_with(bot, close_bot_session=False)
    if file_database:
        backup_factory.assert_called_once_with(bot=bot, db_path=path, interval_hours=24)
        backup.start.assert_called_once_with()
        backup.stop.assert_awaited_once()
    else:
        backup_factory.assert_not_called()
        backup.start.assert_not_called()
        backup.stop.assert_not_awaited()
    assert events == [
        "web stopped",
        "worker stopped",
        "client closed",
        *(["backup stopped"] if file_database else []),
        "bot closed",
        "db disposed",
    ]
    worker.stop.assert_awaited_once()
    bot.session.close.assert_awaited_once()
    db.engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_disposes_engine_if_initialization_fails(monkeypatch):
    db = SimpleNamespace(
        init_db=AsyncMock(side_effect=RuntimeError("init failed")),
        engine=SimpleNamespace(dispose=AsyncMock()),
    )
    monkeypatch.setattr(main_module, "DatabaseManager", Mock(return_value=db))
    bot_factory = Mock()
    monkeypatch.setattr(main_module, "Bot", bot_factory)
    with pytest.raises(RuntimeError, match="init failed"):
        await main_module.main()
    db.engine.dispose.assert_awaited_once()
    bot_factory.assert_not_called()
