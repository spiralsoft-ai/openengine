"""Pausing a turn on a person, and resuming the same provider process.

An approval is the one place where a running agent waits on something outside
the process for an unbounded time, so the interesting cases are all about what
happens to that wait when something else goes wrong: the browser leaves, the
run is stopped, the CLI dies, the server restarts. Each of those has a test
here, because each of them can strand a turn nobody will ever finish or leave a
prompt nobody can ever answer.

The fake runners stand in for the two interactive adapters. They are shaped
like the real ones on the point that matters: the request is awaited inline in
the turn, so whatever happens to that await happens to the turn.
"""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentProfile,
    AgentRunId,
    AgentRunStatus,
    ApprovalDecision,
    ApprovalDecisionSource,
    ApprovalId,
    ApprovalKind,
    ApprovalRecord,
    ApprovalStatus,
    Message,
    SessionGrant,
    SessionGrantId,
    WorkspaceId,
)
from engine.ports import (
    AgentTurn,
    ApprovalCapability,
    ApprovalRequest,
    FinishReason,
    StateStore,
    UserInputAnswer,
    UserInputOption,
    UserInputQuestion,
    UserInputResponse,
)
from engine.runtime import (
    AgentSession,
    ApprovalBroker,
    ApprovalConfig,
    ApprovalsUnsupportedError,
    BashApprovalConfig,
    Capabilities,
    normalized_scope,
)
from permission_fakes import (
    KIND_PERMISSION_TRANSLATOR,
    UNCLASSIFIED_PERMISSION_TRANSLATOR,
)

CODER = AgentId("coder")
PLANNER = AgentId("planner")
PROFILES = {
    CODER: AgentProfile(agent_id=CODER, instructions="Be terse.", description="Codes.")
}
#: The same two roles a deployment offers: one asked to change things, one that
#: never is. Everything else about their conversations is identical.
READING_PROFILES = {
    **PROFILES,
    PLANNER: AgentProfile(
        agent_id=PLANNER,
        instructions="Say what you would change.",
        description="Plans.",
        read_only=True,
    ),
}

RUN_TESTS = ApprovalRequest(
    approval_id="provider-1",
    kind=ApprovalKind.COMMAND_EXECUTION,
    reason="Run the test suite",
    command="pytest",
    cwd="/workspace",
    tool_call_id="call-pytest",
)
WRITE_FILE = ApprovalRequest(
    approval_id="provider-2",
    kind=ApprovalKind.FILE_CHANGE,
    reason="Write the regression test",
    tool_name="Write",
    arguments='{"path":"tests/test_worker.py"}',
    allowed_decisions=(ApprovalDecision.ACCEPT, ApprovalDecision.CANCEL),
)
RUN_LINT = replace(
    RUN_TESTS,
    approval_id="provider-3",
    reason="Lint the tree",
    command="ruff check",
    tool_call_id="call-ruff",
)


class ApprovalRunner:
    """An interactive runner that stops for consent before each action.

    Cancelling ends the turn the way both CLIs end it -- an error finish rather
    than an exception -- so a test asserting that the denied action never ran is
    asserting against the shape the real adapters produce.
    """

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    def __init__(
        self,
        requests: Sequence[ApprovalRequest] = (RUN_TESTS,),
        reply: str = "All tests passed.",
    ) -> None:
        self.requests = tuple(requests)
        self.reply = reply
        self.decisions: list[ApprovalDecision] = []
        self.executed: list[str] = []
        self.runs: list[AgentRunId] = []

    async def run_turn(self, *args: object, **kwargs: object) -> AgentTurn:
        raise AssertionError("an interactive turn must not fall back to run_turn")

    async def run_turn_interactive(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        on_approval,
        on_message=None,
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        self.runs.append(agent_run_id)
        for request in self.requests:
            decision = await on_approval(request)
            self.decisions.append(decision)
            if decision is ApprovalDecision.CANCEL:
                message = Message.assistant("Stopped without running that.")
                if on_message is not None:
                    on_message(message)
                return AgentTurn(message, finish_reason=FinishReason.ERROR)
            self.executed.append(request.command or request.tool_name or "")
        message = Message.assistant(self.reply)
        if on_message is not None:
            on_message(message)
        return AgentTurn(message)

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        pass


class DyingApprovalRunner(ApprovalRunner):
    """A provider whose transport breaks while a decision is outstanding."""

    def __init__(self) -> None:
        super().__init__()
        self.presented = asyncio.Event()

    async def run_turn_interactive(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        on_approval,
        on_message=None,
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        self.runs.append(agent_run_id)
        asking = asyncio.create_task(on_approval(RUN_TESTS))
        try:
            await self.presented.wait()
            raise RuntimeError("codex app-server exited 1")
        finally:
            # What the adapters do when they lose the process: the outstanding
            # request is unwound rather than left holding the turn open.
            asking.cancel()
            await asyncio.gather(asking, return_exceptions=True)


class ClassifyingApprovalRunner(ApprovalRunner):
    """An interactive runner whose requests read as Engine capabilities.

    What both real adapters do, and what a policy test needs: an unclassified
    request is put to a person whatever the configuration says, so a runner that
    reads nothing cannot show a policy being applied.
    """

    permission_translator = KIND_PERMISSION_TRANSLATOR


class PlainRunner:
    """A runner with no interactive method at all, like `codex exec`."""

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    def __init__(self) -> None:
        self.seen: list[tuple[Message, ...]] = []

    async def run_turn(
        self, agent_run_id, profile, messages, tools=(), workspace_id=None
    ) -> AgentTurn:
        self.seen.append(tuple(messages))
        return AgentTurn(Message.assistant("Answered without asking."))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        pass


def _session(
    store: StateStore,
    runner: object,
    reader: object | None = None,
) -> AgentSession:
    unused = object()
    return AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=runner,
            communications=unused,
            workspace_provider=unused,
            state_store=store,
        ),
        profiles=PROFILES if reader is None else READING_PROFILES,
        runners={"test": runner},
        read_only_runners=None if reader is None else {"test": reader},
    )


async def _thread(client: httpx.AsyncClient) -> str:
    created = await client.post(
        "/api/threads", json={"agentId": "coder", "runner": "test"}
    )
    return created.json()["id"]


async def _pending(store: StateStore, after: int = 0) -> ApprovalRecord:
    """Wait until the turn has asked its next question, and return it."""
    for _ in range(400):
        approvals = await store.list_approvals()
        if len(approvals) > after and approvals[-1].is_pending:
            return approvals[-1]
        await asyncio.sleep(0.005)
    raise AssertionError("the run never asked for approval")


async def _ignore(record: ApprovalRecord) -> None:
    """A presenter for tests that decide out of band."""


def _lines(response: httpx.Response) -> list[dict[str, object]]:
    return [json.loads(line) for line in response.text.splitlines() if line]


async def _ask(
    broker: ApprovalBroker,
    instance_id: AgentInstanceId,
    request: ApprovalRequest,
    *,
    agent_run_id: AgentRunId,
    runner: str = "test",
    workspace_id: WorkspaceId | None = None,
    answer: ApprovalDecision | None = None,
    translator: object = UNCLASSIFIED_PERMISSION_TRANSLATOR,
    read_only: bool = False,
) -> tuple[ApprovalDecision, list[ApprovalRecord]]:
    """Put one request to the broker as a turn would, and see what happened.

    Returns the decision the paused turn received and everything the presenter
    was shown -- which is the whole question for a grant: a request that was
    presented still pending is one the user was asked about, and one presented
    already decided is one that was answered for them.

    `answer` stands in for a user, and is only reached if no grant matched.
    """
    presented: list[ApprovalRecord] = []

    async def present(record: ApprovalRecord) -> None:
        presented.append(record)
        if record.is_pending and answer is not None:
            await broker.decide(
                record.approval_id,
                answer,
                instance_id=instance_id,
                agent_run_id=agent_run_id,
            )

    handler = broker.handler(
        agent_run_id=agent_run_id,
        instance_id=instance_id,
        runner=runner,
        present=present,
        workspace_id=workspace_id,
        translator=translator,
        read_only=read_only,
    )
    return await handler(request), presented


