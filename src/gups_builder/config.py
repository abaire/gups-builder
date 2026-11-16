from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Any, Literal

import tomli_w
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

StructuredMode = Literal["json_schema", "json_object", "none"]

# RoleConfig fields, in declaration order.
_ROLE_FIELDS = (
    "base_url",
    "model",
    "api_key",
    "temperature",
    "max_tokens",
    "context_tokens",
    "structured",
    "timeout_s",
)

# RoleConfig field -> environment-variable suffix, uppercased after the role.
_ENV_SUFFIX: dict[str, str] = {
    "base_url": "URL",
    "model": "MODEL",
    "api_key": "API_KEY",
    "temperature": "TEMPERATURE",
    "structured": "STRUCTURED",
    "context_tokens": "CONTEXT_TOKENS",
    "max_tokens": "MAX_TOKENS",
    "timeout_s": "TIMEOUT_S",
}

# RoleConfig field -> the CLI flag fragment after the role, for the flags that exist.
_CLI_KEY: dict[str, str] = {
    "base_url": "url",
    "model": "model",
}

_NUMERIC = {"temperature", "timeout_s", "max_tokens", "context_tokens", "max_repairs"}


class RoleConfig(BaseModel):
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = ""
    api_key: str | None = None
    temperature: float
    max_tokens: int = 4096
    context_tokens: int = 16384
    structured: StructuredMode = "json_schema"
    timeout_s: float = 300.0


class Config(BaseModel):
    logic: RoleConfig = RoleConfig(temperature=0.2)
    prose: RoleConfig = RoleConfig(temperature=0.8)
    max_repairs: int = 3
    gups: str | None = None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def _cast(field: str, value: Any, source: str) -> Any:
    try:
        if field in ("temperature", "timeout_s"):
            return float(value)
        if field in _NUMERIC:
            return int(value)
    except (TypeError, ValueError):
        msg = f"invalid {field} {value!r} from {source}"
        raise ValueError(msg) from None
    return value


def _resolve(
    field: str,
    role: str,
    cli: dict[str, str],
    env: Mapping[str, str],
    world_cfg: dict[str, Any],
    user_cfg: dict[str, Any],
    default: Any,
) -> tuple[Any, str]:
    cli_key = _CLI_KEY.get(field)
    if cli_key is not None:
        value = cli.get(f"{role}_{cli_key}")
        if value is not None:
            return value, f"--{role}-{cli_key}"
    suffix = _ENV_SUFFIX.get(field)
    if suffix is not None:
        var = f"GUPS_BUILDER_{role.upper()}_{suffix}"
        if var in env:
            return env[var], var
    for label, cfg in (("world config", world_cfg), ("user config", user_cfg)):
        table = cfg.get(role)
        if isinstance(table, dict) and field in table:
            return table[field], f"{label} file"
    return default, "default"


def _load_role(
    role: str,
    default_temperature: float,
    cli: dict[str, str],
    env: Mapping[str, str],
    world_cfg: dict[str, Any],
    user_cfg: dict[str, Any],
) -> RoleConfig:
    defaults = RoleConfig(temperature=default_temperature)
    values: dict[str, Any] = {}
    for field in _ROLE_FIELDS:
        value, source = _resolve(
            field, role, cli, env, world_cfg, user_cfg, getattr(defaults, field)
        )
        if field == "structured":
            if value not in ("json_schema", "json_object", "none"):
                msg = f"invalid structured mode {value!r} for {role} from {source}"
                raise ValueError(msg)
        else:
            value = _cast(field, value, source)
        values[field] = value
    return RoleConfig(**values)


def _load_gups(
    cli: dict[str, str],
    env: Mapping[str, str],
    world_cfg: dict[str, Any],
    user_cfg: dict[str, Any],
) -> str | None:
    value = cli.get("gups")
    if value is not None:
        return value
    if "GUPS_BIN" in env:
        return env["GUPS_BIN"]
    for cfg in (world_cfg, user_cfg):
        if "gups" in cfg:
            return str(cfg["gups"])
    return None


def _load_max_repairs(
    env: Mapping[str, str], world_cfg: dict[str, Any], user_cfg: dict[str, Any]
) -> int:
    if "GUPS_BUILDER_MAX_REPAIRS" in env:
        return int(
            _cast("max_repairs", env["GUPS_BUILDER_MAX_REPAIRS"], "GUPS_BUILDER_MAX_REPAIRS")
        )
    for label, cfg in (("world config file", world_cfg), ("user config file", user_cfg)):
        if "max_repairs" in cfg:
            return int(_cast("max_repairs", cfg["max_repairs"], label))
    return 3


def world_config_path(world: Path) -> Path:
    """Return the world-local configuration file path."""
    return world / ".builder" / "config.toml"


def user_config_path(home: Path) -> Path:
    """Return the user-global configuration file path."""
    return home / ".config" / "gups-builder" / "config.toml"


def save_config_value(
    path: Path, role: Literal["logic", "prose"] | None, field: str, value: Any
) -> None:
    """Update a single configuration field in a TOML file, creating it if absent."""
    data = _read_toml(path)
    target: dict[str, Any]
    if role is None:
        target = data
    else:
        existing = data.get(role)
        if isinstance(existing, dict):
            target = existing
        else:
            target = {}
            data[role] = target
    if value is None:
        target.pop(field, None)
    else:
        target[field] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data))


def load_config(world: Path, cli: dict[str, str], env: Mapping[str, str], home: Path) -> Config:
    world_cfg = _read_toml(world_config_path(world))
    user_cfg = _read_toml(user_config_path(home))
    return Config(
        logic=_load_role("logic", 0.2, cli, env, world_cfg, user_cfg),
        prose=_load_role("prose", 0.8, cli, env, world_cfg, user_cfg),
        max_repairs=_load_max_repairs(env, world_cfg, user_cfg),
        gups=_load_gups(cli, env, world_cfg, user_cfg),
    )
