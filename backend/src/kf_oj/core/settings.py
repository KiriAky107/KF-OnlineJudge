from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kf_oj.contracts.judging import AdmissionPolicy


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KF_OJ_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: SecretStr | None = None
    amqp_url: SecretStr | None = None
    browser_origins: tuple[str, ...] = ("http://localhost:5173", "http://localhost:8000")
    session_lifetime_seconds: int = Field(default=43200, ge=300, le=86400)
    artifact_root: Path | None = None
    artifact_max_bytes: int = Field(default=536870912, ge=1, le=16 * 1024**3)
    artifact_max_files: int = Field(default=4096, ge=1, le=65536)
    judge_admission: AdmissionPolicy | None = None

    @model_validator(mode="after")
    def secure_origins(self):
        from urllib.parse import urlsplit

        if not self.browser_origins or len(self.browser_origins) > 8:
            raise ValueError("one to eight browser origins required")
        for origin in self.browser_origins:
            parts = urlsplit(origin)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.path
                or parts.query
                or parts.fragment
                or (self.environment == "production" and parts.scheme != "https")
            ):
                raise ValueError(
                    "browser origins must be explicit origins; production requires HTTPS"
                )
        return self
