// the panel's Saves tab — the list of HA's copies (no PS4 call), Back up now with a status poll, History
// with Keep, Send to PS4 (Apollo's Fake USB folder) + Remove, zip downloads through a signed path.
// Off / On (Automatic removed), rows with Back up / Send to PS4... / More, Send asks first (console
// picked in the dialog), "Unknown game", the skipped-game status, no browser alerts.
// Back up and Download ask too and say what happens; Select → Back up / Send / Download N.
// two panes, On PS4 (a console read, each save badged against HA's copy) | In HA (newest copies + older
// versions); Select per pane with single saves, tri-state game boxes, one version per save; Delete from HA; the Fake USB
// folder as read; a pane toggle on phones.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let Panel;
const elements = {};   // what shadowRoot.querySelector finds: the tests bind real handlers to these stand-ins
const intervals = [];  // setInterval callbacks, run by hand
const clicked = [];    // <a download> clicks
const sandbox = {
  HTMLElement: class {
    attachShadow() { this.shadowRoot = {querySelector: (sel) => elements[sel] || null, querySelectorAll: () => []}; }
  },
  customElements: {define: (_name, cls) => { Panel = cls; }},
  alert: (msg) => { throw new Error(`alert: ${msg}`); }, confirm: () => true, requestAnimationFrame: () => {},
  setInterval: (fn) => { intervals.push(fn); return intervals.length; }, clearInterval: () => {}, console,
  document: {
    createElement: () => ({click() { clicked.push({href: this.href, download: this.download}); }, remove() {}}),
    body: {appendChild: () => {}},
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../custom_components/ps4_goldhen/frontend/ps4-goldhen-panel.js'), 'utf8'), sandbox);

const UID = '10000001';
const OLDER = '2026-10-16 014221';
const LIST = {users: [{uid: UID, name: 'player1', games: [
  {title_id: 'CUSA90003', game: '<b>Game C</b>', last_saved: '2026-10-16T03:00:00Z', bytes: 1888, on_ps4: true, slots: [
    {slot: 'SAVEDATAPlayer', name: 'Player', saved: '2026-10-16T03:00:00Z', bytes: 396, on_ps4: true,
     versions: [{stamp: OLDER, bytes: 396, kept: false, name: 'Prologue'}]},
    {slot: 'sce_sdmemory', name: '(Autosave) Chapter 1 (100.0%)', saved: '2026-10-16T01:43:04Z', bytes: 1096,
     on_ps4: true, versions: []},
  ]},
  {title_id: 'CUSA90006', game: 'CUSA90006', last_saved: '2026-10-16T00:55:31Z', bytes: 736, on_ps4: false, slots: [
    {slot: 'SAVEDATA01', name: null, saved: '2026-10-16T00:55:31Z', bytes: 736, on_ps4: false, versions: []},
  ]},
]}]};

function consoleRead(extra = {}) {
  return {read_at: new Date().toISOString(), power_state: 'on', asleep: false, running: false, sent: null,
    users: [{uid: UID, name: 'player1', games: [
      {title_id: 'CUSA90003', game: '<b>Game C</b>', open: false, last_saved: '2026-10-16T04:00:00Z', slots: [
        {slot: 'SAVEDATAPlayer', name: 'Chapter 3', saved: '2026-10-16T04:00:00Z', bytes: 396, status: 'changed'},
        {slot: 'sce_sdmemory', name: '(Autosave) Chapter 1 (100.0%)', saved: '2026-10-16T01:43:04Z', bytes: 1096, status: 'in_ha'},
        {slot: 'SAVEDATANew', name: null, saved: '2026-10-16T01:00:00Z', bytes: 106, status: 'not_in_ha'},
      ]},
      {title_id: 'CUSA90009', game: 'Game F', open: true, last_saved: '2026-10-16T00:10:00Z', slots: [
        {slot: 'ChicagoSaveSlot', name: 'Main', saved: '2026-10-16T00:10:00Z', bytes: 96000, status: 'not_in_ha'},
      ]},
    ]}],
    fake_usb: [{title_id: 'CUSA90003', game: '<b>Game C</b>', slots: [{slot: 'SAVEDATAPlayer', name: 'Prologue'}]}],
    ...extra};
}

function settingsFor(mode) {
  return {mode, limit_gb: 5, limit_min_gb: 0.5, limit_max_gb: 50, folder: mode === 'off' ? null : '/share/PS4 Saves/PS4 PRO A',
    usage: {bytes: 2624, versions: 1}, running: false, progress: {done: 0, total: 0}, last: null, last_success: null,
    status: 'never', power_state: 'on', sent: null};
}

const ENTRIES = [{entry_id: 'console_a', title: 'PS4 GoldHEN (192.0.2.121)', name: 'PS4 PRO A', ps4_host: '192.0.2.121'}];

function panel(settings, entries = ENTRIES, read = consoleRead()) {
  const calls = [];
  const p = new Panel();
  p._hass = {
    callWS: async (msg) => {
      calls.push(msg);
      if (msg.type === 'ps4_goldhen/list_entries') return {entries};
      if (msg.type === 'ps4_goldhen/saves/get') return {...settings};
      if (msg.type === 'ps4_goldhen/saves/set') return Object.assign(settings, msg);
      if (msg.type === 'ps4_goldhen/saves/list') return LIST;
      if (msg.type === 'ps4_goldhen/saves/console') return {...read, power_state: settings.power_state};
      if (msg.type === 'ps4_goldhen/saves/backup') { settings.running = true; return {started: true}; }
      if (msg.type === 'ps4_goldhen/saves/keep') return {kept: msg.kept};
      if (msg.type === 'ps4_goldhen/saves/delete') return {deleted: msg.items.length};
      if (msg.type === 'ps4_goldhen/saves/zip') {
        return {name: 'PS4 PRO A player1 saves.zip', bytes: 3000000,
                path: '/api/ps4_goldhen/saves/console_a/download?file=PS4%20PRO%20A%20player1%20saves.zip'};
      }
      if (msg.type === 'ps4_goldhen/saves/send') {
        const sent = {when: '2026-10-16T12:00:00Z', from: 'PS4 PRO A', user: 'player1', games: ['<b>Game C</b>'],
          version: null, files: 4, bytes: 1492, saves: []};
        if (msg.target_entry_id === 'console_a') settings.sent = sent;
        return {target: msg.target_entry_id === 'console_a' ? 'PS4 PRO A' : 'PS4 PRO B', ...sent};
      }
      if (msg.type === 'ps4_goldhen/saves/clear_sent') { settings.sent = null; return {deleted: 2}; }
      if (msg.type === 'auth/sign_path') return {path: `${msg.path}&authSig=x`};
      throw new Error(`unexpected ${msg.type}`);
    },
  };
  return {p, calls};
}

async function open(settings, entries, read) {
  const made = panel(settings, entries, read);
  await made.p._loadEntries();
  made.p._tab = 'saves';
  await made.p._svLoad();
  return made;
}

const tick = () => new Promise((r) => setImmediate(r));

// Stand-ins for the elements the tab binds, and a click on something with data-act
function wire(p) {
  for (const id of ['#sv-root', '#sv-confirm-go', '#sv-confirm-cancel', '#sv-also-versions', '#sv-ack']) elements[id] = {};
  p._render();
  const click = async (dataset, extra = {}) => {
    elements['#sv-root'].onclick({target: {closest: () => ({dataset, disabled: false, ...extra})}});
    await tick();
  };
  const ok = async () => {
    elements['#sv-confirm-go'].onclick();
    await tick();
  };
  return {click, ok};
}

function unwire() {
  for (const id of ['#sv-root', '#sv-confirm-go', '#sv-confirm-cancel', '#sv-also-versions', '#sv-ack', '#sv-target']) delete elements[id];
}

const ps4 = {pane: 'ps4', uid: UID};
const ha = {pane: 'ha', uid: UID};

async function main() {
  {
    const {p, calls} = await open(settingsFor('off'));
    const html = p.shadowRoot.innerHTML;
    assert.match(html, /data-tab="saves">Saves</);
    assert.match(html, /<button data-value="off" class="active">Off<\/button>/);
    assert.match(html, /Off: HA doesn't look at this PS4's saves\./);
    assert.doesNotMatch(html, /<h3>On PS4<\/h3>|<h3>In HA<\/h3>|data-act="ask-backup"/);
    assert.equal(calls.filter((c) => /saves\/(list|console)/.test(c.type)).length, 0);
    console.log('PASS: Off shows the switch only, reads nothing');
  }

  {
    const settings = settingsFor('manual');
    const {p, calls} = await open(settings);
    assert.deepEqual(calls.map((c) => c.type).filter((t) => t.startsWith('ps4_goldhen/saves')),
      ['ps4_goldhen/saves/get', 'ps4_goldhen/saves/list', 'ps4_goldhen/saves/console', 'ps4_goldhen/saves/list']);
    assert.equal(calls.find((c) => c.type === 'ps4_goldhen/saves/console').refresh, undefined, 'opening: HA may answer from its read');
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /<button data-value="manual" class="active">On<\/button>/);
    assert.match(html, /<h3>On PS4<\/h3>/);
    assert.match(html, /<h3>In HA<\/h3>/);
    assert.match(html, /class="sv-panes" data-show="ps4"/);
    assert.match(html, /data-act="pane" data-pane="ps4" class="active">On PS4<\/button><button data-act="pane" data-pane="ha" class="">In HA</);
    assert.match(html, /4 saves · 1 in HA · 1 changed since the backup · 2 not in HA · read just now/);
    assert.match(html, /data-act="ask-backup" >Back up 2 new\.\.\.</, 'changed + not in HA, the open game left out');
    assert.match(html, /&lt;b&gt;Game C&lt;\/b&gt;/);
    assert.doesNotMatch(html, /<b>Game C<\/b>/);
    assert.match(html, /1 of 3 in HA/);
    assert.match(html, /Open on the PS4: close it to back up/);
    assert.match(html, /Unknown game<div class="muted game-tid">CUSA90006<\/div>/);
    assert.match(html, /Not on the PS4 now<br>1 save · 0\.0 MB/);
    assert.match(html, /2\.6 KB|0\.0 MB of 5\.0 GB · 3 newest copies · 1 older version/);
    assert.match(html, /<b>In Apollo's Fake USB folder<\/b>/);
    assert.match(html, /<li>&lt;b&gt;Game C&lt;\/b&gt; · Prologue<\/li>/);
    assert.match(html, /Not sent from this HA/);
    assert.match(html, /data-act="clear-sent" >Remove from PS4</);
    assert.doesNotMatch(html, /Chapter 3/, 'games closed');
    assert.match(html, /How to restore/);
    assert.match(html, /Under <b>In HA<\/b>, press <b>Select<\/b>/);
    assert.match(html, /USB Saves Source<\/b> → <b>Fake USB/);
    assert.ok(calls.every((c) => !/screenshots|ftp/.test(c.type)), 'only the saves commands');

    const {click} = wire(p);
    await click({act: 'toggle', ...ps4, tid: 'CUSA90003'});
    await click({act: 'toggle', ...ps4, tid: 'CUSA90009'});
    html = p.shadowRoot.innerHTML;
    assert.match(html, /Chapter 3<div class="muted sv-sdetail">[^<]+ · 0\.0 MB · <span class="sv-tid">SAVEDATAPlayer<\/span>/);
    assert.match(html, /SAVEDATANew<div class="muted sv-sdetail">[^<]+ · 0\.0 MB<\/div>/, 'no name: the slot ID once');
    assert.match(html, /✓ In HA/);
    assert.match(html, /● Changed/);
    assert.match(html, /○ Not in HA/);
    assert.match(html, /<span class="pill">Open<\/span>/);
    assert.match(html, /aria-expanded="true"/);
    await click({act: 'pane', pane: 'ha'});
    assert.match(p.shadowRoot.innerHTML, /class="sv-panes" data-show="ha"/);
    console.log('PASS: two panes, badges, names, Fake USB as read, phone toggle');

    // On PS4 → Select → a game box ticks its saves (the open game can't be ticked) → Back up N to HA
    await click({act: 'select', pane: 'ps4'});
    html = p.shadowRoot.innerHTML;
    assert.match(html, /data-act="ask-backup-sel" disabled>Back up 0 to HA\.\.\./);
    assert.match(html, /class="sv-grow " data-act="toggle" data-pane="ps4" data-uid="10000001" data-tid="CUSA90009"/,
      'the open game: its row only opens');
    assert.doesNotMatch(html, /data-act="pick" data-pane="ps4" data-uid="10000001" data-tid="CUSA90009"/);
    await click({act: 'pick-game', ...ps4, tid: 'CUSA90003'});
    html = p.shadowRoot.innerHTML;
    assert.match(html, /data-act="ask-backup-sel" >Back up 3 to HA\.\.\./);
    assert.match(html, /player1: 3 ticked/);
    assert.match(html, /<span class="sv-box on"/);
    await click({act: 'pick', ...ps4, tid: 'CUSA90003', slot: 'SAVEDATANew'});
    html = p.shadowRoot.innerHTML;
    assert.match(html, /Back up 2 to HA\.\.\./);
    assert.match(html, /<span class="sv-box part"/, 'the game box is half ticked');
    await click({act: 'ask-backup-sel'});
    let ask = p._renderSvConfirm();
    assert.match(ask, /<h3 style="margin-top:0">Back up to HA<\/h3>/);
    assert.match(ask, /<b>2 saves<\/b>/);
    assert.match(ask, /<li>&lt;b&gt;Game C&lt;\/b&gt; · Chapter 3<\/li>/);
    assert.match(ask, /1 new or changed \(at the last read\) will be copied\. 1 is already in HA: copied again only if changed since\./);
    assert.match(p._renderSvConfirm.call(Object.assign(Object.create(p), {_svConfirm: {kind: 'backup', pane: 'ps4',
      params: {uid: UID, items: [{title_id: 'CUSA90003', slot: 'sce_sdmemory'}]}}})), /It(&#39;|')s already in HA \(at the last read\)\. HA checks again/);
    assert.match(p._renderSvConfirm.call(Object.assign(Object.create(p), {_svConfirm: {kind: 'delete', pane: 'ha',
      params: {uid: UID, items: [{title_id: 'CUSA90003', slot: 'SAVEDATAPlayer', stamp: OLDER}]}}})), /HA deletes 1 older version \(0\.0 MB\)\./);
    assert.match(ask, /A game that(&#39;|')s open on the PS4 \(on screen or suspended\) is skipped/);
    assert.equal(calls.filter((c) => c.type === 'ps4_goldhen/saves/backup').length, 0, 'nothing runs before the OK');
    intervals.length = 0;
    const {ok} = wire(p);
    await ok();
    const backup = calls.find((c) => c.type === 'ps4_goldhen/saves/backup');
    assert.equal(JSON.stringify(backup), JSON.stringify({type: 'ps4_goldhen/saves/backup', entry_id: 'console_a', uid: UID,
      items: [{title_id: 'CUSA90003', slot: 'SAVEDATAPlayer'}, {title_id: 'CUSA90003', slot: 'sce_sdmemory'}]}));
    assert.equal(p._svSelecting, null, 'done: Select ends');
    // a progress bar above both panes; before the PS4 has been read there's no count yet
    assert.match(p.shadowRoot.innerHTML, /class="sv-progress" role="progressbar"[^>]*aria-valuemax="0"/);
    assert.match(p.shadowRoot.innerHTML, /Reading the PS4(&#39;|')s saves\.\.\./);
    // in the sticky toolbar, so it stays in view while the list scrolls
    assert.match(p.shadowRoot.innerHTML, /<div class="sticky-bar">\s*<div class="sv-progress"/);
    assert.doesNotMatch(p.shadowRoot.innerHTML, /Backing up 0/);
    // copies land: the bar moves and both panes are read again (not "0 of 5 GB" until the end)
    settings.progress = {done: 12, total: 53};
    const lists = calls.filter((c) => c.type === 'ps4_goldhen/saves/list').length;
    await intervals[0]();
    html = p.shadowRoot.innerHTML;
    assert.match(html, /Backing up 12 of 53 saves/);
    assert.match(html, /aria-valuenow="12"/);
    assert.match(html, /width:23%/);
    assert.equal(calls.filter((c) => c.type === 'ps4_goldhen/saves/list').length, lists + 1, 'the HA pane follows the copies');
    await intervals[0]();  // same count a second later: no new reads
    assert.equal(calls.filter((c) => c.type === 'ps4_goldhen/saves/list').length, lists + 1);
    assert.ok(calls.filter((c) => c.type === 'ps4_goldhen/saves/console').every((c) => !c.refresh), 'no FTP read mid-pass');
    settings.progress = {done: 0, total: 0};
    settings.running = false;
    settings.last = {state: 'up_to_date', checked: 2, copied: 1, skipped: 0, retried: 0, already: 1};
    const reads = calls.filter((c) => c.type === 'ps4_goldhen/saves/console').length;
    await intervals[0]();
    html = p.shadowRoot.innerHTML;
    assert.match(html, /Done: 1 of 2 saves were new or changed\./);
    assert.equal(calls.filter((c) => c.type === 'ps4_goldhen/saves/console').length, reads + 1, 'the PS4 pane follows');
    assert.ok(calls.filter((c) => c.type === 'ps4_goldhen/saves/console').every((c) => !c.refresh));
    console.log('PASS: On PS4 → Select → Back up 2, status poll, both panes follow');

    // Back up everything + Refresh asks HA to read the console again
    await click({act: 'ask-backup'});
    ask = p._renderSvConfirm();
    assert.match(ask, /Back up PS4 PRO A now/);
    assert.match(ask, /HA checks every save on the PS4/);
    await ok();
    assert.equal(JSON.stringify(calls.filter((c) => c.type === 'ps4_goldhen/saves/backup')[1]),
      JSON.stringify({type: 'ps4_goldhen/saves/backup', entry_id: 'console_a'}));
    p._svStopPoll();
    settings.running = false;
    p._svSettings.running = false;
    await click({act: 'read-ps4'});
    assert.equal(calls.filter((c) => c.type === 'ps4_goldhen/saves/console').pop().refresh, true);
    console.log('PASS: Back up everything; Refresh reads the PS4 again');
    assert.equal(p._svResultText({state: 'up_to_date', checked: 1, copied: 0}), 'Checked 1 save: nothing new to copy.');
    unwire();
  }

  // Select all: every save of one user; not the open game on the PS4, HA's newest copies only
  {
    const settings = settingsFor('manual');
    const {p} = await open(settings);
    const {click} = wire(p);
    await click({act: 'select', pane: 'ps4'});
    assert.match(p.shadowRoot.innerHTML, /data-act="pick-all" data-pane="ps4" >Select all</);
    await click({act: 'pick-all', pane: 'ps4'});
    assert.deepEqual([...p._svSel.keys].sort(),
      ['CUSA90003|SAVEDATANew', 'CUSA90003|SAVEDATAPlayer', 'CUSA90003|sce_sdmemory'], 'Game F is open: skipped');
    assert.match(p.shadowRoot.innerHTML, />Clear all</);
    assert.match(p.shadowRoot.innerHTML, /Back up 3 to HA\.\.\./);
    await click({act: 'pick-all', pane: 'ps4'});
    assert.equal(p._svSel.keys.size, 0, 'Clear all');
    await click({act: 'select', pane: 'ps4'});
    await click({act: 'select', pane: 'ha'});
    await click({act: 'pick-all', pane: 'ha'});
    assert.deepEqual([...p._svSel.keys].sort(),
      ['CUSA90003|SAVEDATAPlayer', 'CUSA90003|sce_sdmemory', 'CUSA90006|SAVEDATA01'], 'newest copies, no older version');
    assert.match(p.shadowRoot.innerHTML, /Send 3 to PS4\.\.\./);
    console.log('PASS: Select all / Clear all in both panes');
    unwire();
  }

  // In HA → Select → send single saves, one version per save
  {
    const settings = settingsFor('manual');
    const {p, calls} = await open(settings);
    const {click, ok} = wire(p);
    await click({act: 'toggle', ...ha, tid: 'CUSA90003'});
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /Player<div class="muted sv-sdetail">newest [^<]+ · 0\.0 MB · <span class="sv-tid">SAVEDATAPlayer/);
    assert.match(html, /Prologue<div class="muted sv-sdetail">[^<]+ · 0\.0 MB<\/div>/, 'the older copy keeps its own name');
    assert.match(html, new RegExp(`data-act="keep" data-uid="${UID}" data-tid="CUSA90003" data-slot="SAVEDATAPlayer" data-stamp="${OLDER}"`));
    assert.match(html, /Older versions: every one from the last few saves/);
    await click({act: 'keep', uid: UID, tid: 'CUSA90003', slot: 'SAVEDATAPlayer', stamp: OLDER}, {checked: true});
    assert.equal(JSON.stringify(calls.find((c) => c.type === 'ps4_goldhen/saves/keep')), JSON.stringify({
      type: 'ps4_goldhen/saves/keep', entry_id: 'console_a', uid: UID, title_id: 'CUSA90003', slot: 'SAVEDATAPlayer',
      stamp: OLDER, kept: true}));

    await click({act: 'select', pane: 'ha'});
    html = p.shadowRoot.innerHTML;
    assert.match(html, /data-act="ask-send-sel" disabled>Send 0 to PS4\.\.\./);
    assert.match(html, /data-act="ask-delete-sel" disabled>Delete 0 from HA\.\.\./);
    assert.doesNotMatch(html, /data-act="keep"/, 'Keep boxes hide while ticking');
    await click({act: 'pick-game', ...ha, tid: 'CUSA90003'});
    assert.match(p.shadowRoot.innerHTML, /Send 2 to PS4\.\.\./);
    await click({act: 'pick', ...ha, tid: 'CUSA90003', slot: 'SAVEDATAPlayer', stamp: OLDER});
    html = p.shadowRoot.innerHTML;
    assert.match(html, /Send 2 to PS4\.\.\./, 'the version took the newest copy\'s place');
    assert.match(html, /One version per save: Apollo(&#39;|')s USB folder holds one copy of each save, so the tick moved\./);
    assert.match(html, /<span class="sv-box part"/);
    await click({act: 'ask-send-sel'});
    const ask = p._renderSvConfirm();
    assert.match(ask, /<li>&lt;b&gt;Game C&lt;\/b&gt; · Prologue \(copy from [^)]+\)<\/li>/);
    assert.match(ask, /To <b>PS4 PRO A<\/b>/);
    assert.match(ask, /It replaces what HA sent there before\./);
    await ok();
    const send = calls.find((c) => c.type === 'ps4_goldhen/saves/send');
    assert.equal(JSON.stringify(send), JSON.stringify({type: 'ps4_goldhen/saves/send', entry_id: 'console_a', uid: UID,
      items: [{title_id: 'CUSA90003', slot: 'sce_sdmemory'}, {title_id: 'CUSA90003', slot: 'SAVEDATAPlayer', stamp: OLDER}],
      target_entry_id: 'console_a'}));
    html = p.shadowRoot.innerHTML;
    assert.match(html, /Sent &lt;b&gt;Game C&lt;\/b&gt; to PS4 PRO A \(4 files/);
    assert.match(html, /Sent by HA [^<]+ from PS4 PRO A · player1\./, 'the Fake USB section says who sent it');
    console.log('PASS: In HA → Keep, Select, one version per save, Send N');

    // Download + Delete (with their older versions)
    await click({act: 'select', pane: 'ha'});
    await click({act: 'pick', ...ha, tid: 'CUSA90003', slot: 'SAVEDATAPlayer', stamp: OLDER});
    await click({act: 'ask-zip-sel'});
    assert.match(p._renderSvConfirm(), /HA makes a 0\.0 MB zip/);
    await ok();
    assert.equal(JSON.stringify(calls.find((c) => c.type === 'ps4_goldhen/saves/zip').items),
      JSON.stringify([{title_id: 'CUSA90003', slot: 'SAVEDATAPlayer', stamp: OLDER}]));
    assert.equal(clicked.length, 1);
    assert.match(clicked[0].href, /download\?file=PS4%20PRO%20A%20player1%20saves\.zip&authSig=x/);

    await click({act: 'select', pane: 'ha'});
    await click({act: 'pick', ...ha, tid: 'CUSA90003', slot: 'SAVEDATAPlayer'});
    await click({act: 'pick-game', ...ha, tid: 'CUSA90006'});
    await click({act: 'ask-delete-sel'});
    let del = p._renderSvConfirm();
    assert.match(del, /<h3 style="margin-top:0">Delete from HA<\/h3>/);
    assert.match(del, /HA deletes 2 newest copies \(0\.0 MB\)\. The saves on the PS4 aren(&#39;|')t touched\./);
    // CUSA90006's only copy, and it isn't on the PS4: gone for good, so Delete waits for a tick
    assert.match(del, /<b>After this, HA has no copy of 1 save\.<\/b> It isn(&#39;|')t on the PS4 either: gone for good\./);
    assert.match(del, /Their older versions stay unless you tick the box below\./);
    assert.match(del, /id="sv-also-versions" > Also delete their 1 older version</);
    assert.match(del, /id="sv-ack" > I understand: HA keeps no copy of these 1 save, and 1 is gone for good/);
    assert.match(del, /class="btn-danger" id="sv-confirm-go" disabled>Delete</);
    assert.match(del, /This can(&#39;|')t be undone\./);
    wire(p);
    await ok();
    assert.equal(calls.filter((c) => c.type === 'ps4_goldhen/saves/delete').length, 0, 'not ticked: nothing deleted');
    assert.ok(p._svConfirm, 'the dialog stays');
    elements['#sv-also-versions'].checked = true;
    elements['#sv-also-versions'].onchange();
    del = p._renderSvConfirm();
    assert.match(del, /id="sv-also-versions" checked>/);
    assert.match(del, /HA deletes 2 newest copies and 1 older version/);
    assert.match(del, /no copy of 2 saves\.<\/b> 1 of them isn(&#39;|')t on the PS4 either: gone for good\. The others are still on the PS4/);
    elements['#sv-ack'].checked = true;
    elements['#sv-ack'].onchange();
    del = p._renderSvConfirm();
    assert.match(del, /class="btn-danger" id="sv-confirm-go" >Delete</, 'ticked: Delete is on');
    await ok();
    const gone = calls.find((c) => c.type === 'ps4_goldhen/saves/delete');
    assert.equal(JSON.stringify(gone), JSON.stringify({type: 'ps4_goldhen/saves/delete', entry_id: 'console_a', uid: UID,
      items: [{title_id: 'CUSA90003', slot: 'SAVEDATAPlayer'}, {title_id: 'CUSA90006', slot: 'SAVEDATA01'},
              {title_id: 'CUSA90003', slot: 'SAVEDATAPlayer', stamp: OLDER}]}));
    assert.match(p.shadowRoot.innerHTML, /Deleted 3 copies from HA\./);
    console.log('PASS: Download N, Delete N (+ older versions) from HA');

    await click({act: 'clear-sent'});
    assert.ok(calls.some((c) => c.type === 'ps4_goldhen/saves/clear_sent'));
    assert.match(p.shadowRoot.innerHTML, /Removed 2 files from the PS4(&#39;|')s Fake USB folder\./);
    unwire();
  }

  // Asleep: the last read with its time, no Back up; never read: a hint
  {
    const settings = {...settingsFor('manual'), power_state: 'rest', status: 'up_to_date', last_success: '2026-10-16T03:00:00Z'};
    let {p} = await open(settings);
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /The PS4 is in rest mode: this is what HA read /);
    assert.match(html, /<button class="btn btn-save" data-act="ask-backup" disabled>/);
    assert.match(html, /data-act="read-ps4" disabled>Refresh/);
    assert.match(html, /data-act="clear-sent" disabled>Remove from PS4/);
    assert.match(html, /Up to date · last complete backup /);
    ({p} = await open({...settings, power_state: 'off'}, ENTRIES, {...consoleRead(), read_at: null, users: [], fake_usb: []}));
    html = p.shadowRoot.innerHTML;
    assert.match(html, /The PS4 is off: turn it on to see its saves\./);
    assert.match(html, /data-act="select" data-pane="ps4" disabled>Select/);
    console.log('PASS: asleep: the last read or a hint, Back up / Refresh / Remove disabled');
  }

  {
    const settings = settingsFor('off');
    const {p, calls} = await open(settings);
    await p._svSave({mode: 'manual'});
    assert.equal(calls.filter((c) => c.type === 'ps4_goldhen/saves/set')[0].mode, 'manual');
    assert.ok(calls.some((c) => c.type === 'ps4_goldhen/saves/console'), 'turning it on reads the PS4');
    assert.match(p.shadowRoot.innerHTML, /<h3>On PS4<\/h3>/);
    assert.equal(p._svStamp('2025-10-16 014221').includes('2025'), true, 'another year: shown');
    const year = new Date().getFullYear();
    assert.equal(p._svStamp(`${year}-10-16 014221`).includes(String(year)), false, 'this year: left out');
    assert.equal(p._svStamp('junk'), 'junk');
    console.log('PASS: turning it on reads both sides');

    // errors land in the tab (the sandbox's alert throws)
    const realCall = p._hass.callWS;
    p._hass.callWS = async () => { throw new Error('boom'); };
    await p._svReadPs4(true);
    assert.match(p.shadowRoot.innerHTML, /Couldn(&#39;|')t read the PS4: boom/);
    await p._svKeep(UID, 'CUSA90003', 'SAVEDATAPlayer', OLDER, true);
    assert.equal(p._svStatus, "Couldn't change Keep: boom");
    assert.match(p.shadowRoot.innerHTML, /class="muted bar-status">Couldn&#39;t change Keep: boom/);
    p._hass.callWS = realCall;
    console.log('PASS: read / Keep errors inline, no alert');
  }

  {
    const two = [...ENTRIES, {entry_id: 'console_b', title: 'PS4 GoldHEN (192.0.2.189)', name: 'PS4 PRO B', ps4_host: '192.0.2.189'}];
    const {p, calls} = await open(settingsFor('manual'), two);
    const {click, ok} = wire(p);
    const select = {value: 'console_b'};
    elements['#sv-target'] = select;
    await click({act: 'select', pane: 'ha'});
    await click({act: 'pick', ...ha, tid: 'CUSA90003', slot: 'SAVEDATAPlayer', stamp: OLDER});
    await click({act: 'ask-send-sel'});
    const html = p.shadowRoot.innerHTML;
    assert.match(html, /<select id="sv-target"><option value="console_a" selected>PS4 PRO A<\/option><option value="console_b" >PS4 PRO B<\/option>/);
    select.onchange();
    await ok();
    const send = calls.find((c) => c.type === 'ps4_goldhen/saves/send');
    assert.equal(send.target_entry_id, 'console_b');
    assert.match(p.shadowRoot.innerHTML, /Sent &lt;b&gt;Game C&lt;\/b&gt; to PS4 PRO B/);
    assert.equal(calls.filter((c) => c.type === 'ps4_goldhen/saves/get').length, 1, 'no reload for another console');
    assert.equal(p._svTarget, 'console_b', 'the next Send starts from the same console');
    unwire();
    console.log('PASS: two consoles: the Send dialog picks the other one');
  }
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
