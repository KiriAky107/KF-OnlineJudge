from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kf_oj.contracts.judging_v2 import NodeAdmission
from kf_oj.contracts.learning_model import ProgressiveHintLevel


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KF_OJ_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: SecretStr | None = None
    learning_database_url: SecretStr | None = None
    learning_model_base_url: str | None = None
    learning_model_name: str | None = Field(default=None, min_length=1, max_length=96)
    learning_model_api_key: SecretStr | None = None
    learning_model_reasoning_effort: Literal["none"] | None = None
    learning_chat_reasoning_effort: Literal["none", "low", "high"] | None = None
    learning_chat_max_hint_level: ProgressiveHintLevel = "L3"
    learning_member_max_hint_level: ProgressiveHintLevel = "L3"
    learning_admin_max_hint_level: ProgressiveHintLevel = "L3"
    learning_model_account_calls_per_hour: int = Field(default=12, ge=1, le=1000)
    learning_model_service_calls_per_hour: int = Field(default=120, ge=1, le=10000)
    identity_operator_database_url: SecretStr | None = None
    amqp_url: SecretStr | None = None
    login_require_trusted_origin: bool = True
    browser_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://localhost:8000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8000",
    )
    session_lifetime_seconds: int = Field(default=43200, ge=300, le=86400)
    artifact_root: Path | None = None
    import_staging_root: Path | None = None
    import_worker_cgroup: Path | None = None
    artifact_max_bytes: int = Field(default=536870912, ge=1, le=16 * 1024**3)
    artifact_max_files: int = Field(default=4096, ge=1, le=65536)
    judge_admission: NodeAdmission | None = None
    coordinator_url: str | None = None
    coordinator_token: SecretStr | None = None
    delivery_release_id: str | None = Field(default=None, pattern=r"^sd-[0-9a-f]{12}$")
    delivery_configuration_id: str | None = Field(default=None, pattern=r"^cfg-[0-9a-f]{24}$")

    @model_validator(mode="after")
    def secure_origins(self):
        from ipaddress import ip_address

        if (self.learning_model_base_url is None) != (self.learning_model_name is None):
            raise ValueError("both Responses base URL and model name are required")
        if self.learning_model_base_url is not None:
            endpoint = urlsplit(self.learning_model_base_url)
            try:
                local = endpoint.hostname == "localhost" or (
                    endpoint.hostname is not None and ip_address(endpoint.hostname).is_loopback
                )
            except ValueError:
                local = False
            if (
                not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
                or (endpoint.scheme != "https" and (endpoint.scheme != "http" or not local))
            ):
                raise ValueError("Responses URL must use HTTPS or local HTTP")
            if not local and (
                self.learning_model_api_key is None
                or not self.learning_model_api_key.get_secret_value().strip()
            ):
                raise ValueError("remote Responses model requires a service credential")

        if (self.coordinator_url is None) != (self.coordinator_token is None):
            raise ValueError("both coordinator URL and token required")
        if self.coordinator_url is not None:
            parts = urlsplit(self.coordinator_url)
            try:
                valid = parts.hostname is not None and ip_address(parts.hostname).is_loopback
                port = parts.port
            except ValueError:
                valid, port = False, None
            if (
                not valid
                or parts.scheme != "http"
                or port is None
                or parts.path not in {"", "/"}
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
            ):
                raise ValueError("coordinator must be an explicit numeric loopback HTTP origin")
            import re

            if not re.fullmatch(r"[0-9a-f]{64}", self.coordinator_token.get_secret_value()):
                raise ValueError("invalid coordinator service credential")

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
