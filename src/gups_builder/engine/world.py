"""Pydantic models of the `gups describe-world` JSON document (format 1)."""

from __future__ import annotations

import json
import warnings

from pydantic import BaseModel, ConfigDict, ValidationError

# A newer engine may add fields; they are ignored so old builds still parse.
_IGNORE_EXTRA = ConfigDict(extra="ignore")


class SchemaName(BaseModel):
    model_config = _IGNORE_EXTRA
    name: str
    label: str


class DamageTypeInfo(BaseModel):
    model_config = _IGNORE_EXTRA
    name: str
    label: str
    adjective: str | None = None


class Schema(BaseModel):
    model_config = _IGNORE_EXTRA
    stats: list[SchemaName] = []
    vitals: list[SchemaName] = []
    damage_types: list[DamageTypeInfo] = []
    unarmed_damage_type: str
    channels: list[str] = []


class PackageInfo(BaseModel):
    model_config = _IGNORE_EXTRA
    path: str
    kind: str | None = None
    ok: bool


class DoorInfo(BaseModel):
    model_config = _IGNORE_EXTRA
    state: str
    key: str | None = None
    tags: list[str] = []


class ExitInfo(BaseModel):
    model_config = _IGNORE_EXTRA
    dir: str
    to: str
    one_way: bool = False
    tags: list[str] = []
    door: DoorInfo | None = None


class RoomInfo(BaseModel):
    model_config = _IGNORE_EXTRA
    label: str
    name: str
    size: str = "normal"
    regions: list[str] = []
    tags: list[str] = []
    flags: list[str] = []
    exits: list[ExitInfo] = []


class MobInfo(BaseModel):
    model_config = _IGNORE_EXTRA
    label: str
    name: str
    keywords: list[str] = []
    level: int = 1
    stats: dict[str, int] = {}
    vitals: dict[str, int] = {}
    resists: dict[str, int] = {}
    skills: dict[str, int] = {}
    traits: list[str] = []
    factions: list[str] | None = None


class ItemInfo(BaseModel):
    model_config = _IGNORE_EXTRA
    label: str
    name: str
    keywords: list[str] = []
    kind: str | None = None
    wear: str | None = None
    weight: int = 0
    value: int = 0


class SkillInfo(BaseModel):
    model_config = _IGNORE_EXTRA
    label: str
    kind: str
    target: str | None = None
    range: str | None = None
    cost: dict[str, int] = {}
    warmup: int = 0
    lag: int = 0
    channels: list[str] = []
    tags: list[str] = []


class PluginSummary(BaseModel):
    model_config = _IGNORE_EXTRA
    id: str
    builtins: list[str] = []
    effects: list[str] = []
    traits: list[str] = []


class LoadErrorInfo(BaseModel):
    model_config = _IGNORE_EXTRA
    package: str
    file: str
    line: int | None = None
    message: str


# The document's key is "schema", so the field keeps that name even though it
# shadows the deprecated BaseModel.schema classmethod; pydantic warns once at
# class creation, which is silenced around the definition.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", UserWarning)

    class WorldDescription(BaseModel):
        model_config = _IGNORE_EXTRA
        format: int
        api_version: int
        engine_version: str
        schema: Schema  # type: ignore[assignment]
        features: list[str] = []
        commands: dict[str, str] = {}
        packages: list[PackageInfo] = []
        rooms: list[RoomInfo] = []
        mobs: list[MobInfo] = []
        items: list[ItemInfo] = []
        skills: list[SkillInfo] = []
        factions: list[str] = []
        regions: list[str] = []
        plugins: list[PluginSummary] = []
        errors: list[LoadErrorInfo] = []

        def has_feature(self, f: str) -> bool:
            return f in self.features

        def rooms_in(self, package: str) -> list[RoomInfo]:
            prefix = f"//{package}:"
            return [r for r in self.rooms if r.label.startswith(prefix)]

        def mobs_in(self, package: str) -> list[MobInfo]:
            prefix = f"//{package}:"
            return [m for m in self.mobs if m.label.startswith(prefix)]

        def items_in(self, package: str) -> list[ItemInfo]:
            prefix = f"//{package}:"
            return [i for i in self.items if i.label.startswith(prefix)]

        def skills_in(self, package: str) -> list[SkillInfo]:
            prefix = f"//{package}:"
            return [s for s in self.skills if s.label.startswith(prefix)]

        def label_exists(self, label: str) -> bool:
            for collection in (self.rooms, self.mobs, self.items, self.skills):
                if any(decl.label == label for decl in collection):
                    return True
            return label in self.factions


def parse_describe_world(stdout: str) -> WorldDescription:
    # Lazy import: gups.py imports the models from this module, so the
    # error class is fetched only when the check runs.
    from .gups import EngineError

    try:
        document = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise EngineError(
            f"gups describe-world wrote something that is not JSON: {error}"
        ) from error
    if not isinstance(document, dict):
        raise EngineError("gups describe-world wrote JSON that is not an object")
    if document.get("format") != 1:
        raise EngineError(
            f"gups describe-world format {document.get('format')} is not supported; "
            "this gups-builder needs format 1"
        )
    try:
        return WorldDescription.model_validate(document)
    except ValidationError as error:
        raise EngineError(f"gups describe-world wrote an unusable document: {error}") from error
