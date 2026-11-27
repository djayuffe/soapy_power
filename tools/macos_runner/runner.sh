#!/bin/bash
# Stable launchd shim. Request validation and locking live in the checkout.
set -u -o pipefail
: "${SPR_REPO:?}" "${SPR_SESSIONS:?}"
for candidate in "${SPR_RUNNER_PYTHON:-}" "$SPR_SESSIONS/.venv/bin/python" /opt/homebrew/bin/python3 python3; do
  [ -n "$candidate" ] || continue
  if "$candidate" -c 'import sys;sys.exit(sys.version_info < (3,9))' >/dev/null 2>&1; then
    exec "$candidate" "$SPR_REPO/tools/macos_runner/runner.py"
  fi
done
echo 'Runner requires Python 3.9 or newer' >&2
exit 1
