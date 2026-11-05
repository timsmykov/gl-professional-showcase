"""Privilege restoration must select only trusted ACL SQL and never replay DDL/data."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def restore():
    filename = Path(__file__).resolve().parents[2] / "infra/timeweb/verify-restore.py"
    spec = importlib.util.spec_from_file_location("vm_restore", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_acl_restore_does_not_repeat_schema_or_order_backfill(restore):
    sql = restore.privilege_sql("restore_migrator", "restore_app")
    assert "CREATE TABLE" not in sql
    assert "CREATE INDEX" not in sql
    assert "ALTER TABLE" not in sql
    assert "INSERT INTO" not in sql
    assert 'TO "restore_app"' in sql
    assert 'FOR ROLE "restore_migrator"' in sql
    assert "ON order_intent_requests" in sql
    assert "ON video_sessions" in sql
    assert "gl_api_app" not in sql and "gl_api_migrator" not in sql


def test_old_archive_acl_does_not_reference_new_tables(restore):
    sql = restore.privilege_sql(
        "restore_migrator", "restore_app", revision="0007_reuse_pending_orders"
    )
    assert "video_sessions" not in sql and "webinar_videos" not in sql


def test_invalid_role_is_rejected_before_generating_acl_sql(restore):
    with pytest.raises(AssertionError):
        restore.privilege_sql("restore_migrator", 'invalid"; DROP TABLE users; --')
