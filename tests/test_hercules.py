from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from hermes_room_runtime import HerculesConfig, HerculesRuntime

IMAGE = "testzeus/hercules@sha256:" + "a" * 64


def test_hercules_requires_digest_pinning_and_restricted_network(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pinned"):
        HerculesConfig(image="testzeus/hercules:latest", version="0.1.2", state_root=tmp_path)
    with pytest.raises(ValueError, match="restricted"):
        HerculesConfig(image=IMAGE, version="0.1.2", state_root=tmp_path)


def test_junit_result_is_bounded_and_customer_text_is_redacted(tmp_path: Path) -> None:
    project = tmp_path / "scenario"
    output = project / "output"
    output.mkdir(parents=True)
    (output / "result.xml").write_text(
        """<testsuite tests="1" failures="1"><testcase name="x">
        <failure message="customer@example.com Bearer credential-value"/>
        </testcase></testsuite>""",
        encoding="utf-8",
    )
    result = HerculesRuntime._parse_result(project, "public-check", 1.25)
    assert result is not None
    assert result["outcome"] == "failed"
    assert "customer@example.com" not in result["failure_summary"]
    assert "credential-value" not in result["failure_summary"]


def test_missing_image_fails_closed_without_creating_execution_state(tmp_path: Path) -> None:
    class MissingImageRuntime(HerculesRuntime):
        async def _docker(self, *args: str, timeout: float):
            return 1, b"", b"missing"

    runtime = MissingImageRuntime(
        HerculesConfig(
            image=IMAGE,
            version="0.1.2",
            state_root=tmp_path,
            network_mode="actverse-hermes-egress",
            provider_env={"LLM_MODEL_API_KEY": "test-only"},
        )
    )
    result = asyncio.run(runtime.run({"id": 1, "scenarios": []}))
    assert result.execution_status == "failed"
    assert result.error_reason == "executor-unavailable"
    assert json.dumps(result.results) == "[]"


def test_project_mount_is_writable_without_container_capabilities(tmp_path: Path) -> None:
    class InspectingRuntime(HerculesRuntime):
        async def ensure(self) -> bool:
            return True

        async def _docker(self, *args: str, timeout: float):
            project = next(
                Path(value.split(":", 1)[0])
                for index, value in enumerate(args)
                if args[index - 1] == "-v" and value.endswith(":/testzeus-hercules/opt:rw")
            )
            assert project.stat().st_mode & 0o777 == 0o707
            assert (project / "input").stat().st_mode & 0o777 == 0o707
            assert (project / "input" / "test.feature").stat().st_mode & 0o777 == 0o604
            output = project / "output" / "result.xml"
            output.write_text(
                '<testsuite tests="1"><testcase name="public"/></testsuite>',
                encoding="utf-8",
            )
            return 0, b"", b""

    runtime = InspectingRuntime(
        HerculesConfig(
            image=IMAGE,
            version="0.1.2",
            state_root=tmp_path,
            network_mode="actverse-hermes-egress",
            provider_env={"LLM_MODEL_API_KEY": "test-only"},
        )
    )
    result = asyncio.run(
        runtime.run(
            {
                "id": 1,
                "watchdog_sec": 300,
                "scenarios": [
                    {
                        "slug": "public-check",
                        "definition_gherkin": "Feature: public",
                    }
                ],
            }
        )
    )
    assert result.execution_status == "completed"
    assert result.test_outcome == "passed"


def test_cleanup_uses_networkless_bounded_container_for_root_owned_output(
    tmp_path: Path, monkeypatch
) -> None:
    original_rmtree = shutil.rmtree
    calls = 0

    def leave_first_tree(path, *, ignore_errors):
        nonlocal calls
        calls += 1
        if calls > 1:
            original_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr("hermes_room_runtime.hercules.shutil.rmtree", leave_first_tree)

    class CleanupRuntime(HerculesRuntime):
        async def _docker(self, *args: str, timeout: float):
            assert "--network=none" in args
            assert "--read-only" in args
            assert "--cap-drop=ALL" in args
            assert "--cap-add=DAC_OVERRIDE" in args
            assert "--user=0:0" in args
            assert args[-2:] == ("-c", "find /cleanup -mindepth 1 -delete")
            return 0, b"", b""

    root = tmp_path / "qa-1-test"
    root.mkdir()
    (root / "output.xml").write_text("public", encoding="utf-8")
    runtime = CleanupRuntime(
        HerculesConfig(
            image=IMAGE,
            version="0.1.2",
            state_root=tmp_path,
            network_mode="actverse-hermes-egress",
            provider_env={"LLM_MODEL_API_KEY": "test-only"},
        )
    )

    asyncio.run(runtime._cleanup(root))
    assert calls == 2
    assert not root.exists()
