# Hermes Room Runtime

Actverse Evidence Hub와 Space 사이에서 Hermes 작업을 격리 실행하는 작은 Python 런타임이다.

처음 버전은 카카오톡 대화방마다 장기 세션을 유지하는 구조였다. 현재 버전은 반대로 작업마다
Hermes 상태를 폐기한다. Space는 무상태 소비자로 남고, Hub가 제공한 제한된 근거만 읽으며, 회사
저장소는 명시적으로 허용한 경로만 읽기 전용으로 마운트한다.

## 책임 경계

```text
Space
  └─ Agent Jobs v1 작업 제출과 진행 상태 표시
       ↓
Evidence Hub
  ├─ Status API / Evidence API의 정본 데이터
  └─ 작업 queue · lease · 결과 · 보존
       ↓ worker lease + bounded JSON bundle (Hub token 제외)
Hermes Room Runtime
  ├─ slot 0: job마다 새 HOME/HERMES_HOME/work
  ├─ slot 1: job마다 새 HOME/HERMES_HOME/work
  └─ 구조화 result를 Hub에 완료 기록 후 작업 디렉터리 폐기
```

런타임은 상태를 판정하거나 저장하지 않는다. `HubJobWorker`가 Agent Jobs v1에서 작업을 임대하고,
Status API의 제한된 근거 번들로 Hermes를 실행한 뒤 구조화 결과를 Hub에 완료 기록한다. queue와
결과의 유일한 durable store는 Hub PostgreSQL이다.

## 이전 버전과 달라진 점

| 이전 카톡 룸 | 현재 Actverse 작업 런타임 |
| --- | --- |
| `room_id`마다 `--continue` | 모든 작업이 one-shot이며 대화를 재개하지 않음 |
| room의 `~/.hermes`를 장기 보존 | 작업마다 새 `HOME`과 `HERMES_HOME`, 완료 후 폐기 |
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
- 최대 512 KiB의 일반 JSON 객체만 결과로 허용
- stdout/stderr tail은 기본적으로 반환하지 않음
- timeout 시 해당 slot container 재시작

컨테이너는 VM이 아니다. 특히 `bridge` 네트워크는 일반 인터넷 egress를 차단하지 않는다. 운영에서는
모델 프록시만 포함하는 별도 Docker network와 호스트 방화벽/egress proxy를 함께 사용해야 한다.

## 설치와 실행

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

docker build -t hermes-room-runtime:0.2.0 .
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
- `POST /api/agent/v1/jobs/{job_id}/heartbeat`
- `POST /api/agent/v1/jobs/{job_id}/complete`

`journey_bundle()`에 입력한 원본 analysis ID는 Hub 요청 본문에서만 쓰며 반환 번들에는 복사하지
않는다. HTTP 오류에는 token이나 응답 본문을 포함하지 않는다.

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
export HERMES_SLOTS=2

hermes-room-worker
```

`HERMES_PROVIDER_ENV_FILE`은 `KEY=value` 형식이며 `HUB_*`, `ACTVERSE_*` key는 거부한다. 따라서 Hub
consumer token이나 lease token이 Hermes 환경으로 내려갈 수 없다. 기본 network는 `none`이다.
모델 proxy가 필요한 운영 환경에서만 제한된 named network를 명시해야 한다.

## Space 이행 결과

1. Space는 Agent Jobs v1에 요청하고 목록·결과를 읽는 무상태 proxy다.
2. Hub는 멱등 제출, worker lease, 재시도, 결과와 event 보존을 맡는다.
3. Room Runtime host는 `agent-jobs:work`와 필요한 읽기 scope만 가진다.
4. Hermes slot에는 Hub token, lease token, Space DB가 전달되지 않는다.
5. 운영 이행 전 named network, 전용 consumer token, 이미지 digest, slot 예산을 고정한다.

## 운영상 의도적인 제한

- Hub 관리 API나 source credential은 지원하지 않는다.
- 회사 저장소 쓰기, 자동 commit, 배포 기능은 제공하지 않는다.
- raw customer payload, 이름, 이메일, 영상 URL 저장을 위한 인터페이스가 없다.
- 오래된 카톡식 `HermesRoomManager`와 `--continue` API는 0.2에서 제거했다.

세부 보안 가정과 운영 체크리스트는 [SECURITY.md](SECURITY.md)를 참고한다.
