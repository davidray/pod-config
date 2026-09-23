"""Declarative benchmark cases and suites (benchmarks/tasks/*/case.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qwenbench.config import parse_duration
from qwenbench.paths import repo_root

Category = Literal["fix-failing-test", "feature-from-spec", "refactor", "multi-file-bug", "exploration",
                   "add-tests", "other"]


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str | None = Field(None, description="Local directory (plain or git), relative to the case file")
    git: str | None = Field(None, description="Git URL or local repo path to clone")
    ref: str | None = Field(None, description="Commit/tag to check out; required for git sources")
    overlay: str | None = Field(None, description="Case-relative dir copied in before the starting commit")

    @model_validator(mode="after")
    def _one_source(self) -> Source:
        if bool(self.path) == bool(self.git):
            raise ValueError("source needs exactly one of `path` or `git`")
        if self.git and not self.ref:
            raise ValueError("git sources must pin `ref` (commit SHA or tag) for comparability")
        return self


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = "engineer"
    prompt_file: str
    context_file: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)


class Validation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    restore: list[str] = Field(default_factory=list, description="Paths reset to the starting commit first")
    overlay: str | None = Field(None, description="Case-relative dir of hidden tests copied in before validating")
    commands: list[str]
    timeout: str = "15m"


class CaseLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout: str = "20m"
    max_iterations: int = 60
    max_total_output_tokens: int = 200_000


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    category: Category = "other"
    source: Source
    setup: list[str] = Field(default_factory=list)
    task: TaskSpec
    agent_validation: list[str] = Field(default_factory=list)
    validation: Validation
    limits: CaseLimits = Field(default_factory=CaseLimits)
    case_dir: Path = Field(default=Path("."), exclude=True)

    @property
    def prompt(self) -> str:
        return (self.case_dir / self.task.prompt_file).read_text()

    @property
    def context(self) -> str:
        return (self.case_dir / self.task.context_file).read_text() if self.task.context_file else ""

    @property
    def timeout_s(self) -> float:
        return parse_duration(self.limits.timeout) or 1200

    @property
    def validation_timeout_s(self) -> float:
        return parse_duration(self.validation.timeout) or 900

    def resolve(self, rel: str | None) -> Path | None:
        if rel is None:
            return None
        p = Path(rel).expanduser()
        return p if p.is_absolute() else (self.case_dir / p).resolve()


class SuiteDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_attempts: int = 2


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    cases: list[str]
    defaults: SuiteDefaults = Field(default_factory=SuiteDefaults)


def benchmarks_dir() -> Path:
    return repo_root() / "benchmarks"


def load_case(case_id_or_path: str) -> Case:
    p = Path(case_id_or_path)
    case_file = p if p.suffix in (".yaml", ".yml") else benchmarks_dir() / "tasks" / case_id_or_path / "case.yaml"
    if not case_file.exists():
        raise FileNotFoundError(f"no benchmark case at {case_file}")
    data = yaml.safe_load(case_file.read_text())
    case = Case(**data)
    case.case_dir = case_file.parent.resolve()
    if not (case.case_dir / case.task.prompt_file).exists():
        raise FileNotFoundError(f"{case.id}: prompt file {case.task.prompt_file} missing")
    return case


def load_suite(name_or_path: str) -> tuple[Suite, list[Case]]:
    p = Path(name_or_path)
    suite_file = p if p.suffix in (".yaml", ".yml") else benchmarks_dir() / "suites" / f"{name_or_path}.yaml"
    suite = Suite(**yaml.safe_load(suite_file.read_text()))
    return suite, [load_case(c) for c in suite.cases]
