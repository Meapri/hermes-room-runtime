from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from pathlib import Path

import pytest

from hermes_room_runtime import HubAgentJobClient, HubApiError, HubEvidenceLoader, LeasedHubJob


class _Response:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


def _loader(tmp_path: Path) -> HubEvidenceLoader:
    token = tmp_path / "hub.token"
    token.write_text("t" * 40, encoding="utf-8")
    return HubEvidenceLoader("https://hub.example.test", token)


def _agent_client(tmp_path: Path) -> HubAgentJobClient:
    token = tmp_path / "hub-worker.token"
    token.write_text("w" * 40, encoding="utf-8")
    return HubAgentJobClient("https://hub.example.test", token)


def test_status_bundle_uses_stable_read_contract(tmp_path: Path, monkeypatch) -> None:
    loader = _loader(tmp_path)
    seen = {}

    def fake(request, **kwargs):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return _Response({"api_version": "status-v1", "overall": {"state": "unknown"}})

    monkeypatch.setattr("urllib.request.urlopen", fake)
    bundle = loader.status_bundle("prod")
    assert seen["url"].endswith("/api/status/v1/overview?environment=prod")
    assert seen["auth"].startswith("Bearer ")
    assert bundle["source_contract"] == "actverse-evidence-hub/status-v1"
    assert bundle["status"]["api_version"] == "status-v1"


def test_journey_bundle_does_not_copy_original_analysis_id(tmp_path: Path, monkeypatch) -> None:
    loader = _loader(tmp_path)
    original = "analysis-customer-visible-id"

    def fake(request, **kwargs):
        assert original in request.data.decode()
        return _Response({"items": [], "missing_stages": ["engine"]})

    monkeypatch.setattr("urllib.request.urlopen", fake)
    bundle = loader.journey_bundle(environment="prod", analysis_id=original)
    assert original not in json.dumps(bundle)


def test_hub_error_is_bounded(tmp_path: Path, monkeypatch) -> None:
    loader = _loader(tmp_path)

    def fake(request, **kwargs):
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "too many requests",
            {},
            io.BytesIO(b'{"code":"rate-limited","secret":"hidden"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(HubApiError) as caught:
        loader.status_bundle()
    assert caught.value.status_code == 429
    assert str(caught.value).endswith("code=rate-limited")
    assert "hidden" not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    ["http://hub.example.test", "https://user:pass@hub.example.test", "file:///tmp/hub"],
)
def test_hub_url_must_be_https(url: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        HubEvidenceLoader(url, tmp_path / "hub.token")


def test_agent_job_lease_and_completion_keep_lease_token_in_host_headers(
    tmp_path: Path, monkeypatch
) -> None:
    client = _agent_client(tmp_path)
    lease_token = "lease-token-that-is-long-enough-for-the-contract"  # noqa: S105
    seen: list[dict] = []

    def fake(request, **kwargs):
        seen.append(
            {
                "url": request.full_url,
                "lease": request.get_header("X-agent-lease-token"),
                "body": json.loads(request.data) if request.data else None,
            }
        )
        if request.full_url.endswith("/jobs/lease"):
            return _Response(
                {
                    "job": {
                        "job_id": "018f7bc8-6bbd-7a00-8000-000000000099",
                        "task_kind": "incident-diagnosis",
                        "environment": "prod",
                        "subject_ref": "status:prod:video-analysis",
                        "options": {"locale": "ko", "force": False},
                    },
                    "lease_token": lease_token,
                }
            )
        return _Response({"job": {"status": "succeeded"}})

    monkeypatch.setattr("urllib.request.urlopen", fake)
    job = client.lease(
        worker_id="oracle-room-01",
        task_kinds=["incident-diagnosis"],
    )
    assert job is not None
    client.complete(
        job,
        worker_id="oracle-room-01",
        status="succeeded",
        result={"summary": "ok"},
        evidence_sha256="a" * 64,
    )

    assert seen[0]["lease"] is None
    assert seen[1]["lease"] == lease_token
    assert lease_token not in json.dumps(seen[1]["body"])


def test_empty_agent_queue_has_no_lease_token(tmp_path: Path, monkeypatch) -> None:
    client = _agent_client(tmp_path)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: _Response({"job": None, "lease_token": None}),
    )

    assert client.lease(worker_id="oracle-room-01", task_kinds=["incident-diagnosis"]) is None


def test_lease_bound_evidence_bundle_is_verified_before_use(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _agent_client(tmp_path)
    job = LeasedHubJob(
        job_id="018f7bc8-6bbd-7a00-8000-000000000099",
        task_kind="incident-diagnosis",
        environment="prod",
        subject_ref="status:prod:video-analysis",
        options={"locale": "ko", "force": False},
        lease_token="lease-token-that-is-long-enough-for-the-contract",  # noqa: S106
    )
    unsigned = {
        "api_version": "agent-evidence-bundle-v1",
        "generated_at": "2026-07-20T00:00:00Z",
        "window_hours": 24,
        "job": {
            "job_id": job.job_id,
            "task_kind": job.task_kind,
            "environment": job.environment,
            "subject_ref": job.subject_ref,
        },
        "subject": {"kind": "component", "reference": "video-analysis"},
        "policy": {
            "allowed_result_fields": ["summary", "reason_codes"],
            "must_report_gaps": True,
            "must_be_inconclusive_with_gaps": True,
            "must_be_inconclusive_without_facts": True,
        },
        "facts": [],
        "gaps": ["evidence-bundle-empty"],
        "reason_codes": ["evidence-bundle-empty"],
        "truncated": False,
    }
    bundle = dict(unsigned)
    bundle["bundle_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    seen: dict = {}

    def fake(request, **kwargs):
        seen["url"] = request.full_url
        seen["lease"] = request.get_header("X-agent-lease-token")
        return _Response(bundle)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    fetched = client.evidence_bundle(job, worker_id="oracle-room-01")

    assert fetched == bundle
    assert seen["url"].endswith(
        f"/api/agent/v1/jobs/{job.job_id}/evidence-bundle?worker_id=oracle-room-01"
    )
    assert seen["lease"] == job.lease_token

    broken = dict(bundle)
    broken["bundle_sha256"] = "0" * 64
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _Response(broken))
    with pytest.raises(HubApiError, match="evidence-bundle-digest-mismatch"):
        client.evidence_bundle(job, worker_id="oracle-room-01")


def test_worker_heartbeat_reports_only_bounded_runtime_capabilities(
    tmp_path: Path, monkeypatch
) -> None:
    client = _agent_client(tmp_path)
    seen: dict = {}

    def fake(request, **kwargs):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        return _Response(
            {
                "api_version": "agent-runtime-v1",
                "worker": {
                    "worker_id": "oracle-room-01",
                    "task_kinds": ["incident-diagnosis"],
                    "slots": 2,
                    "runtime_version": "0.3.0",
                    "last_heartbeat_at": "2026-07-20T00:00:00Z",
                    "available_until": "2026-07-20T00:01:00Z",
                },
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake)
    client.worker_heartbeat(
        worker_id="oracle-room-01",
        task_kinds=["incident-diagnosis"],
        slots=2,
        runtime_version="0.3.0",
    )

    assert seen["url"].endswith("/api/agent/v1/workers/heartbeat")
    assert seen["body"] == {
        "worker_id": "oracle-room-01",
        "task_kinds": ["incident-diagnosis"],
        "slots": 2,
        "runtime_version": "0.3.0",
        "heartbeat_ttl_seconds": 60,
    }
    assert "token" not in json.dumps(seen["body"]).lower()
