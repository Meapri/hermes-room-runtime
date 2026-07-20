from __future__ import annotations

import asyncio
import json
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
