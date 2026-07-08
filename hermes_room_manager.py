"""Run per-"room" Hermes agents in isolated Docker containers.

Each room gets its own long-lived, resource- and security-limited container
that bind-mounts a shared read-only "brain" plus a per-room read-write data
dir. Turns run via ``docker exec <container> hermes chat ...``; a fresh config
is seeded before each turn, and per-turn context can be injected through an
ephemeral ``--env-file`` so it never lands in the agent's persistent history.

Extracted and generalized from a production per-room agent gateway. Ships the
orchestration pattern only — no data, brains, or credentials.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HermesRoomManager:
    image: str = "hermes-agent-minimal"
    brain_path: str | None = None                 # mounted read-only into every room
    data_root: str = "./rooms"                    # per-room rw data lives here
    memory: str = "512m"
    cpus: str = "1"
    pids_limit: int = 128
    proxy_url: str | None = None                   # OPENAI_BASE_URL for the agent
    proxy_key: str = "not-used"                    # OPENAI_API_KEY (runtime-injected)
    extra_ro_mounts: dict[str, str] = field(default_factory=dict)  # host -> container
    extra_env: dict[str, str] = field(default_factory=dict)
    cli_timeout: int = 180
    sudo: bool = False                             # prefix docker with sudo if needed

    # ------------------------------------------------------------------ helpers
    def _docker(self, *args: str) -> list[str]:
        return (["sudo"] if self.sudo else []) + ["docker", *args]

    def _name(self, room_id: str) -> str:
        return f"hermes_room_{room_id}"

    def _room_dir(self, room_id: str) -> Path:
        d = Path(self.data_root) / f"room_{room_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _run(self, *args: str, timeout: float | None = None):
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, b"", b"timeout"
        return proc.returncode, out or b"", err or b""

    async def _status(self, name: str) -> str:
        rc, out, _ = await self._run(
            *self._docker("inspect", "-f", "{{.State.Status}}", name)
        )
        return "not_found" if rc != 0 else (out.decode().strip() or "unknown")

    # ------------------------------------------------------------------ config
    def seed_config(self, room_id: str, text: str) -> None:
        """Atomically force-write config.yaml into the room (mounted at ~/.hermes),
        so model/provider/limits cannot drift between turns."""
        cfg = self._room_dir(room_id) / "config.yaml"
        tmp = cfg.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, cfg)

    # --------------------------------------------------------------- lifecycle
    async def ensure(self, room_id: str) -> bool:
        """Guarantee a running container for the room (create/start as needed)."""
        name = self._name(room_id)
        st = await self._status(name)
        if st == "running":
            return True
        if st == "exited":
            rc, _, _ = await self._run(*self._docker("start", name))
            if rc == 0:
                return True
            await self._run(*self._docker("rm", "-f", name))  # recreate below

        host_dir = self._room_dir(room_id)
        mounts = ["-v", f"{host_dir}:/home/user/.hermes"]
        if self.brain_path:
            mounts += ["-v", f"{self.brain_path}:/home/user/.hermes/hermes-agent:ro"]
        for host, cont in self.extra_ro_mounts.items():
            mounts += ["-v", f"{host}:{cont}:ro"]

        env = [
            "-e", "HERMES_HOME=/home/user/.hermes",
            "-e", "HERMES_YOLO_MODE=1",
            "-e", f"HERMES_ROOM_ID={room_id}",
        ]
        if self.proxy_url:
            env += ["-e", f"OPENAI_BASE_URL={self.proxy_url}",
                    "-e", f"OPENAI_API_KEY={self.proxy_key}"]
        for k, v in self.extra_env.items():
            env += ["-e", f"{k}={v}"]

        cmd = self._docker(
            "run", "-d", "--name", name, "--label", f"hermes_room={room_id}",
            f"--memory={self.memory}", f"--cpus={self.cpus}",
            f"--pids-limit={self.pids_limit}",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "-w", "/home/user/.hermes",
            *mounts, *env,
            "--network", "bridge",
            "--add-host=host.docker.internal:host-gateway",
            self.image, "tail", "-f", "/dev/null",
        )
        rc, _, err = await self._run(*cmd)
        return rc == 0

    async def run(self, room_id: str, args: list[str], *,
                  ephemeral_env: dict | None = None) -> str:
        """Run ``hermes <args>`` in the room container. Optional per-turn env
        (e.g. an ephemeral system prompt) is injected via --env-file so it is
        never persisted into the agent's state."""
        if not await self.ensure(room_id):
            return "(container start failed)"
        name = self._name(room_id)
        exec_env: list[str] = []
        eph_file = None
        if ephemeral_env:
            eph_file = self._room_dir(room_id) / f".eph_{os.urandom(4).hex()}.env"
            eph_file.write_text(
                "".join(f"{k}={v}\n" for k, v in ephemeral_env.items()),
                encoding="utf-8",
            )
            exec_env = ["--env-file", str(eph_file)]
        cmd = self._docker("exec", *exec_env, name, "hermes", *args)
        try:
            rc, out, err = await self._run(*cmd, timeout=self.cli_timeout)
            text = out.decode("utf-8", "replace")
            return text if text.strip() else err.decode("utf-8", "replace")
        finally:
            if eph_file:
                eph_file.unlink(missing_ok=True)

    async def chat(self, room_id: str, prompt: str, *, config: str | None = None,
                   ephemeral_context: str | None = None) -> str:
        """Seed config (optional) then run one ``hermes chat`` turn."""
        if config:
            self.seed_config(room_id, config)
        eph = ({"HERMES_EPHEMERAL_SYSTEM_PROMPT": ephemeral_context}
               if ephemeral_context else None)
        return await self.run(room_id, ["chat", "--continue", "-q", prompt],
                              ephemeral_env=eph)

    async def kill(self, room_id: str) -> bool:
        rc, _, _ = await self._run(*self._docker("stop", self._name(room_id)))
        return rc == 0
