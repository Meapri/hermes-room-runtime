"""Environment-driven entry point for the long-running Hub worker host."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from .hub import HubAgentJobClient
from .runtime import HermesJobRuntime, RuntimeConfig
from .worker import SUPPORTED_TASK_KINDS, HubJobWorker

_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _integer(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc


def _optional_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


def _provider_env(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        if not path.is_file() or path.stat().st_size > 65_536:
            raise ValueError("provider environment file is invalid")
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("provider environment file is unavailable") from exc
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (
            not separator
            or not _ENV_KEY.fullmatch(key)
            or key.startswith(("HUB_", "ACTVERSE_"))
            or key in values
        ):
            raise ValueError("provider environment file contains an invalid key")
        if not value or any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("provider environment file contains an invalid value")
        values[key] = value
    return values


def build_worker_from_environment() -> tuple[HubJobWorker, float]:
    provider_env_file = _optional_path("HERMES_PROVIDER_ENV_FILE")
    task_kinds = tuple(
        item.strip()
        for item in os.environ.get(
            "HUB_WORKER_TASK_KINDS",
            ",".join(SUPPORTED_TASK_KINDS),
        ).split(",")
        if item.strip()
    )
    runtime = HermesJobRuntime(
        RuntimeConfig(
            image=_required("HERMES_RUNTIME_IMAGE"),
            state_root=Path(_required("HERMES_STATE_ROOT")),
            slots=_integer("HERMES_SLOTS", 2),
            runtime_id=os.environ.get("HERMES_RUNTIME_ID", "actverse"),
            config_path=_optional_path("HERMES_CONFIG_PATH"),
            auth_path=_optional_path("HERMES_AUTH_PATH"),
            provider_env=_provider_env(provider_env_file),
            network_mode=os.environ.get("HERMES_NETWORK_MODE", "none"),
            memory=os.environ.get("HERMES_MEMORY", "2g"),
            cpus=os.environ.get("HERMES_CPUS", "2"),
            pids_limit=_integer("HERMES_PIDS_LIMIT", 512),
        )
    )
    client = HubAgentJobClient(
        base_url=_required("HUB_BASE_URL"),
        token_file=Path(_required("HUB_WORKER_TOKEN_FILE")),
        timeout_seconds=_float("HUB_TIMEOUT_SECONDS", 15),
    )
    worker = HubJobWorker(
        client=client,
        runtime=runtime,
        worker_id=_required("HUB_WORKER_ID"),
        task_kinds=task_kinds,
        lease_seconds=_integer("HUB_LEASE_SECONDS", 120),
        job_timeout_seconds=_float("HERMES_JOB_TIMEOUT_SECONDS", 300),
    )
    return worker, _float("HUB_POLL_SECONDS", 2)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    worker, poll_seconds = build_worker_from_environment()
    asyncio.run(worker.serve(poll_seconds=poll_seconds))


if __name__ == "__main__":
    main()
