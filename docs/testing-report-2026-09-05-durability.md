# Local durability validation — 2026-09-05

Unreleased changes based on v0.3.0 (`2bcdbe5`). This is new evidence; older versioned
reports have not been rewritten. Runtime: macOS arm64, CPython 3.11.16, isolated
project venv; local Codex CLI 0.153.4.

## Executed checks

- `python -m compileall -q src tests scripts`: passed.
- `ruff check .`: passed.
- `ruff format --check .`: passed.
- `mypy src`: passed, 15 source files.
- `pytest --cov=codex_a2a_gateway --cov-report=term-missing`: **88 passed**, **73.63%** coverage.
- `scripts/check_app_server_schema.py`: passed against the local CLI; includes
  `thread/read` parameters as well as existing turn/model APIs.
- Isolated sdist/wheel build, `twine check`, distribution content check, clean-wheel
  installation and both CLI entrypoints: passed on macOS.
- SHA256SUMS generation and verification: passed.

## Acceptance evidence

`tests/test_durable_contract.py` adds 28 cases including parametrizations:

- Durable outbound identity available while discovery is blocked; restart and
  idempotent lookup without another execution.
- Transport loss before ACK and caller wait expiry after ACK; exact late result
  retrieval after restart, including a later ListTasks page.
- Rejection of context-only, wrong-context, duplicate-candidate and cyclic-page
  recovery evidence; rejection of legacy JSONL records without exact identity.
- Independent contexts in parallel; context-only follow-up after completion and
  explicit rejection of a follow-up while its context is unresolved.
- Input-required continuation on the same job/task, attempt deduplication,
  immutable completed results, wrong-origin/wrong-receipt rejection and idempotent
  result consumption.
- Rejection of stale, uncorrelated task snapshots after losing a continuation ACK.
- Inbound queued handle returned while the previous context turn runs; no duplicate
  execution when replaying an old message after a newer continuation was queued.
- Inbound pre-turn-ACK uncertainty retained without replay; acknowledged turn
  recovery by read-only App Server history and wrong-turn notification rejection.
- End-to-end bundled Hermes plugin + ASGI gateway + fake backend: lost ACK,
  persisted-state reload, duplicate suppression, input continuation and repeated
  receipt acknowledgements with exactly two intended SendMessage executions.
- Explicit plugin resume requires receiver evidence of an unsent exact attempt;
  wrong task/context/request results are rejected; handle capacity never evicts
  an existing job to admit a new one; worker bounce-back is refused.

Existing tests were adjusted where the intended contract changed: early async
responses can be queued/submitted, a wait expiry preserves working state, interrupted
backend jobs remain unknown, and recovery fixtures must carry exact message IDs.
The fake Hermes peer now honors continuation taskId and echoes requestMessageId;
this is a test contract, not a claim about an installed Hermes server.

## Not verified / not implemented

No live Hermes model call, Codex model execution, production service modification,
public deployment or Linux clean-host run was performed. Schema compatibility is
not proof of live reattachment or tool availability.

Peers without exact request correlation retain unknown outcomes. Codex history
recovery requires a saved turn ACK; suspended native approval/input RPC recovery
across process restart is not implemented. Results use pull delivery and caller
receipts; no automatic originating-conversation wake-up/injection or external
exactly-once delivery is claimed. See [the contract](durable-jobs.md).
