/*
 * editor_check.cjs — drive editor.html's real <script> under a stub DOM.
 *
 * Scenarios (exit 0 = pass):
 *   save-fresh   open → save → the write happens and the mtime baseline re-syncs
 *   save-stale   open → the file changes on disk → save is REFUSED (M4, issue #1 §2)
 *   passthrough  edit a med → optional fields the form doesn't know survive
 *
 * The script source is eval'd together with the scenario driver so the
 * driver sees the eval scope (let/const declarations stay inside it).
 */
const fs = require('fs');
const path = require('path');

const scenario = process.argv[2];
const html = fs.readFileSync(path.join(__dirname, '..', 'editor.html'), 'utf8');
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.error('no <script> block found in editor.html'); process.exit(2); }

// ── stub DOM ────────────────────────────────────────────────────────────────
const els = new Map();
function el(id) {
  if (!els.has(id)) {
    els.set(id, {
      value: '', checked: false, innerHTML: '', textContent: '',
      className: '', disabled: false, style: {},
      classList: { add() {}, remove() {}, contains() { return false; } },
      appendChild() {}, addEventListener() {}, scrollIntoView() {},
    });
  }
  return els.get(id);
}
globalThis.document = {
  getElementById: el,
  createElement: () => el('created-' + els.size),
  querySelectorAll: () => [],
  addEventListener() {},
};
globalThis.window = globalThis;
globalThis.confirm = () => true;

// ── scenario state ──────────────────────────────────────────────────────────
const result = { writes: 0, failed: false };
let lastMod = 1000;
const STATE = JSON.stringify({
  version: '1',
  global: {
    timezone: 'America/Los_Angeles',
    quiet_hours: { start: '22:00', end: '07:00' },
    paused: false, digest_time: '08:00', delivery_channel: 'whatsapp',
    log_redact: true,
  },
  tick: { watermark_utc: '2026-04-21T15:00:00+00:00' },
  meds: [{
    id: 'med-001', name: 'Metformin', type: 'pill', dose: '500', unit: 'mg',
    paused: false,
    min_gap_hours: 6,
    active_since_utc: '2026-04-21T13:00:00+00:00',
    start_date: '2026-04-22', end_date: '2026-04-29',
    schedule: { frequency: 'interval', interval_hours: 8, day_of_week: null,
                with_food: false, notes: '', quiet_policy: 'shift' },
    escalation: { late_threshold_minutes: 30, missed_threshold_minutes: 90,
                  late_message: 'Late: {name}', missed_message: 'Missed: {name}' },
    state: { status: 'pending', last_taken: null, last_reminded: null,
             next_due: '2026-04-21T20:00:00+00:00', confirmed_dose: null,
             missed_count: 0, history: [] },
  }],
});
const file = {
  name: 'meds-state.json',
  get lastModified() { return lastMod; },
  async text() { return STATE; },
};
const writable = {
  async write() { result.writes++; },
  async close() {},
};
const handle = {
  async getFile() { return file; },
  async createWritable() { return writable; },
};
globalThis.showOpenFilePicker = async () => [handle];
globalThis.showSaveFilePicker = async () => handle;

function assert(cond, msg) {
  if (!cond) { console.error('ASSERT FAILED: ' + msg); result.failed = true; }
}

// ── scenarios (source-injected into the eval scope) ─────────────────────────
async function drvSaveFresh() {
  await openFile();
  assert(currentState !== null, 'state loaded');
  assert(openedLastModified === lastMod, 'baseline recorded at open');
  await saveFile();
  assert(result.writes === 1, 'save wrote the file (writes=' + result.writes + ')');
  assert(openedLastModified === lastMod, 'baseline re-synced after own write');
}

async function drvSaveStale() {
  await openFile();
  lastMod = 2000;   // the tick / reply.py replaced the file while it was open
  await saveFile();
  assert(result.writes === 0, 'stale save refused (writes=' + result.writes + ')');
  const status = el('status').textContent;
  assert(/changed on disk/i.test(status), 'refusal explains the problem: ' + status);
  assert(el('status').className === 'err', 'refusal is an error status');
}

async function drvPassthrough() {
  await openFile();
  openEditMed('med-001');
  el('f-name').value = 'Metformin XR';
  saveMed();
  const med = currentState.meds[0];
  assert(med.name === 'Metformin XR', 'name edited');
  assert(med.schedule.quiet_policy === 'shift', 'unknown schedule field preserved');
  assert(med.schedule.interval_hours === 8, 'known schedule field intact');
  assert(med.min_gap_hours === 6, 'unknown med-level field preserved');
  assert(med.active_since_utc === '2026-04-21T13:00:00+00:00', 'active_since preserved');
  assert(med.start_date === '2026-04-22' && med.end_date === '2026-04-29',
         'date-range bounds preserved (PR 6)');
  assert(med.state.status === 'pending', 'runtime state block preserved');
  assert(currentState.tick.watermark_utc === '2026-04-21T15:00:00+00:00',
         'top-level tick block preserved');
  assert(currentState.global.log_redact === true, 'unknown global field preserved');
}

const DRIVERS = {
  'save-fresh': drvSaveFresh,
  'save-stale': drvSaveStale,
  'passthrough': drvPassthrough,
};
const driver = DRIVERS[scenario];
if (!driver) { console.error('unknown scenario: ' + scenario); process.exit(2); }

// The editor script references `el`, `result` etc. through the eval's scope
// chain, so the driver body is appended to the eval'd source.
eval(
  m[1] +
  '\n;(async () => { await (' + driver.toString() + ')(); })()' +
  '.then(() => { process.exit(result.failed ? 1 : 0); },' +
  '        (e) => { console.error(e && e.stack || e); process.exit(2); });'
);
