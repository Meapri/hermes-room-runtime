from __future__ import annotations

import asyncio
import json
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
    assert run[run.index("--group-add") + 1] == str(os.getgid())
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
    removed: list[str] = []

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
        if args[0] == "rm":
            removed.append(args[-1])
            return 0, b"", b""
        if args[0] == "exec" and "--env-file" in args:
            env_path_seen = Path(args[args.index("--env-file") + 1])
            assert env_path_seen.stat().st_mode & 0o777 == 0o600
            assert "OPENAI_API_KEY=secret-for-test" in env_path_seen.read_text()
            container_work = args[args.index("-w") + 1]
            job_name = Path(container_work).parts[-2]
            host_work = runtime._slot_root(0) / "jobs" / job_name / "work"
            (host_work / "result.json").write_text('{"summary":"ok"}', encoding="utf-8")
            return 0, b"done", b""
        if args[0] == "exec":
            assert args[-3:] == (
                "sh",
                "-c",
                (
                    "if [ -f result.json ] && [ ! -L result.json ]; "
                    f"then chgrp {os.getgid()} result.json && chmod 0640 result.json; fi"
                ),
            )
            return 0, b"", b""
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
    assert removed == ["hermes_runtime_actverse_0"]
    jobs = runtime._slot_root(0) / "jobs"
    assert list(jobs.iterdir()) == []


def test_job_directories_are_group_private_even_with_restrictive_umask(tmp_path: Path) -> None:
    runtime = HermesJobRuntime(_config(tmp_path))
    original_umask = os.umask(0o077)
    try:
        job_root, _ = runtime._prepare_job(
            0,
            JobRequest("job-private", "incident-diagnosis", "diagnose"),
        )
    finally:
        os.umask(original_umask)

    assert job_root.stat().st_mode & 0o777 == 0o770
    assert all(path.stat().st_mode & 0o777 == 0o770 for path in job_root.iterdir())


def test_timeout_replaces_slot_and_returns_it_to_pool(tmp_path: Path) -> None:
    runtime = HermesJobRuntime(_config(tmp_path))
    container_exists = False
    removed = False

    async def fake(*args: str, timeout: float = 30):
        nonlocal container_exists, removed
        if args[0] == "info":
            return 0, b"", b""
        if args[0] == "inspect":
            if not container_exists:
                return 1, b"", b"Error: No such object"
            return 0, f"running|{runtime._spec_hash()}\n".encode(), b""
        if args[0] == "run":
            container_exists = True
            return 0, b"", b""
        if args[0] == "exec":
            return None, b"", b"timeout"
        if args[0] == "rm":
            container_exists = False
            removed = True
            return 0, b"", b""
        return 0, b"", b""

    runtime._docker = fake  # type: ignore[method-assign]
    result = asyncio.run(
        runtime.run(JobRequest("job-1", "diagnosis", "diagnose", timeout_seconds=1))
    )
    assert result.status is JobStatus.TIMED_OUT
    assert result.timed_out is True
    assert removed is True
    assert runtime._available is not None and runtime._available.qsize() == 1


def test_cancelled_job_replaces_slot_before_propagating_cancellation(tmp_path: Path) -> None:
    runtime = HermesJobRuntime(_config(tmp_path))
    entered = asyncio.Event()
    never = asyncio.Event()
    container_exists = False
    created = 0
    removed = 0

    async def fake(*args: str, timeout: float = 30):
        nonlocal container_exists, created, removed
        if args[0] == "info":
            return 0, b"", b""
        if args[0] == "inspect":
            if container_exists:
                return 0, f"running|{runtime._spec_hash()}\n".encode(), b""
            return 1, b"", b"Error: No such object"
        if args[0] == "run":
            container_exists = True
            created += 1
            return 0, b"", b""
        if args[0] == "rm":
            container_exists = False
            removed += 1
            return 0, b"", b""
        if args[0] == "exec" and "--env-file" in args:
            entered.set()
            await never.wait()
        return 0, b"", b""

    runtime._docker = fake  # type: ignore[method-assign]

    async def scenario() -> None:
        task = asyncio.create_task(runtime.run(JobRequest("job-cancel", "diagnosis", "diagnose")))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert removed == 1
    assert created == 2
    assert runtime._available is not None and runtime._available.qsize() == 1


def test_shutdown_then_startup_creates_a_fresh_generation(tmp_path: Path) -> None:
    runtime = HermesJobRuntime(_config(tmp_path))
    container_exists = False
    created = 0

    async def fake(*args: str, timeout: float = 30):
        nonlocal container_exists, created
        if args[0] == "info":
            return 0, b"", b""
        if args[0] == "inspect":
            if container_exists:
                return 0, f"running|{runtime._spec_hash()}\n".encode(), b""
            return 1, b"", b"Error: No such object"
        if args[0] == "run":
            container_exists = True
            created += 1
            return 0, b"", b""
        if args[0] == "rm":
            container_exists = False
            return 0, b"", b""
        return 0, b"", b""

    runtime._docker = fake  # type: ignore[method-assign]

    async def scenario() -> None:
        assert await runtime.ensure() is True
        await runtime.shutdown()
        assert await runtime.ensure() is True

    asyncio.run(scenario())
    assert created == 2


