from __future__ import annotations

import pytest

from ingest.config import DEFAULT_CONFIG_PATH, Settings, load_settings

# Settings read from the environment; tests must not pick up a developer's .env or shell.
_ENV = (
    "CONFIG_PATH",
    "LAKE_ROOT",
    "STATE_BACKEND",
    "STATE_DIR",
    "STATE_TABLE",
    "ERCOT_USERNAME",
    "ERCOT_PASSWORD",
    "ERCOT_SUBSCRIPTION_KEY",
    "ERCOT_SECRET_ID",
    "INGEST_FUNCTION_NAME",
    "INGEST_LOG_GROUP",
    "ALARM_PREFIX",
    "ALERTS_TOPIC_ARN",
    "ACTIONS_TOPIC_ARN",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("ingest.config.load_dotenv", lambda *_: None)


@pytest.fixture
def cfg() -> Settings:
    return load_settings(DEFAULT_CONFIG_PATH)
