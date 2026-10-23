// the panel's Content tab — the default tab, the cover grid (Games / Apps / All, filter, sort, escaped names, the running
// title), a game's page (details, play time, saves, its screenshots), Launch / Close asking first only when a game
// would close, the buttons greyed while the console sleeps, Back / Escape, the console switch. HA only, except the
// game's screenshots (one list call with its title ID, only while the console is on).
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let Panel;
const intervals = [];
const sandbox = {
  HTMLElement: class { attachShadow() { this.shadowRoot = {querySelector: () => null, querySelectorAll: () => []}; } },
  customElements: {define: (_name, cls) => { Panel = cls; }},
  alert: (msg) => { throw new Error(`alert: ${msg}`); }, confirm: () => true, requestAnimationFrame: () => {},
  setInterval: (fn, ms) => { intervals.push({fn, ms}); return intervals.length; },
  clearInterval: (id) => { if (intervals[id - 1]) intervals[id - 1].cleared = true; },
  setTimeout, clearTimeout, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../custom_components/ps4_goldhen/frontend/ps4-goldhen-panel.js'), 'utf8'), sandbox);

const MP = 'media_player.ps4_pro_a';
const COVER = (tid) => `/api/media_player_proxy/${MP}/browse_media/game/${tid}?token=abc123`;
const ENTRIES = [
  {entry_id: 'console_a', name: 'PS4 PRO A', ps4_host: '192.0.2.121', media_player: MP},
  {entry_id: 'console_b', name: 'PS4 PRO B', ps4_host: '192.0.2.189', media_player: null},
];
const TITLES = [
  {title_id: 'CUSA90003', name: '<b>Game C</b>', kind: 'game', cover: COVER('CUSA90003'), built_in: false,
   play: {seconds: 7200, sessions: 3, last: '2026-10-20T20:00:00+00:00'}},
  {title_id: 'APOL00004', name: 'Apollo Save Tool', kind: 'app', cover: COVER('APOL00004'), built_in: false, play: null},
  {title_id: 'CUSA90001', name: 'Alpha Game', kind: 'game', cover: "javascript:alert(1)", built_in: false,
   play: {seconds: 99000, sessions: 9, last: '2026-09-01T20:00:00+00:00'}},
];
const SAVES_ON = {mode: 'manual', running: false, progress: {done: 0, total: 0}, last: null, power_state: 'on', status: 'never'};
const SHOTS_ON = {enabled: true, syncing: false, progress: {done: 0, total: 0}, last_sync: null, power_state: 'on'};

function detail(over = {}) {
  return {
    title_id: 'CUSA90003', name: '<b>Game C</b>', kind: 'game', built_in: false, cover: COVER('CUSA90003'),
    info: {content_id: 'EP0001-CUSA90003_00-GAMEC', region: 'Europe', size: 16318615552, installed: '2025-10-05 17:14:05',
           folder: 'Racing', version: '1.02'},
    play: {seconds: 7200, sessions: 3, first: '2026-09-01T20:00:00+00:00', last: '2026-10-20T20:00:00+00:00', week: 3600,
           month: 7200, days: Array.from({length: 30}, (_, i) => ({day: `2026-09-${String(i + 1).padStart(2, '0')}`, seconds: i === 29 ? 3600 : 0})),
           recent: [{title_id: 'CUSA90003', start: '2026-10-20T19:00:00+00:00', seconds: 3600, open: false, playing: false}]},
    suspended: null,
    saves: [{uid: '1eb3d8e9', user: 'Player', last_saved: '2026-10-20T21:00:00Z', bytes: 2097152,
             slots: [{slot: 'SAVEDATA00', name: '<i>Chapter 3</i>', saved: '2026-10-20T21:00:00Z', bytes: 2097152, versions: 2, on_ps4: true}]}],
    saves_settings: SAVES_ON, screenshot_settings: SHOTS_ON,
    ...over,
  };
}

function panel({states = {[MP]: {state: 'idle', attributes: {power_state: 'on'}}}, details = {}, shots = []} = {}) {
  const calls = [];
  const services = [];
  const p = new Panel();
  const hass = (st) => ({
    states: st,
    callWS: async (msg) => {
      calls.push(msg);
      if (msg.type === 'ps4_goldhen/list_entries') return {entries: ENTRIES};
      if (msg.type === 'ps4_goldhen/list_payloads') return {payloads: [], payload_dir: '/config/ps4/payloads'};
      if (msg.type === 'ps4_goldhen/games/list') return {titles: msg.entry_id === 'console_a' ? TITLES : []};
      if (msg.type === 'ps4_goldhen/games/detail') return details[msg.title_id] || detail();
      if (msg.type === 'ps4_goldhen/screenshots/list') return {shots, more: false, asleep: false};
      if (msg.type === 'ps4_goldhen/saves/backup') return {started: true};
      if (msg.type === 'ps4_goldhen/saves/get') return p._nextSaves();
      if (msg.type === 'ps4_goldhen/screenshots/sync') return {started: true, total: 0};
      if (msg.type === 'ps4_goldhen/screenshots/get') return SHOTS_ON;
      throw new Error(`unexpected ${msg.type}`);
    },
    callService: async (domain, name, data) => { services.push([domain, name, data]); },
  });
  p._hass = hass(states);
  p._initialized = true;
  const push = (st) => { p.hass = hass(st); };
  return {p, calls, services, push};
}

const types = (calls) => calls.map((c) => c.type);

async function main() {
  // opens on Games, HA only: no FTP listing, no screenshot read
  {
    const {p, calls} = panel();
    assert.equal(p._tab, 'games');
    await p._init();
    assert.deepEqual(types(calls), ['ps4_goldhen/list_entries', 'ps4_goldhen/list_payloads', 'ps4_goldhen/games/list']);
    const html = p.shadowRoot.innerHTML;
    assert.deepEqual([...html.matchAll(/data-tab="(\w+)"/g)].map((m) => m[1]),
                     ['games', 'saves', 'screenshots', 'playtime', 'ftp', 'binloader', 'klog']);
    assert.match(html, /tabbtn active" data-tab="games">Content<\/button>/, 'the tab is called Content');
    console.log('PASS: the panel opens on Content and reads HA only');
  }

  // the grid: games by default, escaped names, covers from HA only, play time line, the running title badged
  {
    const {p} = panel({states: {[MP]: {state: 'playing', attributes: {power_state: 'on', media_content_id: 'CUSA90003',
                                                                      media_content_type: 'game', media_title: 'Game C'}}}});
    await p._loadEntries();
    await p._gmLoad();
    const html = p.shadowRoot.innerHTML;
    assert.match(html, /&lt;b&gt;Game C&lt;\/b&gt;/);
    assert.doesNotMatch(html, /<b>Game C<\/b>/);
    assert.match(html, /Games 2<\/button>/);
    assert.match(html, /Apps 1<\/button>/);
    assert.doesNotMatch(html, /data-tid="APOL00004"/, 'apps only under Apps / All');
    assert.match(html, /src="\/api\/media_player_proxy\/media_player\.ps4_pro_a\/browse_media\/game\/CUSA90003\?token=abc123"/);
    assert.doesNotMatch(html, /javascript:/, 'only HA paths become an img');
    assert.match(html, /2h · /);
    assert.match(html, /gm-card gm-live[\s\S]*data-tid="CUSA90003"/);
    assert.match(html, />PLAYING</);
    assert.match(html, /Now playing · PS4 PRO A/);
    assert.match(html, /id="gm-now" data-tid="CUSA90003"/);
    p._gm.kind = 'all';
    assert.deepEqual(p._gmTitles().map((t) => t.title_id), ['CUSA90003', 'APOL00004', 'CUSA90001'], 'the console order');
    p._gm.sort = 'played';
    assert.deepEqual(p._gmTitles().map((t) => t.title_id), ['CUSA90001', 'CUSA90003', 'APOL00004']);
    p._gm.sort = 'name';
    assert.deepEqual(p._gmTitles().map((t) => t.title_id), ['CUSA90003', 'CUSA90001', 'APOL00004']);  // "<b>Game C" sorts first
    p._gm.filter = 'apollo';
    assert.deepEqual(p._gmTitles().map((t) => t.title_id), ['APOL00004']);
    p._gm.filter = 'cusa90001';
    assert.deepEqual(p._gmTitles().map((t) => t.title_id), ['CUSA90001'], 'the title ID matches too');
    console.log('PASS: the grid filters, sorts, escapes and badges the running title');
  }

  // a game's page: details, play time, HA's saves, that game's screenshots (one list call with its title ID)
  {
    const shot = {rel: 'NPXS20001/CUSA90003/a1/20261020_200000_00000001.jpg', name: '20261020_200000_00000001.jpg',
                  taken: '2026-10-20T20:00:00', title_id: 'CUSA90003', in_ha: true, trophy: true, thumb: 'data:image/jpeg;base64,AA'};
    const {p, calls} = panel({shots: [shot]});
    await p._loadEntries();
    await p._gmLoad();
    p._gmOpen('CUSA90003');
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));
    const list = calls.find((c) => c.type === 'ps4_goldhen/screenshots/list');
    assert.equal(list.title_id, 'CUSA90003');
    assert.equal(list.count, 6);
    const html = p.shadowRoot.innerHTML;
    assert.match(html, /← All content/);
    assert.match(html, /class="gm-title ">&lt;b&gt;Game C&lt;\/b&gt;/);
    for (const chip of ['CUSA90003', 'Version 1.02', 'Europe', '15.20 GB', 'Folder: Racing']) assert.match(html, new RegExp(`>${chip}<`), chip);
    assert.match(html, /Installed Oct 5, 2025/);
    assert.match(html, /LAST PLAYED/);
    assert.match(html, /TOTAL<\/div>\s*<div class="gm-tile-num">2h</);
    assert.equal((html.match(/class="gm-bar /g) || []).length, 30);
    assert.match(html, /&lt;i&gt;Chapter 3&lt;\/i&gt;/);
    assert.match(html, /\+2 older/);
    assert.match(html, /1 save<\/div>/);
    assert.match(html, /data-rel="NPXS20001\/CUSA90003\/a1\/20261020_200000_00000001\.jpg"/);
    assert.match(html, /gm-trophy">Trophy/);
    assert.match(html, /id="gm-play"[^>]*>\s*<ha-icon icon="mdi:play-circle-outline"><\/ha-icon>Launch/);
    assert.doesNotMatch(html, /id="gm-backup"[^>]*disabled/);
    assert.doesNotMatch(html, /id="gm-sync"[^>]*disabled/);
    // Escape goes back to the covers
    assert.ok(p._closeOverlay());
    assert.equal(p._gm.open, null);
    assert.match(p.shadowRoot.innerHTML, /id="gm-grid"/);
    console.log('PASS: a game page shows its details, play time, saves and screenshots; Escape goes back');
  }

  // Launch: straight away when nothing would close; asks when another game is in front; an app never asks
  {
    const {p, services, push} = panel();
    await p._loadEntries();
    await p._gmLoad();
    p._gm.open = 'CUSA90003';
    await p._gmLoadDetail({shots: false});
    await p._gmPlayClick();
    assert.deepEqual(JSON.parse(JSON.stringify(services)),
                     [['media_player', 'play_media', {entity_id: MP, media_content_id: 'CUSA90003', media_content_type: 'game'}]]);
    assert.match(p._gm.status, /is starting/);
    push({[MP]: {state: 'playing', attributes: {power_state: 'on', media_content_id: 'CUSA90001', media_content_type: 'game',
                                                 media_title: 'Alpha Game'}}});
    services.length = 0;
    p._gmPlayClick();
    assert.equal(services.length, 0, 'asks first');
    assert.equal(p._gm.confirm.kind, 'launch');
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /Start &lt;b&gt;Game C&lt;\/b&gt;\?/);
    assert.match(html, /<b>Alpha Game<\/b> is open\. Starting <b>&lt;b&gt;Game C&lt;\/b&gt;<\/b> closes it/);
    await p._gmRun('launch');
    assert.equal(services.length, 1);
    // the app: never asks (an app doesn't close the game)
    services.length = 0;
    p._gm.detail = {...p._gm.detail, kind: 'app', title_id: 'APOL00004', name: 'Apollo Save Tool'};
    await p._gmPlayClick();
    assert.equal(services[0][2].media_content_type, 'app');
    assert.equal(p._gm.confirm, null);
    console.log('PASS: Launch asks only when a game would close; apps start straight away');
  }

  // the game left in the background counts as open too
  {
    const {p, services} = panel({details: {CUSA90003: detail({suspended: {title_id: 'CUSA90001', name: 'Alpha Game'}})}});
    await p._loadEntries();
    p._gm.open = 'CUSA90003';
    await p._gmLoadDetail({shots: false});
    p._gmPlayClick();
    assert.equal(services.length, 0);
    assert.match(p.shadowRoot.innerHTML, /<b>Alpha Game<\/b> is open in the background/);
    console.log('PASS: a game suspended behind the home screen is asked about');
  }

  // this game in front: Close (asks, then close_title on this console); Back up waits until it's closed
  {
    const {p, services} = panel({states: {[MP]: {state: 'playing', attributes: {power_state: 'on', media_content_id: 'CUSA90003',
                                                                                media_content_type: 'game'}}}});
    await p._loadEntries();
    p._gm.open = 'CUSA90003';
    await p._gmLoadDetail({shots: false});
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /PLAYING NOW/);
    assert.match(html, /mdi:close-circle-outline"><\/ha-icon>Close/);
    assert.match(html, /id="gm-backup"[^>]*disabled[\s\S]*Close the game first/);
    p._gmPlayClick();
    assert.equal(p._gm.confirm.kind, 'close');
    assert.match(p.shadowRoot.innerHTML, /Close &lt;b&gt;Game C&lt;\/b&gt;\?/);
    await p._gmRun('close');
    assert.deepEqual(JSON.parse(JSON.stringify(services)),
                     [['ps4_goldhen', 'close_title', {title_id: 'CUSA90003', ps4_host: '192.0.2.121'}]]);
    console.log('PASS: Close asks first and closes this title on this console');
  }

  // asleep: no screenshot read, Back up and Sync greyed with why; saves off / screenshots off say so
  {
    const rest = {[MP]: {state: 'off', attributes: {power_state: 'rest'}}};
    const asleep = detail({saves_settings: {...SAVES_ON, power_state: 'rest'}, screenshot_settings: {...SHOTS_ON, power_state: 'rest'}});
    const {p, calls} = panel({states: rest, details: {CUSA90003: asleep}});
    await p._loadEntries();
    p._gm.open = 'CUSA90003';
    await p._gmLoadDetail();
    assert.ok(!types(calls).includes('ps4_goldhen/screenshots/list'), 'no FTP while it sleeps');
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /id="gm-backup"[^>]*disabled[\s\S]*Turn the PS4 on first/);
    assert.match(html, /id="gm-sync"[^>]*disabled[\s\S]*Turn the PS4 on first/);
    assert.doesNotMatch(html, /id="gm-play"[^>]*disabled/, 'Launch wakes it from rest');
    assert.match(html, /in rest mode: turn it on to see this game(&#39;|')s screenshots/);

    const off = detail({saves: null, saves_settings: {...SAVES_ON, mode: 'off'}, screenshot_settings: {...SHOTS_ON, enabled: false}});
    const b = panel({details: {CUSA90003: off}});
    await b.p._loadEntries();
    b.p._gm.open = 'CUSA90003';
    await b.p._gmLoadDetail();
    html = b.p.shadowRoot.innerHTML;
    assert.match(html, /Save backup is off for this PS4/);
    assert.match(html, /Screenshots are off for this PS4/);
    assert.match(html, /id="gm-backup"[^>]*disabled/);
    assert.match(html, /id="gm-sync"[^>]*disabled/);
    assert.ok(!types(b.calls).includes('ps4_goldhen/screenshots/list'));
    console.log('PASS: a sleeping console gets no FTP, the buttons say why; off features say so');
  }

  // Back up saves: this game only, the progress, then the result line and the page read again
  {
    const {p, calls} = panel();
    const answers = [{...SAVES_ON, running: true, progress: {done: 1, total: 2}},
                     {...SAVES_ON, running: false, last: {state: 'up_to_date', checked: 2, copied: 2}}];
    p._nextSaves = () => answers.shift();
    await p._loadEntries();
    p._gm.open = 'CUSA90003';
    await p._gmLoadDetail({shots: false});
    intervals.length = 0;
    await p._gmRun('backup');
    assert.deepEqual(JSON.parse(JSON.stringify(calls.find((c) => c.type === 'ps4_goldhen/saves/backup'))),
                     {type: 'ps4_goldhen/saves/backup', entry_id: 'console_a', title_id: 'CUSA90003'});
    const poll = intervals.find((t) => t.ms === 1000);
    assert.ok(poll, 'a 1 s poll while it runs');
    await poll.fn();
    assert.match(p.shadowRoot.innerHTML, /Backing up 1 \/ 2/);
    const before = types(calls).filter((t) => t === 'ps4_goldhen/games/detail').length;
    await poll.fn();
    assert.ok(poll.cleared);
    assert.match(p._gm.status, /Done: 2 of 2 saves were new or changed/);
    assert.equal(types(calls).filter((t) => t === 'ps4_goldhen/games/detail').length, before + 1);
    console.log('PASS: Back up saves backs up this game and follows the pass');
  }

  // Sync screenshots: kind game with this title; nothing new says so
  {
    const {p, calls} = panel();
    await p._loadEntries();
    p._gm.open = 'CUSA90003';
    await p._gmLoadDetail({shots: false});
    await p._gmRun('sync');
    assert.deepEqual(JSON.parse(JSON.stringify(calls.find((c) => c.type === 'ps4_goldhen/screenshots/sync'))),
                     {type: 'ps4_goldhen/screenshots/sync', entry_id: 'console_a', kind: 'game', title_id: 'CUSA90003'});
    assert.match(p._gm.status, /Nothing new to copy/);
    console.log('PASS: Sync screenshots syncs this game');
  }

  // switching console: back to that console's covers
  {
    const {p, calls} = panel();
    await p._loadEntries();
    await p._gmLoad();
    p._gm.open = 'CUSA90003';
    await p._gmLoadDetail({shots: false});
    p._selectEntry('console_b');
    assert.equal(p._gm.open, null);
    assert.equal(p._gm.list, null);
    await p._gmLoad();
    assert.equal(calls.filter((c) => c.type === 'ps4_goldhen/games/list').pop().entry_id, 'console_b');
    assert.match(p.shadowRoot.innerHTML, /Nothing yet/);
    console.log('PASS: switching console resets the page');
  }
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
