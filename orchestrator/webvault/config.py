from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
APP_DIR = PACKAGE_DIR.parent
PROJECT_DIR = APP_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="/app/.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    garage_endpoint: str = "http://garage:3900"
    garage_access_key: str = ""
    garage_secret_key: str = ""
    garage_bucket: str = "archives"
    garage_capacity: str = "100G"
    crawler_container_name: str = "webvault-crawler"
    webvault_username: str = ""
    webvault_password: str = ""
    webvault_allow_unauthenticated: bool = False
    webvault_bind_address: str = "127.0.0.1"
    webvault_allowed_hosts: str = "localhost,127.0.0.1,[::1]"
    max_queued_jobs: int = Field(default=100, ge=1, le=10_000)
    allow_private_crawls: bool = False
    screencast_port: int = Field(default=9037, ge=1, le=65535)
    screencast_url: str = ""
    crawl_output_dir: Path | None = None
    profile_dir: Path = Path("/profiles")
    replayweb_dir: Path | None = None
    webvault_db_path: Path | None = None

    @field_validator("garage_endpoint", "screencast_url")
    @classmethod
    def strip_trailing_slash(cls, value: str):
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_security(self):
        if bool(self.webvault_username) != bool(self.webvault_password):
            raise ValueError("WEBVAULT_USERNAME and WEBVAULT_PASSWORD must be configured together")
        if not self.auth_enabled and not self.webvault_allow_unauthenticated:
            raise ValueError(
                "Authentication is required by default. Run setup.sh or explicitly set "
                "WEBVAULT_ALLOW_UNAUTHENTICATED=true for a loopback-only deployment."
            )
        if not self.auth_enabled and self.webvault_bind_address not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError("Authentication is required when binding beyond loopback")
        if not self.allowed_hosts:
            raise ValueError("WEBVAULT_ALLOWED_HOSTS must contain at least one host")
        return self

    @property
    def auth_enabled(self):
        return bool(self.webvault_username and self.webvault_password)

    @property
    def allowed_hosts(self):
        return [host.strip() for host in self.webvault_allowed_hosts.split(",") if host.strip()]

    @property
    def crawl_dir(self):
        if self.crawl_output_dir:
            return self.crawl_output_dir
        container_path = Path("/crawls")
        return container_path if container_path.exists() else PROJECT_DIR / "crawls"

    @property
    def replay_dir(self):
        if self.replayweb_dir:
            return self.replayweb_dir
        container_path = Path("/replayweb")
        return container_path if container_path.exists() else PROJECT_DIR / "replayweb"

    @property
    def database_path(self):
        return self.webvault_db_path or self.crawl_dir / "webvault.db"

    @property
    def resolved_screencast_url(self):
        return self.screencast_url or (
            f"http://{self.crawler_container_name}:{self.screencast_port}"
        )


settings = Settings()
