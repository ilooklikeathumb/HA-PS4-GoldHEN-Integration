// the panel's Play time tab — totals, games, sessions, escaped names, HA-only reads, per console.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let Panel;
const timers = [];
const sandbox = {
  HTMLElement: class { attachShadow() { this.shadowRoot = {querySelector: () => null, querySelectorAll: () => []}; } },
  customElements: {define: (_name, cls) => { Panel = cls; }},
  alert: () => {}, confirm: () => true, requestAnimationFrame: () => {},
  setInterval: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
  clearInterval: (id) => { if (timers[id - 1]) timers[id - 1].cleared = true; },
  console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../custom_components/ps4_goldhen/frontend/ps4-goldhen-panel.js'), 'utf8'), sandbox);

const SUMMARY = {
  today: 4020, week: 4020, month: 7020, total: 7020, since: '2026-09-01T20:00:00+00:00',
  games: [
    {title_id: 'CUSA90005', name: '<b>Game D</b>', seconds: 4020, sessions: 1, first: '2026-10-16T20:00:00+00:00',
     last: '2026-10-16T21:07:00+00:00', week: 4020},
    {title_id: 'CUSA90006', name: null, seconds: 3000, sessions: 2, first: '2026-09-01T20:00:00+00:00',
     last: '2026-10-01T20:00:00+00:00', week: 0},
  ],
  sessions: [
    {title_id: 'CUSA90005', name: '<b>Game D</b>', start: '2026-10-16T20:00:00+00:00', end: '2026-10-16T21:07:00+00:00',
     seconds: 4020, open: true, playing: true},
    {title_id: 'CUSA90006', name: null, start: '2026-10-01T19:10:00+00:00', end: '2026-10-01T20:00:00+00:00',
     seconds: 3000, open: false, playing: false},
  ],
};

function panel(summaries) {
  const calls = [];
  const p = new Panel();
  p._hass = {
    callWS: async (msg) => {
      calls.push(msg);
      if (msg.type === 'ps4_goldhen/list_entries') {
        return {entries: [{entry_id: 'console_a', name: 'PS4 PRO A', ps4_host: '192.0.2.121'},
                          {entry_id: 'console_b', name: 'Console B PS4', ps4_host: '192.0.2.189'}]};
      }
      if (msg.type === 'ps4_goldhen/play_time') {
        const s = summaries[msg.entry_id];
        if (s instanceof Error) throw s;
        return s;
      }
      throw new Error(`unexpected ${msg.type}`);
    },
  };
  return {p, calls};
}

async function main() {
  {
    const {p, calls} = panel({console_a: SUMMARY});
    await p._loadEntries();
    p._render();
    assert.match(p.shadowRoot.innerHTML, /data-tab="playtime">Play time</);
    // tab order
    assert.deepEqual([...p.shadowRoot.innerHTML.matchAll(/data-tab="(\w+)"/g)].map((m) => m[1]),
                     ['games', 'saves', 'screenshots', 'playtime', 'ftp', 'binloader', 'klog']);
    p._tab = 'playtime';
    await p._ptLoad();
    const html = p.shadowRoot.innerHTML;
    assert.deepEqual(calls.filter((c) => c.type !== 'ps4_goldhen/list_entries').map((c) => [c.type, c.entry_id]),
                     [['ps4_goldhen/play_time', 'console_a']]);  // HA only: no FTP, no klog
    assert.match(html, /Today<\/div><div class="pt-num">1h 7m/);
    assert.match(html, /All time<\/div><div class="pt-num">1h 57m/);
    assert.match(html, /&lt;b&gt;Game D&lt;\/b&gt;/);
    assert.doesNotMatch(html, /<b>Game D<\/b>/);
    assert.match(html, /<span class="pt-tid">CUSA90006<\/span>/);  // no name: the title ID
    assert.match(html, /Playing<\/span>/);
    assert.match(html, /50m/);
    console.log('PASS: totals, games, sessions, escaped names; one HA call');
  }

  {
    timers.length = 0;  // the block above left its panel's timer
    const {p} = panel({console_a: SUMMARY});
    await p._loadEntries();
    p._tab = 'playtime';
    await p._ptLoad();
    const live = timers.filter((t) => !t.cleared);
    assert.equal(live.length, 1);
    assert.equal(live[0].ms, 60000);
    await p._ptLoad();
    assert.equal(timers.filter((t) => !t.cleared).length, 1);  // not one more per refresh
    p._selectEntry('console_b');
    assert.equal(timers.filter((t) => !t.cleared).length, 0);  // switching console stops it
    assert.equal(p._pt, null);
    console.log('PASS: refreshes once a minute while open, one timer, stops on console switch');
  }

  {
    const {p} = panel({console_a: {...SUMMARY, games: [], sessions: [], today: 0, week: 0, month: 0, total: 0, since: null}});
    await p._loadEntries();
    p._tab = 'playtime';
    await p._ptLoad();
    assert.match(p.shadowRoot.innerHTML, /No play time yet/);
    console.log('PASS: empty state');
  }

  {
    const {p} = panel({console_a: new Error('Unknown PS4')});
    await p._loadEntries();
    p._tab = 'playtime';
    await p._ptLoad();
    assert.match(p.shadowRoot.innerHTML, /Couldn't load play time: Unknown PS4/);
    console.log('PASS: error shown');
  }

  {
    const {p} = panel({});
    assert.equal(p._ptDuration(0), '0m');
    assert.equal(p._ptDuration(30), '< 1m');
    assert.equal(p._ptDuration(59 * 60), '59m');
    assert.equal(p._ptDuration(3600 + 5 * 60 + 59), '1h 5m');
    assert.equal(p._ptDuration(3 * 3600), '3h');
    assert.equal(p._ptDuration(11244), '3h 7m');  // 187.4 min: what the kiosk showed as "187 min"
    assert.equal(p._ptDuration(24 * 3600), '1d');
    assert.equal(p._ptDuration(52 * 3600 + 59 * 60), '2d 4h');
    console.log('PASS: durations');
  }
}

main().catch((err) => { console.error(err); process.exit(1); });
