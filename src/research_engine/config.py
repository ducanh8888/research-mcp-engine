"""Bootstrap configuration; provider credentials and routing live in SQLite."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

# Environment expansion follows R0Wi/mcp-gateway config.py @59c1efd (MIT).
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_tree(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ValueError(f"Configuration references undefined environment variable: {name}")
        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_tree(item) for key, item in value.items()}
    return value


def _read_secret(path: Path, label: str) -> str:
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"{label} file is missing or unreadable: {path}") from exc
    if not secret:
        raise ValueError(f"{label} file is empty: {path}")
    return secret


class Settings(BaseModel):
    """Only bootstrap settings belong here; no provider keys are accepted."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    data_dir: Path = Path("data")
    database_path: Path | None = None
    blob_dir: Path | None = None
    encryption_key_file: Path
    admin_username: str = "admin"
    admin_password_hash: str = Field(repr=False, min_length=1)
    admin_session_secret_file: Path
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    trusted_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:8765", "http://localhost:8765"]
    )
    public_base_url: str = "http://127.0.0.1:8765"
    max_wait_s: float = Field(default=40, gt=0, le=300)
    document_page_chars: int = Field(default=20_000, ge=100, le=100_000)
    max_response_bytes: int = Field(default=256_000, ge=1024, le=10_000_000)
    _session_secret: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def resolve_defaults(self) -> Settings:
        # Assignment validation would recurse here; defaults are set directly.
        if self.database_path is None:
            object.__setattr__(self, "database_path", self.data_dir / "engine.db")
        if self.blob_dir is None:
            object.__setattr__(self, "blob_dir", self.data_dir / "blobs")
        if not self.admin_username.strip():
            raise ValueError("admin_username must not be empty")
        for origin in [*self.trusted_origins, self.public_base_url]:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Origins and public_base_url must be absolute HTTP(S) URLs")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Origins must not contain credentials, queries or fragments")
            if parsed.path not in {"", "/"}:
                raise ValueError("Origins and public_base_url must not contain a path")
        object.__setattr__(self, "public_base_url", self.public_base_url.rstrip("/"))
        object.__setattr__(
            self, "trusted_origins", [origin.rstrip("/") for origin in self.trusted_origins]
        )
        return self

    def validate_bootstrap(self) -> None:
        _read_secret(self.encryption_key_file, "Encryption key")
        self._session_secret = _read_secret(self.admin_session_secret_file, "Admin session secret")
        if len(self._session_secret) < 32:
            raise ValueError("Admin session secret must contain at least 32 characters")

    @property
    def admin_session_secret(self) -> str:
        if self._session_secret is None:
            self.validate_bootstrap()
        assert self._session_secret is not None
        return self._session_secret


def load_config(path: str | Path | None = None, *, validate: bool = True) -> Settings:
    """Load YAML; resolve filesystem paths relative to its directory."""
    config_path = Path(path or os.environ.get("RESEARCH_ENGINE_CONFIG", "config.yaml")).resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"Configuration file is missing or unreadable: {config_path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a YAML mapping")
    raw = _expand_tree(raw)
    raw.setdefault("data_dir", "data")
    for name in (
        "data_dir", "database_path", "blob_dir", "encryption_key_file",
        "admin_session_secret_file",
    ):
        if raw.get(name) is not None:
            item = Path(raw[name]).expanduser()
            raw[name] = item if item.is_absolute() else config_path.parent / item
    settings = Settings.model_validate(raw)
    if validate:
        settings.validate_bootstrap()
    return settings
