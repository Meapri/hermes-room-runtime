"""Fixed-size, stateless Hermes worker pool for Actverse jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .models import JobRequest, JobResult, JobStatus, RuntimeHealth, validate_hub_result

_SAFE_RUNTIME_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SAFE_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SAFE_NETWORK = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
_RESULT_LIMIT = 256 * 1024
_OUTPUT_TAIL = 4096
_RESTRICTED_NETWORK_LABEL = "com.actverse.hermes-egress"
_RESTRICTED_NETWORK_CONTRACT = "restricted-v1"


def _docker_binary() -> str | None:
    for candidate in ("/opt/homebrew/bin/docker", "/usr/local/bin/docker"):
        if Path(candidate).is_file():
            return candidate
    return shutil.which("docker")


def _validate_env(values: Mapping[str, str]) -> None:
    for key, value in values.items():
        if not _SAFE_ENV_KEY.fullmatch(key):
            raise ValueError(f"invalid environment key: {key!r}")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"environment value for {key} contains a forbidden character")


@dataclass(frozen=True)
class RuntimeConfig:
    image: str
    state_root: Path
    slots: int = 2
    runtime_id: str = "actverse"
    config_path: Path | None = None
    auth_path: Path | None = None
    read_only_mounts: Mapping[str, str] = field(default_factory=dict)
    provider_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    nonsecret_env: Mapping[str, str] = field(default_factory=dict)
    network_mode: str = "none"
    memory: str = "2g"
    cpus: str = "2"
    pids_limit: int = 512
    retain_job_dirs: bool = False
    include_output_tails: bool = False
    docker: str | None = field(default_factory=_docker_binary)
    require_restricted_network: bool = False

    def __post_init__(self) -> None:
        if not self.image.strip() or any(ch.isspace() for ch in self.image):
            raise ValueError("image must be a non-empty Docker image reference")
        if not 1 <= self.slots <= 32:
            raise ValueError("slots must be between 1 and 32")
        if not _SAFE_RUNTIME_ID.fullmatch(self.runtime_id):
            raise ValueError("runtime_id contains unsafe characters")
        if self.network_mode == "host" or not _SAFE_NETWORK.fullmatch(self.network_mode):
            raise ValueError("network_mode must be none, bridge, or a safe named Docker network")
        if self.require_restricted_network and self.network_mode in {
            "none",
            "bridge",
            "default",
            "host",
        }:
            raise ValueError("require_restricted_network needs a dedicated named Docker network")
        if not 16 <= self.pids_limit <= 4096:
            raise ValueError("pids_limit must be between 16 and 4096")
        if self.config_path is not None and not self.config_path.is_file():
            raise ValueError("config_path must point to a readable file")
        if self.auth_path is not None and not self.auth_path.is_file():
            raise ValueError("auth_path must point to a readable file")
        _validate_env(self.provider_env)
        _validate_env(self.nonsecret_env)
        state_root = self.state_root.resolve()
        for host, container in self.read_only_mounts.items():
            host_path = Path(host).resolve()
            if not Path(host).is_absolute() or not host_path.is_dir():
                raise ValueError(f"read-only mount host path is invalid: {host!r}")
            target = PurePosixPath(container)
            if len(target.parts) < 3 or target.parts[:2] != ("/", "repos") or ":" in container:
                raise ValueError(f"read-only mount target is invalid: {container!r}")
            if state_root.is_relative_to(host_path):
                raise ValueError("state_root must not be inside a read-only repository mount")


class HermesJobRuntime:
    """A queue of long-lived containers with fresh, disposable state per job.

    Containers are only execution shells. Every call gets a new HOME, HERMES_HOME,
    evidence file, work directory and result contract; no conversation is resumed.
    """

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self._ensure_lock = asyncio.Lock()
        self._available: asyncio.Queue[int] | None = None
        self._ensured = False
        self._running_slots: set[int] = set()
        self._quarantined_slots: set[int] = set()
        self._lifecycle_generation = 0
        self._stopped = False
        self._network_id: str | None = None

    @property
    def enabled(self) -> bool:
        return self.config.docker is not None and self.config.slots > 0

    def _slot_name(self, slot: int) -> str:
        return f"hermes_runtime_{self.config.runtime_id}_{slot}"

    def _slot_root(self, slot: int) -> Path:
        return self.config.state_root / "slots" / f"slot-{slot}"

    def _spec_hash(self, network_id: str | None = None) -> str:
        spec = {
            "image": self.config.image,
            "network": self.config.network_mode,
            "network_id": network_id,
            "restricted_network": self.config.require_restricted_network,
            "memory": self.config.memory,
            "cpus": self.config.cpus,
            "pids": self.config.pids_limit,
            "host_group": os.getgid(),
            "mounts": sorted(
                (str(Path(host).resolve()), container)
                for host, container in self.config.read_only_mounts.items()
            ),
        }
        return hashlib.sha256(
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]

    async def _docker(self, *args: str, timeout: float = 30.0) -> tuple[int | None, bytes, bytes]:
        if self.config.docker is None:
            return None, b"", b"docker unavailable"
        proc = await asyncio.create_subprocess_exec(
            self.config.docker,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return None, b"", b"timeout"
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode, stdout or b"", stderr or b""

    async def probe(self) -> bool:
        if not self.enabled:
            return False
        returncode, _, _ = await self._docker("info", timeout=20)
        return returncode == 0

    async def _restricted_network_id(self) -> str | None:
        if not self.config.require_restricted_network:
            return None
        returncode, stdout, _ = await self._docker(
            "network",
            "inspect",
            "-f",
            (f'{{{{.Id}}}}|{{{{.Internal}}}}|{{{{index .Labels "{_RESTRICTED_NETWORK_LABEL}"}}}}'),
            self.config.network_mode,
            timeout=15,
        )
        if returncode != 0:
            return None
        network_id, separator, remainder = stdout.decode("utf-8", "replace").strip().partition("|")
        internal, second_separator, contract = remainder.partition("|")
        if (
            not separator
            or not second_separator
            or not network_id
            or internal != "true"
            or contract != _RESTRICTED_NETWORK_CONTRACT
        ):
            return None
        return network_id

    async def _container_has_exact_restricted_network(
        self,
        name: str,
        network_id: str | None,
    ) -> bool:
        if not self.config.require_restricted_network:
            return True
        if network_id is None:
            return False
        returncode, stdout, _ = await self._docker(
            "inspect",
            "-f",
            "{{json .NetworkSettings.Networks}}",
            name,
            timeout=15,
        )
        if returncode != 0:
            return False
        try:
            networks = json.loads(stdout)
        except (UnicodeDecodeError, ValueError):
            return False
        return (
            isinstance(networks, dict)
            and set(networks) == {self.config.network_mode}
            and isinstance(networks[self.config.network_mode], dict)
            and networks[self.config.network_mode].get("NetworkID") == network_id
        )

    async def _ensure_slot(self, slot: int) -> bool:
        name = self._slot_name(slot)
        network_id = await self._restricted_network_id()
        if self.config.require_restricted_network and network_id is None:
            return False
        if self._network_id is not None and self._network_id != network_id:
            return False
        expected = self._spec_hash(network_id)
        returncode, stdout, _ = await self._docker(
            "inspect",
            "-f",
            '{{.State.Status}}|{{index .Config.Labels "hermes-runtime-spec"}}',
            name,
            timeout=15,
        )
        if returncode == 0:
            state, _, found_spec = stdout.decode("utf-8", "replace").strip().partition("|")
            if found_spec != expected:
                if not await self._purge_slot(slot):
                    return False
            elif state == "running":
                if await self._container_has_exact_restricted_network(name, network_id):
                    return True
                if not await self._purge_slot(slot):
                    return False
            else:
                start_code, _, _ = await self._docker("start", name, timeout=30)
                if start_code == 0 and await self._container_has_exact_restricted_network(
                    name,
                    network_id,
                ):
                    return True
                if not await self._purge_slot(slot):
                    return False

        slot_root = self._slot_root(slot)
        jobs_root = slot_root / "jobs"
        jobs_root.mkdir(parents=True, exist_ok=True, mode=0o770)
        os.chmod(slot_root, 0o770)
        os.chmod(jobs_root, 0o770)
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "hermes-room-runtime=1",
            "--label",
            f"hermes-runtime-id={self.config.runtime_id}",
            "--label",
            f"hermes-runtime-spec={expected}",
            f"--memory={self.config.memory}",
            f"--cpus={self.config.cpus}",
            f"--pids-limit={self.config.pids_limit}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--group-add",
            str(os.getgid()),
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--tmpfs",
            "/run:rw,noexec,nosuid,size=64m",
            "-v",
            f"{slot_root.resolve()}:/runtime:rw",
            "-w",
            "/runtime",
            "--network",
            self.config.network_mode,
        ]
        for host, container in sorted(self.config.read_only_mounts.items()):
            args += ["-v", f"{Path(host).resolve()}:{container}:ro"]
        for key, value in sorted(self.config.nonsecret_env.items()):
            args += ["-e", f"{key}={value}"]
        args += ["--entrypoint", "tail", self.config.image, "-f", "/dev/null"]
        returncode, _, _ = await self._docker(*args, timeout=90)
        if returncode != 0:
            return False
        if not await self._container_has_exact_restricted_network(name, network_id):
            await self._purge_slot(slot)
            return False
        return True

    async def ensure(self) -> bool:
        if not await self.probe():
            return False
        network_id = await self._restricted_network_id()
        if self.config.require_restricted_network and network_id is None:
            return False
        async with self._ensure_lock:
            if self._network_id is not None and self._network_id != network_id:
                return False
            self._network_id = network_id
            self._stopped = False
            self.config.state_root.mkdir(parents=True, exist_ok=True, mode=0o770)
            if stat.S_IMODE(self.config.state_root.stat().st_mode) != 0o770:
                os.chmod(self.config.state_root, 0o770)
            if self._available is None:
                self._available = asyncio.Queue()
            for slot in range(self.config.slots):
                if slot in self._running_slots:
                    if await self._container_has_exact_restricted_network(
                        self._slot_name(slot),
                        network_id,
                    ):
                        continue
                    self._running_slots.discard(slot)
                    self._quarantined_slots.add(slot)
                if slot in self._quarantined_slots:
                    if not await self._purge_slot(slot):
                        continue
                    self._quarantined_slots.discard(slot)
                if await self._ensure_slot(slot):
                    self._running_slots.add(slot)
                    self._available.put_nowait(slot)
            self._ensured = bool(self._running_slots)
            return self._ensured

    def _prepare_job(self, slot: int, request: JobRequest) -> tuple[Path, Path]:
        token = uuid.uuid4().hex[:12]
        job_root = self._slot_root(slot) / "jobs" / f"{request.job_id}-{token}"
        hermes_home = job_root / "hermes"
        work = job_root / "work"
        input_dir = job_root / "input"
        home = job_root / "home"
        job_root.mkdir(parents=True, mode=0o770)
        os.chmod(job_root, 0o770)
        for directory in (hermes_home, work, input_dir, home):
            directory.mkdir(mode=0o770)
            os.chmod(directory, 0o770)
        if self.config.config_path is not None:
            shutil.copyfile(self.config.config_path, hermes_home / "config.yaml")
            os.chmod(hermes_home / "config.yaml", 0o640)
        if self.config.auth_path is not None:
            shutil.copyfile(self.config.auth_path, hermes_home / "auth.json")
            os.chmod(hermes_home / "auth.json", 0o640)
        evidence_path = input_dir / "evidence.json"
        evidence_path.write_bytes(request.evidence_bytes)
        os.chmod(evidence_path, 0o640)
        return job_root, work

    def _write_env_file(self, job_root: Path, request: JobRequest) -> Path:
        env_dir = self.config.state_root / ".exec-env"
        env_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Every slot root is mounted at /runtime; jobs live at /runtime/jobs/*.
        relative = job_root.relative_to(job_root.parent.parent)
        container_job = Path("/runtime") / relative
        values = {
            "HOME": str(container_job / "home"),
            "HERMES_HOME": str(container_job / "hermes"),
            "HERMES_SKIP_UPDATES": "1",
            "ACTVERSE_JOB_ID": request.job_id,
            "ACTVERSE_TASK_KIND": request.task_kind,
            "ACTVERSE_EVIDENCE_PATH": str(container_job / "input" / "evidence.json"),
            **self.config.provider_env,
        }
        _validate_env(values)
        path = env_dir / f"{request.job_id}-{uuid.uuid4().hex}.env"
        path.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        return path

    @staticmethod
    def _prompt(request: JobRequest) -> str:
        return (
            "# Actverse isolated job contract\n"
            f"job_id={request.job_id} task_kind={request.task_kind}\n"
            "This is a stateless run. Do not search or resume earlier conversations or memories.\n"
            "The host fetched a bounded, read-only Evidence Hub bundle and wrote it to "
            "$ACTVERSE_EVIDENCE_PATH. Treat missing, stale, rejected, or unknown evidence as "
            "uncertainty, not as proof of an outage. Never invent raw customer identifiers.\n"
            "Mounted product repositories are read-only. Do not attempt to change them.\n"
            "Write the requested JSON object to result.json in the current working directory. "
            "Do not place credentials, raw customer identifiers, or unrelated source "
            "payloads in it.\n\n" + request.prompt
        )

    @staticmethod
    def _load_result(path: Path) -> tuple[dict[str, Any] | None, str | None]:
        try:
            file_stat = path.lstat()
        except OSError:
            return None, "result-missing"
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > _RESULT_LIMIT:
            return None, "result-invalid"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None, "result-invalid"
        if not isinstance(value, dict):
            return None, "result-invalid"
        try:
            validate_hub_result(value)
        except ValueError:
            return None, "result-policy-rejected"
        return value, None

    async def _purge_slot(self, slot: int) -> bool:
        """Remove a slot and verify that no old execution container survived."""

        name = self._slot_name(slot)
        await self._docker("rm", "-f", name, timeout=40)
        inspect_code, _, inspect_stderr = await self._docker("inspect", name, timeout=15)
        if inspect_code != 1:
            return False
        lowered = inspect_stderr.lower()
        return b"no such object" in lowered or b"no such container" in lowered

    async def _recycle_slot(
        self,
        slot: int,
        generation: int,
        job_root: Path | None,
    ) -> None:
        """Destroy all job processes, delete state, then create a clean execution shell."""

        async with self._ensure_lock:
            stale_generation = generation != self._lifecycle_generation or self._stopped
            purged = await self._purge_slot(slot)
            self._running_slots.discard(slot)
            if job_root is not None and not self.config.retain_job_dirs:
                shutil.rmtree(job_root, ignore_errors=True)
            if not purged:
                self._quarantined_slots.add(slot)
                self._ensured = bool(self._running_slots)
                return
            self._quarantined_slots.discard(slot)
            if stale_generation:
                self._ensured = bool(self._running_slots)
                return
            if await self._ensure_slot(slot):
                self._running_slots.add(slot)
                if self._available is not None:
                    self._available.put_nowait(slot)
            self._ensured = bool(self._running_slots)

    async def run(self, request: JobRequest) -> JobResult:
        started = time.monotonic()
        if not await self.ensure() or self._available is None:
            return JobResult(
                job_id=request.job_id,
                task_kind=request.task_kind,
                status=JobStatus.UNAVAILABLE,
                result=None,
                evidence_sha256=request.evidence_sha256,
                slot=-1,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_code="runtime-unavailable",
            )
        queue_timeout = request.timeout_seconds - (time.monotonic() - started)
        if queue_timeout <= 0:
            return JobResult(
                request.job_id,
                request.task_kind,
                JobStatus.TIMED_OUT,
                None,
                request.evidence_sha256,
                -1,
                int((time.monotonic() - started) * 1000),
                error_code="queue-timeout",
                timed_out=True,
            )
        try:
            slot = await asyncio.wait_for(self._available.get(), timeout=queue_timeout)
        except TimeoutError:
            return JobResult(
                request.job_id,
                request.task_kind,
                JobStatus.TIMED_OUT,
                None,
                request.evidence_sha256,
                -1,
                int((time.monotonic() - started) * 1000),
                error_code="queue-timeout",
                timed_out=True,
            )
        generation = self._lifecycle_generation
        job_root: Path | None = None
        env_file: Path | None = None
        try:
            async with self._ensure_lock:
                generation = self._lifecycle_generation
                slot_ready = not self._stopped and await self._ensure_slot(slot)
            if not slot_ready:
                return JobResult(
                    request.job_id,
                    request.task_kind,
                    JobStatus.UNAVAILABLE,
                    None,
                    request.evidence_sha256,
                    slot,
                    int((time.monotonic() - started) * 1000),
                    error_code="slot-unavailable",
                )
            job_root, work = self._prepare_job(slot, request)
            env_file = self._write_env_file(job_root, request)
            container_job = f"/runtime/jobs/{job_root.name}"
            execution_timeout = request.timeout_seconds - (time.monotonic() - started)
            if execution_timeout <= 0:
                return JobResult(
                    request.job_id,
                    request.task_kind,
                    JobStatus.TIMED_OUT,
                    None,
                    request.evidence_sha256,
                    slot,
                    int((time.monotonic() - started) * 1000),
                    error_code="queue-timeout",
                    timed_out=True,
                )
            if not await self._container_has_exact_restricted_network(
                self._slot_name(slot),
                self._network_id,
            ):
                return JobResult(
                    request.job_id,
                    request.task_kind,
                    JobStatus.UNAVAILABLE,
                    None,
                    request.evidence_sha256,
                    slot,
                    int((time.monotonic() - started) * 1000),
                    error_code="restricted-network-mismatch",
                )
            returncode, stdout, stderr = await self._docker(
                "exec",
                "--env-file",
                str(env_file),
                "-w",
                f"{container_job}/work",
                self._slot_name(slot),
                "hermes",
                "--yolo",
                "--ignore-rules",
                "--usage-file",
                f"{container_job}/work/usage.json",
                "-z",
                self._prompt(request),
                timeout=execution_timeout,
            )
            timed_out = returncode is None
            if not timed_out:
                # Hermes may use a restrictive umask. The fixed, non-symlink check lets the
                # host worker read the group-owned result without making it world-readable.
                if await self._container_has_exact_restricted_network(
                    self._slot_name(slot),
                    self._network_id,
                ):
                    await self._docker(
                        "exec",
                        "-w",
                        f"{container_job}/work",
                        self._slot_name(slot),
                        "sh",
                        "-c",
                        (
                            "if [ -f result.json ] && [ ! -L result.json ]; "
                            f"then chgrp {os.getgid()} result.json && chmod 0640 result.json; fi"
                        ),
                        timeout=10,
                    )
                else:
                    returncode = 125
                    stderr = b"restricted network membership changed"
            result, result_error = self._load_result(work / "result.json")
            if timed_out:
                status = JobStatus.TIMED_OUT
                error_code = "job-timeout"
            elif returncode != 0:
                status = JobStatus.FAILED
                error_code = "hermes-failed"
            elif result_error:
                status = JobStatus.INCONCLUSIVE
                error_code = result_error
            else:
                status = JobStatus.SUCCEEDED
                error_code = None
            return JobResult(
                job_id=request.job_id,
                task_kind=request.task_kind,
                status=status,
                result=result,
                evidence_sha256=request.evidence_sha256,
                slot=slot,
                duration_ms=int((time.monotonic() - started) * 1000),
                returncode=returncode,
                error_code=error_code,
                timed_out=timed_out,
                stdout_tail=(
                    stdout[-_OUTPUT_TAIL:].decode("utf-8", "replace")
                    if self.config.include_output_tails
                    else ""
                ),
                stderr_tail=(
                    stderr[-_OUTPUT_TAIL:].decode("utf-8", "replace")
                    if self.config.include_output_tails
                    else ""
                ),
            )
        finally:
            if env_file is not None:
                env_file.unlink(missing_ok=True)
            recycle = asyncio.create_task(self._recycle_slot(slot, generation, job_root))
            try:
                await asyncio.shield(recycle)
            except asyncio.CancelledError:
                await recycle
                raise

    async def health(self) -> RuntimeHealth:
        if not await self.probe():
            return RuntimeHealth(False, self.config.slots, 0, "docker-unavailable")
        running = 0
        for slot in range(self.config.slots):
            returncode, stdout, _ = await self._docker(
                "inspect", "-f", "{{.State.Status}}", self._slot_name(slot), timeout=10
            )
            if returncode == 0 and stdout.strip() == b"running":
                running += 1
        return RuntimeHealth(
            ready=running == self.config.slots,
            configured_slots=self.config.slots,
            running_slots=running,
            reason=None if running == self.config.slots else "slots-not-ready",
        )

    def prune_stale_jobs(self, max_age_seconds: int = 86400) -> int:
        if max_age_seconds < 60:
            raise ValueError("max_age_seconds must be at least 60")
        cutoff = time.time() - max_age_seconds
        removed = 0
        slots_root = self.config.state_root / "slots"
        if not slots_root.exists():
            return 0
        for slot_root in slots_root.glob("slot-*/jobs"):
            for job_dir in slot_root.iterdir():
                try:
                    if job_dir.is_dir() and job_dir.stat().st_mtime < cutoff:
                        shutil.rmtree(job_dir)
                        removed += 1
                except OSError:
                    continue
        return removed

    async def shutdown(self) -> None:
        async with self._ensure_lock:
            self._lifecycle_generation += 1
            self._stopped = True
            for slot in range(self.config.slots):
                if not await self._purge_slot(slot):
                    self._quarantined_slots.add(slot)
                else:
                    self._quarantined_slots.discard(slot)
            self._ensured = False
            self._running_slots.clear()
            self._available = None
            self._network_id = None
