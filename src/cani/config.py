"""Loading, validation, and persistence of checks.yaml probe configuration."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

Category = Literal["network", "filesystem", "process"]


class ConfigError(Exception):
    """Raised when checks.yaml is missing, unparsable, or schema-invalid."""


class Check(BaseModel):
    """A single benign probe action.

    Exit code 0 means the action is ALLOWED by the current sandbox/policy;
    anything else means DENIED.
    """

    id: str
    name: str
    category: Category
    command: str
    timeout: float | None = None  # seconds; falls back to settings.default_timeout_seconds
    description: str = ""
    # What the applied sandbox/policy SHOULD do. Set when the check was
    # derived from an OpenShell policy; None = "just probe and report".
    expect: Literal["allowed", "denied"] | None = None

    @field_validator("command")
    @classmethod
    def _command_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("command must not be empty")
        return v

    def effective_timeout(self, default: float) -> float:
        return self.timeout if self.timeout is not None else default


class Settings(BaseModel):
    interval_seconds: float = Field(default=3.0, gt=0)
    default_timeout_seconds: float = Field(default=5.0, gt=0)


class Config(BaseModel):
    version: int = 1
    settings: Settings = Field(default_factory=Settings)
    checks: list[Check]

    @field_validator("checks")
    @classmethod
    def _unique_ids(cls, checks: list[Check]) -> list[Check]:
        seen: set[str] = set()
        for c in checks:
            if c.id in seen:
                raise ValueError(f"duplicate check id: {c.id!r}")
            seen.add(c.id)
        if not checks:
            raise ValueError("at least one check is required")
        return checks

    @model_validator(mode="after")
    def _resolve_timeouts(self) -> "Config":
        # Bake the default timeout in so consumers never handle None.
        for c in self.checks:
            if c.timeout is None:
                c.timeout = self.settings.default_timeout_seconds
        return self

    def to_yaml(self) -> str:
        """Serialize to checks.yaml text (round-trips through parse_config)."""
        data = self.model_dump(mode="json", exclude_none=True)
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def _format_validation_error(exc: PydanticValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"  - {loc}: {err['msg']} (got {err.get('input')!r})")
    return "invalid checks.yaml schema:\n" + "\n".join(lines)


def parse_config(text: str) -> Config:
    """Parse and validate raw YAML text into a Config."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("checks.yaml must be a YAML mapping at the top level")
    try:
        return Config.model_validate(data)
    except PydanticValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc


def load_config(path: str | Path) -> Config:
    """Load and validate a checks.yaml file from disk."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    return parse_config(p.read_text(encoding="utf-8"))


def save_config(path: str | Path, text: str) -> Config:
    """Validate raw YAML text, then atomically write it to disk.

    The file is left untouched if validation fails.
    """
    cfg = parse_config(text)  # raises ConfigError before any write
    p = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return cfg