def test_an_agent_that_only_reads_is_refused_the_edit_the_policy_allows() -> None:
    """Where the read-only runner stops being a suggestion.

    Withholding the tools before the turn only preapproves less; a provider
    that asks anyway reaches this, and a deployment allowing `edit` would
    otherwise hand back exactly what was withheld -- with nobody asked, because
    the policy is a standing yes. Refused here instead, and refused rather than
    put to a person: this one was settled by what the agent is.
    """
    store = InMemoryStateStore()
    broker = ApprovalBroker(
        store,
        ApprovalConfig(allow=(ApprovalCapability.READ, ApprovalCapability.EDIT)),
    )

    async def scenario():
        instance = await store.create_instance(AgentId("planner"), runner="test")
        decision, presented = await _ask(
            broker,
            instance.instance_id,
            WRITE_FILE,
            agent_run_id=AgentRunId("ar-1"),
            translator=KIND_PERMISSION_TRANSLATOR,
            read_only=True,
        )
        return decision, presented

    decision, presented = asyncio.run(scenario())

    assert decision is ApprovalDecision.CANCEL
    # Recorded, and recorded as settled: "what was this agent allowed to do, and
    # on whose say-so" stays answerable for a refusal too.
    assert [record.status for record in presented] == [ApprovalStatus.DECIDED]
    assert presented[0].decision_source is ApprovalDecisionSource.POLICY


def test_the_same_edit_is_allowed_for_an_agent_that_is_not_read_only() -> None:
    """The other half of the pair: nothing about the policy changed."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(
        store,
        ApprovalConfig(allow=(ApprovalCapability.READ, ApprovalCapability.EDIT)),
    )

    async def scenario():
        instance = await store.create_instance(CODER, runner="test")
        return await _ask(
            broker,
            instance.instance_id,
            WRITE_FILE,
            agent_run_id=AgentRunId("ar-1"),
            translator=KIND_PERMISSION_TRANSLATOR,
        )

    decision, presented = asyncio.run(scenario())

    assert decision is ApprovalDecision.ACCEPT
    assert presented[0].decision_source is ApprovalDecisionSource.POLICY


def test_a_planning_chat_refuses_the_edit_its_deployment_allows(web_app) -> None:
    """The same refusal, down the path a real chat takes.

    `ThreadService.start_run` is what installs the handler, so a property proved
    against the broker alone is a property of the broker. Here the policy is the
    one this repository ships, the conversation is an ordinary chat, and the
    only thing that differs is the agent it was started with.
    """
    store = InMemoryStateStore()
    writer = ClassifyingApprovalRunner((WRITE_FILE,))
    reader = ClassifyingApprovalRunner((WRITE_FILE,))
    app = web_app(
        _session(store, writer, reader),
        {"test": writer},
        approval_policy=ApprovalConfig(
            allow=(ApprovalCapability.READ, ApprovalCapability.EDIT)
        ),
    )
    service = app.state.thread_service

    async def scenario():
        planning = await service.create(PLANNER, "test")
        run = await service.start_run(planning.instance_id, "how would you", None)
        async for _line in run.stream():
            pass
        coding = await service.create(CODER, "test")
        second = await service.start_run(coding.instance_id, "do it", None)
        async for _line in second.stream():
            pass
        return await store.list_approvals()

    approvals = asyncio.run(scenario())

    # The planner's request was refused by the configuration; the coder's, in
    # the same process under the same policy, was allowed by it.
    assert reader.decisions == [ApprovalDecision.CANCEL]
    assert writer.decisions == [ApprovalDecision.ACCEPT]
    assert reader.executed == []
    assert [
        (record.decision, record.decision_source) for record in approvals
    ] == [
        (ApprovalDecision.CANCEL, ApprovalDecisionSource.POLICY),
        (ApprovalDecision.ACCEPT, ApprovalDecisionSource.POLICY),
    ]


def test_a_chat_asks_the_runner_that_answers_it_whether_it_can_pause(web_app) -> None:
    """Both halves of the handler are read off the runner that runs the turn.

    A read-only runner need not be the same kind of object as the write-enabled
    one it stands in for -- `codex exec` cannot pause at all. Installing a
    handler because the *other* one could would fail every planning turn.
    """
    store = InMemoryStateStore()
    writer = ApprovalRunner()
    reader = PlainRunner()
    service = web_app(
        _session(store, writer, reader), {"test": writer}, approval_policy=ApprovalConfig()
    ).state.thread_service

    async def scenario():
        thread = await service.create(PLANNER, "test")
        run = await service.start_run(thread.instance_id, "how would you", None)
        async for _line in run.stream():
            pass
        return await service.history(thread.instance_id)

    history = asyncio.run(scenario())

    assert [message.content for message in history] == [
        "how would you",
        "Answered without asking.",
    ]
    assert writer.runs == []


def test_plan_approval_ignores_system_auto_approve() -> None:
    async def scenario() -> tuple[object, list[ApprovalRecord]]:
        store = InMemoryStateStore()
        instance = await store.create_instance(CODER)
        broker = ApprovalBroker(store, ApprovalConfig(auto_approve=True))
        presented: list[ApprovalRecord] = []

        async def present(record: ApprovalRecord) -> None:
            presented.append(record)
            if record.is_pending:
                await broker.decide(
                    record.approval_id,
                    ApprovalDecision.ACCEPT,
                    instance_id=instance.instance_id,
                    agent_run_id=AgentRunId("run-plan"),
                )

        response = await broker.handler(
            agent_run_id=AgentRunId("run-plan"),
            instance_id=instance.instance_id,
            runner="claude",
            present=present,
        )(
            ApprovalRequest(
                approval_id="provider-plan",
                kind=ApprovalKind.PLAN_APPROVAL,
                tool_name="ExitPlanMode",
                allowed_decisions=(ApprovalDecision.ACCEPT, ApprovalDecision.CANCEL),
                requires_human=True,
            )
        )
        return response, presented

    response, presented = asyncio.run(scenario())

    assert response is ApprovalDecision.ACCEPT
    assert presented[0].status is ApprovalStatus.PENDING
    assert presented[0].decision_source is None


def test_structured_question_returns_and_persists_human_answers() -> None:
    async def scenario() -> tuple[object, ApprovalRecord]:
        store = InMemoryStateStore()
        instance = await store.create_instance(CODER)
        broker = ApprovalBroker(store, ApprovalConfig(auto_approve=True))
        run_id = AgentRunId("run-question")

        async def present(record: ApprovalRecord) -> None:
            if record.is_pending:
                await broker.answer(
                    record.approval_id,
                    (UserInputAnswer("api", ("Public",)),),
                    instance_id=instance.instance_id,
                    agent_run_id=run_id,
                )

        response = await broker.handler(
            agent_run_id=run_id,
            instance_id=instance.instance_id,
            runner="codex",
            present=present,
        )(
            ApprovalRequest(
                approval_id="provider-question",
                kind=ApprovalKind.USER_INPUT,
                tool_name="request_user_input",
                allowed_decisions=(ApprovalDecision.CANCEL,),
                questions=(UserInputQuestion(
                    question_id="api",
                    header="API",
                    question="Which API?",
                    options=(UserInputOption("Public"), UserInputOption("Internal")),
                ),),
                requires_human=True,
            )
        )
        records = await store.list_approvals()
        return response, records[0]

    response, record = asyncio.run(scenario())

    assert response == UserInputResponse((UserInputAnswer("api", ("Public",)),))
    assert record.status is ApprovalStatus.DECIDED
    assert json.loads(record.answers or "{}") == {"api": ["Public"]}
    assert record.decision_source is ApprovalDecisionSource.USER


def test_structured_question_can_be_cancelled_without_answers() -> None:
    async def scenario() -> tuple[object, ApprovalRecord]:
        store = InMemoryStateStore()
        instance = await store.create_instance(CODER)
        broker = ApprovalBroker(store)
        run_id = AgentRunId("run-question")

        async def present(record: ApprovalRecord) -> None:
            if record.is_pending:
                await broker.decide(
                    record.approval_id,
                    ApprovalDecision.CANCEL,
                    instance_id=instance.instance_id,
                    agent_run_id=run_id,
                )

        response = await broker.handler(
            agent_run_id=run_id,
            instance_id=instance.instance_id,
            runner="codex",
            present=present,
        )(
            ApprovalRequest(
                approval_id="provider-question",
                kind=ApprovalKind.USER_INPUT,
                tool_name="request_user_input",
                allowed_decisions=(ApprovalDecision.CANCEL,),
                questions=(
                    UserInputQuestion(
                        question_id="api",
                        header="API",
                        question="Which API?",
                    ),
                ),
                requires_human=True,
            )
        )
        records = await store.list_approvals()
        return response, records[0]

    response, record = asyncio.run(scenario())

    assert response is ApprovalDecision.CANCEL
    assert record.status is ApprovalStatus.DECIDED
    assert record.decision is ApprovalDecision.CANCEL
    assert record.answers is None


def _approval_url(thread_id: str, approval_id: str) -> str:
    return f"/api/threads/{thread_id}/runs/current/approvals/{approval_id}"


# --- the broker -------------------------------------------------------------


def test_a_request_is_durable_before_anybody_is_shown_it() -> None:
    """Otherwise a decision could arrive for something no reader can explain."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)
    when_presented: list[ApprovalRecord | None] = []

    async def scenario() -> ApprovalDecision:
        instance = await store.create_instance(CODER)
        presented: asyncio.Future = asyncio.get_running_loop().create_future()

        async def present(record: ApprovalRecord) -> None:
            # What the store already knows at the moment it becomes visible.
            when_presented.append(await store.load_approval(record.approval_id))
            presented.set_result(record)

        handler = broker.handler(
            agent_run_id=AgentRunId("ar-1"),
            instance_id=instance.instance_id,
            runner="test",
            present=present,
        )
        paused = asyncio.create_task(handler(RUN_TESTS))
        record = await presented
        await broker.decide(
            record.approval_id,
            ApprovalDecision.ACCEPT,
            instance_id=instance.instance_id,
            agent_run_id=AgentRunId("ar-1"),
        )
        return await paused

    decision = asyncio.run(scenario())
    stored = when_presented[0]

    assert decision is ApprovalDecision.ACCEPT
    assert stored is not None
    assert stored.is_pending
    assert stored.command == "pytest"
    assert stored.kind is ApprovalKind.COMMAND_EXECUTION
    assert stored.allowed_decisions == tuple(ApprovalDecision)


