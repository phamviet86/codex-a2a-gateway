# Client/server deployment

Use the current [deployment guide](deployment.md), [Hermes server guide](server-hermes.vi.md), and [Codex client guide](client-codex.vi.md).

## Long conversations and actionable errors (0.8)

See the [English AI operating guide](ai-operations.md) and [versioned Hermes compatibility patch](hermes-compatibility.md). The gateway wheel alone does not remove the receiver's legacy conversation limit. Verify the authenticated peer policy with `client-doctor`. Keep the same context, observe existing operations with get/wait, and never blindly resend ambiguous work.
