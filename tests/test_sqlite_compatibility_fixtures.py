"""Compatibility checks for immutable SQLite files from released versions."""

import asyncio
from hashlib import sha256
from pathlib import Path
import shutil

from engine.domain import AgentId, AgentInstanceId, Message, RunId, RunPhase


V0_DATABASE = (
    Path(__file__).parents[1] / "apps" / "web" / "e2e" / "fixtures" / "v0.0.0.sqlite3"
)
V0_SHA256 = "bb09ee6db190f9af9eaff7faec9823606a99c485bde8786631b26d2863650dc1"


def test_v0_database_restores_history_and_accepts_new_writes(
    tmp_path: Path, *, sqlite_store
) -> None:
    assert sha256(V0_DATABASE.read_bytes()).hexdigest() == V0_SHA256
    database = tmp_path / "conversations.sqlite3"
    shutil.copyfile(V0_DATABASE, database)

    async def scenario() -> None:
        store = sqlite_store(database)
        try:
            run = await store.load(RunId("run-seeded-history"))
            assert run is not None
            assert run.phase is RunPhase.SUCCEEDED

            conversation = await store.load_conversation(
                AgentInstanceId("agi-seeded-chat")
            )
            assert conversation is not None
            assert [message.content for message in conversation.messages] == [
                "What survives when the web process restarts?",
                "The SQLite-backed conversation history survives.",
                "Can I still navigate back to this answer?",
                "Yes. This second turn proves the complete history loaded.",
            ]

            new_instance = await store.create_instance(AgentId("coder"))
            await store.append_messages(
                new_instance.instance_id,
                (Message.user("A new conversation after migration."),),
            )
            restored = await store.load_conversation(new_instance.instance_id)
            assert restored is not None
            assert restored.messages[0].content == "A new conversation after migration."
        finally:
            store.close()

    asyncio.run(scenario())
