#!/usr/bin/env bash
HARVEY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HARVEY_DIR"
source .venv/bin/activate
harvey dashboard

