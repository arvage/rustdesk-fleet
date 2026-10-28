"""DATA_DIR is closed to other local users (app/paths.py close_data_dir)."""
import stat

from app import paths


def test_an_open_data_dir_is_closed_to_others(tmp_path, monkeypatch):
    d = tmp_path / "fleet"
    d.mkdir()
    d.chmod(0o755)
    monkeypatch.setattr(paths, "DATA_DIR", d)
    assert paths.close_data_dir() is True
    assert stat.S_IMODE(d.stat().st_mode) == 0o750
    assert paths.close_data_dir() is False          # already closed: nothing to do


def test_a_missing_data_dir_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "nope")
    assert paths.close_data_dir() is False
