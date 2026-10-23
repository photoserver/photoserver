from pathlib import Path

import pytest

from photoserver.config import Settings

def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PHOTOSERVER_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("PHOTOSERVER_PORT", "9000")

    settings = Settings(_env_file=None)

    assert settings.storage_root == tmp_path
    assert settings.port == 9000

def test_storage_root_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOTOSERVER_STORAGE_ROOT", raising=False)

    with pytest.raises(ValueError):
        Settings(_env_file=None)