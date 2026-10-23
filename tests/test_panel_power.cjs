// the panel's Wake / Rest button in the top bar: the selected console's media player from HA's live states, one
// button that flips with the power state, Rest asks first while a game runs, Wake needs a wake method, errors
// inline (no alert), the button waits for the power state to follow, and the Saves / Screenshots tabs follow it.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let Panel;
const timers = [];  // setTimeout callbacks, run by hand
const sandbox = {
  HTMLElement: class { attachShadow() { this.shadowRoot = {querySelector: () => null, querySelectorAll: () => []}; } },
  customElements: {define: (_name, cls) => { Panel = cls; }},
  alert: (msg) => { throw new Error(`alert: ${msg}`); }, confirm: () => true, requestAnimationFrame: () => {},
  setInterval, clearInterval, console,
  setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; }, clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../custom_components/ps4_goldhen/frontend/ps4-goldhen-panel.js'), 'utf8'), sandbox);

const MP = 'media_player.ps4_pro_a';
const TURN_ON = 128, TURN_OFF = 256;
const ENTRIES = [
  {entry_id: 'console_a', title: 'PS4 GoldHEN (192.0.2.121)', name: 'PS4 PRO A', ps4_host: '192.0.2.121', media_player: MP},
  {entry_id: 'console_b', title: 'PS4 GoldHEN (192.0.2.189)', name: 'PS4 PRO B', ps4_host: '192.0.2.189', media_player: null},
];

function mp(state, power, extra = {}) {
  return {[MP]: {state, attributes: {power_state: power, supported_features: TURN_ON | TURN_OFF, ...extra}}};
}

function panel(states, service = async () => {}) {
  const services = [];
  const p = new Panel();
  const hass = (st) => ({
    states: st,
    callWS: async (msg) => {
      if (msg.type === 'ps4_goldhen/list_entries') return {entries: ENTRIES};
      if (msg.type === 'ps4_goldhen/ftp_list_dir') return {path: msg.path, entries: [{name: 'data', path: '/data', is_dir: true}]};
      throw new Error(`unexpected ${msg.type}`);
    },
    callService: async (domain, name, data) => { services.push([domain, name, data.entity_id]); return service(domain, name, data); },
  });
  p._hass = hass(states);
  p._initialized = true;
  // HA hands the panel a new hass object on every state change
  const push = (st) => { p.hass = hass(st); };
  return {p, services, push};
}

