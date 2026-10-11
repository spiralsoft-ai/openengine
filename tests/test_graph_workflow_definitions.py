"""The repository's graph workflow: what it is, and how it is offered.

Two things can go wrong with `workflows/implementation_review_graph.py`:

* the definition itself is wrong -- a stage renamed, an agent left without a
  checkout to work in -- and the tests read that off the compiled graph;
* it breaks something that reads the workflow directory, which is every app
  here. A directory that refuses to load takes the deployment down.

The interface offers these graphs and picking one starts it on the graph
engine. That is checked here at `/api/config` -- the dropdown a person actually
meets -- and end to end in `tests/test_web_app.py`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import tomllib
from pathlib import Path

import pytest

from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_ROOT_DIRECTORY,
    GitWorktreeWorkspaceProvider,
)
from engine.apps.control_server.__main__ import main as control_server
from engine.apps.control_server.composition import Settings as ControlServerSettings
from engine.apps.web.__main__ import build_app
from engine.apps.web.composition import Settings
from engine.apps.worker.__main__ import main as worker
from engine.apps.worker.composition import Settings as WorkerSettings
from engine.domain import STATE_INPUT, WorkflowId, WorkspaceId, WorkState
from engine.graph_runtime import GraphId, GraphWorkflow
from engine.graph_runtime_langgraph.components import HumanReviewNode, NameNode
from engine.graph_runtime_langgraph.components.forge import PUBLISH_CHANGE
from engine.graph_runtime_langgraph.components.name import NAMING_PROMPT
from engine.graph_runtime_langgraph.workflows import sqlite_runtime
from engine.ports import Workspace
from engine.runtime.workflows import load_workflow_catalog

#: The repository's own workflow directory: what a deployment here actually
#: loads, rather than a fixture shaped like one.
WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"

#: Started by every composition root under test, and by the interface.
CONFIG = Path(__file__).resolve().parents[1] / "engine.toml"

GRAPHS = ("implementation-review-rerank",)


@pytest.fixture
def configured_checkouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep composition checks independent of host checkouts and SSH access."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for path in tomllib.loads(CONFIG.read_text()).get("repos", {}).values():
        Path(path).expanduser().mkdir(parents=True, exist_ok=True)


class RecordingWorkspaceProvider:
    """A `WorkspaceProvider` that provisions nothing.

    Enough of the port to be handed to a node, and no more: these tests ask
    which provider a node holds, never what it produced.
    """

    async def provision(
        self, repository: str, base_ref: str, *, co_author: str = ""
    ) -> Workspace:
        return Workspace(
            workspace_id=WorkspaceId("ws-recorded"),
            root_path="/checkouts/ws-recorded",
            repository=repository,
            base_ref=base_ref,
            ref="engine/ws-recorded",
        )


def catalog():
    return load_workflow_catalog(WORKFLOWS)


def definition_module():
    """The workflow module, imported by path the way the loader imports one.

    By path because `workflows/` is a directory of definitions, not a package.
    That is the point of it: a deployment swaps the directory, not an import.
    """
    path = WORKFLOWS / "implementation_review_graph.py"
    spec = importlib.util.spec_from_file_location("_implementation_review_graph", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def nodes_of(builder) -> dict[str, object]:
    """The node objects a `StateGraph` was built from, before compilation.

    The one place these tests reach into LangGraph's own structure, kept to one
    function so a change in its shape is one fix. Worth reaching for: `cwd` is
    not in the topology a client sees, and it is the field whose absence would
    put an agent in the server's own repository.
    """
    return {
        name: spec.runnable.afunc or spec.runnable.func
        for name, spec in builder.nodes.items()
    }


# --- the definition ----------------------------------------------------------


def test_the_repository_offers_the_same_workflow_on_either_engine() -> None:
    loaded = catalog()

    assert [str(one.graph_id) for one in loaded.graphs] == list(GRAPHS)
    assert [one.name for one in loaded.graphs] == [
        "Implementation review rerank",
    ]
    # One graph exposes both runner selections as creation inputs.
    assert all(isinstance(one, GraphWorkflow) for one in loaded.graphs)


def test_the_workflow_retires_the_ids_it_used_to_have(tmp_path: Path) -> None:
    """The rename in #367 left WorkOrders behind, and this is what keeps them.

    The runner used to be part of the id -- one graph per agent -- and became a
    creation input instead. Every WorkOrder started before that remembers the
    id it began with, so the engine goes on answering for those ids: a run of
    one still has a topology to be drawn with, which is the difference between
    an old WorkOrder opening and an old WorkOrder failing to load.
    """
    graphs = catalog().graphs
    assert [str(one) for one in graphs[0].previous_ids] == [
        "implementation-review-codex",
        "implementation-review-claude",
    ]

    async def scenario():
        async with sqlite_runtime(graphs, tmp_path / "state") as runtime:
            return [
                runtime.topology(GraphId(retired))
                for retired in ("implementation-review-codex", "unheard-of")
            ]

    retired, unheard = asyncio.run(scenario())

    assert retired is not None and str(retired.graph_id) == GRAPHS[0]
    # Retiring an id is not answering for every id: a graph this deployment
    # never had is still nothing.
    assert unheard is None


def test_the_graph_names_the_workorder_then_runs_the_step_version_s_stages(
    tmp_path: Path,
) -> None:
    """Read off the compiled graph, so a stage cannot be renamed by accident.

    Compiling is the only way to see the topology in a repository that cannot
    yet run one -- and worth the trouble, because a definition nothing runs is
    exactly the kind that rots unnoticed.
    """

    async def scenario():
        async with sqlite_runtime(catalog().graphs, tmp_path / "state") as runtime:
            return runtime.graphs()

    topologies = asyncio.run(scenario())
    codex = next(one for one in topologies if str(one.graph_id) == GRAPHS[0])

    assert [str(node.node_id) for node in codex.nodes] == [
        "workspace",
        "naming",
        "implementation",
        "ci-check",
        "review-security",
        "review-bugs",
        "review-performance",
        "review-conciseness",
        "review-dryness",
        "reranker",
        "impact-analysis",
        "human-review",
        "triage",
    ]
    assert [node.name for node in codex.nodes] == [
        "Workspace",
        "Naming",
        "Implementation",
        "CI check",
        "Review (Security)",
        "Review (Bugs & task adherence)",
        "Review (Performance)",
        "Review (Conciseness)",
        "Review (DRYness & code duplication)",
        "Reranker",
        "Impact analysis",
        "Human review",
        "Triage",
    ]
    # Every node belongs to one of the shared WorkOrder states.
    assert [node.group for node in codex.nodes] == [
        "Planning",
        "Planning",
        "Implementation",
        "Implementation",
        "Review",
        "Review",
        "Review",
        "Review",
        "Review",
        "Review",
        "Review",
        "Review",
        "Review",
    ]
    # Reviewers, the reranker and triage say where their findings are kept.
    assert {
        str(node.node_id): node.findings_key for node in codex.nodes if node.findings_key
    } == {
        **{f"review-{facet}": f"review-{facet}" for facet in (
            "security", "bugs", "performance", "conciseness", "dryness",
        )},
        "reranker": "review",
        "triage": "review",
    }
    # The kinds: a checkout, nine agents (implementation + 5 reviewers +
    # reranker + naming + impact analysis), and the one stage that is a person.
    assert [node.kind for node in codex.nodes] == [
        "workspace",
        "agent",
        "agent",
        "tool",
        "agent",
        "agent",
        "agent",
        "agent",
        "agent",
        "agent",
        "agent",
        "human",
        "human",
    ]
    # The implementation, review facets, and reranker are conversations a
    # person reads and can talk to. The checkout, naming turn, and verdict
    # are stages of the run rather than conversations in it.
    assert [node.show_in_sidebar for node in codex.nodes] == [
        False,
        False,
        True,
        False,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        False,
        False,
    ]
    for topology in topologies:
        assert [str(node.node_id) for node in topology.nodes if node.always_open] == [
            "implementation"
        ]
    assert str(codex.entry_point) == "workspace"


def test_a_variant_replaces_where_it_works_by_calling_rather_than_copying() -> None:
    """The check that keeps one copy of this workflow.

    Something that needs its checkouts elsewhere -- the browser tier, whose
    whole premise is that a run's worktrees belong to the test -- can either
    call `pipeline` with its own provider or paste the graph into a second file
    and change one line. Only the first is available if this argument exists.

    Asserted on the provider the node ends up holding, because the failure
    worth catching is a `pipeline` that takes the argument and builds its
    default anyway.
    """
    module = definition_module()
    mine = RecordingWorkspaceProvider()

    nodes = nodes_of(module.pipeline("codex", workspace_provider=mine))

    assert nodes[module.WORKSPACE].provider is mine
    # And the default is still a real provider, for the deployment that passes
    # nothing. An accidental `None` would be a run with nowhere to work.
    default = nodes_of(module.pipeline("codex"))[module.WORKSPACE].provider
    assert isinstance(default, GitWorktreeWorkspaceProvider)


def test_the_default_root_is_the_one_every_composition_root_uses() -> None:
    """One string, not a fifth copy of it.

    Three apps already honour `Settings.workspace_root`. A workflow restating
    the path beside them is a deployment whose two halves disagree about where
    its worktrees are, with both halves in the same process.
    """
    assert Settings().workspace_root == DEFAULT_ROOT_DIRECTORY
    assert WorkerSettings().workspace_root == DEFAULT_ROOT_DIRECTORY
    assert ControlServerSettings().workspace_root == DEFAULT_ROOT_DIRECTORY
    assert DEFAULT_ROOT_DIRECTORY not in (
        WORKFLOWS / "implementation_review_graph.py"
    ).read_text(encoding="utf-8")


def test_every_agent_node_works_in_the_run_s_own_checkout() -> None:
    """The property that would be worst to get wrong.

    An agent node given no working directory opens its session in the server's
    own repository. `NoWorkingDirectoryError` makes that impossible to reach
    quietly; this checks the definition never has to. Written over *every*
    agent node, so it still means something when a third is added.
    """
    module = definition_module()
    nodes = nodes_of(module.pipeline("codex"))
    agents = [
        node
        for node in nodes.values()
        if getattr(node, "graph_node_kind", "") == "agent"
    ]

    assert len(agents) == 9
    assert all(node.cwd is module.checkout for node in agents)
    # And something upstream of them actually provisions one.
    assert nodes["workspace"].graph_node_kind == "workspace"


def test_implementation_and_review_receive_run_bound_workflow_tools() -> None:
    module = definition_module()
    nodes = nodes_of(module.pipeline("codex"))

    implementation = nodes[module.IMPLEMENTATION]
    assert len(implementation.mcp_server_bindings) == 1
    impl_binding = implementation.mcp_server_bindings[0]
    assert impl_binding.repository_tools == (
        "git_subcommand", "open_pull_request", "list_pipeline_status", "get_job_logs",
    )
    assert impl_binding.required_outputs == ("pr_url",)

    # Each review facet gets read-only repository tools (no add_comment).
    for facet in module.REVIEW_FACETS:
        facet_node = nodes[f"review-{facet.id}"]
        assert len(facet_node.mcp_server_bindings) == 1
        binding = facet_node.mcp_server_bindings[0]
        assert binding.repository_tools == (
            "view_change_request",
            "list_pipeline_status",
            "get_job_logs",
        )
        assert binding.required_outputs == ("findings",)

    # The reranker gets add_comment so it can post the final findings.
    assert nodes[module.IMPLEMENTATION].mcp_server_bindings[0].create_workorder
    reranker = nodes[module.RERANKER]
    assert reranker.mcp_server_bindings[0].create_workorder
    assert len(reranker.mcp_server_bindings) == 1
    reranker_binding = reranker.mcp_server_bindings[0]
    assert reranker_binding.repository_tools == (
        "view_change_request",
        "list_pipeline_status",
        "get_job_logs",
        "add_comment",
    )
    assert reranker_binding.required_outputs == ("findings",)
    # A run started in review keeps its findings for a person, so the reranker
    # is not served add_comment.
    reviewing = reranker_binding.for_state({"inputs": {STATE_INPUT: WorkState.REVIEW}})
    assert "add_comment" not in reviewing.repository_tools
    assert reranker_binding.for_state({"inputs": {}}) is reranker_binding
    # Unless it was asked to post its review to the pull request.
    publishing = {"inputs": {STATE_INPUT: WorkState.REVIEW, module.PUBLISH_INPUT: "true"}}
    assert reranker_binding.for_state(publishing) is reranker_binding


def test_the_naming_node_uses_the_selected_runner_and_names_the_task() -> None:
    module = definition_module()
    naming = nodes_of(module.pipeline("claude"))[module.NAMING]

    assert isinstance(naming, NameNode)
    assert naming.agent == "claude"
    assert naming.output_key == "name"
    assert naming.prompt({"task": "Resolve issue 270"}) == (
        NAMING_PROMPT.format(task="Resolve issue 270")
    )
    assert "at most twelve words" in NAMING_PROMPT
    assert "do not perform the task" in NAMING_PROMPT.lower()
    assert naming.graph_node_show_in_sidebar is False


def test_the_human_stage_is_the_shared_component_rather_than_a_bespoke_node() -> None:
    """A person's verdict, raised the way everything expects to find it.

    A verdict and an agent asking permission both arrive as approvals on the
    same feed, and `HumanReviewNode`'s tool name is what tells them apart. A
    hand-rolled stopping point would be a pause no client could label.
    """
    human = nodes_of(definition_module().pipeline("codex"))["human-review"]

    assert isinstance(human, HumanReviewNode)
    assert human.graph_node_kind == "human"


# --- how it is offered -------------------------------------------------------


@pytest.mark.parametrize("has_findings", [False, True])
@pytest.mark.parametrize("verification_enabled", [False, True])
def test_review_feedback_returns_to_implementation_at_most_once(
    monkeypatch, tmp_path, has_findings, verification_enabled,
) -> None:
    from langchain_core.runnables import RunnableLambda
    from engine.graph_runtime_langgraph.components import RerankerNode

    module = definition_module()
    from engine.graph_runtime_langgraph.components import OpenVerify
    from unittest.mock import AsyncMock

    verification = (
        OpenVerify(uploader=AsyncMock(), output_directory=tmp_path)
        if verification_enabled else None
    )
    builder = module.pipeline("codex", verification=verification)
    implementation = nodes_of(builder)[module.IMPLEMENTATION]
    visited = []
    prompts = []
    findings = [{"tagline": "Fix the bug", "description": "The result is wrong."}]
    pr_url = "https://github.com/owner/repo/pull/42"

    async def rerank(self, state):
        visited.append(module.RERANKER)
        # Findings persist after the fix to prove that the loop is bounded.
        return {module.REVIEW: findings if has_findings else []}

    monkeypatch.setattr(RerankerNode, "__call__", rerank)

    def stub(name):
        def run(state):
            visited.append(name)
            if name == module.IMPLEMENTATION:
                prompts.append(implementation.prompt(state))
                return {"pr_url": pr_url}
            if name == module.CI_CHECK:
                return {"ci_check": {"passed": True}}
            return {}
        return RunnableLambda(run)

    for name, spec in builder.nodes.items():
        if name != module.RERANKER:
            spec.runnable = stub(name)

    result = asyncio.run(builder.compile().ainvoke({"task": "Repair the result"}))

    rounds = 2 if has_findings else 1
    assert result["review_rounds"] == rounds
    assert visited.count(module.IMPLEMENTATION) == rounds
    assert visited.count(module.CI_CHECK) == rounds
    assert visited.count(module.RERANKER) == rounds
    for facet in module.REVIEW_FACETS:
        assert visited.count(f"review-{facet.id}") == rounds
    assert visited.count(module.WORKSPACE) == 1
    assert visited.count(module.IMPACT_ANALYSIS) == 1
    final_stages = [module.IMPACT_ANALYSIS]
    if verification_enabled:
        final_stages.append(module.VERIFICATION)
    final_stages.append(module.HUMAN_REVIEW)
    assert visited[-len(final_stages):] == final_stages
    assert prompts[0] == module.IMPLEMENTATION_PROMPT.format(
        publish=PUBLISH_CHANGE.connected, task="Repair the result",
    )
    if has_findings:
        assert "Fix the bug" in prompts[1]
        assert "Repair the result" in prompts[1]
        assert pr_url in prompts[1]
        assert "same PR branch" in prompts[1]
        assert "Do not open another pull request" in prompts[1]


@pytest.mark.parametrize("has_findings", [False, True])
def test_a_disconnected_run_is_told_to_stay_off_the_forge(
    monkeypatch, has_findings,
) -> None:
    """Every prompt is worded for a run that commits locally and posts nothing."""
    from langchain_core.runnables import RunnableLambda
    from engine.graph_runtime_langgraph.components import RerankerNode

    module = definition_module()
    builder = module.pipeline("codex")
    nodes = nodes_of(builder)
    visited = []
    prompts = {}
    findings = [{"tagline": "Fix the bug", "description": "The result is wrong."}]

    async def rerank(self, state):
        visited.append(module.RERANKER)
        prompts.setdefault(module.RERANKER, nodes[module.RERANKER].prompt(state))
        return {module.REVIEW: findings if has_findings else []}

    monkeypatch.setattr(RerankerNode, "__call__", rerank)

    def stub(name):
        def run(state):
            visited.append(name)
            if name in (module.IMPLEMENTATION, module.IMPACT_ANALYSIS):
                prompts.setdefault(name, []).append(nodes[name].prompt(state))
            if name == module.CI_CHECK:
                return {"ci_check": {"passed": True, "skipped": True}}
            return {}
        return RunnableLambda(run)

    for name, spec in builder.nodes.items():
        if name != module.RERANKER:
            spec.runnable = stub(name)

    asyncio.run(builder.compile().ainvoke({
        "task": "Repair the result", "inputs": {"mode": "disconnected"},
    }))

    rounds = 2 if has_findings else 1
    assert visited.count(module.IMPLEMENTATION) == rounds
    assert visited.count("review-security") == rounds
    assert prompts[module.IMPLEMENTATION][0] == module.IMPLEMENTATION_PROMPT.format(
        publish=PUBLISH_CHANGE.disconnected, task="Repair the result",
    )
    for prompt in (*prompts[module.IMPLEMENTATION], *prompts[module.IMPACT_ANALYSIS]):
        assert "disconnected from the forge" in prompt
        assert "open_pull_request" not in prompt
        assert "add_comment" not in prompt
    if has_findings:
        assert "Fix the bug" in prompts[module.IMPLEMENTATION][1]
    assert "add_comment" not in prompts[module.RERANKER]
    assert "Pull request:" not in prompts[module.RERANKER]


def test_a_run_started_in_review_triages_before_it_fixes(monkeypatch) -> None:
    """Started in review, the run checks out the change, reviews it and asks.

    Nothing is posted by the reranker, and only the findings a person chose at
    triage are sent to implementation; its fix is reviewed and triaged again.
    """
    from langchain_core.runnables import RunnableLambda
    from engine.graph_runtime_langgraph.components import RerankerNode

    module = definition_module()
    builder = module.pipeline("codex")
    nodes = nodes_of(builder)
    visited = []
    prompts = {}
    findings = [
        {"tagline": "Fix the bug", "description": "The result is wrong.", "agent": "claude", "facet": "bugs"},
        {"tagline": "Rename it", "description": "The name misleads.", "agent": "claude", "facet": "conciseness"},
    ]
    pr_url = "https://github.com/owner/repo/pull/42"
    choices = iter([[findings[0]], []])

    async def rerank(self, state):
        visited.append(module.RERANKER)
        prompts.setdefault(module.RERANKER, nodes[module.RERANKER].prompt(state))
        return {module.REVIEW: findings}

    monkeypatch.setattr(RerankerNode, "__call__", rerank)

    def stub(name):
        def run(state):
            visited.append(name)
            if name == module.IMPLEMENTATION:
                prompts.setdefault(name, []).append(nodes[name].prompt(state))
                return {"pr_url": pr_url}
            if name == "review-security":
                prompts.setdefault(name, nodes[name].prompt(state))
            if name == module.CI_CHECK:
                return {"ci_check": {"passed": True}}
            if name == module.TRIAGE:
                return {module.FIX: next(choices)}
            return {}
        return RunnableLambda(run)

    for name, spec in builder.nodes.items():
        if name != module.RERANKER:
            spec.runnable = stub(name)

    asyncio.run(builder.compile().ainvoke({
        "task": f"Review pull request {pr_url}",
        "inputs": {"state": "Review", "ref": "origin/feature", "pr_url": pr_url, "branch": "feature"},
    }))

    assert visited[0] == module.WORKSPACE
    assert module.NAMING not in visited
    assert visited.count(module.RERANKER) == 2
    assert visited.count(module.TRIAGE) == 2
    assert visited.count(module.IMPLEMENTATION) == 1
    assert module.IMPACT_ANALYSIS not in visited and module.HUMAN_REVIEW not in visited
    assert visited[-1] == module.TRIAGE
    assert pr_url in prompts["review-security"]
    assert "add_comment" not in prompts[module.RERANKER]
    assert "somewhat aggressively" in prompts[module.RERANKER]
    fix, = prompts[module.IMPLEMENTATION]
    assert "Fix the bug" in fix and "Rename it" not in fix
    assert pr_url in fix and "same PR branch" in fix
    # The workspace's own branch is not the pull request's, so the prompt names it.
    assert "git push origin HEAD:feature" in fix
    assert "Reply to each review comment" not in fix


@pytest.mark.parametrize("verification_enabled", [False, True])
def test_a_review_requested_on_the_forge_posts_its_findings_and_impact(
    monkeypatch, tmp_path, verification_enabled,
) -> None:
    """Asked to publish, a run started in review posts instead of triaging.

    The reranker posts the surviving findings and impact analysis posts its
    rating to the pull request; nothing waits on a person afterwards.
    """
    from langchain_core.runnables import RunnableLambda
    from engine.graph_runtime_langgraph.components import RerankerNode

    module = definition_module()
    from engine.graph_runtime_langgraph.components import OpenVerify

    from unittest.mock import AsyncMock

    verification = (
        OpenVerify(uploader=AsyncMock(), output_directory=tmp_path)
        if verification_enabled else None
    )
    builder = module.pipeline("codex", verification=verification)
    nodes = nodes_of(builder)
    visited = []
    prompts = {}
    findings = [
        {"tagline": "Fix the bug", "description": "The result is wrong.", "agent": "claude", "facet": "bugs"},
    ]
    pr_url = "https://github.com/owner/repo/pull/42"

    async def rerank(self, state):
        visited.append(module.RERANKER)
        prompts[module.RERANKER] = nodes[module.RERANKER].prompt(state)
        return {module.REVIEW: findings}

    monkeypatch.setattr(RerankerNode, "__call__", rerank)

    def stub(name):
        def run(state):
            visited.append(name)
            if name == module.IMPACT_ANALYSIS:
                prompts[name] = nodes[name].prompt(state)
            return {}
        return RunnableLambda(run)

    for name, spec in builder.nodes.items():
        if name != module.RERANKER:
            spec.runnable = stub(name)

    asyncio.run(builder.compile().ainvoke({
        "task": f"Review pull request {pr_url}",
        "inputs": {
            "state": "Review", "mode": "connected", "ref": "origin/feature",
            "pr_url": pr_url, "branch": "feature", module.PUBLISH_INPUT: "true",
        },
    }))

    assert visited.count(module.RERANKER) == 1
    assert visited[-1] == module.IMPACT_ANALYSIS
    for skipped in (
        module.TRIAGE, module.IMPLEMENTATION, module.HUMAN_REVIEW, module.VERIFICATION,
    ):
        assert skipped not in visited
    assert "add_comment" in prompts[module.RERANKER]
    assert "add_comment" in prompts[module.IMPACT_ANALYSIS]
    assert pr_url in prompts[module.IMPACT_ANALYSIS]
    assert "Fix the bug" in prompts[module.IMPACT_ANALYSIS]


def test_a_disconnected_run_is_served_no_forge_tools(monkeypatch) -> None:
    from types import SimpleNamespace
    from engine.domain import RunId
    from engine.graph_runtime_langgraph import terminal_mcp
    from engine.runtime.terminal_mcp import TerminalMcpBroker

    module = definition_module()
    nodes = nodes_of(module.pipeline("codex"))
    brokers = []

    def capture_broker(**kwargs):
        broker = TerminalMcpBroker(**kwargs)
        brokers.append(broker)
        return broker

    monkeypatch.setattr(terminal_mcp, "TerminalMcpBroker", capture_broker)
    served = {}

    def enable(self, source_control, names, workspace, approve):
        served[self._step.step_id] = (tuple(names), self._step.required_outputs)

    monkeypatch.setattr(TerminalMcpBroker, "enable_repository_tools", enable)
    source_control = SimpleNamespace(**{
        method: (lambda *a, **k: None)
        for method in ("run_git", "request_review", "add_comment",
                       "view_change_request", "list_pipeline_status", "get_job_logs")
    })
    execution = SimpleNamespace(
        run_id=RunId("run"), execution_id="one", node_id="node",
        runtime=SimpleNamespace(
            source_control=source_control, store=SimpleNamespace(),
            workorder_creator=None,
        ),
    )
    state = {"workspaceId": "workspace", "inputs": {"mode": "disconnected"}}

    async def scenario():
        for name in (module.IMPLEMENTATION, "review-security", module.RERANKER,
                     module.IMPACT_ANALYSIS):
            binding, = nodes[name].mcp_server_bindings
            async with binding(state, execution, None):
                pass

    asyncio.run(scenario())

    assert served == {
        module.IMPLEMENTATION: (("git_subcommand",), ()),
        "review-security": ((), ("findings",)),
        module.RERANKER: ((), ("findings",)),
        module.IMPACT_ANALYSIS: ((), ("impact_level", "impact_rationale")),
    }


def test_the_catalog_answers_for_the_workflow_by_id() -> None:
    """What the interface looks a picked workflow up by.

    A dropdown sends back an id; this is the lookup that turns it into
    something startable, and the one that refuses an id nobody offers.
    """
    loaded = catalog()

    assert len(loaded) == len(GRAPHS)
    for graph_id in GRAPHS:
        assert WorkflowId(graph_id) in loaded
        assert str(loaded.require(WorkflowId(graph_id)).graph_id) == graph_id
    assert loaded.get(WorkflowId("nothing-ships-this")) is None


def test_the_interface_offers_the_graphs_by_their_own_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_checkouts: None,
    *,
    async_client,
) -> None:
    """The dropdown itself, through the endpoint the client reads it from.

    Every entry is a workflow under the name its definition gives it.

    Asked of a *started* application, which is the whole condition for a graph
    being offered: the engine that runs one is opened on startup, and an engine
    that did not open means no entries rather than entries nothing can start.
    Starting it here also compiles every graph in the directory against real
    files, so a graph this repository could not actually run fails this.
    """
    monkeypatch.setenv("ENGINE_CONFIG", str(CONFIG))
    # engine.toml commits this deployment's real GitHub login client id and
    # callback URL, completed by a secret that stays out of the file, this
    # test, and CI -- wherever it is picked up from (a developer's own
    # server-local .env included). Blanking all three of the config's login
    # values here through their env overrides, rather than touching
    # engine.toml or supplying any secret, keeps this test -- which is about
    # the workflow dropdown, not login -- unauthenticated the way it was
    # before login was configured.
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_REDIRECT_URI", "")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "")
    monkeypatch.chdir(tmp_path)
    app = build_app()

    async def ask() -> dict:
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                answered = await client.get("/api/config")
            assert answered.status_code == 200
            return answered.json()

    offered = asyncio.run(ask())["workflows"]

    assert [one["id"] for one in offered] == list(GRAPHS)
    assert [one["name"] for one in offered] == ["Implementation review rerank"]
    # Every entry declares the inputs the creation form asks for.
    assert [
        [item["name"] for item in one["inputs"]] for one in offered
    ] == [[
        "implementation_runner", "review_runner", "mode", "state", "ref", "pr_url", "branch",
        "publish_review",
    ]]


# --- and nothing falls over --------------------------------------------------


def test_every_composition_root_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_checkouts: None
) -> None:
    """All three read the workflow directory at startup, so all three are here.

    One test, two failure modes: the loader refusing an export it did not
    recognise, and an app that cannot import what the workflow file imports.
    """
    monkeypatch.chdir(tmp_path)

    assert worker(["--config", str(CONFIG)]) == 0
    assert control_server(["--config", str(CONFIG)]) == 0
    # The interface has no exit code to check. Building the app is what
    # `engine-web` does before it serves anything, so building it is the test.
    monkeypatch.setenv("ENGINE_CONFIG", str(CONFIG))
    # See the matching comment above: blank all three of the committed and
    # locally-supplied login values through their env overrides so this
    # composition root starts the same unauthenticated way it did before
    # login was configured, without touching engine.toml or needing a secret.
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_REDIRECT_URI", "")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "")
    assert build_app() is not None


@pytest.mark.parametrize("implementation", ("codex", "claude"))
@pytest.mark.parametrize("review", ("codex", "claude"))
def test_stage_runners_configure_models_and_mcp_identity(
    implementation, review,
) -> None:
    module = definition_module()
    graph = module.graph_for("codex")
    assert [item.name for item in graph.inputs] == [
        "implementation_runner", "review_runner", "mode", "state", "ref", "pr_url", "branch",
        "publish_review",
    ]
    nodes = nodes_of(graph.builder)
    observed = [
        nodes[name]._for_runner(runner)
        for name, runner in (
            ("implementation", implementation), ("review-security", review),
            ("review-bugs", review), ("reranker", implementation),
        )
    ]
    assert [node.agent for node in observed] == [implementation, review, review, implementation]
    for node in observed:
        assert all(binding.agent_id == node.agent for binding in node.mcp_server_bindings)
    assert observed[1].session_config["model"] == module.REVIEW_MODELS[review]["elevated"]
    assert observed[2].session_config["model"] == module.REVIEW_MODELS[review]["default"]


def test_input_runner_can_be_reset_to_workflow_default_and_retried(tmp_path):
    """Run the real ACP node and persist the selection across a runtime restart."""
    from dataclasses import replace
    from engine.graph_runtime import EventLog, NodeId
    from engine.graph_runtime_langgraph import State, graph_workflow
    from langgraph.graph import START, END, StateGraph
    from langgraph_acp import ACPAgentRegistry
    from test_graph_runtime_langgraph_acp import registry, until

    module = definition_module()
    provider = registry(tmp_path).resolve("stub")
    agents = ACPAgentRegistry([
        replace(provider, name="codex"), replace(provider, name="claude"),
    ])
    # Keep the repository's real input-aware node, replacing only the external
    # agent process and the checkout/MCP dependencies this test does not need.
    node = replace(
        nodes_of(module.pipeline("codex", agents=agents))["implementation"],
        cwd=str(tmp_path), mcp_server_bindings=(),
    )
    builder = StateGraph(State)
    builder.add_node("implementation", node)
    builder.add_edge(START, "implementation")
    builder.add_edge("implementation", END)
    graph = graph_workflow(builder, id="input-retry", name="Input retry")
    implementation = NodeId("implementation")

    async def scenario():
        async with sqlite_runtime((graph,), tmp_path / "runtime") as runtime:
            log = EventLog()
            runtime.observe(log.append)
            run = await runtime.start(graph.graph_id, {
                "inputs": {"implementation_runner": "claude"},
            })
            events = await until(log, run.run_id, "run.finished")
            assert next(e for e in events if e.kind.value == "conversation.started").payload["agent"] == "claude"
            snapshot = await runtime.snapshot(run.run_id)
            default = runtime.topology(graph.graph_id).node(implementation).runner
            assert snapshot.runner_overrides.get(implementation, default) == "claude"
            point = next(p for p in await runtime.history(run.run_id) if implementation in p.next_nodes)
            changed = await runtime.set_runner(run.run_id, implementation, "codex")
            assert changed.runner_overrides.get(implementation, default) == "codex"
            assert changed.values["inputs"]["implementation_runner"] == "claude"

        async with sqlite_runtime((graph,), tmp_path / "runtime") as runtime:
            log = EventLog()
            runtime.observe(log.append)
            assert (await runtime.snapshot(run.run_id)).runner_overrides == {}
            await runtime.resume_from(run.run_id, point.checkpoint_id)
            events = await until(log, run.run_id, "run.finished")
            assert next(e for e in events if e.kind.value == "conversation.started").payload["agent"] == "codex"
            assert (await runtime.store.session(run.run_id, "implementation")).agent == "codex"

    asyncio.run(scenario())


@pytest.mark.parametrize("level", ["Green", "Orange", "Red"])
@pytest.mark.parametrize(
    ("invalid_outputs", "error"),
    [
        ({"impact_level": "green", "impact_rationale": "Evidence"}, "impact_level"),
        ({"impact_level": "Blue", "impact_rationale": "Evidence"}, "impact_level"),
        ({"impact_level": "Green", "impact_rationale": ""}, "impact_rationale"),
        ({"impact_level": "Green", "impact_rationale": " \n"}, "impact_rationale"),
    ],
)
def test_impact_analysis_rejects_then_accepts_corrected_assessment(
    monkeypatch, level, invalid_outputs, error,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from engine.domain import RunId
    from engine.ports import CommentResult
    from engine.graph_runtime_langgraph import terminal_mcp
    from engine.runtime.terminal_mcp import TerminalMcpBroker
    from tests.test_terminal_mcp import _request

    module = definition_module()
    node = nodes_of(module.pipeline("codex"))[module.IMPACT_ANALYSIS]
    binding, = node._for_runner("codex").mcp_server_bindings
    brokers = []

    def capture_broker(**kwargs):
        broker = TerminalMcpBroker(**kwargs)
        brokers.append(broker)
        return broker

    monkeypatch.setattr(terminal_mcp, "TerminalMcpBroker", capture_broker)

    async def scenario():
        pr_url = "https://github.com/acme/api/pull/42"
        source_control = SimpleNamespace(add_comment=AsyncMock(
            return_value=CommentResult(123, f"{pr_url}#issuecomment-123"),
        ))
        store = SimpleNamespace(
            pull_requests=AsyncMock(return_value=(("acme/api", 42),)),
            remember_comment=AsyncMock(),
        )
        execution = SimpleNamespace(
            run_id=RunId("run"), execution_id="impact", node_id=module.IMPACT_ANALYSIS,
            runtime=SimpleNamespace(
                source_control=source_control, store=store,
            ),
        )
        async with binding({"workspaceId": "workspace"}, execution, None):
            broker, = brokers
            comment = f"{level}: assessment\n\nEvidence and required human actions"
            posted = await broker._submit(_request(broker, "comment", "add_comment", {
                "pr_url": pr_url, "comment": comment,
            }))
            assert posted["ok"] is True
            source_control.add_comment.assert_awaited_once_with(
                pr_url, comment, None, None, None,
            )
            store.remember_comment.assert_awaited_once()
            rejected = await broker._submit(_request(broker, 1, "complete_step", {
                "outcome": "success", "summary": "Assessment",
                "outputs": invalid_outputs,
            }))
            assert rejected["ok"] is False
            assert error in rejected["error"]
            assert not broker._result.done()
            accepted = await broker._submit(_request(broker, 2, "complete_step", {
                "outcome": "success", "summary": f"{level}: assessment",
                "outputs": {"impact_level": level, "impact_rationale": "Evidence"},
            }))
            assert accepted["ok"] is True
            update = node._terminal_update(await broker.result())
            assert update == {
                module.IMPACT_ANALYSIS: f"{level}: assessment",
                "impact_level": level, "impact_rationale": "Evidence",
            }

    asyncio.run(scenario())


def test_impact_analysis_receives_final_evidence_and_selected_review_runner():
    module = definition_module()
    node = nodes_of(module.pipeline("codex"))[module.IMPACT_ANALYSIS]
    assert node.graph_node_runner_input == "review_runner"
    selected = node._for_runner("codex")
    assert selected.agent == "codex"
    binding, = selected.mcp_server_bindings
    assert binding.agent_id == "codex"
    assert binding.required_outputs == ("impact_level", "impact_rationale")
    assert binding.repository_tools == (
        "view_change_request", "list_pipeline_status", "get_job_logs",
        "add_comment",
    )
    prompt = node.prompt({
        "task": "Repair saving", "pr_url": "https://example.com/pull/42",
        module.IMPLEMENTATION: "Fixed saving", "ci_check": {"passed": True},
        module.REVIEW: [{"tagline": "Remaining finding"}],
    })
    for evidence in ("Repair saving", "https://example.com/pull/42", "Fixed saving",
                     '"passed": true', "Remaining finding",
                     "Green 🟢", "Orange 🟠", "Red 🔴",
                     "use add_comment to post one general comment",
                     "must not be merged without a human"):
        assert evidence in prompt