def test_a_decision_is_durable_before_the_provider_is_told() -> None:
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)
    when_resumed: list[ApprovalRecord | None] = []

    async def scenario() -> None:
        instance = await store.create_instance(CODER)
        presented: asyncio.Future = asyncio.get_running_loop().create_future()

        async def present(record: ApprovalRecord) -> None:
            presented.set_result(record)

        handler = broker.handler(
            agent_run_id=AgentRunId("ar-1"),
            instance_id=instance.instance_id,
            runner="test",
            present=present,
        )

        async def paused_turn() -> ApprovalDecision:
            decision = await handler(RUN_TESTS)
            # What the store knows at the instant the turn is let go again.
            record = presented.result()
            when_resumed.append(await store.load_approval(record.approval_id))
            return decision

        turn = asyncio.create_task(paused_turn())
        record = await presented
        await broker.decide(
            record.approval_id,
            ApprovalDecision.ACCEPT,
            instance_id=instance.instance_id,
            agent_run_id=AgentRunId("ar-1"),
        )
        assert await turn is ApprovalDecision.ACCEPT

    asyncio.run(scenario())
    decided = when_resumed[0]

    assert decided is not None
    assert decided.status is ApprovalStatus.DECIDED
    assert decided.decision is ApprovalDecision.ACCEPT
    assert decided.decision_source is ApprovalDecisionSource.USER
    assert decided.decided_at is not None


