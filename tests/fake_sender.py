"""
fake_sender.py — test double for MEDS_SEND_CMD (issue #1 testing plan).

Appends each message to $FAKE_SEND_OUT as "<text>\\n---\\n" so tests can
count and inspect sends. Behavior switches via marker files:

  $FAKE_SEND_FAIL exists   → exit 1, nothing appended
  $FAKE_SEND_SLEEP exists  → sleep float(file contents) seconds first
  $FAKE_SEND_TRIP exists   → append, then hang (killed by the 60s timeout;
                             used to simulate a dead process mid-send)
"""
import os
import sys
import time

# stdin arrives UTF-8 regardless of the Windows console code page
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

out = os.environ.get("FAKE_SEND_OUT", os.devnull)

if os.path.exists(os.environ.get("FAKE_SEND_SLEEP", "")):
    with open(os.environ["FAKE_SEND_SLEEP"]) as f:
        time.sleep(float(f.read() or "5"))

text = sys.stdin.read()

if os.path.exists(os.environ.get("FAKE_SEND_FAIL", "")):
    sys.stderr.write("fake_sender: injected failure\n")
    sys.exit(1)

with open(out, "a", encoding="utf-8") as f:
    f.write(text + "\n---\n")
