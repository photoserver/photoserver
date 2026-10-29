from pathlib import Path

import pytest
from pydantic import ValidationError

from photoserver.config import Settings

BASE_ENV = {
    "PHOTOSERVER_STORAGE_ROOT": "/tmp/photo_storage",
    "PHOTOSERVER_DATABASE_URL": "postgres://user:password@host:port/database",
}


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PHOTOSERVER_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("PHOTOSERVER_PORT", "9000")
    monkeypatch.setenv("PHOTOSERVER_DATABASE_URL", "postgres://user:password@host:port/database")

    settings = Settings(_env_file=None)

    assert settings.storage_root == tmp_path
    assert settings.port == 9000
    assert settings.database_url.get_secret_value() == "postgres://user:password@host:port/database"


@pytest.mark.parametrize(
    "env_var_to_delete, expected_field",
    [
        ("PHOTOSERVER_STORAGE_ROOT", "storage_root"),
        ("PHOTOSERVER_DATABASE_URL", "database_url"),
    ],
)
def test_required_settings_fields(
    monkeypatch: pytest.MonkeyPatch, env_var_to_delete: str, expected_field: str
) -> None:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)

    monkeypatch.delenv(env_var_to_delete, raising=False)

    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None)

    errors = e.value.errors()
    first_error = errors[0]

    assert first_error["type"] == "missing"
    assert first_error["msg"] == "Field required"
    assert first_error["loc"] == (expected_field,)
