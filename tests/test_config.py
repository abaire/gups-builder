from pathlib import Path

import pytest

from gups_builder.config import Config, load_config


def _world_with(tmp: Path, toml: str) -> Path:
    world = tmp / "world"
    (world / ".builder").mkdir(parents=True)
    (world / ".builder" / "config.toml").write_text(toml)
    return world


def _home_with(tmp: Path, toml: str) -> Path:
    home = tmp / "home"
    (home / ".config" / "gups-builder").mkdir(parents=True)
    (home / ".config" / "gups-builder" / "config.toml").write_text(toml)
    return home


def test_defaults(tmp_path: Path) -> None:
    cfg: Config = load_config(tmp_path / "world", {}, {}, tmp_path / "home")
    assert cfg.logic.base_url == "http://127.0.0.1:11434/v1"
    assert cfg.logic.model == ""
    assert cfg.logic.api_key is None
    assert cfg.logic.temperature == 0.2
    assert cfg.logic.max_tokens == 4096
    assert cfg.logic.context_tokens == 16384
    assert cfg.logic.structured == "json_schema"
    assert cfg.logic.timeout_s == 300.0
    assert cfg.prose.temperature == 0.8
    assert cfg.prose.model == ""
    assert cfg.max_repairs == 3
    assert cfg.gups is None


def test_cli_overrides_env(tmp_path: Path) -> None:
    cfg = load_config(
        tmp_path / "world",
        {"logic_model": "from-cli"},
        {"GUPS_BUILDER_LOGIC_MODEL": "from-env"},
        tmp_path / "home",
    )
    assert cfg.logic.model == "from-cli"


def test_env_overrides_world(tmp_path: Path) -> None:
    world = _world_with(tmp_path, '[logic]\nmodel = "from-world"\n')
    cfg = load_config(world, {}, {"GUPS_BUILDER_LOGIC_MODEL": "from-env"}, tmp_path / "home")
    assert cfg.logic.model == "from-env"


def test_world_overrides_user(tmp_path: Path) -> None:
    world = _world_with(tmp_path, '[logic]\nmodel = "from-world"\n')
    home = _home_with(tmp_path, '[logic]\nmodel = "from-user"\n')
    cfg = load_config(world, {}, {}, home)
    assert cfg.logic.model == "from-world"


def test_user_overrides_default(tmp_path: Path) -> None:
    home = _home_with(tmp_path, '[logic]\nmodel = "from-user"\n')
    cfg = load_config(tmp_path / "world", {}, {}, home)
    assert cfg.logic.model == "from-user"


def test_per_field_merge(tmp_path: Path) -> None:
    world = _world_with(tmp_path, '[logic]\nmodel = "from-world"\n')
    home = _home_with(tmp_path, '[logic]\nbase_url = "http://user-host/v1"\n')
    cfg = load_config(world, {}, {}, home)
    assert cfg.logic.model == "from-world"
    assert cfg.logic.base_url == "http://user-host/v1"


def test_env_mapping(tmp_path: Path) -> None:
    env = {
        "GUPS_BUILDER_LOGIC_URL": "http://env-logic/v1",
        "GUPS_BUILDER_LOGIC_TEMPERATURE": "0.5",
        "GUPS_BUILDER_LOGIC_API_KEY": "key-logic",
        "GUPS_BUILDER_PROSE_MODEL": "env-prose",
        "GUPS_BUILDER_PROSE_STRUCTURED": "json_object",
        "GUPS_BUILDER_PROSE_CONTEXT_TOKENS": "8000",
        "GUPS_BIN": "/opt/gups/bin/gups",
        "GUPS_BUILDER_MAX_REPAIRS": "5",
    }
    cfg = load_config(tmp_path / "world", {}, env, tmp_path / "home")
    assert cfg.logic.base_url == "http://env-logic/v1"
    assert cfg.logic.temperature == 0.5
    assert cfg.logic.api_key == "key-logic"
    assert cfg.prose.model == "env-prose"
    assert cfg.prose.structured == "json_object"
    assert cfg.prose.context_tokens == 8000
    assert cfg.gups == "/opt/gups/bin/gups"
    assert cfg.max_repairs == 5


def test_bad_structured_from_env_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="GUPS_BUILDER_LOGIC_STRUCTURED"):
        load_config(
            tmp_path / "world", {}, {"GUPS_BUILDER_LOGIC_STRUCTURED": "bogus"}, tmp_path / "home"
        )


def test_bad_structured_from_file_raises(tmp_path: Path) -> None:
    world = _world_with(tmp_path, '[logic]\nstructured = "bogus"\n')
    with pytest.raises(ValueError, match="world config"):
        load_config(world, {}, {}, tmp_path / "home")


def test_env_mapping_max_tokens_and_timeout(tmp_path: Path) -> None:
    env = {"GUPS_BUILDER_LOGIC_MAX_TOKENS": "1024", "GUPS_BUILDER_PROSE_TIMEOUT_S": "12.5"}
    cfg = load_config(tmp_path / "world", {}, env, tmp_path / "home")
    assert cfg.logic.max_tokens == 1024
    assert cfg.prose.timeout_s == 12.5


def test_bad_number_names_its_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="GUPS_BUILDER_LOGIC_TEMPERATURE"):
        load_config(
            tmp_path / "world", {}, {"GUPS_BUILDER_LOGIC_TEMPERATURE": "hot"}, tmp_path / "home"
        )
    world = _world_with(tmp_path, 'max_repairs = "many"\n')
    with pytest.raises(ValueError, match="world config"):
        load_config(world, {}, {}, tmp_path / "home")
