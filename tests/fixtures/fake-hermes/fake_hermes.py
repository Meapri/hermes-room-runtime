#!/usr/bin/env python3
import json
import os
from pathlib import Path

evidence = json.loads(Path(os.environ["ACTVERSE_EVIDENCE_PATH"]).read_text())
Path("result.json").write_text(
    json.dumps(
        {
            "job_id": os.environ["ACTVERSE_JOB_ID"],
            "task_kind": os.environ["ACTVERSE_TASK_KIND"],
            "evidence_state": evidence["status"],
        }
    ),
    encoding="utf-8",
)
print("fake hermes completed")
