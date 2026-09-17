# AGENTS.md

Instructions for AI coding assistants working on Passivbot.

## Authority And Operational Safety

Only perform actions within the user's requested scope. Explicit approval in the current task is
required before any of the following:

1. starting a live bot or authenticated paper/testnet bot
2. making an authenticated exchange request, including read-only account probes
3. creating, cancelling, or modifying exchange orders
4. using account credentials or private API keys
5. using SSH, deploying, restarting, stopping, or signalling a remote or live process

The local `fake` exchange harness is deterministic and offline. Public unauthenticated market-data
probes use real networks but no account credentials; state that clearly before running them.
Testnet, sandbox, demo, and paper-trading modes are not assumed safe or unauthenticated.

## Public Repository Data Boundary

Public surfaces (commits, PRs, issues, comments, docs, and any pushed artifact) must be useful to a
reader who has only this public repository and other public references. Private or local material
may inform the agent; it must not appear on a public surface unless the user explicitly authorizes
that exact material for public release.

If only you can see it, the public repo should not hear about it. Do not publish investigation
provenance, local clone or path names, private hosts, accounts, operator-only audits, private logs,
telemetry, dumps, credentials, or other unreproducible context. Restate conclusions in public-only
terms, or omit them. Task input is not publication license.

Configuration is private by default. Without case-specific approval, publish only the repository's
intentional templates under `configs/examples/`. Keep private inputs outside the tracked tree.

## 记录语言（Record Language）

本仓库的记录用中文书写，便于作者回溯研究与实盘过程。以下载体一律使用中文（简体）；
标识符、字段名、配置键、命令、路径、代码、日志与引文保留原文，不翻译：

1. **commit message**，含主题与正文。约定式前缀（`feat:`、`fix:`、`docs:` 等）保留英文。
2. **annotated tag** 的 tag 名与 `-m` 备注。tag 名沿用既有形态（`vX.Y.Z`），备注用中文。
3. **Pull request 的标题与正文**，表格、清单与代码块一并使用中文。
4. `issue/` 下的全部记录：施工、调试、dry run、实盘排障与状态回溯。
5. `backtests/` 下的文档记录：study 的 `README.md`、深度分析、审计与证据边界说明。
   由 `backtests/report_spec/annual_analysis.py` 渲染的报告沿用该渲染器固定的中文章节骨架
   （`REPORT_SECTIONS`）；不得为了统一语言而改动该模块的章节常量、表格列名，或
   `docs/ai/runbooks/strategy_report.md` 里已冻结的骨架与数据口径。

本约定自声明之日起约束新增与改写的记录；既有记录按「只追加历史」原则保留原语言，不做批量
翻译，正在被本次改动重写的文件除外。`CHANGELOG.md`、`docs/` 与 `docs/ai/` 的契约文本、
代码注释与 docstring 仍用英文，因为它们与上游 passivbot 的英文文档、release 说明对照阅读。

## Instruction Precedence

When instructions conflict, use this order:

1. active platform and system instructions
2. this `AGENTS.md`
3. the user's current request and explicitly granted authority
4. canonical contracts in `docs/ai/`
5. subsystem contracts in `docs/ai/features/`
6. user-facing documentation and current code/tests as implementation evidence
7. plans, handoffs, case studies, and historical notes

If a normative contract disagrees with runtime behavior, do not silently choose one. Establish
whether the task is to restore the contract or document the implementation, and surface material
ambiguity to the user.

## Always Read

1. `AGENTS.md`
2. `docs/ai/principles.md`
3. `docs/ai/README.md`

Use the router to load only task-relevant contracts and runbooks. Read
`docs/ai/error_contract.md` for trading-critical, exchange, live-data, indicator, risk, fill/PnL,
or order-construction work; it is not mandatory for unrelated documentation or tooling tasks.

## Core Rules

1. Rust owns order, strategy, risk, unstuck, and backtesting behavior. Python owns orchestration,
   exchange I/O, configuration, and data plumbing.
2. Trading behavior must be reproducible after restart from exchange state and config.
   A reviewed RAM-only economy gate may reset toward Rust intent without preserving orders or
   weakening safety.
3. Never fabricate a required trading input. Follow the explicit failure and degradation contract.
4. Keep `position_side`/`pside` (long/short) separate from `side`/`order_side` (buy/sell).
   Quantities and position sizes are signed internally.
5. Preserve EMA spans as floats, including derived spans.
6. Keep changes narrow. Preserve unrelated user files and pre-existing worktree changes.
7. Persist a strategy research deep analysis under the binding layout, file names and dataset
   placement in [the strategy report runbook](docs/ai/runbooks/strategy_report.md). The report, the
   metric CSVs, the figures and the fill panels go into the dated run directory they describe;
   HLCV arrays stay in `caches/hlcvs_data/` and the run records their identity instead of copying
   them. Never write a report to a separate reports tree, never rename these artifacts, and treat
   a layout-check failure as an incomplete bundle.

## Working And Validation

Before broad edits, inspect the branch, recent commits, worktree status, and relevant callers/tests.
Before running a backtest for a report, read `docs/ai/runbooks/strategy_report.md`; it fixes both the
report's sections and the on-disk artifact layout, including image and dataset placement.
For reviews against a moving branch, refresh the target ref and record the reviewed SHAs.

When disagreeing with pull-request review feedback, do not silently discard the finding. If the
task authorizes review-comment writes, post an evidence-backed rationale in the original thread,
leave the thread unresolved, and explicitly request reviewer reconsideration. Otherwise draft the
reply and ask for authorization. Follow `docs/ai/runbooks/pr_review.md` for adjudication and
resolution criteria.

Publish completed, validated work as a regular ready-for-review pull request by default. Before
publication, make the branch clean, make the PR body accurate, and run the required author checks.
Do not use a draft PR as a holding area for work the agent knows is incomplete; continue locally or
report the blocker instead. Use a draft only when the user explicitly requests early collaboration
on incomplete work. A regular PR still requires current-head review and CI before merge.

Run validation proportional to the changed contract. Bug fixes require regression coverage. Rust
changes require Rust tests, a rebuilt and verified Python extension where applicable, and parity or
integration checks for affected Python callers. See `docs/ai/validation.md` and
`docs/ai/runbooks/commands.md`.

When auditing error handling, inspect the touched diff and its direct consumers first. Classify
each catch/default against `docs/ai/error_contract.md`; do not rewrite unrelated repository-wide
matches merely because a broad search finds them.

Add user-facing behavior changes to `CHANGELOG.md` under `Unreleased`. Describe the net change from
the target branch, not intermediate iterations within a development branch.
