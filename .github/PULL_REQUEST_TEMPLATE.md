## Summary

## Verification

- [ ] `ruff check .`
- [ ] `ruff format --check .`
- [ ] `mypy src`
- [ ] `pytest --cov=codex_hermes_a2a_bridge`
- [ ] Package build/check if metadata or packaging changed

## Safety and compatibility

- [ ] No secrets, transcripts, runtime databases, backups or personal paths
- [ ] MCP stdio writes no diagnostics to stdout
- [ ] Loopback-only policy remains enforced, or the security impact is explicitly reviewed
- [ ] Public contracts and CHANGELOG are updated when needed
