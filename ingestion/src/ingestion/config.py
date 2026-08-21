"""sources.yaml 加载与校验 — 配置驱动的数据源清单(spec 6.1)。"""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError, model_validator


class SourceFilters(BaseModel):
    labels: list[str] = []
    state: str | None = None
    answered: bool | None = None
    since: str | None = None


class SourceConfig(BaseModel):
    name: str
    type: Literal["github_issues", "github_discussions", "manual_qa"]
    repo: str | None = None
    repo_org: str | None = None
    path: str | None = None
    product_area: str
    filters: SourceFilters = SourceFilters()

    @model_validator(mode="after")
    def check_type_requirements(self):
        if self.type == "github_issues" and not self.repo:
            raise ValueError("github_issues source requires 'repo'")
        if self.type == "github_discussions" and not (self.repo or self.repo_org):
            raise ValueError("github_discussions source requires 'repo' or 'repo_org'")
        if self.type == "manual_qa" and not self.path:
            raise ValueError("manual_qa source requires 'path'")
        return self


def load_sources(path: Path) -> list[SourceConfig]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = []
    for entry in raw.get("sources", []):
        name = entry.get("name", "<unnamed>")
        try:
            sources.append(SourceConfig(**entry))
        except (ValidationError, ValueError) as e:
            raise ValueError(f"invalid source '{name}': {e}") from e
    return sources