def test_a_run_that_ends_without_answering_interrupts_what_it_asked() -> None:
    """The backstop for a turn torn down while a question was outstanding."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)

    async def scenario() -> Sequence[ApprovalRecord]:
        instance = await store.create_instance(CODER)
        await store.record_approval(
            ApprovalRecord(
                approval_id=ApprovalId("apv-orphan"),
                agent_run_id=AgentRunId("ar-1"),
                instance_id=instance.instance_id,
                runner="test",
                kind=ApprovalKind.COMMAND_EXECUTION,
                requested_at=datetime.now(UTC),
                allowed_decisions=tuple(ApprovalDecision),
            )
        )
        await broker.interrupt_run(AgentRunId("ar-1"))
        return await store.list_approvals()

    approvals = asyncio.run(scenario())

    assert [record.status for record in approvals] == [ApprovalStatus.INTERRUPTED]
    # Interrupted is not a decision somebody made, and does not pretend to be.
    assert approvals[0].decision is None


def test_an_approval_callback_on_a_runner_that_cannot_pause_is_refused() -> None:
    """Ignoring it would run unattended what the caller believes is gated."""
    store = InMemoryStateStore()
    runner = PlainRunner()
    session = _session(store, runner)

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        raise AssertionError("nothing should reach the handler")

    async def scenario() -> None:
        instance = await session.start(CODER)
        await session.say(instance.instance_id, "go", on_approval=approve)

    with pytest.raises(ApprovalsUnsupportedError):
        asyncio.run(scenario())
    assert runner.seen == []


def test_a_cancelled_turn_is_recorded_as_cancelled_not_failed() -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    session = _session(store, runner)
    broker = ApprovalBroker(store)
    cancelled = AgentRunId("ar-cancelled")

    async def scenario():
        instance = await session.start(CODER)
        handler = broker.handler(
            agent_run_id=cancelled,
            instance_id=instance.instance_id,
            runner="test",
            present=_ignore,
        )
        turn = asyncio.create_task(
            session.say(
                instance.instance_id,
                "run the tests",
                on_approval=handler,
                agent_run_id=cancelled,
            )
        )
        request = await _pending(store)
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        return (
            await store.agent_run(cancelled),
            await store.load_approval(request.approval_id),
            broker.is_waiting(request.approval_id),
        )

    agent_run, approval, waiting = asyncio.run(scenario())

    assert agent_run is not None
    assert agent_run.status is AgentRunStatus.CANCELLED
    assert approval is not None
    assert approval.status is ApprovalStatus.INTERRUPTED
    assert waiting is False
    assert runner.executed == []


# --- persistence ------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def store(request, sqlite_store) -> StateStore:
    """The two stores a running process uses, held to the same contract."""
    if request.param == "memory":
        return InMemoryStateStore()
    return sqlite_store()


def test_stores_agree_on_what_an_approval_is(store: StateStore) -> None:
    async def scenario():
        instance = await store.create_instance(CODER, runner="claude")
        asked = ApprovalRecord(
            approval_id=ApprovalId("apv-1"),
            agent_run_id=AgentRunId("ar-1"),
            instance_id=instance.instance_id,
            runner="claude",
            kind=ApprovalKind.COMMAND_EXECUTION,
            requested_at=datetime.now(UTC),
            reason="Run the test suite",
            command="pytest",
            cwd="/workspace",
            tool_name="Bash",
            arguments='{"command":"pytest"}',
            allowed_decisions=(ApprovalDecision.ACCEPT, ApprovalDecision.CANCEL),
        )
        later = replace(
            asked,
            approval_id=ApprovalId("apv-2"),
            agent_run_id=AgentRunId("ar-2"),
            kind=ApprovalKind.TOOL_USE,
            command=None,
            cwd=None,
            tool_name="WebFetch",
        )
        await store.record_approval(asked)
        await store.record_approval(later)
        decided = replace(
            asked,
            status=ApprovalStatus.DECIDED,
            decision=ApprovalDecision.CANCEL,
            decision_source=ApprovalDecisionSource.USER,
            decided_at=datetime.now(UTC),
        )
        await store.record_approval(decided)
        return (
            decided,
            await store.load_approval(ApprovalId("apv-1")),
            await store.load_approval(ApprovalId("apv-missing")),
            await store.list_approvals(),
            await store.list_approvals(agent_run_id=AgentRunId("ar-2")),
            await store.list_approvals(status=ApprovalStatus.PENDING),
            await store.list_approvals(instance_id=AgentInstanceId("agi-elsewhere")),
        )

    decided, loaded, missing, everything, by_run, pending, elsewhere = asyncio.run(
        scenario()
    )

    assert loaded == decided
    assert missing is None
    # Answering a request does not move it in the log of what was asked.
    assert [record.approval_id for record in everything] == ["apv-1", "apv-2"]
    assert [record.approval_id for record in by_run] == ["apv-2"]
    assert [record.approval_id for record in pending] == ["apv-2"]
    assert elsewhere == ()


def test_stores_agree_on_what_a_session_grant_is(store: StateStore) -> None:
    """A grant that did not survive the process would expire before the turn it
    exists to answer: the provider it was given to is gone by then."""

    async def scenario():
        instance = await store.create_instance(CODER, runner="claude")
        elsewhere = await store.create_instance(CODER)
        granted = SessionGrant(
            grant_id=SessionGrantId("grant-1"),
            instance_id=instance.instance_id,
            runner="claude",
            approval_kind=ApprovalKind.COMMAND_EXECUTION,
            normalized_scope="command_execution|/workspace|pytest",
            created_from_approval_id=ApprovalId("apv-1"),
            created_at=datetime.now(UTC),
            workspace_id=WorkspaceId("ws-1"),
        )
        other_conversation = replace(
            granted,
            grant_id=SessionGrantId("grant-2"),
            instance_id=elsewhere.instance_id,
        )
        await store.record_session_grant(granted)
        await store.record_session_grant(other_conversation)
        revoked = replace(granted, revoked_at=datetime.now(UTC))
        await store.record_session_grant(revoked)
        return (
            revoked,
            await store.list_session_grants(),
            await store.list_session_grants(instance_id=instance.instance_id),
        )

    revoked, everything, mine = asyncio.run(scenario())

    # Revoking is an update to one grant, not a new one and not a deletion:
    # what was in force for a while is how a past request came to be allowed.
    assert [grant.grant_id for grant in everything] == ["grant-1", "grant-2"]
    assert [grant.grant_id for grant in mine] == ["grant-1"]
    assert mine[0] == revoked
    assert mine[0].is_active is False
    assert mine[0].workspace_id == WorkspaceId("ws-1")
    assert mine[0].created_from_approval_id == ApprovalId("apv-1")


def test_a_session_grant_needs_a_conversation_to_belong_to(store: StateStore) -> None:
    """Grants never cross conversations, so one with nowhere to belong is a
    grant nothing could ever match -- and nothing could ever audit."""
    orphan = SessionGrant(
        grant_id=SessionGrantId("grant-1"),
        instance_id=AgentInstanceId("agi-nope"),
        runner="test",
        approval_kind=ApprovalKind.COMMAND_EXECUTION,
        normalized_scope="command_execution||pytest",
        created_from_approval_id=ApprovalId("apv-1"),
        created_at=datetime.now(UTC),
    )

    with pytest.raises(KeyError):
        asyncio.run(store.record_session_grant(orphan))


def test_an_approval_needs_a_conversation_to_belong_to(store: StateStore) -> None:
    """An approval no reviewer could reach from a conversation is a leak."""
    orphan = ApprovalRecord(
        approval_id=ApprovalId("apv-1"),
        agent_run_id=AgentRunId("ar-1"),
        instance_id=AgentInstanceId("agi-nope"),
        runner="test",
        kind=ApprovalKind.TOOL_USE,
        requested_at=datetime.now(UTC),
    )

    with pytest.raises(KeyError):
        asyncio.run(store.record_approval(orphan))


# --- the HTTP surface -------------------------------------------------------


def test_approving_resumes_the_paused_turn_and_records_the_decision(web_app) -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = web_app(_session(store, runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            started = asyncio.create_task(
                client.post(
                    f"/api/threads/{thread_id}/runs", json={"text": "run the tests"}
                )
            )
            request = await _pending(store)
            decided = await client.post(
                _approval_url(thread_id, request.approval_id),
                json={"decision": "accept"},
            )
            streamed = await started
            history = await client.get(f"/api/threads/{thread_id}/messages")
            stored = await store.load_approval(request.approval_id)
            return request, decided, streamed, history, stored

    request, decided, streamed, history, stored = asyncio.run(scenario())
    events = _lines(streamed)
    approvals = [event for event in events if event["type"] == "approval"]

    assert request.agent_run_id == runner.runs[0]
    assert request.runner == "test"
    assert decided.status_code == 200
    assert decided.json()["approval"]["status"] == "decided"
    assert decided.json()["approval"]["decision"] == "accept"
    # The whole request reaches the stream, before any decision was made.
    assert approvals[0]["approval"] == {
        "id": str(request.approval_id),
        "status": "pending",
        "kind": "command_execution",
        "reason": "Run the test suite",
        "command": "pytest",
        "cwd": "/workspace",
        "toolName": None,
        # The call the provider named, so the client can show the request beside
        # it rather than at the end of the turn.
        "toolCallId": "call-pytest",
        "arguments": None,
        "allowedDecisions": ["accept", "accept_for_session", "cancel"],
        "decision": None,
        "decisionSource": None,
    }
    assert events[-1]["type"] == "done"
    assert runner.decisions == [ApprovalDecision.ACCEPT]
    assert runner.executed == ["pytest"]
    assert stored is not None
    assert stored.status is ApprovalStatus.DECIDED
    assert stored.decision is ApprovalDecision.ACCEPT
    assert [
        (message["role"], message["content"][0]["text"])
        for message in history.json()["messages"]
    ] == [("user", "run the tests"), ("assistant", "All tests passed.")]


def test_a_decision_reaches_the_client_even_when_the_turn_asks_again(web_app) -> None:
    """Otherwise the card you just answered waits on an answer it already got.

    A turn let go by a decision usually asks its next question immediately --
    Claude Code asks before every tool call -- and does so without ever handing
    the event loop back, so both snapshots land between two wakes of the same
    stream. The one being replaced is the decision, which is the only thing the
    client is waiting to hear.
    """
    store = InMemoryStateStore()
    runner = ApprovalRunner([RUN_TESTS, WRITE_FILE])
    app = web_app(_session(store, runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            started = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "go"})
            )
            first = await _pending(store)
            await client.post(
                _approval_url(thread_id, first.approval_id),
                json={"decision": "accept"},
            )
            second = await _pending(store, after=1)
            await client.post(
                _approval_url(thread_id, second.approval_id),
                json={"decision": "accept"},
            )
            return first, second, await started

    first, second, streamed = asyncio.run(scenario())
    events = _lines(streamed)

    assert [
        (event["approval"]["id"], event["approval"]["status"])
        for event in events
        if event["type"] == "approval"
    ] == [
        (str(first.approval_id), "pending"),
        (str(first.approval_id), "decided"),
        (str(second.approval_id), "pending"),
        (str(second.approval_id), "decided"),
    ]
    assert events[-1]["type"] == "done"
    assert runner.executed == ["pytest", "Write"]


def test_cancelling_a_request_ends_the_turn_without_running_the_action(web_app) -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = web_app(_session(store, runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            started = asyncio.create_task(
                client.post(
                    f"/api/threads/{thread_id}/runs", json={"text": "run the tests"}
                )
            )
            request = await _pending(store)
            decided = await client.post(
                _approval_url(thread_id, request.approval_id),
                json={"decision": "cancel"},
            )
            streamed = await started
            return decided, streamed, await store.load_approval(request.approval_id)

    decided, streamed, stored = asyncio.run(scenario())

    assert decided.status_code == 200
    assert runner.decisions == [ApprovalDecision.CANCEL]
    assert runner.executed == []
    assert stored is not None
    assert stored.status is ApprovalStatus.DECIDED
    assert stored.decision is ApprovalDecision.CANCEL
    assert _lines(streamed)[-1]["type"] == "done"


def test_a_second_decision_on_the_same_request_is_refused(web_app) -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = web_app(_session(store, runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            started = asyncio.create_task(
                client.post(
                    f"/api/threads/{thread_id}/runs", json={"text": "run the tests"}
                )
            )
            request = await _pending(store)
            url = _approval_url(thread_id, request.approval_id)
            first = await client.post(url, json={"decision": "accept"})
            second = await client.post(url, json={"decision": "cancel"})
            await started
            return first, second

    first, second = asyncio.run(scenario())

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already decided" in second.json()["error"]
    # The losing decision changed nothing: the command ran once, as approved.
    assert runner.decisions == [ApprovalDecision.ACCEPT]
    assert runner.executed == ["pytest"]


@pytest.mark.parametrize(
    ("decision", "detail"),
    [("maybe", "unknown decision"), ("accept_for_session", "not one of the decisions")],
)
def test_a_decision_the_request_never_offered_is_refused(
    decision: str, detail: str, *, web_app
) -> None:
    """Including one that exists: the provider decides what it can honour."""
    store = InMemoryStateStore()
    runner = ApprovalRunner([WRITE_FILE])
    app = web_app(_session(store, runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            started = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "write it"})
            )
            request = await _pending(store)
            refused = await client.post(
                _approval_url(thread_id, request.approval_id),
                json={"decision": decision},
            )
            unchanged = await store.load_approval(request.approval_id)
            await client.delete(f"/api/threads/{thread_id}/runs/current")
            await started
            return refused, unchanged

    refused, unchanged = asyncio.run(scenario())

    assert refused.status_code == 400
    assert detail in refused.json()["error"]
    # Refusing a decision must not resolve the request it was aimed at.
    assert unchanged is not None
    assert unchanged.is_pending
    assert runner.executed == []


def test_a_request_from_a_finished_run_cannot_be_answered_later(web_app) -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner([RUN_TESTS, WRITE_FILE])
    app = web_app(_session(store, runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            first_run = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "first"})
            )
            stale = await _pending(store)
            await client.post(
                _approval_url(thread_id, stale.approval_id),
                json={"decision": "accept"},
            )
            second = await _pending(store, after=1)
            await client.post(
                _approval_url(thread_id, second.approval_id),
                json={"decision": "accept"},
            )
            await first_run

            # A second turn of the same conversation, and the first turn's
            # request offered again once it is paused.
            runner.requests = (RUN_TESTS,)
            second_run = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "second"})
            )
            current = await _pending(store, after=2)
            refused = await client.post(
                _approval_url(thread_id, stale.approval_id),
                json={"decision": "accept"},
            )
            await client.delete(f"/api/threads/{thread_id}/runs/current")
            await second_run
            return refused, current, await store.list_approvals()

    refused, current, approvals = asyncio.run(scenario())

    assert refused.status_code == 409
    # Two sequential requests in one turn, each its own record, then a third
    # belonging to a run that has nothing to do with them.
    assert [record.command for record in approvals] == ["pytest", None, "pytest"]
    assert approvals[0].agent_run_id == approvals[1].agent_run_id
    assert approvals[2].agent_run_id == current.agent_run_id
    assert approvals[2].agent_run_id != approvals[0].agent_run_id
    assert runner.executed == ["pytest", "Write"]


def test_another_conversations_request_is_not_found_here(web_app) -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = web_app(_session(store, runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            asking = await _thread(client)
            bystander = await _thread(client)
            started = asyncio.create_task(
                client.post(f"/api/threads/{asking}/runs", json={"text": "run"})
            )
            request = await _pending(store)
            refused = await client.post(
                _approval_url(bystander, request.approval_id),
                json={"decision": "accept"},
            )
            unchanged = await store.load_approval(request.approval_id)
            await client.delete(f"/api/threads/{asking}/runs/current")
            await started
            return refused, unchanged

    refused, unchanged = asyncio.run(scenario())

    assert refused.status_code == 404
    assert unchanged is not None
    assert unchanged.is_pending
    assert runner.executed == []


def test_a_reconnecting_browser_is_told_what_the_run_is_waiting_on(web_app) -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = web_app(_session(store, runner), {"test": runner})
    service = app.state.thread_service

    async def scenario():
        thread = await service.create(CODER, "test")
        run = await service.start_run(thread.instance_id, "run the tests", None)
        request = await _pending(store)

        original = run.stream()
        first = [json.loads((await anext(original)).decode()) for _ in range(2)]
        await original.aclose()  # the browser went away

        # The provider is alive and still waiting, so the pause is still real.
        assert service.active_run(thread.instance_id) is run
        still_pending = await store.load_approval(request.approval_id)

        resumed = run.stream()
        replayed = [json.loads((await anext(resumed)).decode()) for _ in range(2)]
        await resumed.aclose()

        await service.decide_approval(thread.instance_id, request.approval_id, "accept")
        async for _line in run.stream():
            pass
        return first, replayed, still_pending

    first, replayed, still_pending = asyncio.run(scenario())

    assert still_pending is not None
    assert still_pending.is_pending
    assert first[0]["type"] == "approval"
    assert first[0]["approval"]["status"] == "pending"
    assert first[0]["approval"]["command"] == "pytest"
    assert first[1]["type"] == "content"
    # The same pause, told to the connection that arrived after it started.
    assert replayed == first
    assert runner.executed == ["pytest"]


def test_stopping_a_waiting_run_cancels_the_request_and_the_turn(web_app) -> None:
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = web_app(_session(store, runner), {"test": runner})
    service = app.state.thread_service

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = AgentInstanceId(await _thread(client))
            started = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "run"})
            )
            request = await _pending(store)
            run = service.active_run(thread_id)
            assert run is not None
            stopped = await client.delete(f"/api/threads/{thread_id}/runs/current")
            streamed = await started
            return (
                stopped,
                streamed,
                await store.load_approval(request.approval_id),
                await store.agent_run(run.agent_run_id),
                service.approvals.is_waiting(request.approval_id),
                service.active_run(thread_id),
            )

    stopped, streamed, stored, agent_run, waiting, active = asyncio.run(scenario())

    assert stopped.status_code == 204
    assert stored is not None
    assert stored.status is ApprovalStatus.DECIDED
    assert stored.decision is ApprovalDecision.CANCEL
    assert agent_run is not None
    assert agent_run.status is AgentRunStatus.CANCELLED
    # Nothing is left holding the turn open, and nothing is left to answer.
    assert waiting is False
    assert active is None
    assert runner.executed == []
    events = _lines(streamed)
    assert events[-1]["type"] == "done"
    # The client is told the prompt is gone rather than left showing it.
    assert [
        event["approval"]["status"] for event in events if event["type"] == "approval"
    ][-1] == "decided"


def test_a_provider_that_dies_mid_question_leaves_the_request_interrupted(
    web_app,
) -> None:
    store = InMemoryStateStore()
    runner = DyingApprovalRunner()
    app = web_app(_session(store, runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            started = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "run"})
            )
            request = await _pending(store)
            runner.presented.set()  # the CLI goes away mid-question
            streamed = await started
            late = await client.post(
                _approval_url(thread_id, request.approval_id),
                json={"decision": "accept"},
            )
            return streamed, late, await store.load_approval(request.approval_id)

    streamed, late, stored = asyncio.run(scenario())

    assert stored is not None
    assert stored.status is ApprovalStatus.INTERRUPTED
    assert stored.decision is None
    assert late.status_code == 409
    events = _lines(streamed)
    assert events[-1]["type"] == "error"
    # The prompt stops being a prompt for the client too, not only in the store.
    assert [
        event["approval"]["status"] for event in events if event["type"] == "approval"
    ][-1] == "interrupted"


def test_a_restart_interrupts_the_requests_it_cannot_resume(
    tmp_path, *, sqlite_store, web_app
) -> None:
    """The CLI died with the server, so its question can no longer be put."""
    database = tmp_path / "conversations.sqlite3"
    before = sqlite_store(database)
    instance = asyncio.run(before.create_instance(CODER, runner="test"))
    asyncio.run(
        before.record_approval(
            ApprovalRecord(
                approval_id=ApprovalId("apv-before-restart"),
                agent_run_id=AgentRunId("ar-before-restart"),
                instance_id=instance.instance_id,
                runner="test",
                kind=ApprovalKind.COMMAND_EXECUTION,
                requested_at=datetime.now(UTC),
                command="pytest",
                tool_call_id="thread-1:cmd-1",
                allowed_decisions=tuple(ApprovalDecision),
            )
        )
    )
    before.close()

    after = sqlite_store(database)
    runner = ApprovalRunner()
    app = web_app(_session(after, runner), {"test": runner}, approval_policy=ApprovalConfig())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/threads")
            refused = await client.post(
                _approval_url(instance.instance_id, "apv-before-restart"),
                json={"decision": "accept"},
            )
            recovered = await after.load_approval(ApprovalId("apv-before-restart"))
            return listed, refused, recovered

    try:
        listed, refused, recovered = asyncio.run(scenario())
    finally:
        after.close()

    assert listed.status_code == 200
    assert recovered is not None
    assert recovered.status is ApprovalStatus.INTERRUPTED
    # The request survives the restart intact -- including the call it was about,
    # which is what keeps a reloaded transcript showing it in the right place.
    # Only its answerability changes.
    assert recovered.command == "pytest"
    assert recovered.tool_call_id == "thread-1:cmd-1"
    assert refused.status_code == 409
    assert "interrupted" in refused.json()["error"]


# --- session grants ---------------------------------------------------------
#
# The session is the conversation, not the CLI subprocess: both providers forget
# a session grant when their process exits, and ours exits at the end of every
# turn. So the tests that matter here are the ones that cross a run boundary --
# a grant that only worked inside one turn would be indistinguishable from the
# provider's own memory, and would not be worth persisting.


def _record(**overrides: object) -> ApprovalRecord:
    fields: dict[str, object] = {
        "approval_id": ApprovalId("apv-1"),
        "agent_run_id": AgentRunId("ar-1"),
        "instance_id": AgentInstanceId("agi-1"),
        "runner": "test",
        "kind": ApprovalKind.COMMAND_EXECUTION,
        "requested_at": datetime.now(UTC),
        "allowed_decisions": tuple(ApprovalDecision),
    }
    fields.update(overrides)
    return ApprovalRecord(**fields)  # type: ignore[arg-type]


def test_a_command_normalizes_to_itself_and_where_it_would_run() -> None:
    """Re-rendered whitespace is the same command; another directory is not."""
    here = normalized_scope(_record(command=" pytest   -q\n", cwd="/workspace/"))

    assert here == "command_execution|/workspace|pytest -q"
    assert here == normalized_scope(_record(command="pytest -q", cwd="/workspace"))
    assert here != normalized_scope(_record(command="pytest -q", cwd="/elsewhere"))
    assert here != normalized_scope(_record(command="pytest -q .", cwd="/workspace"))


def test_a_file_change_normalizes_to_the_files_it_touches() -> None:
    """Claude names the path; Codex maps paths to edits. One file either way.

    Not the edit itself: two writes to one file differ in every byte of their
    content, so a grant scoped to the content would never match anything and
    the feature would quietly do nothing.
    """
    claude = normalized_scope(
        _record(
            kind=ApprovalKind.FILE_CHANGE,
            cwd="/workspace",
            tool_name="Write",
            arguments='{"content": "print(1)\\n", "file_path": "src/app.py"}',
        )
    )
    codex = normalized_scope(
        _record(
            kind=ApprovalKind.FILE_CHANGE,
            cwd="/workspace",
            tool_name="file_change",
            arguments='{"changes": {"/workspace/src/app.py": {"kind": "update"}}}',
        )
    )
    rewritten = normalized_scope(
        _record(
            kind=ApprovalKind.FILE_CHANGE,
            cwd="/workspace",
            tool_name="Write",
            arguments='{"content": "print(2)\\n", "file_path": "/workspace/src/app.py"}',
        )
    )

    assert claude == "file_change|/workspace/src/app.py"
    assert codex == claude
    assert rewritten == claude
    assert claude != normalized_scope(
        _record(
            kind=ApprovalKind.FILE_CHANGE,
            cwd="/workspace",
            arguments='{"file_path": "src/other.py"}',
        )
    )


def test_a_tool_use_normalizes_to_the_tool_and_its_arguments() -> None:
    """Argument order is a serializer's choice, not part of what was asked."""
    scope = normalized_scope(
        _record(
            kind=ApprovalKind.TOOL_USE,
            tool_name="WebFetch",
            arguments='{"prompt": "read it", "url": "https://example.test"}',
        )
    )

    assert scope == normalized_scope(
        _record(
            kind=ApprovalKind.TOOL_USE,
            tool_name="WebFetch",
            arguments='{"url": "https://example.test", "prompt": "read it"}',
        )
    )
    assert scope != normalized_scope(
        _record(
            kind=ApprovalKind.TOOL_USE,
            tool_name="WebFetch",
            arguments='{"url": "https://elsewhere.test", "prompt": "read it"}',
        )
    )


