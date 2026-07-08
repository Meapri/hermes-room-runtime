# hermes-room-runtime

Run many isolated [Hermes](https://pypi.org/project/hermes-agent/) agents — one
hardened Docker container per "room" (a chat, tenant, session, …) — all sharing
a single read-only "brain" while keeping each room's data and tool execution
fully isolated.

This is the container-orchestration layer extracted from a production per-room
agent gateway. It ships the **pattern**, not any app-specific glue, data, or
credentials.

## Why

An agent that runs tools (shell, file writes, MCP servers) on behalf of
untrusted, concurrent conversations needs real isolation:

- **Blast-radius containment** — each room is its own container with
  `--cap-drop=ALL`, `--security-opt=no-new-privileges`, a non-root user, and
  memory / CPU / PID limits. A tool call gone wrong can't reach other rooms or
  the host.
- **Shared brain, isolated state** — one large agent "brain" (prompts, skills,
  tools) is bind-mounted **read-only** into every room, so it's defined once;
  each room gets its own read-write `~/.hermes` for history and scratch files.
- **No config drift** — the manager force-seeds `config.yaml` before each turn,
  so a room can't silently change model, provider, or limits.
- **Ephemeral per-turn context** — extra context is injected via a one-shot
  `--env-file` (`HERMES_EPHEMERAL_SYSTEM_PROMPT`) that is deleted after the turn
  and never persisted into the agent's `--continue` history.
- **Cheap concurrency** — containers are long-lived (`tail -f /dev/null`); a
  turn is a fast `docker exec`, not a cold start.

## Architecture

```
  host process          HermesRoomManager
  (your gateway)   ensure() · chat() · run() · kill()
                          │ docker exec        │ docker exec
              ┌───────────▼──────┐   ┌──────────▼───────┐
              │  hermes_room_A   │   │  hermes_room_B   │   one container / room
              │   ~/.hermes  rw  │   │   ~/.hermes  rw  │   (mem/cpu/pids capped,
              │   brain      ro  │   │   brain      ro  │    cap-drop=ALL, non-root)
              └──────────────────┘   └──────────────────┘
                       └────── shared brain (read-only) ──────┘
```

## Usage

Build the image (the agent itself comes from the public `hermes-agent` package):

```bash
docker build -t hermes-agent-minimal .
```

Drive it from your own process:

```python
import asyncio
from hermes_room_manager import HermesRoomManager

mgr = HermesRoomManager(
    image="hermes-agent-minimal",
    brain_path="/opt/hermes-brain",     # mounted read-only into every room
    data_root="./rooms",                # per-room read-write data
    memory="512m", cpus="1",
    proxy_url="http://host.docker.internal:8765/v1",
    proxy_key="...",                    # injected at runtime, never baked in
)

async def main():
    cfg = open("config.example.yaml").read()
    reply = await mgr.chat(
        "room-42",
        "Summarize the last message.",
        config=cfg,
        ephemeral_context="[context for this turn only — not persisted]",
    )
    print(reply)

asyncio.run(main())
```

See `example.py` for a two-room demo.

## API

| method | purpose |
|---|---|
| `ensure(room_id)` | create/start the room's persistent container if needed |
| `seed_config(room_id, text)` | atomically force-write `config.yaml` for the room |
| `chat(room_id, prompt, config=…, ephemeral_context=…)` | seed + run one `hermes chat` turn |
| `run(room_id, args, ephemeral_env=…)` | run any `hermes <args>` in the container |
| `kill(room_id)` | stop the room's container |

## Security notes

- API keys are **injected at runtime** (`-e` / `--env-file`), never written into
  the image or committed to the repo.
- The brain is mounted **read-only** — a room cannot mutate shared state.
- Each container drops all Linux capabilities, forbids privilege escalation,
  runs as a non-root user, and is capped on memory / CPU / PIDs.
- This repo intentionally contains **no data, no brain, and no credentials** —
  only the orchestration pattern. `data/`, `rooms/`, and `*.env` are gitignored.

## License

[MIT](LICENSE)