def test_restricted_network_fails_closed_and_binds_identity_into_slot_spec(
    tmp_path: Path,
) -> None:
    runtime = HermesJobRuntime(
        _config(
            tmp_path,
            network_mode="actverse-hermes-egress",
            require_restricted_network=True,
        )
    )
    calls: list[tuple[str, ...]] = []
    container_exists = False

    async def valid(*args: str, timeout: float = 30):
        nonlocal container_exists
        calls.append(args)
        if args[:2] == ("network", "inspect"):
            return 0, b"network-id-1|true|restricted-v1\n", b""
        if args[0] == "inspect":
            if "{{json .NetworkSettings.Networks}}" in args:
                return (
                    0,
                    b'{"actverse-hermes-egress":{"NetworkID":"network-id-1"}}\n',
                    b"",
                )
            if container_exists:
                return 0, f"running|{runtime._spec_hash('network-id-1')}\n".encode(), b""
            return 1, b"", b"Error: No such object"
        if args[0] == "run":
            container_exists = True
        return 0, b"", b""

    runtime._docker = valid  # type: ignore[method-assign]
    assert asyncio.run(runtime._ensure_slot(0)) is True
    run = next(args for args in calls if args[0] == "run")
    assert "--network" in run
    assert run[run.index("--network") + 1] == "actverse-hermes-egress"
    assert f"hermes-runtime-spec={runtime._spec_hash('network-id-1')}" in run

    async def not_internal(*args: str, timeout: float = 30):
        if args[:2] == ("network", "inspect"):
            return 0, b"network-id-1|false|restricted-v1\n", b""
        raise AssertionError("slot creation must not run for an unrestricted network")

    runtime._docker = not_internal  # type: ignore[method-assign]
    assert asyncio.run(runtime._ensure_slot(1)) is False


def test_restricted_network_identity_change_requires_runtime_restart(tmp_path: Path) -> None:
    runtime = HermesJobRuntime(
        _config(
            tmp_path,
            network_mode="actverse-hermes-egress",
            require_restricted_network=True,
        )
    )
    network_id = "network-id-1"
    container_exists = False

    async def fake(*args: str, timeout: float = 30):
        nonlocal container_exists
        if args[0] == "info":
            return 0, b"", b""
        if args[:2] == ("network", "inspect"):
            return 0, f"{network_id}|true|restricted-v1\n".encode(), b""
        if args[0] == "inspect":
            if "{{json .NetworkSettings.Networks}}" in args:
                payload = {
                    "actverse-hermes-egress": {"NetworkID": network_id},
                }
                return 0, json.dumps(payload).encode(), b""
            if container_exists:
                return 0, f"running|{runtime._spec_hash(network_id)}\n".encode(), b""
            return 1, b"", b"Error: No such object"
        if args[0] == "run":
            container_exists = True
        if args[0] == "rm":
            container_exists = False
        return 0, b"", b""

    runtime._docker = fake  # type: ignore[method-assign]

    async def scenario() -> tuple[bool, bool]:
        nonlocal network_id
        first = await runtime.ensure()
        network_id = "network-id-2"
        second = await runtime.ensure()
        return first, second

    assert asyncio.run(scenario()) == (True, False)


def test_purge_requires_explicit_container_absence(tmp_path: Path) -> None:
    runtime = HermesJobRuntime(_config(tmp_path))

    async def uncertain(*args: str, timeout: float = 30):
        if args[0] in {"rm", "inspect"}:
            return None, b"", b"timeout"
        raise AssertionError(args)

    runtime._docker = uncertain  # type: ignore[method-assign]
    assert asyncio.run(runtime._purge_slot(0)) is False


def test_restricted_slot_rejects_an_additional_network(tmp_path: Path) -> None:
    runtime = HermesJobRuntime(
        _config(
            tmp_path,
            network_mode="actverse-hermes-egress",
            require_restricted_network=True,
        )
    )
    container_exists = True
    removed = False

    async def fake(*args: str, timeout: float = 30):
        nonlocal container_exists, removed
        if args[:2] == ("network", "inspect"):
            return 0, b"network-id-1|true|restricted-v1\n", b""
        if args[0] == "inspect" and "{{json .NetworkSettings.Networks}}" in args:
            if not container_exists:
                return 1, b"", b"Error: No such object"
            return (
                0,
                b'{"actverse-hermes-egress":{"NetworkID":"network-id-1"},'
                b'"bridge":{"NetworkID":"external-id"}}',
                b"",
            )
        if args[0] == "inspect":
            if container_exists:
                return 0, f"running|{runtime._spec_hash('network-id-1')}\n".encode(), b""
            return 1, b"", b"Error: No such object"
        if args[0] == "rm":
            removed = True
            container_exists = False
            return 0, b"", b""
        if args[0] == "run":
            container_exists = True
            return 0, b"", b""
        return 0, b"", b""

    runtime._docker = fake  # type: ignore[method-assign]
    assert asyncio.run(runtime._ensure_slot(0)) is False
    assert removed is True


@pytest.mark.parametrize("network", ["none", "bridge", "default", "host"])
def test_restricted_network_rejects_builtin_networks(tmp_path: Path, network: str) -> None:
    with pytest.raises(ValueError, match="dedicated named|safe named"):
        _config(
            tmp_path,
            network_mode=network,
            require_restricted_network=True,
        )


def test_result_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"secret":"no"}', encoding="utf-8")
    link = tmp_path / "result.json"
    os.symlink(target, link)
    result, error = HermesJobRuntime._load_result(link)
    assert result is None and error == "result-invalid"


def test_result_rejected_by_hub_policy_becomes_inconclusive_input(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text('{"email":"person@example.com"}', encoding="utf-8")
    result, error = HermesJobRuntime._load_result(result_path)
    assert result is None
    assert error == "result-policy-rejected"


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