@pytest.mark.parametrize(
    ("what", "record"),
    [
        ("a command nobody told us", _record(command=None)),
        ("a file change with no file in it", _record(kind=ApprovalKind.FILE_CHANGE)),
        (
            "a file change whose arguments will not parse",
            _record(kind=ApprovalKind.FILE_CHANGE, arguments="<not json>"),
        ),
        ("a tool with no name", _record(kind=ApprovalKind.TOOL_USE, tool_name=None)),
    ],
)
def test_a_request_we_cannot_reduce_to_one_action_is_never_reusable(
    what: str, record: ApprovalRecord
) -> None:
    """It will be asked every time, which is the safe direction to fail in."""
    assert normalized_scope(record) is None, what


def test_allowing_for_the_session_records_exactly_what_it_covers() -> None:
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)

    async def scenario():
        instance = await store.create_instance(CODER, runner="test")
        decision, presented = await _ask(
            broker,
            instance.instance_id,
            RUN_TESTS,
            agent_run_id=AgentRunId("ar-1"),
            workspace_id=WorkspaceId("ws-1"),
            answer=ApprovalDecision.ACCEPT_FOR_SESSION,
        )
        return instance, decision, presented, await store.list_session_grants()

    instance, decision, presented, grants = asyncio.run(scenario())
    grant = grants[0]

    assert decision is ApprovalDecision.ACCEPT_FOR_SESSION
    assert len(grants) == 1
    assert grant.instance_id == instance.instance_id
    assert grant.runner == "test"
    assert grant.approval_kind is ApprovalKind.COMMAND_EXECUTION
    assert grant.normalized_scope == "command_execution|/workspace|pytest"
    assert grant.workspace_id == WorkspaceId("ws-1")
    assert grant.created_from_approval_id == presented[0].approval_id
    assert grant.created_at is not None
    assert grant.revoked_at is None


