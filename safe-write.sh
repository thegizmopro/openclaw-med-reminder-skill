#!/usr/bin/env bash
# Deprecated (issue #1 §8): validation, locking, backups and the atomic
# replace all live in safe_write.py now — the inline validator is gone.
# Reads the new state on stdin (the old file-argument form is gone) and
# execs safe_write.py, passing the current state file's sha256 as the
# write token ('absent' when no state file exists yet).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHA="$(python3 -c 'import hashlib, os, sys
path = os.environ.get("MEDS_STATE_FILE") or os.path.join(sys.argv[1], "meds-state.json")
try:
    print(hashlib.sha256(open(path, "rb").read()).hexdigest())
except OSError:
    print("absent")' "$DIR")"
exec python3 "$DIR/safe_write.py" --expected-sha256 "$SHA"
