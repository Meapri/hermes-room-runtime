# Hermes Room Runtime

Actverse Evidence Hub의 Hermes 추론 작업과 Hercules QA 실행을 격리하는 Python 런타임이다.

처음 버전은 카카오톡 대화방마다 장기 세션을 유지하는 구조였다. 현재 버전은 반대로 작업마다
Hermes 상태를 폐기한다. Space는 무상태 소비자로 남고, Hub가 제공한 제한된 근거만 읽으며, 회사
저장소는 명시적으로 허용한 경로만 읽기 전용으로 마운트한다.

## 책임 경계

```text
Space
  └─ 상태·Finding·QA 실행 UI와 무상태 BFF
       ↓
Evidence Hub
  ├─ Status API / Evidence API의 정본 데이터
  ├─ Hermes Agent Jobs queue · lease · 구조화 결과
  └─ Hercules QA Runs queue · Finding · 결과 · 증거 링크
       ↓ lease + bounded bundle/manifest (Hub token 제외)
Hermes Room Runtime
  ├─ Hermes: one-shot container/HOME/HERMES_HOME/work
  ├─ Hercules: digest-pinned ephemeral test container
  └─ 두 결과를 Hub에 완료 기록한 뒤 작업 디렉터리 폐기
```

런타임은 상태를 저장하지 않는다. `HubJobWorker`가 Agent Jobs v1과 QA Executor v1에서 작업을 임대하고,
활성 lease에 묶인 Agent Evidence Bundle로 Hermes를 실행한 뒤 구조화 결과를 Hub에 완료 기록한다. queue와
결과의 유일한 durable store는 Hub PostgreSQL이다. Hermes는 진단·제안·시나리오 작성만 하고,
Hercules만 테스트를 실행한다.

## 이전 버전과 달라진 점

| 이전 카톡 룸 | 현재 Actverse 작업 런타임 |
| --- | --- |
| `room_id`마다 `--continue` | 모든 작업이 one-shot이며 대화를 재개하지 않음 |
| room의 `~/.hermes`를 장기 보존 | 작업마다 container와 `HOME`·`HERMES_HOME`을 만들고 완료 후 폐기 |
| 임의 room 수 생성 | CPU·메모리 예산이 고정된 slot pool |
| 모델 키를 컨테이너 생성 환경에 보존 | 실행 시 0600 env-file로만 주입하고 즉시 삭제 |
| bridge와 host gateway를 항상 허용 | 기본 `--network=none`, 운영에서는 제한된 named network를 명시 |
| 임의 read-only mount | 호출자가 허용한 절대경로만 `/repos/*` 등에 read-only mount |
| 결과가 자유 텍스트 | `result.json` 객체가 없으면 `inconclusive` |

## 보안 경계

각 slot은 non-root 이미지와 다음 Docker 제한을 사용한다.

- `--cap-drop=ALL`, `no-new-privileges`
- CPU, memory, PID 제한
- read-only root filesystem과 크기 제한 tmpfs
- Docker socket과 호스트 홈 미마운트
- 회사 저장소 read-only mount
- 작업별 증거 SHA-256과 최대 1 MiB 입력
- Hub와 같은 최대 256 KiB·깊이·민감정보 제한을 통과한 JSON 객체만 결과로 허용
- stdout/stderr tail은 기본적으로 반환하지 않음
- 성공·실패·timeout·취소와 무관하게 해당 slot container를 삭제하고 새로 생성

컨테이너는 VM이 아니다. 특히 `bridge` 네트워크는 일반 인터넷 egress를 차단하지 않는다. 운영에서는
모델 프록시만 포함하는 별도 Docker network와 호스트 방화벽/egress proxy를 함께 사용해야 한다.

## 설치와 실행

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

docker build -t hermes-room-runtime:0.5.0 .
pytest
ruff check .
```

Python 사용 예시는 [example.py](example.py)에 있다. 핵심 API는 다음 셋이다.

- `RuntimeConfig`: image, slot 수, 제한된 network와 read-only mount 설정
- `JobRequest`: 작업 ID, 종류, prompt, 이미 제한된 Hub evidence bundle
- `HermesJobRuntime.run()`: slot을 빌려 one-shot 실행 후 `JobResult` 반환

## Evidence Hub 연결

`HubEvidenceLoader`는 호스트에서만 Hub token을 읽는다. token은 Hermes 컨테이너로 전달되지 않는다.

```python
from pathlib import Path
from hermes_room_runtime import HubEvidenceLoader

