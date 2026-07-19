from pathlib import Path

import pytest

from hermes_room_runtime.cli import _provider_env, build_worker_from_environment


def test_worker_environment_keeps_hub_token_out_of_provider_environment(
    tmp_path: Path, monkeypatch
) -> None:
    token = tmp_path / "hub.token"
    token.write_text("h" * 40, encoding="utf-8")
    provider = tmp_path / "provider.env"
    provider.write_text("OPENAI_API_KEY=provider-secret\n", encoding="utf-8")
    state = tmp_path / "state"
    values = {
        "HUB_BASE_URL": "https://hub.example.test",
        "HUB_WORKER_TOKEN_FILE": str(token),
        "HUB_WORKER_ID": "oracle-room-01",
        "HERMES_RUNTIME_IMAGE": "hermes-room-runtime:test",
        "HERMES_STATE_ROOT": str(state),
        "HERMES_PROVIDER_ENV_FILE": str(provider),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    worker, poll_seconds = build_worker_from_environment()

    assert poll_seconds == 2
    assert worker.client.token_file == token
    assert worker.runtime.config.provider_env == {"OPENAI_API_KEY": "provider-secret"}
    assert "HUB_WORKER_TOKEN" not in worker.runtime.config.provider_env
    assert worker.runtime_heartbeat_seconds == 20
    assert worker.runtime_heartbeat_ttl_seconds == 60


@pytest.mark.parametrize(
    "line",
    [
        "HUB_WORKER_TOKEN=secret\n",
        "ACTVERSE_JOB_ID=override\n",
        "BAD-KEY=value\n",
        "OPENAI_API_KEY=\n",
    ],
)
def test_provider_environment_rejects_host_control_and_invalid_entries(
    tmp_path: Path, line: str
) -> None:
    path = tmp_path / "provider.env"
    path.write_text(line, encoding="utf-8")

    with pytest.raises(ValueError):
        _provider_env(path)
