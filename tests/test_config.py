from pathlib import Path

import pytest
from pydantic import ValidationError
from webvault.config import Settings

PROJECT_DIR = Path(__file__).parents[1]


def test_compose_binds_web_ui_to_loopback_by_default():
    compose = (PROJECT_DIR / "docker-compose.yml").read_text()
    assert "${WEBVAULT_BIND_ADDRESS:-127.0.0.1}:8008:8080" in compose


def test_auth_credentials_must_be_configured_together():
    with pytest.raises(ValidationError, match="must be configured together"):
        Settings(webvault_username="admin", webvault_password="")

    with pytest.raises(ValidationError, match="must be configured together"):
        Settings(webvault_username="", webvault_password="secret")


def test_auth_accepts_explicit_loopback_opt_out_or_complete_credentials():
    assert not Settings(webvault_allow_unauthenticated=True).auth_enabled
    configured = Settings(
        webvault_username="admin",
        webvault_password="secret",
        webvault_allow_unauthenticated=False,
    )
    assert configured.auth_enabled


def test_auth_is_required_by_default(monkeypatch):
    monkeypatch.delenv("WEBVAULT_ALLOW_UNAUTHENTICATED", raising=False)
    with pytest.raises(ValidationError, match="Authentication is required by default"):
        Settings(webvault_allow_unauthenticated=False)


def test_unauthenticated_non_loopback_binding_is_rejected():
    with pytest.raises(ValidationError, match="binding beyond loopback"):
        Settings(
            webvault_allow_unauthenticated=True,
            webvault_bind_address="0.0.0.0",
        )
