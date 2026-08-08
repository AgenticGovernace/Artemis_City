#!/usr/bin/env python3
"""Canonical runtime storage paths for Artemis City.

All persistent runtime state belongs under the repository's top-level
``data/`` directory and all human-readable logs belong under ``logs/``.
Callers may still provide absolute paths for tests and containers, while
relative overrides are anchored to the corresponding canonical directory
instead of the process's current working directory.
"""

from __future__ import annotations

import os
from os import PathLike
from pathlib import Path
from typing import Optional, Union

PathValue = Union[str, PathLike[str]]

REPO_ROOT = Path(__file__).resolve().parents[1]


def _directory(env_var: str, default_name: str) -> Path:
    """Resolve a canonical runtime directory independently of ``cwd``."""
    configured = os.environ.get(env_var)
    path = Path(configured).expanduser() if configured else REPO_ROOT / default_name
    if not path.is_absolute():
        path = REPO_ROOT / path
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(REPO_ROOT)
        except ValueError as exc:
            raise ValueError(
                f"relative {env_var} must stay within the repository"
            ) from exc
        return resolved
    return path.resolve(strict=False)


def data_dir() -> Path:
    """Return the canonical persistent-data directory."""
    return _directory("ARTEMIS_DATA_DIR", "data")


def logs_dir() -> Path:
    """Return the canonical human-readable log directory."""
    return _directory("ARTEMIS_LOG_DIR", "logs")


def _runtime_path(
    *,
    value: Optional[PathValue],
    env_var: Optional[str],
    base_dir: Path,
    default_relative: str,
    canonical_prefix: str,
) -> str:
    """Resolve a runtime file or directory under a canonical base.

    Absolute values are preserved for deliberate test/container isolation.
    ``:memory:`` is also preserved for SQLite callers. Relative values such
    as ``data/custom.db`` and ``custom.db`` both resolve underneath the
    configured data directory.
    """
    selected: Optional[PathValue] = value
    if selected in (None, "") and env_var:
        selected = os.environ.get(env_var)
    if selected in (None, ""):
        selected = default_relative
    if str(selected) == ":memory:":
        return ":memory:"

    path = Path(selected).expanduser()
    if path.is_absolute():
        return str(path.resolve(strict=False))

    parts = path.parts
    if parts and parts[0] == canonical_prefix:
        path = Path(*parts[1:]) if len(parts) > 1 else Path()
    resolved = (base_dir / path).resolve(strict=False)
    try:
        resolved.relative_to(base_dir)
    except ValueError as exc:
        raise ValueError("relative runtime paths must stay within their root") from exc
    return str(resolved)


def data_path(
    default_relative: str,
    value: Optional[PathValue] = None,
    *,
    env_var: Optional[str] = None,
) -> str:
    """Resolve a persistent data file or subdirectory."""
    return _runtime_path(
        value=value,
        env_var=env_var,
        base_dir=data_dir(),
        default_relative=default_relative,
        canonical_prefix="data",
    )


def log_path(
    default_relative: str,
    value: Optional[PathValue] = None,
    *,
    env_var: Optional[str] = None,
) -> str:
    """Resolve a log file or subdirectory."""
    return _runtime_path(
        value=value,
        env_var=env_var,
        base_dir=logs_dir(),
        default_relative=default_relative,
        canonical_prefix="logs",
    )


__all__ = ["REPO_ROOT", "data_dir", "data_path", "logs_dir", "log_path"]
