---
slug: orchestrate-graphs-and-loops
title: How to use OpenEngine to orchestrate graphs and loops
authors: [sheahawkins]
tags: [openengine, graphs, loops, code-review]
---

The software factory has two fundamental primitives: **graphs** and **loops.** A graph is a process that must be followed every time, no matter what. Waiting for CI to be green. Reviewing for a certain class of errors. Writing a spec. Doing TDD (test-driven development).

These semi-deterministic graphs are a repeatable way to apply agentic engineering. They let you configure the checkpoints that must be passed in order for a change to be ready.

<!-- truncate -->

![A graph that details a project, specs a change, tests it, implements it, reviews it for bugs, performance and security, reranks the findings, documents it, and ends in human review](/img/blog/graph-pipeline.png)

_Adversarial reviews_ are a technique where a critic agent reviews the changes produced by another agent: [https://asdlc.io/patterns/adversarial-code-review/](https://asdlc.io/patterns/adversarial-code-review/). Several case studies have already found that these patterns detect issues that would have otherwise gone unnoticed. The software field needs a tool which makes these patterns repeatable and accessible.

This week, we released the **graph and loops** CLI in [OpenEngine](https://github.com/OpenEngine/OpenEngine) v1.5.0. It makes it easy to add and execute predefined graphs.

```text
engine graph run adversarial-review --branch feat/my_feat --agent codex
* fanning out 5 reviewers..
* found 3 findings. review? y/n
```

For this `adversarial-review` graph, a series of agents will all review the branch you've pointed them at, and they'll collect their findings and present them to you. It's included out-of-the-box with OpenEngine.

Graphs can be composed. So if you need to do spec-driven development, then run reviews, and then impact analysis:

```bash
engine graph run spec implement review --agent round_robin "build my really awesome feature"
```

## What about loops?

A loop is taking a graph and executing it on a schedule. These are great for long-running tasks. In this first example, we'll start with an agent using static code analysis tools for discovering dead code.

![Static analysis, code coverage and other data sources feed an agent that discovers opportunities for code erasure and new tests until its budget is reached; when it finds one, an agent specs a change and the graph's steps run as before](/img/blog/dead-code-loop.png)

We start with a graph. This one has three steps. Claude runs the analyzers, which are `vulture` and `ruff` for python and `knip` for TypeScript. A different agent (Codex) then plays the adversary and tries to prove that each candidate is still in use. It looks for string references, entry points, plugin registration and public APIs. Whatever survives that challenge gets removed in one pull request.

```yaml
apiVersion: openengine.cc/v1
kind: Graph
name: dead-code
plan:
  scan:      { agent: claude, prompt: "Run vulture/ruff/knip … report only", outputs: {findings: {type: findings}} }
  challenge: { agent: {not: scan}, prompt: "Prove each is still used; keep only what's dead", … }
implementation:
  remove:    { agent: {same: scan}, tools: [git_subcommand, open_pull_request], prompt: "Delete it, run tests, open a PR" }
flow:
  - scan -> challenge
  - from: challenge
    route: [{when: "size(outputs.challenge.findings) > 0", to: remove}, {to: end}]
loop:
  every: 1d
  instruction: Look for dead code under packages/ and cli/.
```

Full code [here](https://github.com/OpenEngine/OpenEngine/blob/main/docs/examples/graphs/dead-code.yaml).

Register it, try a single run, then install it as a loop with guardrails:

```bash
engine graph add dead-code.yaml
engine graph run dead-code "Look for dead code under packages/scoper" --repo you/your-repo --wait
engine loop add dead-code --repo you/your-repo --max-prs 3 --max-spend 10
```

A loop never runs twice at the same time. It pauses itself when it reaches its PR or spending limit, and its limits keep counting across restarts. When you need a break, `engine loop pause dead-code --reason "release freeze"` stops it.

```text
shea@Sheas-MacBook-Air engine % engine loops --pretty
NAME              GRAPH         STATE   EVERY  NEXT                       ACTIVE RUN        PRS  SPEND         PAUSED BECAUSE
dead-code         dead-code v3  paused  1d     -                          -                 0/3  $0.00/$10.00  article test
dead-code-report  dead-code v5  active  1d     2026-10-10T18:49:24+00:00  run-be4f6747c377  0/∞  $0.00/$5.00   -
```

Or just have your agent manage your loops — our skill is [here](https://github.com/OpenEngine/OpenEngine/blob/main/.agents/skills/manage-graphs/SKILL.md).

For the details, see [Graphs](/docs/graphs/), [Loops](/docs/loops/) and the [CLI reference](/docs/cli-reference/).
