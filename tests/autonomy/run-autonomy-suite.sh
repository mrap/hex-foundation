#!/bin/bash
set -uo pipefail
cd "$(dirname "$0")"
python3 run_autonomy_suite.py "$@"
