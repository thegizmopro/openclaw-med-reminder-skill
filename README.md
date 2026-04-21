# Med Reminder Skill

An OpenClaw skill that sends medication reminders through whatever messaging app you already use — WhatsApp, Telegram, SMS, or anything else you can script. Reminders land in a conversation you're already in, not as a notification you swipe away.

Three escalation tiers per dose: on-time reminder → late nudge → missed log. Each med has its own schedule, dose, and thresholds. State lives in a single JSON file — no database, no backend, no account.

---

## Prerequisites

- Python 3.9+
- Windows: Git Bash (for `safe-write.sh`) + Task Scheduler (built-in)
- Mac/Linux: bash + crontab (built-in)
- Chrome, Edge, or Brave (for the HTML editor — requires File System Access API)
- OpenClaw with a messaging channel configured

On Windows, install the IANA timezone database:
```
pip install tzdata
```

---

## Install

```bash
git clone <repo-url>
cd med-reminder-skill
cp meds-state.template.json meds-state.json
```

That's it. No build step, no package manager.

---

## Configure your medications

Two ways — pick one:

### Option A: HTML editor (recommended)

Open `editor.html` in Chrome, Edge, or Brave. Click **Open file**, load your `meds-state.json`, edit global settings and medications, click **Save**. All validation runs in the browser before writing.

### Option B: Conversational interview

In OpenClaw, start a conversation with the med reminder skill and say "set up my meds." The AI walks you through timezone, quiet hours, and each medication one question at a time. See `SKILL.md` for the full question flow.

Both paths validate the state file before writing. Invalid state can never reach disk.

---

## Wire up your messaging channel

Create `send-message.sh` in the skill directory. It receives the message text on **stdin** and must exit 0 on success.

```bash
#!/usr/bin/env bash
# send-message.sh — wire this to your channel

MESSAGE=$(cat)   # full message text arrives on stdin

# Example: WhatsApp via OpenClaw gateway
# curl -s -X POST "$OPENCLAW_WEBHOOK" \
#   -H "Content-Type: application/json" \
#   -d "{\"text\": $(echo "$MESSAGE" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}"

# Example: Telegram
# curl -s "https://api.telegram.org/bot$TELEGRAM_TOKEN/sendMessage" \
#   -d chat_id="$TELEGRAM_CHAT_ID" \
#   --data-urlencode text="$MESSAGE" > /dev/null

# Example: log to file only (for testing)
echo "$MESSAGE" >> ~/med-reminder-sent.log
```

Make it executable:
```bash
chmod +x send-message.sh
```

Alternatively, set the `MEDS_SEND_CMD` environment variable to any shell command that reads from stdin.

---

## Register the scheduler

After saving your `meds-state.json`, register the scheduled tasks:

```bash
# Preview what will be registered (no changes)
python3 setup-tasks.py --dry-run

# Register (Windows: Task Scheduler, Mac/Linux: crontab)
python3 setup-tasks.py
```

Re-run `setup-tasks.py` any time you add, edit, or remove a medication.

**Windows:** Tasks appear in Task Scheduler Library → MedReminder.  
**Mac/Linux:** Tasks are written to your crontab.

To remove all tasks:
```bash
python3 setup-tasks.py --clear
```

---

## Daily digest

A morning summary fires once daily at the time you set in global settings (default: 8:00am):

```
Meds for Mon Apr 21:
1. Metformin 500mg | 8:00am, 8:00pm (with food)
2. Lisinopril 10mg | 9:00pm

Reply: 'all taken' | 'skip [name]' | 'took [name]: [dose]' | 'done [name1], [name2]'
```

Reply handling runs on the AI side — the skill reads the reply, resolves med names by fuzzy match, and updates `meds-state.json` via `safe-write.sh`.

---

## Escalation flow

For each scheduled dose:

