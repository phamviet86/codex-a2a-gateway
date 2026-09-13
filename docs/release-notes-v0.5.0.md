# v0.5.0 — Agent-led workstation setup

Install the release wheel, run `install-skills`, then ask your agent to use `codex-a2a-setup`. The agent configures the requested direction, reuses existing authentication, and asks only for missing required values such as the inbound workspace. The companion `codex-a2a` skill teaches delegation, continuation, retrieval and conservative timeout handling.

- Packaged setup/usage skills with an absolute nonsecret runtime reference; no checkout or PATH dependency after installation.
- `install-skills` supports custom destinations, read-only checks/dry-runs, unchanged-install idempotency and reviewed replacement of managed files while preserving unrelated content.
- `doctor --mode outbound|inbound|both` adds inbound readiness without Hermes; sign-in, model execution and worker tools remain explicitly unverified by these read-only checks.
- MCP stdio compatibility covers modern `2026-07-28` and legacy `2025-11-25`, schema discovery, structured/text results, execution-error flags and unknown-tool protocol errors.
- Persistent launcher guidance handles GUI startup, environment-sourced secrets and explicit workspace/state ownership. Auto-start remains optional. Codex worker MCP visibility is checked in its actual execution context.

Install on macOS/Linux with CPython 3.11. Download the wheel and source distribution together with `SHA256SUMS`, verify the checksums, then install the wheel into a dedicated environment. See [agent-led setup](https://github.com/phamviet86/codex-a2a-gateway/blob/v0.5.0/docs/agent-setup.md) and [deployment/upgrade](https://github.com/phamviet86/codex-a2a-gateway/blob/v0.5.0/docs/deployment.md).

This release preserves the v0.4.0 durable-task contract and adds no database schema migration. Results still require pull retrieval; cancellation is best-effort; Desktop-only tools are not automatically inherited. Windows/WSL, public multi-user deployment and live authentication/model/RAG execution are not newly certified by this release. No PyPI publication is included.

The release workflow builds and tags the exact successful main/push CI commit bearing `[release v0.5.0]`, uploads a draft, downloads and verifies all three assets, then publishes. Existing mismatching tags or published assets are never overwritten. See [validation](https://github.com/phamviet86/codex-a2a-gateway/blob/v0.5.0/docs/testing-report-v0.5.0.md).
