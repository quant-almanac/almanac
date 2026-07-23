from __future__ import annotations

from almanac.runtime_config import resolve_db_path


def test_resolve_db_path_prefers_existing_almanac_db(tmp_path, monkeypatch):
    monkeypatch.delenv("ALMANAC_DB_PATH", raising=False)
    (tmp_path / "nexustrader.db").write_text("legacy", encoding="utf-8")
    (tmp_path / "almanac.db").write_text("new", encoding="utf-8")

    assert resolve_db_path(tmp_path) == tmp_path / "almanac.db"


def test_resolve_db_path_falls_back_to_existing_legacy_db(tmp_path, monkeypatch):
    monkeypatch.delenv("ALMANAC_DB_PATH", raising=False)
    (tmp_path / "nexustrader.db").write_text("legacy", encoding="utf-8")

    assert resolve_db_path(tmp_path) == tmp_path / "nexustrader.db"
