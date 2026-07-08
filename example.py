"""Minimal demo of HermesRoomManager.

Requires Docker and the built image:  docker build -t hermes-agent-minimal .
"""
import asyncio

from hermes_room_manager import HermesRoomManager


async def main() -> None:
    mgr = HermesRoomManager(image="hermes-agent-minimal", data_root="./rooms")
    with open("config.example.yaml", encoding="utf-8") as f:
        cfg = f.read()

    # Two rooms run in fully isolated containers, sharing nothing but the image.
    print(await mgr.chat("alice", "Hello — who are you?", config=cfg))
    print(await mgr.chat("bob", "Say hi in one word.", config=cfg,
                         ephemeral_context="[for this turn only: reply in Korean]"))

    await mgr.kill("alice")
    await mgr.kill("bob")


if __name__ == "__main__":
    asyncio.run(main())
