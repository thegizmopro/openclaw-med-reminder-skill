---
name: med-reminder
description: Send medication reminders over the user's messaging channel — interview once to build meds-state.json, then handle dose replies (took / took-at / snooze / skip / undo) through reply.py.
---

# Med Reminder Skill — Interview Guide

This file tells the AI how to interview a user and produce a valid `meds-state.json`.
Follow the sections in order. Each section has one primary question, the fields it maps
to, how to handle common answers, and what defaults to apply silently.

The delivery channel (`global.delivery_channel`) is always set from context — whoever
invoked the skill (WhatsApp, Telegram, SMS, etc.) is the channel. Never ask the user
what app they're using.

---

## Section 0 — Timezone

**Ask:**
> "What city or timezone are you in? (e.g. New York, Chicago, Los Angeles, London)"

**Maps to:** `global.timezone`

**Handling:**
- Convert city names to IANA strings: New York → `America/New_York`,
  Chicago → `America/Chicago`, LA / Los Angeles → `America/Los_Angeles`,
  London → `Europe/London`, Sydney → `Australia/Sydney`, etc.
- If the user says a US state, use its most populous city's timezone.
- If ambiguous (e.g. "Indiana"), ask one follow-up: "Do you observe daylight saving time?"
- If still unsure, ask them to confirm from this list:
  `America/New_York`, `America/Chicago`, `America/Denver`, `America/Los_Angeles`
- Never infer from phone number or language.

**Silent defaults:** none — timezone is required.

---

## Section 1 — Quiet hours & digest time

**Ask:**
> "What time do you usually go to sleep and wake up?"

**Maps to:** `global.quiet_hours.start`, `global.quiet_hours.end`, `global.digest_time`

