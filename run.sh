#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
exec uv run --extra terminal design-agent chat "$@"
