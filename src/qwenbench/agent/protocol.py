"""The contract between an orchestrator (Claude/Hero, or the benchmark harness)
and an implementation worker (the Qwen agent today, any model tomorrow).

Versioned and JSON-serializable so another worker can be dropped in without
touching Hero routing or benchmark semantics. See docs/agent-protocol.md.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = "qwenbench.dispatch/v1"


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_iterations: int = 60
    timeout_s: float = 1800
    max_total_output_tokens: int = 200_000
    command_timeout_s: int = 600


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: str = PROTOCOL_VERSION
    role: str = Field(description="Hero agent role being performed, e.g. api-engineer")
    task: str = Field(description="What to implement. Specs can be long; put them here or in context.")
    repo_path: str
    context: str = Field("", description="Spec text, conventions, relevant decisions, file pointers")
    acceptance_criteria: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)
    spec_ref: str | None = Field(None, description="Hero spec slug or path, for provenance")
    profile: str | None = Field(None, description="Compute profile; default resolved from Hero models.roles")
    limits: Limits = Field(default_factory=Limits)
    attempt: int = 1
    allow_network: bool = False


class FileChange(BaseModel):
    path: str
    status: Literal["added", "modified", "deleted", "renamed"]
    added: int = 0
    deleted: int = 0


class ValidationResult(BaseModel):
    command: str
    passed: bool
    exit_code: int | None
    duration_s: float
    timed_out: bool = False
    output_tail: str = ""


class Failure(BaseModel):
    kind: Literal[
        "endpoint_unavailable", "llm_error", "validation_failed", "agent_gave_up", "agent_blocked",
        "iteration_limit", "timeout", "token_budget", "no_progress", "policy", "internal",
    ]
    message: str
    retryable: bool = False


class DispatchResult(BaseModel):
    protocol: str = PROTOCOL_VERSION
    dispatch_id: str
    status: Literal["completed", "failed", "blocked", "error"]
    summary: str = ""
    agent_notes: str | None = None
    role: str
    files_changed: list[FileChange] = Field(default_factory=list)
    lines_added: int = 0
    lines_deleted: int = 0
    validation: list[ValidationResult] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    model: dict[str, Any] = Field(default_factory=dict, description="Proof of who did the work")
    failure: Failure | None = None
    base_tree: str | None = None
    result_tree: str | None = None
    artifacts: dict[str, str] = Field(default_factory=dict, description="transcript, diff, requests paths")
