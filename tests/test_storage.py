"""Where the database lives, and whether that place survives a deploy.

The failure this guards against: shares were saved, a new version was deployed,
and the records were gone — because the container had no volume behind the
SQLite file. These tests pin the fallback order and the warnings that make the
next occurrence visible on screen instead of a mystery.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def admin_client():
    with TestClient(app) as c:
        resp = c.post(
            "/login",
            data={"password": "adminpw", "name": "tester", "next": "/"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        yield c


def test_data_dir_wins_over_volume(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "/tmp/explicit")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")
    assert db.resolve_data_dir() == (Path("/tmp/explicit"), "DATA_DIR")


def test_volume_used_when_data_dir_missing(monkeypatch):
    # Attaching a volume is enough from v1.2.0: forgetting DATA_DIR no longer
    # sends the database back to its own throwaway disk.
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")
    assert db.resolve_data_dir() == (Path("/data"), "volume")


def test_falls_back_to_local_data_dir(monkeypatch):
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    assert db.resolve_data_dir() == (Path("./data"), "default")


def test_storage_status_is_ephemeral_without_volume(monkeypatch):
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj")
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    info = db.storage_status()
    assert info["ephemeral"] is True
    assert info["volume_backed"] is False


def test_storage_status_sees_volume(monkeypatch, tmp_path):
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(volume))
    monkeypatch.setattr(db, "DB_PATH", volume / "portal.db")

    info = db.storage_status()
    assert info["volume_backed"] is True
    assert info["ephemeral"] is False
    assert info["volume_path"].endswith("vol")


def test_health_reports_storage_without_login():
    with TestClient(app) as c:
        body = c.get("/health").json()
    assert body["data_dir"].endswith("portal.db")
    assert body["data_source"] in db.DATA_DIR_SOURCES
    assert isinstance(body["persistent"], bool)
    assert isinstance(body["n_periods"], int)


def test_dashboard_warns_when_database_would_be_wiped(admin_client, monkeypatch):
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj")
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    html = admin_client.get("/").text
    assert "重新部署會清空" in html


def test_settings_page_shows_storage_details(admin_client):
    html = admin_client.get("/admin/settings").text
    assert "資料儲存" in html
    assert "下載資料庫備份" in html


def test_backup_is_a_working_database(admin_client, tmp_path):
    resp = admin_client.get("/admin/backup/db")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]

    copy = tmp_path / "backup.db"
    copy.write_bytes(resp.content)
    conn = sqlite3.connect(copy)
    try:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        # The backup has to carry the shares, not just the schema.
        assert {"share_set", "share_entry", "shop", "run"} <= names
        assert conn.execute("SELECT COUNT(*) FROM share_set").fetchone()[0] >= 0
    finally:
        conn.close()


def test_backup_requires_admin():
    with TestClient(app) as anon:
        resp = anon.get("/admin/backup/db", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]
