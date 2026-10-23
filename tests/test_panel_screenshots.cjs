// the panel's Screenshots tab — console names in the picker, settings, grid, selection, Sync dialog.
// "Sync" everywhere, only an In HA pill, Unknown game, the Delete dialog with nothing in HA, the viewer
// opening on the thumbnail, Escape / backdrop, no browser alerts, the phone menu button.
// a tap only views (HA reads a PS4-only shot from the console, nothing kept; the viewer's Sync keeps
// it), no Never / On request setting, Sync all... + Select → Sync N.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let Panel;
const sandbox = {
  HTMLElement: class { attachShadow() { this.shadowRoot = {querySelector: () => null, querySelectorAll: () => []}; } },
  customElements: {define: (_name, cls) => { Panel = cls; }},
  alert: (msg) => { throw new Error(`alert: ${msg}`); }, confirm: () => true, requestAnimationFrame: () => {}, setInterval, clearInterval, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../custom_components/ps4_goldhen/frontend/ps4-goldhen-panel.js'), 'utf8'), sandbox);

const HOME = 'NPXS20001/NPXS20001/147/20261015_214637_00434169.jpg';
const GAME_D = 'NPXS20001/CUSA90005/3e9/20261015_111203_00205776.jpg';
const shots = [
  {rel: HOME, name: '20261015_214637_00434169.jpg', game: 'Home screen', title_id: 'NPXS20001', size: 383363,
   taken: '2026-10-15T21:46:37', trophy: false, in_ha: true, thumb: 'data:image/jpeg;base64,AAAA'},
  {rel: GAME_D, name: '20261015_111203_00205776.jpg', game: '<b>Game D</b>', title_id: 'CUSA90005', size: 885853,
   taken: '2026-10-15T11:12:03', trophy: true, in_ha: false, thumb: null},
];

let importResult = () => ({in_ha: true});
let listResult = (msg) => ({shots: shots.slice(msg.offset, msg.offset + msg.count), more: false});

function panel(settings) {
  const calls = [];
  const p = new Panel();
  p._hass = {
    callWS: async (msg) => {
      calls.push(msg);
      if (msg.type === 'ps4_goldhen/list_entries') {
        return {entries: [{entry_id: 'console_a', title: 'PS4 GoldHEN (192.0.2.121)', name: 'PS4 PRO A', ps4_host: '192.0.2.121'},
                          {entry_id: 'old', title: 'PS4 GoldHEN (192.0.2.9)', ps4_host: '192.0.2.9'}]};
      }
      if (msg.type === 'ps4_goldhen/screenshots/get') return settings;
      if (msg.type === 'ps4_goldhen/screenshots/set') return Object.assign(settings, msg);
      if (msg.type === 'ps4_goldhen/screenshots/list') return listResult(msg);
      if (msg.type === 'ps4_goldhen/screenshots/sync') return {started: true, total: (msg.rels || []).length};
      if (msg.type === 'ps4_goldhen/screenshots/delete') return {ha_deleted: msg.ha ? 1 : 0, ps4_deleted: msg.ps4 ? msg.rels.length : 0};
      if (msg.type === 'ps4_goldhen/screenshots/import') return importResult(msg);
      if (msg.type === 'auth/sign_path') return {path: `${msg.path}&authSig=x`};
      throw new Error(`unexpected ${msg.type}`);
    },
  };
  return {p, calls};
}

const SETTINGS_ON = {enabled: true, mode: 'on_request', limit_gb: 2, limit_min_gb: 0.5, limit_max_gb: 50,
  folder: '/media/PS4 Screenshots/PS4 PRO A', usage: {count: 1, bytes: 383363}, syncing: false,
  progress: {done: 0, total: 0}, last_sync: null};

async function main() {
  // Picker: the console's HA name, else the entry title (no more "IP — IP")
  {
    const {p} = panel({});
    await p._loadEntries();
    p._render();
    const html = p.shadowRoot.innerHTML;
    assert.match(html, /<option value="console_a" selected>PS4 PRO A — 192\.0\.2\.121<\/option>/);
    assert.match(html, /<option value="old" >PS4 GoldHEN \(192\.0\.2\.9\) — 192\.0\.2\.9<\/option>/);
    assert.match(html, /data-tab="screenshots">Screenshots</);
    console.log('PASS: picker shows console names; Screenshots tab exists');
  }

  // Off: settings only, the PS4 is never listed
  {
    const {p, calls} = panel({...SETTINGS_ON, enabled: false});
    await p._loadEntries();
    p._tab = 'screenshots';
    await p._ssLoad();
    assert.ok(!calls.some((c) => c.type === 'ps4_goldhen/screenshots/list'));
    const html = p.shadowRoot.innerHTML;
    assert.match(html, /Off: HA doesn't look at this PS4's screenshots/);
    assert.doesNotMatch(html, /Sync to HA/);
    console.log('PASS: off = settings only, no listing');
  }

  // On: newest 24, thumbnails, In HA / PS4 only, trophy badge, escaped game names
  {
    const {p, calls} = panel({...SETTINGS_ON});
    await p._loadEntries();
    p._tab = 'screenshots';
    await p._ssLoad();
    const list = calls.find((c) => c.type === 'ps4_goldhen/screenshots/list');
    assert.deepEqual([list.entry_id, list.offset, list.count], ['console_a', 0, 24]);
    const html = p.shadowRoot.innerHTML;
    assert.match(html, /<img src="data:image\/jpeg;base64,AAAA"/);
    assert.match(html, /<span class="pill in-ha">In HA<\/span>/);
    assert.doesNotMatch(html, /PS4 only/, 'only In HA gets a pill');
    assert.match(html, /<span class="ss-game">Home screen<\/span>/);
    assert.doesNotMatch(html, /id="ss-mode"/, 'no Never / On request any more');
    assert.match(html, /Tap a shot to see it: HA reads it from the PS4 and keeps nothing\./);
    assert.match(html, /id="ss-sync" >Sync all\.\.\.</);
    assert.match(html, /Trophy/);
    assert.match(html, /&lt;b&gt;Game D&lt;\/b&gt;/);
    assert.doesNotMatch(html, /<b>Game D<\/b>/);
    assert.match(html, /id="ss-limit" type="range" min="0.5" max="50" step="0.5" value="2"/);
    assert.doesNotMatch(html, /automatic|Automatic/, 'there is no Automatic mode');
    assert.doesNotMatch(html, /data-value="never"/);
    console.log('PASS: grid renders shots, badges, slider; names escaped');

    // Select mode — any shot can be ticked; the toolbar acts on the ticks only (no Sync button)
    p._ssSelecting = true;
    await p._ssCardClick(HOME);
    await p._ssCardClick(GAME_D);
    assert.deepEqual([...p._ssSelected].sort(), [HOME, GAME_D].sort());
    p._render();
    let sel = p.shadowRoot.innerHTML;
    assert.match(sel, /id="ss-copy-selected" >Sync 1 to HA</);
    assert.match(sel, /id="ss-delete" >Delete 2\.\.\.</);
    assert.match(sel, /id="ss-select">Cancel</);
    assert.doesNotMatch(sel, /id="ss-sync"/, 'Sync to HA... is hidden while selecting (easy to hit with ticks set)');
    assert.equal((sel.match(/class="ss-check"/g) || []).length, 2, 'every card gets a tick box');
    console.log('PASS: Select mode ticks any shot; toolbar = Sync N / Delete N / Cancel');

    // Delete: asks every time, HA copy ticked, PS4 original not
    p._ssOpenDelete();
    assert.deepEqual({...p._ssDelete}, {ha: true, ps4: false});
    let dlg = p._renderSsDelete();
    assert.match(dlg, /Delete 2 screenshots/);
    assert.match(dlg, /id="ss-del-ha" checked/);
    assert.match(dlg, /HA copy \(1 of 2 is in HA\)/);
    assert.match(dlg, /id="ss-del-ps4" >/);
    assert.match(dlg, />Delete 1 HA copy</);
    assert.doesNotMatch(dlg, /permanent/);
    p._ssDelete.ps4 = true;
    dlg = p._renderSsDelete();
    assert.match(dlg, />Delete 1 HA copy and 2 PS4 originals</);
    assert.match(dlg, /permanent/);
    p._ssDelete.ha = false;
    p._ssDelete.ps4 = false;
    assert.match(p._renderSsDelete(), /id="ss-del-go" disabled>Delete</, 'nothing ticked: Delete is there, disabled');
    assert.match(p._renderSsDelete(), /Tick what to delete\./);
    p._ssDelete = {ha: true, ps4: true};
    await p._ssRunDelete();
    const del = calls.find((c) => c.type === 'ps4_goldhen/screenshots/delete');
    assert.equal(JSON.stringify([[...del.rels].sort(), del.ha, del.ps4]), JSON.stringify([[HOME, GAME_D].sort(), true, true]));  // arrays from the panel's vm realm
    assert.equal(p._ssStatus, 'Deleted 1 HA copy and 2 from the PS4.');
    assert.equal(p._ssSelecting, false);
    console.log('PASS: Delete dialog defaults (HA copy on, PS4 off), warns, sends the ticks');

    // Sync N to HA: only the ticked PS4-only shots, no second prompt
    p._ssSelecting = true;
    p._ssSelected = new Set([HOME, GAME_D]);
    await p._ssCopySelected();
    const sync = calls.find((c) => c.type === 'ps4_goldhen/screenshots/sync');
    assert.deepEqual([sync.kind, sync.rels], ['selected', [GAME_D]]);
    assert.equal(p._ssSelecting, false);
    console.log('PASS: Sync N to HA sends only the PS4-only ticks');
  }

  // Sync dialog: count-and-ask, newest that fit under the room, per-game choice
  {
    const {p} = panel({...SETTINGS_ON});
    await p._loadEntries();
    p._tab = 'screenshots';
    p._ssSettings = {...SETTINGS_ON};
    const plan = {total: 3, in_ha: 1, used_bytes: 383363, limit_bytes: 2 * 1073741824, room_bytes: 1000000, new: [
      {rel: GAME_D, title_id: 'CUSA90005', game: 'Game D', size: 885853, modified_utc: '2026-10-15T15:12:03Z'},
      {rel: 'NPXS20001/CUSA90005/375/20261015_110310_00680394.jpg', title_id: 'CUSA90005', game: 'Game D', size: 1028262,
       modified_utc: '2026-10-15T15:03:10Z'},
    ]};
    p._ssDialog = {plan, kind: 'all', titleId: 'CUSA90005', count: 2};
    let html = p._renderSsDialog();
    assert.match(html, /2 new on the PS4 \(1\.8 MB\) · 1 already in HA/);
    assert.match(html, /Only 1 of these 2 fit under the 2 GB limit/);
    assert.match(html, /Sync 1 \(0\.8 MB\)/);
    assert.match(html, /id="ss-dialog" data-dismiss/);
    p._ssDialog.kind = 'newest';
    p._ssDialog.count = 1;
    assert.deepEqual(p._ssChosen().map((s) => s.rel), [GAME_D]);
    plan.room_bytes = 0;
    html = p._renderSsDialog();
    assert.match(html, /None of these fit/);
    assert.doesNotMatch(html, /id="ss-go"/);
    console.log('PASS: Sync dialog counts, asks, and shows what fits');
  }

  // nothing ticked is in HA → the HA box is unticked and says so
  {
    const {p} = panel({...SETTINGS_ON});
    p._ssShots = shots.map((x) => ({...x}));
    p._ssSelecting = true;
    p._ssSelected = new Set([GAME_D]);
    p._ssOpenDelete();
    assert.deepEqual({...p._ssDelete}, {ha: false, ps4: false});
    const dlg = p._renderSsDelete();
    assert.match(dlg, /<input type="checkbox" disabled> HA copy \(none of these are in HA\)/);
    assert.doesNotMatch(dlg, /id="ss-del-ha"/);
    assert.match(dlg, /id="ss-del-go" disabled>Delete</);
    console.log('PASS: Delete dialog with nothing in HA: HA box off + explained');
  }

  // a tap views: HA's copy, or the shot read from the PS4 (no import call); Sync to HA in the viewer
  {
    const {p, calls} = panel({...SETTINGS_ON});
    await p._loadEntries();
    p._tab = 'screenshots';
    p._ssSettings = {...SETTINGS_ON, power_state: 'on'};
    const shot = {...shots[0], rel: 'NPXS20001/CUSA90008/1/x.jpg', name: 'x.jpg', game: 'CUSA90008', title_id: 'CUSA90008', in_ha: false};
    p._ssShots = [shot];
    await p._ssCardClick(shot.rel);
    assert.ok(!calls.some((c) => c.type === 'ps4_goldhen/screenshots/import'), 'viewing never syncs');
    let view = p._renderSsView();
    assert.match(view, /id="ss-view-img" src="\/api\/ps4_goldhen\/screenshots\/console_a\/full\?rel=NPXS20001%2FCUSA90008%2F1%2Fx\.jpg&amp;authSig=x"/);
    assert.match(view, /style="background-image:url\('data:image\/jpeg;base64,AAAA'\)"/, 'the thumbnail shows while it loads');
    assert.match(view, /On the PS4 · not in HA/);
    assert.match(view, /id="ss-view-sync" >Sync to HA</);
    assert.match(view, /Unknown game · /);
    assert.match(p.shadowRoot.innerHTML, /<span class="ss-game">Unknown game<\/span>/);

    // The viewer's Sync to HA keeps it
    await p._ssViewSync();
    assert.ok(calls.some((c) => c.type === 'ps4_goldhen/screenshots/import' && c.rel === shot.rel));
    view = p._renderSsView();
    assert.equal(shot.in_ha, true);
    assert.match(view, /Synced to HA\./);
    assert.match(view, /<span class="pill in-ha">In HA<\/span>/);
    assert.doesNotMatch(view, /id="ss-view-sync"/);

    // Doesn't fit: says so
    shot.in_ha = false;
    importResult = () => ({in_ha: false});
    await p._ssViewSync();
    assert.match(p._renderSsView(), /Not synced: it doesn&#39;t fit under the storage limit/);
    importResult = () => ({in_ha: true});
    assert.equal(p._closeOverlay(), true, 'Escape closes the viewer');
    assert.equal(p._closeOverlay(), false, 'nothing left to close');

    // Asleep: the thumbnail + a note, no signed path, no FTP
    p._ssSettings = {...SETTINGS_ON, power_state: 'rest'};
    const signs = calls.filter((c) => c.type === 'auth/sign_path').length;
    await p._ssCardClick(shot.rel);
    view = p._renderSsView();
    assert.match(view, /The PS4 is in rest mode or off: turn it on to see this one full size\./);
    assert.match(view, /class="ss-view-img ss-view-wait" src="data:image\/jpeg;base64,AAAA"/);
    assert.equal(calls.filter((c) => c.type === 'auth/sign_path').length, signs);
    console.log('PASS: a tap views (from the PS4 if needed, nothing kept); Sync to HA in the viewer; asleep = thumbnail');
  }

  // Select → the ticked ones only; a hint when they're all in HA already
  {
    const {p} = panel({...SETTINGS_ON});
    await p._loadEntries();
    p._tab = 'screenshots';
    await p._ssLoad();
    p._ssSelecting = true;
    p._ssSelected = new Set([HOME]);
    p._render();
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /id="ss-copy-selected" disabled>Sync 0 to HA</);
    assert.match(html, /It&#39;s already in HA|It's already in HA/);
    p._ssSelected = new Set([HOME, GAME_D]);
    p._render();
    html = p.shadowRoot.innerHTML;
    assert.match(html, /id="ss-copy-selected" >Sync 1 to HA</);
    assert.match(html, /1 already in HA/);
    console.log('PASS: Select hints which ticks are already in HA');
  }

  // errors land in the tab, never a browser alert; narrow → a menu button that opens HA's sidebar
  {
    const {p} = panel({...SETTINGS_ON});
    await p._loadEntries();
    p._tab = 'screenshots';
    p._ssSettings = {...SETTINGS_ON};
    p._hass.callWS = async () => { throw new Error('boom'); };
    await p._ssSave({mode: 'never'});
    assert.equal(p._ssStatus, "Couldn't save the screenshot settings: boom");
    await p._ssStartSync({kind: 'all'});
    assert.equal(p._ssStatus, "Couldn't sync: boom");
    assert.match(p.shadowRoot.innerHTML, /class="sticky-bar"[\s\S]*Couldn&#39;t sync: boom/);
    assert.doesNotMatch(p.shadowRoot.innerHTML, /id="btn-menu"/);
    p._initialized = true;
    p.narrow = true;
    assert.match(p.shadowRoot.innerHTML, /id="btn-menu"/);
    console.log('PASS: errors inline (no alert); menu button when narrow');
  }

  // Rest mode / off: HA's last read, no PS4 actions (Refresh, Sync all, Sync N, PS4 delete, viewer Sync)
  {
    const READ_AT = '2026-10-15T22:00:00+00:00';
    listResult = (msg) => ({shots: shots.slice(msg.offset, msg.offset + msg.count), more: false,
      asleep: true, power_state: 'rest', read_at: READ_AT});
    const {p, calls} = panel({...SETTINGS_ON, power_state: 'rest'});
    await p._loadEntries();
    p._tab = 'screenshots';
    await p._ssLoad();
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /The PS4 is in rest mode: this is what HA read [^<]*\d[^<]*\./, 'the read time (locale format)');
    assert.doesNotMatch(html, /Reading the PS4/);
    assert.match(html, /id="ss-sync" disabled>Sync all\.\.\.</);
    assert.match(html, /id="ss-refresh" disabled>Refresh</);
    assert.match(html, /id="ss-select">Select</, 'Select stays: HA copies can still be deleted');
    assert.match(html, /<span class="ss-game">Home screen<\/span>/, 'the last read shows');
    p._ssSelecting = true;
    p._ssSelected = new Set([HOME, GAME_D]);
    p._render();
    html = p.shadowRoot.innerHTML;
    assert.match(html, /id="ss-copy-selected" disabled>Sync 1 to HA</);
    assert.match(html, /id="ss-delete" >Delete 2\.\.\.</);
    p._ssOpenDelete();
    const dlg = p._renderSsDelete();
    assert.match(dlg, /<input type="checkbox" disabled> PS4 original \(the PS4 is in rest mode or off\)/);
    assert.doesNotMatch(dlg, /id="ss-del-ps4"/);
    p._ssDelete = null;
    p._ssSelecting = false;
    await p._ssCardClick(GAME_D);
    assert.match(p._renderSsView(), /id="ss-view-sync" disabled>Sync to HA</);
    p._ssView = null;

    // Off, nothing read since HA started: says to turn it on, not "No screenshots"
    listResult = () => ({shots: [], more: false, asleep: true, power_state: 'off', read_at: null});
    await p._ssLoadShots(true);
    html = p.shadowRoot.innerHTML;
    assert.match(html, /The PS4 is off: turn it on to see its screenshots\./);
    assert.doesNotMatch(html, /No screenshots on this PS4 yet/);
    assert.equal(calls.filter((c) => c.type === 'ps4_goldhen/screenshots/plan').length, 0);

    // On again: the buttons come back, the line goes
    listResult = (msg) => ({shots: shots.slice(msg.offset, msg.offset + msg.count), more: false,
      asleep: false, power_state: 'on', read_at: READ_AT});
    await p._ssLoadShots(true);
    html = p.shadowRoot.innerHTML;
    assert.doesNotMatch(html, /The PS4 is (in rest mode|off)/);
    assert.match(html, /id="ss-sync" >Sync all\.\.\.</);
    assert.match(html, /id="ss-refresh" >Refresh</);
    listResult = (msg) => ({shots: shots.slice(msg.offset, msg.offset + msg.count), more: false});
    console.log('PASS: asleep = last read + its time, PS4 actions greyed, HA deletes still allowed');
  }

  // Console clock shown as is
  {
    const {p} = panel({});
    const shown = p._ssTaken('2026-10-15T11:03:10');
    assert.match(shown, /11:03|11\.03/);
    assert.equal(p._ssTaken('junk'), '');
    console.log('PASS: taken time is the console clock');
  }
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
