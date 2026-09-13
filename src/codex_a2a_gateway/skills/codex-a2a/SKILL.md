---
name: codex-a2a
description: Delegate work between Codex and a configured local Hermes/A2A peer, then retrieve, continue or cancel durable tasks. Use for gateway conversations and results, not installation or general Hermes administration.
---

# Use Codex A2A Gateway

Use the gateway when the user asks for the other agent or delegation materially helps the task. Do simple work locally. The outbound tools route to Hermes's active `default` profile; a requested specialty is an instruction, not selection of a separate profile. Model/reasoning selection is available only on negotiated inbound A2A → Codex requests.

## Codex → Hermes through MCP

- Start with `hermes_status` when readiness is unknown. If tools are missing, use the installed `codex-a2a-setup` skill; setup details are not part of each delegation.
- Call `hermes_chat` with the task, a stable `conversation_key`, and an `idempotency_key` for a new request that could change anything. Separate independent jobs into separate contexts; reuse the returned `context_id` or conversation key for follow-ups. Include available real `origin` conversation/question IDs for attribution; never invent a Desktop ID.
- Long work uses `mode="async"`. Save the returned `bridge_task_id`; use `hermes_task_wait`/`hermes_task_get` to retrieve it. Wait expiry means the task may still be active. Ambiguous transport results are `outcome_unknown`, not permission to resend. Reconcile only saved task/message identities; preserve ambiguity when evidence is insufficient.
- For `input_required`, send the answer through `hermes_chat` with the existing `task_id`, same context, and a new `idempotency_key` for the new answer. Ordinary follow-ups after completion use the same context and a new request identity.
- Use `hermes_tasks_list` to find durable handles and `hermes_contexts` to inspect/close local mappings. Cancellation via `hermes_task_cancel` is best-effort; never claim computation stopped unless upstream evidence proves it.
- Consume the retrieved result in the originating task. Where supplied, check origin/result identity and acknowledge using `hermes_task_get(acknowledge_result_id=..., expected_origin=...)`. Results are pulled through get/wait, not automatically delivered into the Desktop conversation.

## Hermes/A2A → Codex

When the available client is the bundled Hermes plugin, `codex_a2a_call` returns a local handle for asynchronous work. Retain it and use `codex_a2a_get`, `codex_a2a_wait`, `codex_a2a_list`, or `codex_a2a_cancel`. Answer input-required with `codex_a2a_call`, the same local `task_id`, and a new `message_id`; serialize operations on one handle. Do not retry an ambiguous submission or switch to native synchronous `a2a_call` to bypass recovery. Generic A2A clients use the same task lifecycle through the advertised Agent Card.

Worker tools come from the actual Codex execution host/configuration. Desktop-only injected plugins are not automatically inherited; shared local MCP configuration may already be available. Check actual worker discovery before promising RAG, browser or other tools. Return unavailable capabilities and completed artifacts honestly. Do not delegate a worker job back to its parent or enable agent chaining to evade the anti-loop marker.

For CLI troubleshooting outside the checkout, [runtime.json](references/runtime.json) contains the installed absolute Python command; append arguments using an argument array. It contains no secrets. If moved, repair setup instead of guessing a different runtime.
