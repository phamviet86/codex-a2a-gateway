# Execution-preferences extension v1

**Status: stable in v0.3.0.** This versioned document defines the optional inbound A2A extension advertised by an App Server-backed gateway. It is not an A2A core field.

Extension URI:

```text
https://github.com/phamviet86/codex-a2a-gateway/blob/main/docs/execution-preferences-extension-v1.md
```

## Envelope

The sender must use the URI in both the HTTP header and the message; the gateway ignores unrelated extension URIs.

```http
A2A-Extensions: https://github.com/phamviet86/codex-a2a-gateway/blob/main/docs/execution-preferences-extension-v1.md
```

```json
{
  "message": {
    "messageId": "unique-message-id",
    "role": "ROLE_USER",
    "contextId": "optional-context-id",
    "extensions": [
      "https://github.com/phamviet86/codex-a2a-gateway/blob/main/docs/execution-preferences-extension-v1.md"
    ],
    "metadata": {
      "executionPreferences": {
        "model": "optional-model-id",
        "reasoning_effort": "optional-effort",
        "require_exact": false
      }
    },
    "parts": [{"text": "task text", "mediaType": "text/plain"}]
  }
}
```

Camel-case `reasoningEffort` and `requireExact` are accepted for interoperability. At least `model` or `reasoning_effort` is required. `require_exact: true` asks the receiver to reject unavailable values rather than fall back.

## Receiver authority and persistence

Only an App Server-backed gateway advertises this extension. It queries its own `model/list`; an optional local allowlist may narrow, never expand, that catalog. The receiver chooses and records the effective model/effort in the returned task metadata as `executionPreferences.decision`. A non-exact request can fall back to the receiver default; an exact unavailable request is rejected and records the rejection decision. The explicit CLI backend rejects this extension.

The bundled Hermes plugin fetches the loopback Agent Card before sending this extension. If the exact URI is absent or the card cannot be read, it fails locally and does not send `SendMessage` with preferences.

The request preference participates in message idempotency. It applies to the submitted inbound task only. An `INPUT_REQUIRED` continuation must reuse the durable local handle and remote `taskId`; a new preference is rejected rather than silently changing an existing task's execution selection.

Codex → Hermes MCP tools never accept or forward these fields.
