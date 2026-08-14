"""db.py 低覆盖分支补充测试：内存库、迁移回填、连接池边界。"""

import json

import pytest

from app.db import ConnectionPool, _migrate_report_enrichment, _migrate_report_enrichment_once, get_db


async def test_get_db_memory_creates_usable_db() -> None:
    """:memory: 连接可用（注意：每个连接是独立库，仅单连接场景有效）。"""
    conn = await get_db(":memory:")
    try:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM sessions")
        row = await cursor.fetchone()
        assert row["n"] == 0
    finally:
        await conn.close()


def test_connection_pool_rejects_memory_path() -> None:
    with pytest.raises(ValueError, match="ConnectionPool does not support"):
        ConnectionPool(":memory:")


async def test_migrate_report_enrichment_backfills_legacy_report(tmp_path) -> None:
    """旧报告缺少增强字段时，迁移回填并持久化。"""
    path = tmp_path / "enrich.db"
    conn = await get_db(str(path))
    legacy_report = {
        "summary": "Bug",
        "root_cause": "Root",
        "confidence": "medium",
        "evidence": [{"path": "src/a.py", "lines": "L1", "reason": "why"}],
        "proposed_changes": ["Fix"],
        "patch": None,
        "tests": [],
        "risks": [],
        "files_examined": ["src/a.py"],
    }
    await conn.execute(
        "INSERT INTO sessions (session_id, issue_url, report_json) VALUES (?, ?, ?)",
        ("s1", "https://github.com/a/b/issues/1", json.dumps(legacy_report)),
    )
    await conn.commit()
    await conn.close()

    # 重新打开（新连接）并运行迁移
    conn = await get_db(str(path))
    try:
        await _migrate_report_enrichment(conn)
        await conn.commit()
        row = await (await conn.execute("SELECT report_json FROM sessions WHERE session_id='s1'")).fetchone()
        enriched = json.loads(row["report_json"])
        assert enriched["impact"]["severity"] == "medium"
        assert enriched["hypotheses"][0]["status"] == "accepted"
        assert enriched["evidence"][0]["strength"] == "moderate"
        assert enriched["confidence_rationale"]
    finally:
        await conn.close()


async def test_migrate_report_enrichment_is_idempotent(tmp_path) -> None:
    """已 enriched 的报告不会被重复改写。"""
    path = tmp_path / "enrich2.db"
    conn = await get_db(str(path))
    enriched = {
        "summary": "Bug",
        "root_cause": "Root",
        "confidence": "medium",
        "evidence": [],
        "proposed_changes": [],
        "patch": None,
        "tests": [],
        "risks": [],
        "impact": {"severity": "high", "likelihood": "high", "blast_radius": ["auth"]},
        "hypotheses": [{"statement": "Root", "status": "accepted", "rationale": "r"}],
        "confidence_rationale": "already set",
        "fix_rationale": "already set",
    }
    await conn.execute(
        "INSERT INTO sessions (session_id, issue_url, report_json) VALUES (?, ?, ?)",
        ("s2", "https://github.com/a/b/issues/2", json.dumps(enriched)),
    )
    await conn.commit()
    await _migrate_report_enrichment(conn)
    await conn.commit()
    row = await (await conn.execute("SELECT report_json FROM sessions WHERE session_id='s2'")).fetchone()
    assert json.loads(row["report_json"]) == enriched  # 原样保留
    await conn.close()


async def test_migrate_report_enrichment_skips_invalid_json(tmp_path) -> None:
    """损坏的 report_json 不抛异常、不改写。"""
    path = tmp_path / "enrich3.db"
    conn = await get_db(str(path))
    await conn.execute(
        "INSERT INTO sessions (session_id, issue_url, report_json) VALUES (?, ?, ?)",
        ("s3", "https://github.com/a/b/issues/3", "not-json{{{"),
    )
    await conn.commit()
    await _migrate_report_enrichment(conn)  # 不应抛异常
    row = await (await conn.execute("SELECT report_json FROM sessions WHERE session_id='s3'")).fetchone()
    assert row["report_json"] == "not-json{{{"
    await conn.close()


async def test_migrate_report_enrichment_once_runs_only_once(tmp_path, monkeypatch) -> None:
    """进程级 gate：第二次调用直接短路。"""
    path = tmp_path / "enrich4.db"
    conn = await get_db(str(path))
    calls = {"n": 0}
    original = _migrate_report_enrichment

    async def counting_migration(conn):
        calls["n"] += 1
        await original(conn)

    monkeypatch.setattr("app.db._migrate_report_enrichment", counting_migration)
    monkeypatch.setattr("app.db._enrichment_migration_done", False)
    await _migrate_report_enrichment_once(conn)
    await _migrate_report_enrichment_once(conn)
    assert calls["n"] == 1
    await conn.close()


async def test_connection_pool_release_closed_connection_decrements(tmp_path) -> None:
    """归还已关闭的连接：丢弃并减少计数，避免复用坏连接。"""
    pool = ConnectionPool(str(tmp_path / "pool.db"), size=2)
    conn = await pool.acquire()
    assert pool._created == 1
    await conn.close()  # 模拟连接中途被关闭
    await pool.release(conn)
    assert pool._created == 0
    await pool.close()


async def test_connection_pool_acquire_times_out(tmp_path, monkeypatch) -> None:
    """连接池耗尽且等待超时：抛 RuntimeError 而非永久阻塞。"""
    pool = ConnectionPool(str(tmp_path / "pool2.db"), size=1)
    first = await pool.acquire()  # 占住唯一连接

    async def fake_wait_for(coro, timeout):
        coro.close()  # 丢弃未 await 的协程，避免 RuntimeWarning
        raise TimeoutError("pool exhausted")

    monkeypatch.setattr("app.db.asyncio.wait_for", fake_wait_for)
    with pytest.raises(RuntimeError, match="pool exhausted"):
        await pool.acquire()
    await pool.release(first)
    await pool.close()
