"""Shared CLI plumbing."""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from typing import Any, TypeVar

import typer
from rich.console import Console

from qwenbench.config import Config, load_config

console = Console()
err = Console(stderr=True)

F = TypeVar("F", bound=Callable[..., Any])


def cfg() -> Config:
    try:
        return load_config()
    except Exception as e:
        err.print(f"[red]config error:[/] {e}")
        raise typer.Exit(2) from e


def provider(config: Config | None = None):
    from qwenbench.runpod.provider import RunpodPodsProvider

    return RunpodPodsProvider(config or cfg())


def handle_errors(fn: F) -> F:
    """Turn expected failures into clean messages and non-zero exits."""
    from qwenbench.runpod.client import RunpodError
    from qwenbench.runpod.provider import GuardViolation, ProvisionError
    from qwenbench.secrets import MissingSecret

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except MissingSecret as e:
            err.print(f"[red]missing credential:[/] {e}")
            raise typer.Exit(2) from e
        except GuardViolation as e:
            err.print(f"[red]refused by spend guard:[/] {e}")
            raise typer.Exit(3) from e
        except ProvisionError as e:
            err.print(f"[red]provisioning failed:[/] {e}")
            raise typer.Exit(1) from e
        except RunpodError as e:
            err.print(f"[red]Runpod API error:[/] {e}")
            raise typer.Exit(1) from e
        except KeyError as e:
            err.print(f"[red]error:[/] {e.args[0] if e.args else e}")
            raise typer.Exit(2) from e

    return wrapper  # type: ignore[return-value]


def is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