| Time | What happens |
|------|-------------|
| Dose time | Initial reminder sent |
| Dose time + late threshold (default: 30min) | Late nudge sent if not confirmed |
| Dose time + missed threshold (default: 90min) | Logged as missed if still unconfirmed |

Thresholds are configurable per medication.

**Quiet hours:** No reminders sent during the window you configure (default: 10pm–7am).  
**Global pause:** Silence everything temporarily — one checkbox in the editor.  
**Per-med pause:** Skip a single medication without touching others.

---

## Med passport

Generate a printable medication card for doctor visits or emergency contacts:

```bash
python3 generate-passport.py --name "Jane Smith" --dob "1975-03-12"
```

Or say "generate passport" in a conversation with the skill. Output is `med-passport.html` — open in a browser and print/save as PDF. Paused medications are excluded.

Options:
```
--name NAME     Patient name on the card
--dob  DATE     Date of birth (optional)
--out  PATH     Output file path (default: med-passport.html)
--no-prompt     Skip the name prompt
--dry-run       Print HTML to stdout
```

---

## State file

`meds-state.json` is the only file that changes at runtime. It lives wherever you put it (default: same directory as the scripts).

`safe-write.sh` is the only path that writes it programmatically:
1. Validates structure (required fields, formats, constraints)
2. Acquires a lockfile — prevents concurrent writes
3. Checks the file hasn't changed since the caller read it
4. Backs up the current file to `meds-state.json.bak`
5. Atomically replaces the state file

The HTML editor uses the browser File System Access API and validates with the bundled schema before saving — a separate safe path.

Environment overrides:
```bash
MEDS_STATE_FILE=/path/to/meds-state.json
MEDS_LOG_FILE=/path/to/dispatch.log
MEDS_SEND_CMD="curl -X POST ..."   # overrides send-message.sh
```

---

## Test the dispatch engine

```bash
# Dry-run: print what would be sent without writing state or sending messages
python3 dispatch.py --dry-run fire  med-001 0
python3 dispatch.py --dry-run check med-001 0
python3 dispatch.py --dry-run miss  med-001 0
python3 dispatch.py --dry-run digest
```

---

## Run the test suite

```bash
pip install pytest tzdata   # one-time
pytest tests/ -v
```

57 tests covering escalation resolver, next-due calculation, history trimming, digest logic, safe-write atomicity, and passport generation.

---

## File structure

```
med-reminder-skill/
├── meds-state.json          # your state file (git-ignored, created from template)
├── meds-state.template.json # blank valid state — copy to start
├── meds-state.json.bak      # auto-created by safe-write.sh before each write
├── meds.schema.json         # JSON Schema for state validation
├── dispatch.py              # reminder dispatch engine (fire/check/miss/digest)
├── setup-tasks.py           # registers Task Scheduler / crontab entries
├── safe-write.sh            # atomic validated state writer
├── generate-passport.py     # med passport HTML generator
├── editor.html              # browser-based state editor
├── send-message.sh          # YOU CREATE THIS — wires to your messaging channel
├── SKILL.md                 # AI interview guide and reply handler spec
├── requirements.txt         # Python dependencies (tzdata on Windows)
└── tests/
    ├── conftest.py
    ├── test_resolver.py
    ├── test_compute_next_due.py
    ├── test_history_trim.py
    ├── test_dispatch_digest.py
    ├── test_safe_write.py
    └── test_passport.py
```

---

## Quick start checklist

- [ ] Clone repo, `cp meds-state.template.json meds-state.json`
- [ ] Open `editor.html` in Chrome/Edge/Brave, load the file, add your meds, save
- [ ] Create `send-message.sh` and test it: `echo "test" | bash send-message.sh`
- [ ] Run `python3 setup-tasks.py --dry-run` to preview tasks
- [ ] Run `python3 setup-tasks.py` to register tasks
- [ ] Wait for the first digest, reply to confirm it's working
- [ ] Run `pytest tests/` to verify the engine is healthy
