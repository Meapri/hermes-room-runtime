import pytest

from hermes_room_runtime import JobRequest, JobResult, JobStatus


def test_job_request_is_bounded_and_stable() -> None:
    request = JobRequest(
        job_id="incident-42",
        task_kind="incident-diagnosis",
        prompt="diagnose",
        evidence={"b": 2, "a": 1},
    )
    same = JobRequest(
        job_id="incident-42",
        task_kind="incident-diagnosis",
        prompt="diagnose",
        evidence={"a": 1, "b": 2},
    )
    assert request.evidence_sha256 == same.evidence_sha256


@pytest.mark.parametrize("value", ["../escape", "has space", "", "a" * 65])
def test_job_request_rejects_unsafe_ids(value: str) -> None:
    with pytest.raises(ValueError):
        JobRequest(job_id=value, task_kind="diagnosis", prompt="hello")


def test_job_request_rejects_oversized_evidence() -> None:
    with pytest.raises(ValueError, match="1 MiB"):
        JobRequest(
            job_id="job-1",
            task_kind="diagnosis",
            prompt="hello",
            evidence={"value": "x" * (1024 * 1024)},
        )


def test_job_result_record_excludes_process_output() -> None:
    result = JobResult(
        job_id="job-1",
        task_kind="diagnosis",
        status=JobStatus.FAILED,
        result=None,
        evidence_sha256="a" * 64,
        slot=1,
        duration_ms=20,
        error_code="hermes-failed",
        stdout_tail="must-not-persist",
        stderr_tail="must-not-persist",
    )
    record = result.as_record()
    assert record["status"] == "failed"
    assert "stdout_tail" not in record and "stderr_tail" not in record