**Handling:**
- Sleep time → `quiet_hours.start` (round to nearest half-hour, format `HH:MM` 24h)
- Wake time  → `quiet_hours.end`   (same)
- Set `global.digest_time` = wake time + 30 minutes (silently, don't ask)
- If wake time + 30 min >= quiet start, cap digest at quiet start - 5 min
- Accept natural language: "midnight" → `00:00`, "noon" → `12:00`,
  "10ish" → `22:00`, "around 7" → `07:00`

**Silent defaults:**
- If the user doesn't know: `quiet_hours.start = "22:00"`, `quiet_hours.end = "07:00"`,
  `digest_time = "07:30"`

**Quiet-hours rules (re-checked by `reply.py` at write time):**

Quiet hours mute escalations, not dose-time reminders (the user chose the dose
time, so it always sends). Because of that, a dose scheduled *inside* the quiet
window would nudge and log missed while muted — so at interview time:

- **No dose time inside the quiet window.** If the user asks for one, refuse and
  offer the fix: move the dose before `quiet_hours.start`, or shift quiet hours
  later. Explain why: "its late nudges would be muted while you sleep."
- **No dose whose T+late nudge lands in the muted part of quiet hours** (more
  than 30 min after quiet start). Nudges may still land in the first 30 min.
  Offer the same fix: move the dose or adjust the window.
- Never silently accept a conflicting time to "sort it out later."
- `reply.py add-med` / `edit-med` / `set-quiet-hours` enforce both rules and
  refuse with a suggested fix. Relay that suggestion verbatim, re-ask, re-run.

---

## Section 2 — Medication list

**Ask:**
> "What medications do you take? List them all — name, dose, and how often.
> For example: 'Metformin 500mg twice a day, Lisinopril 10mg once a day at night'"

**Maps to:** one `meds[]` entry per medication — `name`, `dose`, `unit`, `type`, `schedule`

**Handling per med:**

### name
Use the name exactly as the user says it. Do not expand abbreviations unless certain.

### dose + unit
Split the user's dose string into numeric `dose` and string `unit`:
- `"500mg"` → `dose: "500"`, `unit: "mg"`
- `"2 tablets"` → `dose: "2"`, `unit: "tablet"`
- `"10ml"` → `dose: "10"`, `unit: "ml"`
- `"1 unit"` → `dose: "1"`, `unit: "unit"`
- If no unit given (e.g. just "1"), default `unit: "tablet"` for pills, ask if unclear.

### type
Infer from context:
| User says | `type` |
|-----------|--------|
| pill, tablet, capsule | `pill` |
| liquid, syrup, solution | `liquid` |
| injection, shot, pen, vial | `injection` |
| patch, transdermal | `patch` |
| inhaler, puffer, pump | `inhaler` |
| anything else | `other` |

### schedule.frequency + times
| User says | `frequency` | `times` |
|-----------|-------------|---------|
| once a day / daily | `once_daily` | ask what time |
| twice a day / BID | `twice_daily` | ask what times, or use `["08:00","20:00"]` |
| every N hours | `interval` | set `interval_hours: N`, no times |
| weekly / once a week | `weekly` | ask what time on what day* |
| as needed / PRN | `as_needed` | no times |

*Weekly: store the dose time in `times[0]` and the day in `schedule.day_of_week`
(`"mon"`, `"tue"`, `"wed"`, `"thu"`, `"fri"`, `"sat"`, `"sun"`). Required — without
it reminders fire every day. `schedule.notes` stays free-text ("Every Monday at
breakfast" is fine, but the machine-readable day lives in `day_of_week`).

### schedule.quiet_policy — interval meds only
Interval meds fire around the clock, so the §1 rules can't place their doses.
Ask each interval med's owner once:

> "Should [name] still remind during quiet hours, or hold reminders until
> quiet hours end?"

- "remind anyway" / "yes, always" → `schedule.quiet_policy: "ignore"`
- "hold" / "wait until morning" / "not at night" → `schedule.quiet_policy: "shift"`
- If the user doesn't care: set `"ignore"` silently (the default — omit the
  field and the schema treats it as `ignore`).

Do not ask this for scheduled-time meds (`once_daily`, `twice_daily`, `weekly`)
or `as_needed` — the field is interval-only.

### schedule.times — asking for times
Only ask for times if `frequency` is `once_daily`, `twice_daily`, or `weekly`.
Ask:
> "What time(s) do you take [name]?"

Accept 12h or 24h. Convert to zero-padded 24h `HH:MM`:
- `"8am"` → `"08:00"`, `"8:30pm"` → `"20:30"`, `"noon"` → `"12:00"`
- `"morning"` → `"08:00"`, `"night"` → `"21:00"`, `"bedtime"` → `"22:00"`
- `"with breakfast"` → `"08:00"` + set `with_food: true`
- `"with dinner"` → `"18:00"` + set `with_food: true`

### schedule.with_food
Set `true` if the user mentions food in any form ("with food", "with meals", "after eating").
Never ask explicitly — infer from their wording.

### schedule.notes
Copy any timing instruction the user gives verbatim (e.g. "Take on empty stomach",
"With full glass of water"). Leave empty string `""` if nothing.

### id
Auto-generate sequentially: `med-001`, `med-002`, etc. Never ask.

### paused
Always `false` for new meds. Never ask.

### start_date / end_date — courses and seasonal meds (optional)
Ask one follow-up when a med sounds time-bounded (antibiotics, a steroid
taper, allergy meds for pollen season):

> "Does [name] start on a specific day, and do you know when it ends?"

- Already running → omit `start_date`. Starts later → `start_date: "YYYY-MM-DD"`
  — nothing is reminded before that local date.
- Known end → `end_date: "YYYY-MM-DD"` — **inclusive** (the last reminded day).
  After it passes the med drops out of the digest with a one-time
  "Course complete" line; remove the med whenever.
- Dates are calendar dates in the user's timezone (`global.timezone`).
  `reply.py add-med` / `edit-med` reject malformed dates and end-before-start;
  clear a bound later with `"start_date": null` / `"end_date": null` in
  `edit-med --json -`.
- Everyday / open-ended meds → omit both fields silently. Never ask for those.

---

## Section 3 — Escalation

**Ask (once, for all meds together):**
> "If you forget a dose, how long should I wait before sending a reminder?
> And after how long should I just log it as missed?"

**Maps to:** `escalation.late_threshold_minutes`, `escalation.missed_threshold_minutes`
(applied to every med — use the same values unless the user specifies per-med)

**Handling:**
- Convert to minutes: "30 minutes" → `30`, "an hour" → `60`, "2 hours" → `120`
- Enforce: `late_threshold < missed_threshold`
- If only one number given: use it as `late_threshold`, set `missed_threshold = late_threshold * 3`
- If the user says "don't nag me": `late_threshold: 60`, `missed_threshold: 180`

**Silent defaults:** `late_threshold_minutes: 30`, `missed_threshold_minutes: 90`

**T+late vs quiet hours:** thresholds are the last input, so the §1 cross-check
happens here: for every scheduled-time med, compute dose time + `late_threshold_minutes`
and refuse or move any dose whose nudge lands in the muted part of quiet hours
(more than 30 min after quiet start), exactly as §1 describes. `reply.py add-med`
enforces this when the med is written — relay its suggested fix verbatim.

### Messages
Do not ask. Generate from templates and fill in the med's name/dose/unit:

```
late_message:   "Hey — still need to take your {name} ({dose}{unit})."
missed_message: "Missed your {name} dose. Logged."
```

If a med is sensitive (insulin, blood pressure, seizure) add urgency to the late message:
```
late_message: "Important: your {name} ({dose}{unit}) is overdue."
```

---

## Section 4 — Confirmation

Before writing the file, read back a compact summary:

```
Here's what I've got:

Timezone: America/Los_Angeles
Quiet: 10pm – 7am | Digest: 7:30am

Meds:
1. Metformin 500mg — twice daily at 8am, 8pm (with food)
2. Lisinopril 10mg — once daily at 9pm
3. Vitamin D 2000IU — once daily at 8am

Reminders: nudge after 30min, log missed after 90min.

Does this look right? Say 'yes' to save, or tell me what to fix.
```

**On "yes":** create or update the state through the safe paths below, then
register the scheduler. Never compose and write the full state yourself.
**On correction:** apply the change, re-read the affected line only, ask again.

1. **If `meds-state.json` already exists:** never rewrite it. Apply the changes
   with `reply.py` commands only (`set-quiet-hours`, `add-med`, `edit-med`, `remove`).

2. **If it does not exist yet:** create it once with the global answers only —
   no meds, no runtime state:

   ```bash
   python3 - << 'EOF' | python3 safe_write.py --expected-sha256 absent
   {
     "version": "1",
     "global": {
       "timezone": "America/Los_Angeles",
       "quiet_hours": { "start": "22:00", "end": "07:00" },
       "paused": false,
       "digest_time": "07:30",
       "delivery_channel": "telegram"
     },
     "meds": []
   }
   EOF
   ```

   This is the only write that doesn't go through `reply.py`, and it is safe
   because the `absent` token means the file must not exist yet and the content
   has nothing that can go stale. Fill the values from the interview;
   `delivery_channel` comes from context (never ask).

3. **Add every med through Section 7's `add-med`** so validation, id assignment
   and the §1/§3 quiet-hours checks run per med:

   ```bash
   python3 reply.py add-med --json - << 'EOF'
   {
     "name": "Metformin", "type": "pill", "dose": "500", "unit": "mg",
     "paused": false,
     "schedule": { "frequency": "twice_daily", "times": ["08:00", "20:00"] },
     "escalation": { "late_threshold_minutes": 30,
                     "missed_threshold_minutes": 90,
                     "late_message": "Hey — still need to take your {name} ({dose}{unit}).",
                     "missed_message": "Missed your {name} dose. Logged." }
   }
   EOF
   ```

   (`id`, `state` and tracking anchors are filled in by the command. A failed
   add-med prints why — relay it, fix the med, re-run.)

4. **Register the scheduler once:** `python3 setup-tasks.py --dry-run` to
   preview, then `python3 setup-tasks.py`. One repeating tick task is
   registered per state file; later `add-med` / `edit-med` / `remove` /
   `pause` need no re-run — the tick picks up changes on its next run.

---

## Section 5 — Med passport (optional)

After confirming, offer once:
> "Want me to generate a printable med card you can keep in your wallet or share with a doctor?"

If yes → run the `generate passport` command (see SKILL.md §6).
If no → skip silently.

---

## Section 6 — `generate passport` command

Triggered by the user saying: "generate passport", "med card", "print my meds", or similar.

Reads `meds-state.json` and produces a self-contained `med-passport.html` with:

**Layout (single page, wallet-card proportions or A5 landscape):**
- Header: patient name (ask if not known), date generated
- One row per active med: Name | Dose | Frequency | With food? | Notes
- Footer: "Generated by Med Reminder" + date
- Print-ready: `@media print` hides everything except the card

**Field mapping:**
| Passport field | Source |
|----------------|--------|
| Med name | `med.name` |
| Dose | `{med.dose}{med.unit}` |
| Frequency | human-readable from `schedule.frequency` + `times` |
| With food | `schedule.with_food` → "Yes" / "-" |
| Notes | `schedule.notes` |

Paused meds are excluded from the passport.

Output file: `med-passport.html` in the same directory as `meds-state.json`.
Confirm with: "Med passport saved to med-passport.html — open it in a browser to print."

---

## Section 7 — Reply handler

This section governs every inbound message that is **not** a setup interview or passport
command. It covers dose confirmations, snoozes, skips, undos, med edits, and status queries.

This is the most load-bearing part of the skill. An incorrect write corrupts state.
Follow the protocol below exactly for every reply that mutates state.

---

### Write protocol (mandatory for all state mutations)

Every state change is a single `reply.py` invocation. The command itself reads
fresh state under a lock, validates, writes atomically, and appends history.
The agent never reads-then-writes `meds-state.json`, never composes full-state
JSON, never calls `dispatch.py` (scheduler-era; its old confirm/defer writers
are gone), and never calls `safe-write.sh` (a deprecated shim around
`safe_write.py`).

**Step 1 — Resolve the med name**
Map the user's words to a med ID or digest position using the fuzzy match rules
below. If the match is ambiguous: ask before proceeding. Never guess.

**Step 2 — Run one `reply.py` command**
Exactly one per reply. Do not chain a write behind a failed command.

**Step 3 — Relay the command's output verbatim**
It names the med and the dose credited ("Logged as your 8:00am dose."). Never
fabricate a summary, never invent a next-dose time.

```bash
# Took the prescribed dose, just now:
python3 reply.py confirm med-001

# …by position in today's digest ("took the second one"):
python3 reply.py confirm --position 2

# Different actual dose:
python3 reply.py confirm med-001 --dose-taken 250mg

# "took it at 8" — credit a specific past time:
python3 reply.py confirm med-001 --taken-at 08:00

# Pin which scheduled dose (after an exit-4 ambiguity):
python3 reply.py confirm med-001 --dose 20:00

# Everything pending at once (skips paused and as-needed meds):
python3 reply.py confirm --all

# Not yet / remind me later (default 30 min):
python3 reply.py snooze med-001 --minutes 30

# Skip this dose entirely (logged skipped, never missed):
python3 reply.py skip med-001

# Undo the last taken/skipped for this med (within 24h):
python3 reply.py undo med-001
```

### Exit codes — confirm

| Exit | Meaning | Agent action |
|------|---------|--------------|
| 0 | Written | Relay the credited-dose line |
| 3 | Duplicate: already logged within the med's minimum gap, nothing written | Relay the "You logged X at …" warning and ask. Only if the user insists they really took it again, re-run once with the printed nonce: `--force <nonce>`. Never pass the nonce without an explicit user OK |
| 4 | Ambiguous which dose, nothing written | The command prints both candidate doses and "Reply with the time you took it". Ask the user, then re-run with `--dose HH:MM` (which scheduled dose) or `--taken-at HH:MM` (when it was taken) |

---

### Fuzzy name matching rules

The user will refer to meds by partial name, nickname, or position number from the
last digest. Map to `med.id` using these rules in order:

1. **Exact match** (case-insensitive): `"metformin"` → `med.name == "Metformin"` → use it.
2. **Prefix match**: `"met"` matches `"Metformin"`. Use it if unique.
3. **Substring match**: `"formin"` matches `"Metformin"`. Use it if unique.
4. **Position from last digest**: `"skip 2"` → second med in the last digest message.
5. **Ambiguous** (matches multiple meds): ask the user to clarify before proceeding.
6. **No match**: tell the user and list the known med names.

Never write state based on an ambiguous match. Always confirm with the user first:
> "Did you mean Metformin 500mg? (yes/no)"

---

### Reply intent map

| User says | Intent | Command |
|-----------|--------|---------|
| `yes`, `done`, `all taken`, `took them all` | Confirm all | `reply.py confirm --all` |
| `took metformin`, `done met`, `met done` | Confirm one | `reply.py confirm <id>` |
| `took met: 250mg`, `metformin 250` | Confirm one, different dose | `reply.py confirm <id> --dose-taken 250mg` |
| `took it at 8`, `met at 8am`, `took met this morning` | Confirm with a time | `reply.py confirm <id> --taken-at 08:00` |
| `snooze met`, `later`, `not yet met`, `remind me in 30` | Snooze | `reply.py snooze <id> [--minutes N]` |
| `skip metformin`, `skip 2` | Skip a dose | `reply.py skip <id>` |
| `undo`, `undo met`, `I didn't take it after all` | Undo last taken/skipped | `reply.py undo <id>` |
| `status`, `what's due`, `my meds` | Info only | `reply.py status` — relay output, no write |
| `pause metformin` | Pause one med | `reply.py pause <id>` (confirm with the user first) |
| `unpause metformin` | Unpause one med | `reply.py unpause <id>` |
| `add a med`, `new med` | Start interview | Section 2 flow, written via `reply.py add-med --json -` |
| `change met to 9pm`, `met is now 1000mg` | Edit a med | `reply.py edit-med <id> --json -` |
| `quiet hours 11 to 8` | Change quiet hours | `reply.py set-quiet-hours 23:00 08:00` |
| `remove metformin`, `delete met` | Delete a med | Confirm name + deletion with the user, then `reply.py remove <id> --yes` |

Any `<id>` may be replaced by `--position N` (the Nth med in **today's** digest
order; a stale yesterday's order is refused and the resolved name is echoed).

---

### Pause / unpause

Confirm the action with the user first: *"Pause Metformin — correct? It won't
receive reminders until you unpause it."* Then:

```bash
python3 reply.py pause med-001
python3 reply.py unpause med-001
```

Unpause restarts tracking from now — doses during the pause aren't counted as
missed. No `setup-tasks.py` re-run is needed: one tick task covers all meds,
paused or not.

---

### Status query (no write)

When the user asks what's due, run the read-only summary and relay it:

```bash
python3 reply.py status
```

It prints one line per med with schedule, last dose, and current stage
(✓ taken, ⏳ pending/reminded, ⚠️ late, ✗ missed, ⏸ paused), plus quiet hours
and digest time. Write nothing.

---

### After a reply — what to say back

After every successful write, give a one-line confirmation built from the
command's own output:
- Med name(s) affected
- What happened (taken / skipped / snoozed / undone / paused) and, for confirms,
  which scheduled dose was credited
- Next due time only if the command printed one

Example:
> Metformin 500mg — logged as your 8:00am dose.
> Lisinopril 10mg — snoozed 30 min.

Do not recap fields the user didn't ask about. Do not show internal IDs.

---

### Error handling — reply handler

| Situation | Action |
|-----------|--------|
| `reply.py` exits 1 | Show the error output verbatim, do not retry automatically |
| `reply.py confirm` exits 3 (duplicate) | Relay the "You logged X at …" warning; only re-run with `--force <nonce>` if the user explicitly confirms a second dose |
| `reply.py confirm` exits 4 (ambiguous) | Relay the two candidate doses it printed; re-run with `--dose` or `--taken-at` once the user answers |
| Fuzzy match is ambiguous | Ask for clarification before any write |
| User says "undo" after a confirm | Run `reply.py undo <id>` — works within 24h of the last taken/skipped and echoes what was undone |
| User names a paused med in "all taken" | `confirm --all` skips paused (and as-needed) meds automatically — inform the user |
| User confirms an `as_needed` med | Run `reply.py confirm <id>` — it works for as_needed; no next_due is set |
| `add-med` / `edit-med` / `set-quiet-hours` refuses a quiet-hours conflict | Relay the suggested fix verbatim; move the dose or adjust quiet hours, then re-run |
| `--position N` refused as stale | The digest order is from a previous day — resolve the med by name instead |
| State file not found | Tell the user; the command's error prints the fix (`cp meds-state.template.json meds-state.json`) and the path from `MEDS_STATE_FILE` |

---

## Error handling

| Situation | Action |
|-----------|--------|
| User gives a med name only, no dose | Ask: "What dose of [name]?" |
| User gives dose but no unit | Infer from type; if unclear ask once |
| Conflicting times (e.g. twice daily but only one time given) | Ask for the second time |
| `late_threshold >= missed_threshold` | Tell the user and ask them to pick again |
| Unknown timezone | Ask for the nearest major city |
| User says "I take a lot of meds, let me list them" | Wait for the full list before asking follow-ups |
| User edits a med after confirmation | Re-generate that med's entry only; re-confirm the changed line |
