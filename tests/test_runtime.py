from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from hermes_room_runtime import HermesJobRuntime, JobRequest, JobStatus, RuntimeConfig


def _config(tmp_path: Path, **overrides) -> RuntimeConfig:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: {}\n", encoding="utf-8")
    values = {
        "image": "hermes-room-runtime:test",
        "state_root": tmp_path / "state",
        "slots": 1,
        "config_path": config_path,
        "provider_env": {"OPENAI_API_KEY": "secret-for-test"},
        "docker": "/usr/bin/true",
    }
    values.update(overrides)
    return RuntimeConfig(**values)


def test_slot_creation_is_hardened_and_secret_free(tmp_path: Path) -> None:
    runtime = HermesJobRuntime(_config(tmp_path))
    calls: list[tuple[str, ...]] = []

    async def fake(*args: str, timeout: float = 30):
        calls.append(args)
        if args[0] == "inspect":
            return 1, b"", b"not found"
        return 0, b"", b""

    runtime._docker = fake  # type: ignore[method-assign]
    assert asyncio.run(runtime._ensure_slot(0)) is True
    run = next(args for args in calls if args[0] == "run")
    joined = " ".join(run)
    assert "--cap-drop=ALL" in run
    assert "--security-opt=no-new-privileges" in run
    assert "--read-only" in run
    assert "--network none" in joined
    assert "host.docker.internal" not in joined
    assert "secret-for-test" not in joined
    assert run[-5:] == (
        "--entrypoint",
        "tail",
        "hermes-room-runtime:test",
        "-f",
        "/dev/null",
    )


def test_run_uses_ephemeral_secret_and_cleans_job_state(tmp_path: Path) -> None:
    runtime = HermesJobRuntime(_config(tmp_path))
    inspect_count = 0
    env_path_seen: Path | None = None

    async def fake(*args: str, timeout: float = 30):
        nonlocal inspect_count, env_path_seen
        if args[0] == "info":
            return 0, b"", b""
        if args[0] == "inspect":
            inspect_count += 1
            if inspect_count == 1:
                return 1, b"", b"not found"
            return 0, f"running|{runtime._spec_hash()}\n".encode(), b""
        if args[0] == "run":
            return 0, b"", b""
        if args[0] == "exec":
            env_path_seen = Path(args[args.index("--env-file") + 1])
            assert env_path_seen.stat().st_mode & 0o777 == 0o600
            assert "OPENAI_API_KEY=secret-for-test" in env_path_seen.read_text()
            container_work = args[args.index("-w") + 1]
            job_name = Path(container_work).parts[-2]
            host_work = runtime._slot_root(0) / "jobs" / job_name / "work"
            (host_work / "result.json").write_text('{"summary":"ok"}', encoding="utf-8")
            return 0, b"done", b""
        return 0, b"", b""

    runtime._docker = fake  # type: ignore[method-assign]
    result = asyncio.run(
        runtime.run(
            JobRequest(
                job_id="incident-42",
                task_kind="incident-diagnosis",
                prompt="diagnose",
                evidence={"status": "unknown"},
            )
        )
    )
    assert result.status is JobStatus.SUCCEEDED
    assert result.result == {"summary": "ok"}
    assert env_path_seen is not None and not env_path_seen.exists()
    jobs = runtime._slot_root(0) / "jobs"
    assert list(jobs.iterdir()) == []


def test_timeout_restarts_slot_and_returns_it_to_pool(tmp_path: Path) -> None:
    runtime = HermesJobRuntime(_config(tmp_path))
    inspect_count = 0
    restarted = False

    async def fake(*args: str, timeout: float = 30):
        nonlocal inspect_count, restarted
        if args[0] == "info":
            return 0, b"", b""
        if args[0] == "inspect":
            inspect_count += 1
            if inspect_count == 1:
                return 1, b"", b""
            return 0, f"running|{runtime._spec_hash()}\n".encode(), b""
        if args[0] == "exec":
            return None, b"", b"timeout"
        if args[0] == "restart":
            restarted = True
        return 0, b"", b""

    runtime._docker = fake  # type: ignore[method-assign]
    result = asyncio.run(
        runtime.run(JobRequest("job-1", "diagnosis", "diagnose", timeout_seconds=1))
    )
    assert result.status is JobStatus.TIMED_OUT
    assert result.timed_out is True
    assert restarted is True
    assert runtime._available is not None and runtime._available.qsize() == 1


def test_result_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"secret":"no"}', encoding="utf-8")
    link = tmp_path / "result.json"
    os.symlink(target, link)
    result, error = HermesJobRuntime._load_result(link)
    assert result is None and error == "result-invalid"


def test_mounts_are_limited_to_repo_directories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="mount target"):
        _config(tmp_path, read_only_mounts={str(repo): "/etc/override"})

    state = repo / "runtime-state"
    with pytest.raises(ValueError, match="state_root"):
        _config(
            tmp_path,
            state_root=state,
            read_only_mounts={str(repo): "/repos/company"},
        )
