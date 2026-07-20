"""Ephemeral, digest-pinned Hercules executor for Hub-owned QA runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SENSITIVE = re.compile(
    r"(?:\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|\bBearer\s+\S+|\beyJ\S+\.)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HerculesConfig:
    image: str
    version: str
    state_root: Path
    provider_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    network_mode: str = "none"
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 1024
    docker: str | None = field(default_factory=lambda: shutil.which("docker"))

    def __post_init__(self) -> None:
        if _IMAGE.fullmatch(self.image) is None:
            raise ValueError("Hercules image must be pinned by a full sha256 digest")
        if not self.version or len(self.version) > 64:
            raise ValueError("Hercules version is invalid")
        if self.network_mode in {"none", "bridge", "default", "host"}:
            raise ValueError("Hercules requires the dedicated restricted network")
        if not 16 <= self.pids_limit <= 4096:
            raise ValueError("Hercules pids limit is invalid")
        for key, value in self.provider_env.items():
            if _SAFE_ENV_KEY.fullmatch(key) is None or any(ch in value for ch in "\x00\r\n"):
                raise ValueError("Hercules provider environment is invalid")

    @property
    def digest(self) -> str:
        return self.image.rsplit("@", 1)[1]


@dataclass(frozen=True)
class HerculesResult:
    execution_status: str
    test_outcome: str | None
    error_reason: str | None
    exit_code: int | None
    results: list[dict[str, Any]]
    result_sha256: str
    browser_version: str | None = None

    def completion(self, *, worker_id: str, config: HerculesConfig, runtime_version: str) -> dict:
        return {
            "worker_id": worker_id,
            "execution_status": self.execution_status,
            "test_outcome": self.test_outcome,
            "error_reason": self.error_reason,
            "exit_code": self.exit_code,
            "results": self.results,
            "result_sha256": self.result_sha256,
            "hercules_version": config.version,
            "container_digest": config.digest,
            "browser_version": self.browser_version,
            "runtime_version": runtime_version,
        }


class HerculesRuntime:
    def __init__(self, config: HerculesConfig) -> None:
        self.config = config

    async def _docker(self, *args: str, timeout: float) -> tuple[int | None, bytes, bytes]:
        if self.config.docker is None:
            return 127, b"", b"docker unavailable"
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
        return proc.returncode, stdout or b"", stderr or b""

    async def ensure(self) -> bool:
        code, stdout, _ = await self._docker(
            "image", "inspect", "--format", "{{json .RepoDigests}}", self.config.image, timeout=20
        )
        if code != 0:
            return False
        try:
            digests = json.loads(stdout)
        except (UnicodeDecodeError, ValueError):
            return False
        if not isinstance(digests, list) or not any(
            isinstance(value, str) and value.endswith(f"@{self.config.digest}") for value in digests
        ):
            return False
        code, stdout, _ = await self._docker(
            "network",
            "inspect",
            "-f",
            '{{.Internal}}|{{index .Labels "com.actverse.hermes-egress"}}',
            self.config.network_mode,
            timeout=15,
        )
        return code == 0 and stdout.decode().strip() == "true|restricted-v1"

    def _prepare(self, run: Mapping[str, Any]) -> tuple[Path, Path]:
        token = uuid.uuid4().hex[:12]
        root = self.config.state_root / f"qa-{run['id']}-{token}"
        root.mkdir(parents=True, mode=0o700)
        os.chmod(root, 0o700)
        env_path = root / ".provider.env"
        values = {
            **self.config.provider_env,
            "AUTO_MODE": "1",
            "TELEMETRY_ENABLED": "0",
            "HEADLESS": "true",
            "RECORD_VIDEO": "false",
        }
        env_path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        os.chmod(env_path, 0o600)
        return root, env_path

    @staticmethod
    def _safe_failure(value: str | None) -> str | None:
        if not value:
            return None
        cleaned = _SENSITIVE.sub("[redacted]", value.replace("\x00", " ")).strip()
        return cleaned[:800] or None

    @classmethod
    def _parse_result(cls, root: Path, slug: str, duration: float) -> dict[str, Any] | None:
        xmls = sorted((root / "output").glob("**/*.xml"), key=lambda path: path.stat().st_mtime)
        if not xmls:
            return None
        try:
            document = ET.parse(xmls[-1]).getroot()
        except (ET.ParseError, OSError):
            return None
        cases = list(document.iter("testcase"))
        if not cases:
            return None
        failed = None
        for case in cases:
            fault = next((node for node in case if node.tag in {"failure", "error"}), None)
            if fault is not None:
                failed = fault.get("message") or fault.text
                break
        return {
            "scenario_slug": slug,
            "outcome": "failed" if failed is not None else "passed",
            "duration_sec": round(duration, 3),
            "failure_summary": cls._safe_failure(failed),
        }

    async def _run_scenario(
        self,
        *,
        root: Path,
        env_path: Path,
        scenario: Mapping[str, Any],
        timeout: float,
    ) -> tuple[dict[str, Any] | None, int | None, bool]:
        slug = str(scenario["slug"])
        definition = str(scenario["definition_gherkin"])
        if _SAFE_SLUG.fullmatch(slug) is None or not 1 <= len(definition.encode()) <= 65536:
            return None, 2, False
        project = root / slug
        project.mkdir(mode=0o707)
        os.chmod(project, 0o707)
        for directory in ("input", "output", "test_data", "proofs", "log_files"):
            path = project / directory
            path.mkdir(mode=0o707)
            os.chmod(path, 0o707)
        feature = project / "input" / "test.feature"
        feature.write_text(definition, encoding="utf-8")
        os.chmod(feature, 0o604)
        started = time.monotonic()
        code, _, _ = await self._docker(
            "run",
            "--rm",
            "--name",
            f"actverse-hercules-{slug}-{uuid.uuid4().hex[:8]}",
            "--network",
            self.config.network_mode,
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--memory={self.config.memory}",
            f"--cpus={self.config.cpus}",
            f"--pids-limit={self.config.pids_limit}",
            "--env-file",
            str(env_path),
            "-v",
            f"{project.resolve()}:/testzeus-hercules/opt:rw",
            self.config.image,
            timeout=timeout,
        )
        duration = time.monotonic() - started
        if code is None:
            return None, None, True
        result = self._parse_result(project, slug, duration)
        return result, code, False

    async def _cleanup(self, root: Path) -> None:
        state_root = self.config.state_root.resolve()
        resolved = root.resolve()
        if resolved.parent != state_root or not resolved.name.startswith("qa-"):
            return
        shutil.rmtree(resolved, ignore_errors=True)
        if not resolved.exists():
            return
        await self._docker(
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--cap-add=DAC_OVERRIDE",
            "--security-opt=no-new-privileges",
            "--memory=128m",
            "--cpus=0.25",
            "--pids-limit=64",
            "--user=0:0",
            "--entrypoint=/bin/sh",
            "-v",
            f"{resolved}:/cleanup:rw",
            self.config.image,
            "-c",
            "find /cleanup -mindepth 1 -delete",
            timeout=30,
        )
        shutil.rmtree(resolved, ignore_errors=True)

    async def run(self, run: Mapping[str, Any]) -> HerculesResult:
        if not await self.ensure():
            return self._result("failed", None, "executor-unavailable", 127, [])
        root, env_path = self._prepare(run)
        results: list[dict[str, Any]] = []
        last_code: int | None = 0
        try:
            scenarios = run.get("scenarios")
            if not isinstance(scenarios, list) or not scenarios:
                return self._result("failed", None, "manifest-invalid", 2, [])
            deadline = time.monotonic() + float(run.get("watchdog_sec") or 300)
            for scenario in scenarios:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._result("timed_out", None, "watchdog-timeout", None, results)
                result, last_code, timed_out = await self._run_scenario(
                    root=root,
                    env_path=env_path,
                    scenario=scenario,
                    timeout=remaining,
                )
                if timed_out:
                    return self._result("timed_out", None, "watchdog-timeout", None, results)
                if result is None:
                    return self._result("failed", None, "result-missing", last_code, results)
                results.append(result)
            outcome = "failed" if any(item["outcome"] == "failed" for item in results) else "passed"
            return self._result("completed", outcome, None, last_code, results)
        finally:
            await self._cleanup(root)

    @staticmethod
    def _result(
        execution_status: str,
        test_outcome: str | None,
        error_reason: str | None,
        exit_code: int | None,
        results: list[dict[str, Any]],
    ) -> HerculesResult:
        payload = {
            "execution_status": execution_status,
            "test_outcome": test_outcome,
            "error_reason": error_reason,
            "exit_code": exit_code,
            "results": results,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return HerculesResult(
            execution_status=execution_status,
            test_outcome=test_outcome,
            error_reason=error_reason,
            exit_code=exit_code,
            results=results,
            result_sha256=digest,
        )