async function main() {
  // On: the state and a Rest button; Rest sends turn_off, then waits for the console to report rest
  {
    const {p, services, push} = panel(mp('idle', 'on'));
    await p._loadEntries();
    p._render();
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /<button class="btn" id="btn-power" data-power="rest">Rest<\/button><span class="power-state" id="power-state"><span class="power-dot on"><\/span>On<\/span>/);
    await p._pwClick('rest');
    assert.equal(JSON.stringify(services), JSON.stringify([['media_player', 'turn_off', MP]]));
    html = p.shadowRoot.innerHTML;
    assert.match(html, /<button class="btn" disabled>Going to rest\.\.\.<\/button>/, 'waits for the power state');
    assert.doesNotMatch(html, /id="btn-power"/);
    assert.equal(timers.at(-1).ms, 90000);
    push(mp('off', 'rest'));
    html = p.shadowRoot.innerHTML;
    assert.match(html, /id="btn-power" data-power="wake">Wake<\/button><span class="power-state" id="power-state"><span class="power-dot rest"><\/span>Rest mode</);
    assert.equal(timers.at(-1).fn, null, 'the wait timer is cleared');
    console.log('PASS: On → Rest sends turn_off, waits, flips to Wake on rest');
  }

  // Rest while a game runs asks first; Escape closes it; OK sends it
  {
    const {p, services} = panel(mp('playing', 'on', {media_title: '<b>Game C</b>'}));
    await p._loadEntries();
    p._render();
    assert.match(p.shadowRoot.innerHTML, /id="power-state"><span class="power-dot on"><\/span>Playing</);
    p._pwClick('rest');
    assert.equal(services.length, 0, 'nothing sent before the OK');
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /id="pw-confirm" data-dismiss/);
    assert.match(html, /Put PS4 PRO A in rest mode\?/);
    assert.match(html, /<b>&lt;b&gt;Game C&lt;\/b&gt;<\/b> is running\. Rest mode suspends it\./);
    assert.equal(p._closeOverlay(), true, 'Escape closes it');
    assert.doesNotMatch(p.shadowRoot.innerHTML, /pw-confirm/);
    p._pwClick('rest');
    await p._pwRun('rest');
    assert.equal(JSON.stringify(services), JSON.stringify([['media_player', 'turn_off', MP]]));
    assert.doesNotMatch(p.shadowRoot.innerHTML, /pw-confirm/);
    console.log('PASS: Rest asks first while a game runs');
  }

  // Wake: turn_on; no wake method → a hint, no call; an error → inline, the button comes back
  {
    let fail = null;
    const {p, services, push} = panel(mp('off', 'rest'), async () => { if (fail) throw {code: 'home_assistant_error', message: fail}; });
    await p._loadEntries();
    await p._pwClick('wake');
    assert.equal(JSON.stringify(services), JSON.stringify([['media_player', 'turn_on', MP]]));
    assert.match(p.shadowRoot.innerHTML, /disabled>Waking\.\.\.</);
    push(mp('idle', 'on'));
    assert.match(p.shadowRoot.innerHTML, /id="btn-power" data-power="rest">Rest<\/button><span class="power-state" id="power-state"><span class="power-dot on"><\/span>On</);

    push(mp('off', 'off'));
    fail = 'The PS4 didn\'t answer';
    await p._pwClick('wake');
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /<button class="btn" id="btn-power" data-power="wake">Wake<\/button><span class="power-state" id="power-state"><span class="power-dot off"><\/span>Off<\/span>/);
    assert.match(html, /<span class="muted">Couldn&#39;t wake it: The PS4 didn&#39;t answer<\/span>/);

    push(mp('off', 'rest', {supported_features: TURN_OFF}));
    services.length = 0;
    p._pwClick('wake');
    assert.equal(services.length, 0);
    assert.match(p.shadowRoot.innerHTML, /No wake method yet: pair with the PS4, or add a wake entity, in the PS4 GoldHEN options\./);
    console.log('PASS: Wake sends turn_on; no wake method = a hint; errors inline');
  }

  // a timeout gives the button back; unknown power = no button; unavailable or no media player = nothing
  {
    const {p, push} = panel(mp('idle', 'on'));
    await p._loadEntries();
    await p._pwClick('rest');
    timers.at(-1).fn();
    assert.match(p.shadowRoot.innerHTML, /id="btn-power" data-power="rest">Rest</, 'still on after 90 s: Rest again');
    push(mp('idle', null));
    let html = p.shadowRoot.innerHTML;
    assert.match(html, /id="power-state"><span class="power-dot unknown"><\/span>Unknown<\/span><\/span>/);
    assert.doesNotMatch(html, /id="btn-power"/);
    push(mp('unknown', 'on', {not_responding: true}));  // frozen: neither button works
    html = p.shadowRoot.innerHTML;
    assert.match(html, /id="power-state"><span class="power-dot unknown"><\/span>Not responding<\/span><\/span>/);
    assert.doesNotMatch(html, /id="btn-power"/);
    push(mp('idle', 'on', {not_responding: false}));
    assert.match(p.shadowRoot.innerHTML, /id="btn-power" data-power="rest">Rest</, 'answers again: Rest is back');
    push(mp('unavailable', null));
    assert.doesNotMatch(p.shadowRoot.innerHTML, /class="power"/);
    p._selectEntry('console_b');
    p._render();
    assert.doesNotMatch(p.shadowRoot.innerHTML, /class="power"/, 'no media player: no button');
    console.log('PASS: timeout, unknown, not responding, unavailable, no media player');
  }

  // renders only when the selected console's power changes; Saves + Screenshots follow; never mid-typing
  {
    const {p, push} = panel(mp('idle', 'on'));
    await p._loadEntries();
    let renders = 0;
    const render = p._render.bind(p);
    p._render = () => { renders += 1; render(); };
    push(mp('idle', 'on'));
    renders = 0;
    push({...mp('idle', 'on'), 'light.kitchen': {state: 'on', attributes: {}}});
    assert.equal(renders, 0, 'another entity changed: no render');
    p._ssSettings = {enabled: true, power_state: 'on'};
    p._svSettings = {mode: 'manual', power_state: 'on'};
    push(mp('off', 'rest'));
    assert.equal(renders, 1);
    assert.equal(p._ssSettings.power_state, 'rest');
    assert.equal(p._svSettings.power_state, 'rest');
    assert.equal(p._ssAsleep(), true);
    p._editing = {path: '/x', content: 'typed'};
    push(mp('idle', 'on'));
    assert.equal(renders, 1, 'the FTP editor is open: no render');
    assert.equal(p._ssSettings.power_state, 'on', 'the state still follows');
    p._editing = null;
    console.log('PASS: renders on power changes only; tabs follow; not while editing');
  }

  // FTP tab: nothing listed while the console sleeps, the buttons wait; once it's on, Refresh lists
  {
    const calls = [];
    const {p, push} = panel(mp('off', 'rest'));
    const spy = (h) => { const ws = h.callWS; h.callWS = async (msg) => { calls.push(msg.type); return ws(msg); }; };
    spy(p._hass);
    await p._loadEntries();
    p._tab = 'ftp';  // the panel opens on Games
    await p._loadDir('/');
    let html = p.shadowRoot.innerHTML;
    assert.ok(!calls.includes('ps4_goldhen/ftp_list_dir'), 'no FTP to a sleeping console');
    assert.match(html, /The PS4 is in rest mode: turn it on to browse its files\./);
    for (const id of ['btn-root', 'btn-back', 'btn-refresh', 'btn-ftp-select', 'btn-ftp-upload']) {
      assert.match(html, new RegExp(`id="${id}" disabled`), id);
    }
    push(mp('idle', 'on'));
    spy(p._hass);
    html = p.shadowRoot.innerHTML;
    assert.match(html, /Refresh to list the PS4(&#39;|')s files\./);
    assert.match(html, /id="btn-refresh" >Refresh</);
    await p._loadDir();
    assert.ok(calls.includes('ps4_goldhen/ftp_list_dir'));
    html = p.shadowRoot.innerHTML;
    assert.match(html, /data-path="\/data"/);
    assert.doesNotMatch(html, /Refresh to list/);
    push(mp('off', 'off'));
    html = p.shadowRoot.innerHTML;
    assert.doesNotMatch(html, /data-path="\/data"/, 'what was listed goes when it sleeps');
    assert.match(html, /The PS4 is off: turn it on to browse its files\./);
    console.log('PASS: FTP tab waits while the console sleeps, Refresh lists once it is on');
  }

  // one shape for everything clickable (6 px rounded rectangles); ovals only on status badges
  {
    const {p} = panel(mp('idle', 'on'));
    await p._loadEntries();
    p._render();
    const css = p.shadowRoot.innerHTML;
    for (const sel of ['.tabbtn', '.seg', '.editor-actions button', '.actions button, .nav-btns button, .btn', '.gm-ctl']) {
      const rule = css.match(new RegExp(`\\n\\s*${sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{([^}]*)\\}`));
      assert.ok(rule, sel);
      assert.match(rule[1], /border-radius:6px/, sel);
    }
    assert.match(css, /\.pill \{[^}]*border-radius:999px/, 'badges stay oval');
    assert.doesNotMatch(css, /class="pill" id="power-state"/, 'the power state is not a badge between buttons');
    assert.match(css, /<\/select>\s*<span class="power"><button class="btn" id="btn-power"/, 'Wake / Rest right after the picker');
    assert.doesNotMatch(css, /btn-reload-entries|>Reload</, 'no Reload: the browser refreshes');
    console.log('PASS: buttons, tabs and toggles share one shape');
  }

  // switching console mid-wait drops the wait
  {
    const {p} = panel(mp('idle', 'on'));
    await p._loadEntries();
    const going = p._pwRun('rest');
    p._selectEntry('console_b');
    await going;
    assert.equal(p._pw.busy, null);
    console.log('PASS: switching console drops the wait');
  }
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
