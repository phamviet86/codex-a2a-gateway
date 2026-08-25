#!/bin/sh
set -eu

apply=false
include_gateway=false
for argument in "$@"; do
  case "$argument" in
    --apply) apply=true ;;
    --include-gateway-service) include_gateway=true ;;
    *) echo "Unknown option: $argument" >&2; exit 2 ;;
  esac
done

if [ "$apply" != "true" ]; then
  echo "Rollback plan (no changes made):"
  echo "  codex mcp remove codex-hermes-a2a-bridge"
  echo "  hermes config unset gateway.platforms.a2a"
  echo "  hermes plugins disable a2a-platform"
  echo "Gateway service is preserved because it may serve unrelated platforms."
  echo "Re-run with --apply to execute. Add --include-gateway-service only if this rollout exclusively owns it."
  echo "Source, venv, SQLite and Hermes transcripts are retained."
  exit 0
fi

codex mcp remove codex-hermes-a2a-bridge || true
hermes config unset gateway.platforms.a2a || true
hermes plugins disable a2a-platform || true

if [ "$include_gateway" = "true" ]; then
  hermes gateway stop || true
  hermes gateway uninstall || true
fi

echo "Rollback applied. Project source, .venv, persistent data and Hermes source were retained."
