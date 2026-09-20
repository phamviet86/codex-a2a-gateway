# Security Policy

## Supported versions

| Version | Security fixes |
| --- | --- |
| 0.7.0 | Current release; report the exact installed version and transport mode |
| 0.6 / 0.5 and older | Retired runtime; follow the v0.7 migration guide |

## Reporting a vulnerability

Do not post tokens, private prompts, transcripts or sensitive exploit details in
public issues. Use the repository's GitHub **Report a vulnerability** entry.

## Security boundary

The broker authenticates each device before operation, event, receipt or artifact
access. Workstations use verified HTTPS, including when connected through SSH.
Hermes A2A stays loopback-only on the server; no model-provided URL or credential
is accepted. PostgreSQL stays private. The client Unix socket and state belong to
one OS account; they are not a sandbox against another process using that account.
Native MCP metadata binds a result to its originating task and never replaces
server-side device authentication.

The daemon's optional OpenSSH process uses strict known-host verification,
noninteractive authentication and one loopback forward. Connection configuration
is trusted operator input. Host-key/authentication/configuration errors fail
closed; the daemon does not disable TLS, adopt an unknown listener, or kill an
unrelated SSH connection to recover.

Queued prompt payloads are encrypted with environment-provided keys and expire
under explicit TTLs. Results, artifacts, runtime logs and backups may still be
sensitive; restrict permissions and retention. Never log tokens, encryption keys
or plaintext prompts. Codex and Hermes have their own session retention policies.
Preserve keys with existing encrypted records when changing installation names.

A mutating Hermes request with an uncertain outcome is never blindly replayed.
Only exact saved identity may reconcile it. Native queue insertion is likewise
not idempotent: a lost acknowledgement remains `delivery_outcome_unknown` unless
exact evidence reconciles it. Native delivery is gated by the tested CLI version
and schema; queue acknowledgement does not prove consumption.

Uploads are bounded, digest-checked, device-scoped and expire. They are inert
objects, not executable content. Only valid UTF-8 `text/plain` may be attached as
untrusted reference text for Hermes. The gateway does not provide virus scanning.

Legacy inbound gateway modes, the Hermes-to-Codex plugin and old executable/
configuration aliases are absent in v0.7. Follow
[migration and rollback](docs/migration-v0.7.md); preserve private backups before
removing obsolete operational installations.

This independent project is not an official product of Nous Research or OpenAI.