@pytest.mark.parametrize(
    "decision", [ApprovalDecision.ACCEPT, ApprovalDecision.CANCEL]
)
def test_the_other_two_decisions_grant_nothing(decision: ApprovalDecision) -> None:
    """Approving once is approving once. Only the session button is durable."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)

    async def scenario():
        instance = await store.create_instance(CODER, runner="test")
        await _ask(
            broker,
            instance.instance_id,
            RUN_TESTS,
            agent_run_id=AgentRunId("ar-1"),
            answer=decision,
        )
        return await store.list_session_grants()

    assert asyncio.run(scenario()) == ()


def test_a_matching_request_in_a_later_run_is_answered_by_the_grant() -> None:
    """The point of the whole thing: a new subprocess, and no second prompt."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)

    async def scenario():
        instance = await store.create_instance(CODER, runner="test")
        first, asked = await _ask(
            broker,
            instance.instance_id,
            RUN_TESTS,
            agent_run_id=AgentRunId("ar-1"),
            workspace_id=WorkspaceId("ws-1"),
            answer=ApprovalDecision.ACCEPT_FOR_SESSION,
        )
        # A later turn of the same conversation: a provider process that has
        # never heard of the first one, asking the same thing again.
        second, reused = await _ask(
            broker,
            instance.instance_id,
            replace(RUN_TESTS, approval_id="provider-later"),
            agent_run_id=AgentRunId("ar-2"),
            workspace_id=WorkspaceId("ws-1"),
        )
        return (
            first,
            asked,
            second,
            reused,
            await store.list_approvals(),
            await store.list_session_grants(),
        )

    first, asked, second, reused, approvals, grants = asyncio.run(scenario())

    assert first is ApprovalDecision.ACCEPT_FOR_SESSION
    assert asked[0].is_pending  # a card, answered by a person
    assert second is ApprovalDecision.ACCEPT_FOR_SESSION
    # Presented already decided: something to show, nothing to answer.
    assert [record.status for record in reused] == [ApprovalStatus.DECIDED]
    assert reused[0].decision_source is ApprovalDecisionSource.SESSION_GRANT
    # Still a persisted request, and still auditable as one.
    assert len(approvals) == 2
    assert approvals[1].agent_run_id == AgentRunId("ar-2")
    assert approvals[1].status is ApprovalStatus.DECIDED
    assert approvals[1].decision is ApprovalDecision.ACCEPT_FOR_SESSION
    assert approvals[1].decision_source is ApprovalDecisionSource.SESSION_GRANT
    assert approvals[1].decided_at is not None
    # Reusing consent does not compound it into a second grant.
    assert len(grants) == 1


