"""Pytest session isolation so local .env credentials & dev SQLite DBs never interfere with tests."""

from __future__ import annotations

import pytest

from neon_sync.config import Settings


@pytest.fixture(autouse=True, scope="session")
def _isolate_tests_from_local_dotenv() -> None:
    """Prevent pydantic-settings from reading developer .env files during automated tests."""
    prev = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    try:
        yield
    finally:
        Settings.model_config["env_file"] = prev
