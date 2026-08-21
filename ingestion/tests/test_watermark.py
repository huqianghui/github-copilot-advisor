from datetime import datetime, timezone

from ingestion.watermark import WatermarkStore


def test_get_unknown_source_returns_none(tmp_path):
    store = WatermarkStore(tmp_path / "wm.json")
    assert store.get("nope") is None


def test_set_then_get_roundtrip(tmp_path):
    store = WatermarkStore(tmp_path / "wm.json")
    ts = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    store.set("vscode", ts)
    assert store.get("vscode") == ts


def test_persists_across_instances(tmp_path):
    path = tmp_path / "wm.json"
    ts = datetime(2026, 3, 1, tzinfo=timezone.utc)
    WatermarkStore(path).set("a", ts)
    assert WatermarkStore(path).get("a") == ts


def test_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "wm.json"
    WatermarkStore(path).set("a", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert path.exists()
