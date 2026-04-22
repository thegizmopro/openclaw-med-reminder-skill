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

*Weekly: store the dose time in `times[0]`. The day-of-week is not in the schema v1 —
just note it in `schedule.notes` (e.g. `"Every Monday"`).

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

**On "yes":** write the state file and run `setup-tasks.py`.
**On correction:** apply the change, re-read the affected line only, ask again.

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
command. It covers dose confirmations, deferrals, and status queries.

This is the most load-bearing part of the skill. An incorrect write corrupts state.
Follow the protocol below exactly for every reply that mutates state.

---

### Write protocol (mandatory for all state mutations)

Every reply that changes state must follow these four steps in order. Do not skip any.

**Step 1 — Read fresh state**
Before computing anything, read the current `meds-state.json`. Never use state from
earlier in the conversation — another process may have written since then.

**Step 2 — Resolve med names**
Map the user's words to med IDs using the fuzzy match rules below.
If the match is ambiguous: ask before proceeding. Never guess.

**Step 3 — Compute updated state in memory**
Build the full updated state object. Do not call `safe-write.sh` until the state
is complete and you are certain it is correct.

**Step 4 — Call dispatch.py**
Use the appropriate subcommand. `dispatch.py` validates, writes atomically via
`safe-write.sh`, recomputes `next_due`, and appends history. Do not write
`meds-state.json` directly — always go through `dispatch.py`.

```bash
# Confirm one med as taken (prescribed dose):
python3 reply.py confirm med-001

# Confirm one med with a different actual dose:
python3 reply.py confirm med-001 --dose-taken 250mg

# Confirm all pending/reminded/late meds at once:
python3 reply.py confirm --all

# Defer one med until next digest cycle:
python3 reply.py defer med-001
```

After the command runs, report the result to the user using the output printed by
`dispatch.py` (it prints a confirmation line). Do not fabricate a summary.

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

| User says | Intent | Action |
|-----------|--------|--------|
| `yes`, `done`, `all taken`, `took them all` | Confirm all | `dispatch.py confirm --all` |
| `took metformin`, `done met`, `met done` | Confirm one | `dispatch.py confirm <id>` |
| `took met: 250mg`, `metformin 250` | Confirm one, different dose | `dispatch.py confirm <id> --dose-taken 250mg` |
| `skip metformin`, `skip 2`, `not yet met` | Defer one | `dispatch.py defer <id>` |
| `status`, `what's due`, `my meds` | Info only | Read state, reply with summary — no write |
| `pause metformin` | Pause one med | Edit `med.paused = true` → `safe-write.sh` (see below) |
| `unpause metformin` | Unpause one med | Edit `med.paused = false` → `safe-write.sh` (see below) |
| `add a med`, `new med` | Start interview | Begin Section 2 interview flow |
| `remove metformin`, `delete met` | Delete a med | Confirm name, confirm deletion, then edit state |

---

### Pause / unpause (direct state edit)

`dispatch.py` does not have a pause subcommand. For pause/unpause, edit the state
directly following the write protocol:

1. Read `meds-state.json`
2. Resolve the med name (fuzzy match + confirm if ambiguous)
3. Confirm the action with the user: *"Pause Metformin — correct? It won't receive
   reminders until you unpause it."*
4. Set `med.paused = true` (or `false`)
5. Write via `safe-write.sh`: `echo <updated-json> | bash safe-write.sh`
6. Re-run `setup-tasks.py` to re-register (paused meds are excluded from scheduler)

---

### Status query (no write)

When the user asks what's due, read state and reply with a compact summary.
Do not write anything.

Template:
```
Your meds right now:
• Metformin 500mg — confirmed at 8:14am ✓
• Lisinopril 10mg — due at 9:00pm (pending)
• Vitamin D 2000IU — missed (logged)
```

Use ✓ for confirmed, ⏳ for pending/reminded, ⚠️ for late, ✗ for missed, ⏸ for paused.

---

### After a reply — what to say back

After every successful write, give a one-line confirmation that includes:
- Med name(s) affected
- What happened (taken / skipped / paused)
- Next due time if relevant (taken from `dispatch.py` output)

Example:
> Metformin 500mg marked as taken. Next dose: 8:00pm.
> Lisinopril 10mg skipped — I'll reset it at midnight.

Do not recap fields the user didn't ask about. Do not show internal IDs.

---

### Error handling — reply handler

| Situation | Action |
|-----------|--------|
| `dispatch.py` exits non-zero | Show the error output verbatim, do not retry automatically |
| Fuzzy match is ambiguous | Ask for clarification before any write |
| User says "undo" after a confirm | Explain that the log entry can't be deleted, but you can re-run confirm with the correct dose |
| User names a paused med in "all taken" | `dispatch.py confirm --all` skips paused meds automatically — inform the user |
| User confirms an `as_needed` med | Run `dispatch.py confirm <id>` — it works for as_needed; no next_due is set |
| State file not found | Tell the user and give the path from `MEDS_STATE_FILE` env or the script directory |

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
