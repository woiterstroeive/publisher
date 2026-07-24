"""
config.py

Centralized application configuration loader.

Responsibility:
    - Ensure environment variables are loaded (from a local .env file,
      if present) exactly once, early in the application lifecycle.
    - Load and expose generic, publisher-agnostic backend settings
      (filesystem paths, logging level) as a validated, immutable
      configuration object.

This module MUST remain publisher-agnostic and business-logic-free.
It has no knowledge of PeerTube, YouTube, Rumble, or any other
publisher, and it does NOT expose the full process environment to
the rest of the application.

Publisher-specific configuration (e.g. API tokens, instance URLs)
is owned entirely by each publisher module. Publishers read
`os.environ` directly and are responsible for validating their own
settings into their own dedicated config object (e.g. PeerTubeConfig
in publishers/peertube.py). This keeps config.py permanently closed
for modification as publishers are added, removed, or reordered.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class AppConfig:
    """
    Immutable, validated core application configuration.

    Deliberately contains ONLY generic, publisher-agnostic settings.
    Publisher credentials and platform-specific options must NOT be
    added here — see the module docstring for rationale.

    Attributes:
        base_dir: Root directory of the project (used to resolve
            relative paths such as workspace/, published/, failed/, logs/).
        workspace_dir: Directory containing media productions awaiting
            publication.
        published_dir: Directory where successfully published items
            are moved/recorded.
        failed_dir: Directory where failed publication attempts are
            moved/recorded.
        logs_dir: Directory for log file output.
        log_level: Logging verbosity, e.g. "INFO", "DEBUG".
    """

    base_dir: Path
    workspace_dir: Path
    published_dir: Path
    failed_dir: Path
    logs_dir: Path
    log_level: str


def ensure_environment_loaded(base_dir: Path | None = None) -> None:
    """
    Load a local .env file into the process environment, if present.

    This is intentionally separate from AppConfig loading. It exists
    purely so that both `load_config()` and individual publisher
    modules (e.g. `PeerTubeConfig.from_env()`) can rely on `os.environ`
    already being populated, without each of them re-implementing
    dotenv loading.

    Safe to call multiple times; subsequent calls are no-ops in
    practice since already-set environment variables take precedence
    and python-dotenv does not override existing values by default.

    Args:
        base_dir: Directory to look for a .env file in. Defaults to
            the directory containing this file.
    """
    resolved_base = base_dir or Path(__file__).resolve().parent
    env_file = resolved_base / ".env"

    if not env_file.exists():
        return

    if load_dotenv is None:
        logger.warning(
            "Found .env file at %s but python-dotenv is not installed; "
            "install it or set environment variables manually.",
            env_file,
        )
        return

    load_dotenv(dotenv_path=env_file)
    logger.debug("Loaded environment from %s", env_file)


def load_config(base_dir: Path | None = None) -> AppConfig:
    """
    Load core application configuration.

    Ensures the environment is loaded, then reads only generic,
    publisher-agnostic settings into an AppConfig instance.

    Args:
        base_dir: Root directory of the project. Defaults to the
            directory containing this file.

    Returns:
        A validated, immutable AppConfig instance.
    """
    resolved_base = base_dir or Path(__file__).resolve().parent
    ensure_environment_loaded(resolved_base)

    workspace_dir = resolved_base / os.environ.get("WORKSPACE_DIR", "workspace")
    published_dir = resolved_base / os.environ.get("PUBLISHED_DIR", "published")
    failed_dir = resolved_base / os.environ.get("FAILED_DIR", "failed")
    logs_dir = resolved_base / os.environ.get("LOGS_DIR", "logs")
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    for directory in (workspace_dir, published_dir, failed_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        base_dir=resolved_base,
        workspace_dir=workspace_dir,
        published_dir=published_dir,
        failed_dir=failed_dir,
        logs_dir=logs_dir,
        log_level=log_level,
    )
