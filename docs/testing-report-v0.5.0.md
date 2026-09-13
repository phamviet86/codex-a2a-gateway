# v0.5.0 release validation

Date: 2026-09-13. This report records the agent-led onboarding and MCP compatibility changes. It preserves [v0.4.0 live evidence](testing-report-v0.4.0.md) as historical evidence rather than treating those runs as tests of the new release.

## Scope and observed checks

The implementation was checked on macOS with CPython 3.11, using isolated skill roots, fake A2A services and no live model turns or real user configuration changes. Compile, Ruff lint/format, mypy, the complete pytest suite and coverage requirement passed. Fresh wheel/sdist builds passed Twine, content checks, clean-environment installation and SHA256 manifest verification. Both packaged skills passed the skill frontmatter validator.

The final local release suite passed 119 tests at 74.02% coverage, including version alignment and publisher gate regression checks. The release's linked successful main CI run supplies the Linux/macOS matrix evidence for its exact source commit.

Independent installed-wheel verification covered:

- Both skills installed outside the checkout into paths containing spaces; dry-run/check do not mutate, repeated installation is unchanged, managed replacement preserves unrelated notes, and generated runtime commands use the installed environment.
- A private launcher authored from the installed setup reference retained an explicit workspace, an absolute fake Codex executable and a protected credential source with a scrubbed parent environment. Inbound readiness reached an authenticated loopback fixture without exposing the fixture token or requiring Hermes.
- Modern `2026-07-28` and legacy `2025-11-25` wire clients negotiated, discovered seven tools, checked schemas and structured/text parity, observed `isError` for execution failures, JSON-RPC `-32602` for unknown tools, and protocol-only stdout with clean EOF shutdown.

The unchanged runtime/protocol implementation was independently checked with MCP SDK 2.0 and 2.2; version preparation keeps the `>=2.0,<3` dependency range. The CI clean-wheel check installs the currently resolved supported SDK.

## Publication identity and limits

The v0.5.0 publisher accepts only a completed successful `CI` workflow for a same-repository `push` to `main`, with the release marker and exact matching head SHA. It verifies current `main`, checked-out source/runtime version and tag target; builds from that SHA with a fixed source timestamp; uploads only to a draft; downloads and checks every asset before publishing. It never retargets a tag or overwrites published assets. The release tag and associated workflow run identify the exact source and final assets.

No fresh live sign-in, model task, browser operation or actual worker-to-RAG call is claimed here. Read-only readiness leaves authentication, model execution and worker tools unverified. Desktop-injected tools are not automatically inherited; same-host Codex MCP configuration can still be shared. Startup services remain an optional setup action rather than a bundled service manager. No production deployment or PyPI upload is included.
