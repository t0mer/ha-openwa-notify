#!/usr/bin/env bash
# Local pre-commit security scan. Reports go to the gitignored scans/ dir.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p scans
status=0

run() {
  local name="$1"; shift
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "skip: $1 not installed"
    return
  fi
  echo "==> $name"
  "$@" || status=1
}

run bandit   bandit -r custom_components -f txt -o scans/bandit.txt
# Audits the environment of $PYTHON (default: the project .venv with the
# test requirements installed, since they need Python 3.13+).
PYTHON="${PYTHON:-.venv/bin/python}"
run pip-audit "$PYTHON" -m pip_audit -o scans/pip-audit.txt
run gitleaks gitleaks detect --source . --no-banner --report-path scans/gitleaks.json
run trivy    trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL \
               --skip-dirs .venv --output scans/trivy.txt --exit-code 1 .

exit "$status"