hub = HubEvidenceLoader(
    base_url="https://evidence-hub.internal.example",
    token_file=Path("/run/secrets/evidence-hub-runtime.token"),
)
bundle = hub.status_bundle(environment="prod")
```

지원하는 읽기 계약은 다음과 같다.

- `GET /api/status/v1/overview`
- `POST /api/evidence/v1/journeys/timeline`

`HubAgentJobClient`와 `HubJobWorker`는 다음 작업 계약을 사용한다.

- `POST /api/agent/v1/jobs/lease`
- `GET /api/agent/v1/jobs/{job_id}/evidence-bundle`
- `POST /api/agent/v1/jobs/{job_id}/heartbeat`
- `POST /api/agent/v1/jobs/{job_id}/complete`
- `POST /api/agent/v1/workers/heartbeat`
- `POST /api/qa/v1/executors/heartbeat`
- `POST /api/qa/v1/executions/lease`
- `POST /api/qa/v1/executions/{run_id}/heartbeat`
- `POST /api/qa/v1/executions/{run_id}/complete`

worker는 lease token과 worker ID가 일치할 때만 `agent-evidence-bundle-v1`을 읽는다. bundle의 작업
정보·크기·구조·SHA-256이 계약과 다르면 Hermes를 실행하지 않고 작은 `inconclusive` 결과로 닫는다.
검증된 `bundle_sha256`을 completion의 `evidence_sha256`으로 그대로 돌려준다. policy가 근거 없음 또는
gap·reason code 존재 시 `inconclusive`를 요구하면 facts가 일부 있어도 Hermes를 실행해 성공을 추정하지
않는다.
구형 Hub의 endpoint가 없을 때 status overview로 후퇴하지 않으므로 근거 범위가 묵시적으로 넓어지지
않는다. `journey_bundle()`에 입력한 원본 analysis ID는 Hub 요청 본문에서만 쓰며 반환 번들에는
복사하지 않는다. HTTP 오류에는 token이나 응답 본문을 포함하지 않는다.

## 지속 실행 worker

패키지를 설치하면 `hermes-room-worker` 명령을 사용할 수 있다. Hub token은 host process만 읽고,
provider credential만 매 실행의 임시 `docker exec --env-file`에 들어간다.

```bash
export HUB_BASE_URL=https://evidence-hub.internal.example
export HUB_WORKER_TOKEN_FILE=/run/secrets/evidence-hub-room-worker.token
export HUB_WORKER_ID=oracle-room-01
export HERMES_RUNTIME_IMAGE=hermes-room-runtime@sha256:...
export HERMES_STATE_ROOT=/var/lib/hermes-room-runtime
export HERMES_CONFIG_PATH=/etc/hermes-room-runtime/config.yaml
export HERMES_PROVIDER_ENV_FILE=/run/secrets/hermes-provider.env
export HERMES_NETWORK_MODE=actverse-hermes-egress
export HERMES_REQUIRE_RESTRICTED_NETWORK=true
export HERMES_SLOTS=5
export HERCULES_ENABLED=true
export HERCULES_IMAGE=testzeus/hercules@sha256:11ff3700104f92230bafdff1e85f43b8932e8a7df5ab85b7f7d00d3cea61f52c
export HERCULES_VERSION=0.1.2
export HERCULES_STATE_ROOT=/var/lib/hermes-room-runtime/hercules
export HERCULES_PROVIDER_ENV_FILE=/etc/hermes-room-runtime/hercules-provider.env

hermes-room-worker
```

`HERMES_PROVIDER_ENV_FILE`은 `KEY=value` 형식이며 `HUB_*`, `ACTVERSE_*` key는 거부한다. 따라서 Hub
consumer token이나 lease token이 Hermes 환경으로 내려갈 수 없다. 기본 network는 `none`이다.
모델 proxy가 필요한 운영 환경에서는 `HERMES_REQUIRE_RESTRICTED_NETWORK=true`를 켠다. 이 모드는
`none`·`bridge`·`default`를 거부하고 named network가 `Internal=true`이며
`com.actverse.hermes-egress=restricted-v1` label을 가졌는지 매번 검사한다. network ID도 slot spec에
포함하므로 같은 이름으로 network가 교체되면 기존 container를 재사용하지 않는다.

```bash
docker network create --internal \
  --label com.actverse.hermes-egress=restricted-v1 \
  actverse-hermes-egress
```

모델 proxy만 이 internal network와 별도 upstream network 양쪽에 연결한다. Hermes slot은 internal
network 하나에만 연결하고 host 방화벽에서도 직접 egress를 차단한다.

`HERCULES_PROVIDER_ENV_FILE`은 별도 파일이다. 최소 `LLM_MODEL_API_KEY`가 필요하고 보통
`LLM_MODEL_NAME`, `LLM_MODEL_API_TYPE`도 함께 둔다. Hub/Space에는 이 자격증명을 저장하지 않는다.
Hercules 이미지는 태그가 아니라 ARM64 manifest digest까지 고정하며, 실행 결과에서는 JUnit의
bounded 요약과 버전/다이제스트/결과 해시만 Hub로 보낸다. 영상·원문 로그·고객 식별자는 폐기한다.

Oracle에서는 Antigravity의 OpenAI 호환 `/v1` endpoint를 `LLM_MODEL_BASE_URL`로 사용할 수 있다.
Hercules container는 host endpoint에 직접 연결하지 않고 restricted proxy의 명시적 allowlist를
통과하며, `LLM_MODEL_NAME=gemini-3.5-flash-high`로 고정한다. proxy API key는 Runtime 전용
provider 파일에만 둔다.

## Space 이행 결과

1. Space는 Agent Jobs v1에 요청하고 목록·결과를 읽는 무상태 proxy다.
2. Hub는 멱등 제출, worker lease, 재시도, 결과와 event 보존을 맡는다.
3. Room Runtime host는 `agent-jobs:work`와 `qa:work`만 가진다.
4. Hermes slot에는 Hub token, lease token, Space DB가 전달되지 않는다.
5. 운영 이행 전 named network, 전용 consumer token, 이미지 digest, slot 예산을 고정한다.

## 운영상 의도적인 제한

- Hub 관리 API나 source credential은 지원하지 않는다.
- 회사 저장소 쓰기, 자동 commit, 배포 기능은 제공하지 않는다.
- raw customer payload, 이름, 이메일, 영상 URL 저장을 위한 인터페이스가 없다.
- 오래된 카톡식 `HermesRoomManager`와 `--continue` API는 0.2에서 제거했다.

세부 보안 가정과 운영 체크리스트는 [SECURITY.md](SECURITY.md)를 참고한다.
Ubuntu 지속 실행 예시는 [ops/systemd/README.md](ops/systemd/README.md)에 있다.
