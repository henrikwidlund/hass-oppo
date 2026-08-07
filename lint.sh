#!/usr/bin/env bash
set -euo pipefail

# Run `uv sync --frozen --group dev` first to set up/update the environment.
uv run --frozen --no-build --no-sync ruff check custom_components/ --fix && \
uv run --frozen --no-build --no-sync ruff format --check custom_components/ && \
uv run --frozen --no-build --no-sync python -m pyright custom_components/