@pytest.mark.parametrize(
    # `asked` rather than `request`, which pytest reserves for its own fixture.
    ("difference", "asked", "runner", "workspace_id"),
    [
        ("a different command", RUN_LINT, "test", WorkspaceId("ws-1")),
        (
            "the same command somewhere else",
            replace(RUN_TESTS, cwd="/elsewhere"),
            "test",
            WorkspaceId("ws-1"),
        ),
        ("a different worktree", RUN_TESTS, "test", WorkspaceId("ws-2")),
        ("no worktree at all", RUN_TESTS, "test", None),
        ("a different runner", RUN_TESTS, "other", WorkspaceId("ws-1")),
        (
            "a different kind of action",
            replace(WRITE_FILE, allowed_decisions=tuple(ApprovalDecision)),
            "test",
            WorkspaceId("ws-1"),
        ),
        (
            "a request this provider will not reuse consent for",
            replace(
                RUN_TESTS,
                allowed_decisions=(ApprovalDecision.ACCEPT, ApprovalDecision.CANCEL),
            ),
            "test",
            WorkspaceId("ws-1"),
        ),
    ],
)
def test_a_grant_answers_nothing_it_was_not_given_for(
    difference: str,
    asked: ApprovalRequest,
    runner: str,
    workspace_id: WorkspaceId | None,
) -> None:
    """Every axis is compared for equality, and any one of them asks again."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)

    async def scenario():
        instance = await store.create_instance(CODER, runner="test")
        await _ask(
            broker,
            instance.instance_id,
            RUN_TESTS,
            agent_run_id=AgentRunId("ar-1"),
            workspace_id=WorkspaceId("ws-1"),
            answer=ApprovalDecision.ACCEPT_FOR_SESSION,
        )
        _decision, presented = await _ask(
            broker,
            instance.instance_id,
            asked,
            agent_run_id=AgentRunId("ar-2"),
            runner=runner,
            workspace_id=workspace_id,
            answer=ApprovalDecision.CANCEL,
        )
        return presented

    presented = asyncio.run(scenario())

    assert presented[0].is_pending, f"{difference} should have been asked about"
    assert presented[0].decision_source is None


def test_a_grant_does_not_reach_another_conversation() -> None:
    """Cross-conversation reuse is explicitly out of scope, and stays out."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)

    async def scenario():
        granting = await store.create_instance(CODER, runner="test")
        bystander = await store.create_instance(CODER, runner="test")
        await _ask(
            broker,
            granting.instance_id,
            RUN_TESTS,
            agent_run_id=AgentRunId("ar-1"),
            answer=ApprovalDecision.ACCEPT_FOR_SESSION,
        )
        _decision, presented = await _ask(
            broker,
            bystander.instance_id,
            RUN_TESTS,
            agent_run_id=AgentRunId("ar-2"),
            answer=ApprovalDecision.CANCEL,
        )
        return presented

    assert asyncio.run(scenario())[0].is_pending


def test_a_revoked_grant_stops_answering() -> None:
    store = InMemoryStateStore()
    broker = ApprovalBroker(store)

    async def scenario():
        instance = await store.create_instance(CODER, runner="test")
        await _ask(
            broker,
            instance.instance_id,
            RUN_TESTS,
            agent_run_id=AgentRunId("ar-1"),
            answer=ApprovalDecision.ACCEPT_FOR_SESSION,
        )
        granted = (await store.list_session_grants())[0]
        await store.record_session_grant(replace(granted, revoked_at=datetime.now(UTC)))
        _decision, presented = await _ask(
            broker,
            instance.instance_id,
            RUN_TESTS,
            agent_run_id=AgentRunId("ar-2"),
            answer=ApprovalDecision.CANCEL,
        )
        return presented

    assert asyncio.run(scenario())[0].is_pending


def test_a_granted_action_does_not_pause_the_next_turn_of_a_chat(web_app) -> None:
    """The same thing again, through the whole stack the browser talks to."""
    store = InMemoryStateStore()
    runner = ApprovalRunner()
    app = web_app(_session(store, runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            first = asyncio.create_task(
                client.post(
                    f"/api/threads/{thread_id}/runs", json={"text": "run the tests"}
                )
            )
            request = await _pending(store)
            await client.post(
                _approval_url(thread_id, request.approval_id),
                json={"decision": "accept_for_session"},
            )
            await first

            # A second turn. Nobody answers anything, and it finishes anyway.
            second = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "and again"}
            )
            history = await client.get(f"/api/threads/{thread_id}/messages")
            return second, history, await store.list_approvals()

    second, history, approvals = asyncio.run(scenario())
    events = _lines(second)
    approval_events = [event for event in events if event["type"] == "approval"]

    assert second.status_code == 200
    # No card: the only snapshot the client is sent is an answered one.
    assert [event["approval"]["status"] for event in approval_events] == ["decided"]
    assert approval_events[0]["approval"]["decision"] == "accept_for_session"
    assert approval_events[0]["approval"]["decisionSource"] == "session_grant"
    assert events[-1]["type"] == "done"
    # The action itself happened both times, which is what was approved.
    assert runner.executed == ["pytest", "pytest"]
    assert runner.decisions == [
        ApprovalDecision.ACCEPT_FOR_SESSION,
        ApprovalDecision.ACCEPT_FOR_SESSION,
    ]
    assert approvals[1].decision_source is ApprovalDecisionSource.SESSION_GRANT
    assert [
        (message["role"], message["content"][0]["text"])
        for message in history.json()["messages"]
    ] == [
        ("user", "run the tests"),
        ("assistant", "All tests passed."),
        ("user", "and again"),
        ("assistant", "All tests passed."),
    ]


