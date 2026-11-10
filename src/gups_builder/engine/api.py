"""The subset of the `gups docs --json` API model the builder reads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

_IGNORE_EXTRA = ConfigDict(extra="ignore")


class ApiParam(BaseModel):
    model_config = _IGNORE_EXTRA
    name: str
    required: bool = False
    default: str | None = None
    allowed_values: list[str] = []
    doc: str = ""
    feature: str | None = None
    type: str | None = None


class ApiEntry(BaseModel):
    model_config = _IGNORE_EXTRA
    name: str
    doc: str = ""
    params: list[ApiParam] = []
    feature: str | None = None


class ApiModel(BaseModel):
    model_config = _IGNORE_EXTRA
    api_version: int
    declarations: list[ApiEntry] = []
    effects: list[ApiEntry] = []
    behaviors: list[ApiEntry] = []

    def declaration(self, name: str) -> ApiEntry:
        for entry in self.declarations:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def effect(self, name: str) -> ApiEntry | None:
        return next((entry for entry in self.effects if entry.name == name), None)

    def behavior(self, name: str) -> ApiEntry | None:
        return next((entry for entry in self.behaviors if entry.name == name), None)