def test_a_runner_that_cannot_pause_runs_exactly_as_before(web_app) -> None:
    store = InMemoryStateStore()
    runner = PlainRunner()
    app = web_app(_session(store, runner), {"test": runner})

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            streamed = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "no approvals here"}
            )
            return streamed, await store.list_approvals()

    streamed, approvals = asyncio.run(scenario())

    assert streamed.status_code == 200
    assert _lines(streamed)[-1]["type"] == "done"
    assert approvals == ()
    assert [message.content for message in runner.seen[0]] == ["no approvals here"]


# --- the configured policy --------------------------------------------------


class PolicyRunner(ApprovalRunner):
    """An interactive runner whose requests the policy can actually read."""

    permission_translator = KIND_PERMISSION_TRANSLATOR


def test_a_policy_that_allows_it_answers_without_asking_anybody() -> None:
    """The point of writing one down: the question is not put at all.

    Still a persisted request, and still auditable as one -- what distinguishes
    it from a decision somebody made is `decision_source`, exactly as it is for
    a session grant.
    """
    store = InMemoryStateStore()
    broker = ApprovalBroker(
        store,
        ApprovalConfig(bash=BashApprovalConfig(allow=("pytest **",))),
    )

    async def scenario():
        instance = await store.create_instance(CODER, runner="test")
        decision, presented = await _ask(
            broker,
            instance.instance_id,
            RUN_TESTS,
            agent_run_id=AgentRunId("ar-1"),
            translator=KIND_PERMISSION_TRANSLATOR,
        )
        return decision, presented, await store.list_approvals()

    decision, presented, approvals = asyncio.run(scenario())

    assert decision is ApprovalDecision.ACCEPT
    # Presented already decided: something to show, nothing to answer.
    assert [record.status for record in presented] == [ApprovalStatus.DECIDED]
    assert len(approvals) == 1
    assert approvals[0].command == "pytest"
    assert approvals[0].decision is ApprovalDecision.ACCEPT
    assert approvals[0].decision_source is ApprovalDecisionSource.POLICY
    assert approvals[0].decided_at is not None


def test_a_policy_that_denies_it_refuses_without_asking_anybody() -> None:
    """A request that was always going to be refused is not a question."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(
        store,
        ApprovalConfig(
            auto_approve=True, bash=BashApprovalConfig(deny=("pytest **",))
        ),
    )

    async def scenario():
        instance = await store.create_instance(CODER, runner="test")
        decision, presented = await _ask(
            broker,
            instance.instance_id,
            RUN_TESTS,
            agent_run_id=AgentRunId("ar-1"),
            translator=KIND_PERMISSION_TRANSLATOR,
        )
        return decision, presented, await store.list_approvals()

    decision, presented, approvals = asyncio.run(scenario())

    assert decision is ApprovalDecision.CANCEL
    assert [record.status for record in presented] == [ApprovalStatus.DECIDED]
    assert approvals[0].decision is ApprovalDecision.CANCEL
    assert approvals[0].decision_source is ApprovalDecisionSource.POLICY


def test_a_capability_the_policy_has_not_ruled_on_is_still_put_to_a_person() -> None:
    """Absence from `allow` is silence, and the pause is what silence means."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(store, ApprovalConfig(allow=(ApprovalCapability.READ,)))

    async def scenario():
        instance = await store.create_instance(CODER, runner="test")
        decision, presented = await _ask(
            broker,
            instance.instance_id,
            WRITE_FILE,
            agent_run_id=AgentRunId("ar-1"),
            answer=ApprovalDecision.ACCEPT,
            translator=KIND_PERMISSION_TRANSLATOR,
        )
        return decision, presented, await store.list_approvals()

    decision, presented, approvals = asyncio.run(scenario())

    assert decision is ApprovalDecision.ACCEPT
    assert presented[0].is_pending  # a card, answered by a person
    assert approvals[0].decision_source is ApprovalDecisionSource.USER


def test_a_decision_the_provider_never_offered_is_put_to_a_person_instead() -> None:
    """Enforcement stops at the protocol: a provider not offering a cancellation
    has no way to honour one, and inventing the message would not refuse
    anything -- so the request goes to somebody who can answer it."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(
        store, ApprovalConfig(bash=BashApprovalConfig(deny=("pytest **",)))
    )
    uncancellable = replace(RUN_TESTS, allowed_decisions=(ApprovalDecision.ACCEPT,))

    async def scenario():
        instance = await store.create_instance(CODER, runner="test")
        decision, presented = await _ask(
            broker,
            instance.instance_id,
            uncancellable,
            agent_run_id=AgentRunId("ar-1"),
            answer=ApprovalDecision.ACCEPT,
            translator=KIND_PERMISSION_TRANSLATOR,
        )
        return decision, presented

    decision, presented = asyncio.run(scenario())

    assert decision is ApprovalDecision.ACCEPT
    assert presented[0].is_pending


def test_a_runner_whose_requests_nobody_can_read_is_asked_about_regardless() -> None:
    """Fail closed. A policy that allowed the unclassifiable would be allowing
    whatever a provider release invents next."""
    store = InMemoryStateStore()
    broker = ApprovalBroker(
        store, ApprovalConfig(allow=tuple(ApprovalCapability))
    )

    async def scenario():
        instance = await store.create_instance(CODER, runner="test")
        decision, presented = await _ask(
            broker,
            instance.instance_id,
            RUN_TESTS,
            agent_run_id=AgentRunId("ar-1"),
            answer=ApprovalDecision.ACCEPT,
        )
        return decision, presented

    decision, presented = asyncio.run(scenario())

    assert decision is ApprovalDecision.ACCEPT
    assert presented[0].is_pending


def test_a_turn_the_policy_covers_runs_start_to_finish_without_a_prompt(
    web_app,
) -> None:
    """The whole of it, over HTTP: nothing pending, and the action happened."""
    store = InMemoryStateStore()
    runner = PolicyRunner(requests=(RUN_TESTS, RUN_LINT))
    app = web_app(
        _session(store, runner),
        {"test": runner},
        approval_policy=ApprovalConfig(
            bash=BashApprovalConfig(allow=("pytest **", "ruff check **"))
        ),
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            thread_id = await _thread(client)
            streamed = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "run the tests"}
            )
            return streamed, await store.list_approvals()

    streamed, approvals = asyncio.run(scenario())

    assert streamed.status_code == 200
    assert _lines(streamed)[-1]["type"] == "done"
    assert runner.executed == ["pytest", "ruff check"]
    assert [record.status for record in approvals] == [
        ApprovalStatus.DECIDED,
        ApprovalStatus.DECIDED,
    ]
    assert {record.decision_source for record in approvals} == {
        ApprovalDecisionSource.POLICY
    }
    # Shown to the client all the same, so it can say what ran on its behalf.
    # The stream is a snapshot poll rather than an event log, so two requests
    # settled between ticks arrive as the later one; both are in the store.
    streamed_sources = [
        line["approval"]["decisionSource"]
        for line in _lines(streamed)
        if line["type"] == "approval"
    ]
    assert streamed_sources and set(streamed_sources) == {"policy"}
