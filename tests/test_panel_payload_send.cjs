// a payload click asks first (firmware support unknown); Cancel sends nothing, Send anyway sends once.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let Panel;
const els = {};  // id selector → fake element the panel binds onclick to
const sandbox = {
  HTMLElement: class {
    attachShadow() {
      this.shadowRoot = {innerHTML: '', querySelector: (sel) => els[sel] || null, querySelectorAll: () => []};
    }
  },
  customElements: {define: (_name, cls) => { Panel = cls; }},
  alert: (m) => { throw new Error(`unexpected alert: ${m}`); }, requestAnimationFrame: () => {}, console,
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../custom_components/ps4_goldhen/frontend/ps4-goldhen-panel.js'), 'utf8'), sandbox);

async function main() {
  const sent = [];
  const p = new Panel();
  p._hass = {
    callWS: async (msg) => (msg.type === 'ps4_goldhen/list_entries'
      ? {entries: [
        {entry_id: 'a', name: 'Console <A>', ps4_host: '192.0.2.189', binloader_port: 9090, firmware: '11.00'},
        {entry_id: 'b', name: 'Console B', ps4_host: '192.0.2.121', binloader_port: 9021, firmware: null}]}
      : {payloads: [], payload_dir: '/config/ps4/payloads'}),
    callService: async (domain, service, data) => { sent.push({domain, service, data}); },
  };
  const realRender = p._render.bind(p);
  p._render = () => {};
  await p._loadEntries();
  p._tab = 'binloader';
  const render = () => {
    for (const k of Object.keys(els)) delete els[k];
    els['#pl-confirm-cancel'] = {};
    els['#pl-confirm-go'] = {};
    realRender();
    return p.shadowRoot.innerHTML;
  };

  // console B, firmware not read yet: the dialog says so, Cancel sends nothing
  p._selectEntry('b');
  p._askSendPayload('disable-updates.bin');
  assert.deepEqual(sent, []);
  let out = render();
  assert.match(out, /id="pl-confirm"/);
  assert.match(out, /disable-updates\.bin<\/b> → <b>Console B<\/b>/);
  assert.match(out, /\(firmware unknown\)/);
  assert.match(out, /supports this console's firmware/);
  assert.match(out, /Send anyway/);
  els['#pl-confirm-cancel'].onclick();
  assert.equal(p._plConfirm, null);
  assert.doesNotMatch(render(), /pl-confirm/);
  assert.deepEqual(sent, []);

  // Escape / backdrop path closes it too
  p._askSendPayload('ftp.bin');
  assert.equal(p._closeOverlay(), true);
  assert.equal(p._plConfirm, null);
  assert.deepEqual(sent, []);

  // console A: firmware named, the name escaped; Send anyway sends once, to the console the dialog showed
  p._selectEntry('a');
  p._askSendPayload('ftp.bin');
  out = render();
  assert.match(out, /Console &lt;A&gt;/);
  assert.match(out, /\(firmware 11\.00\)/);
  assert.match(out, /supports firmware 11\.00/);
  assert.doesNotMatch(out, /id="bin-host"|id="bin-port"/, 'no Host / Port boxes: the target is the selected console');
  await els['#pl-confirm-go'].onclick();
  assert.equal(p._plConfirm, null);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].service, 'send_payload');
  assert.equal(sent[0].data.payload_file, 'ftp.bin');
  assert.equal(sent[0].data.ps4_host, '192.0.2.189');
  assert.equal(sent[0].data.binloader_port, undefined, 'HA takes the port from the console\'s entry');

  // switching tab or console drops an open dialog
  p._askSendPayload('ftp.bin');
  p._selectEntry('b');
  assert.equal(p._plConfirm, null);
  assert.equal(sent.length, 1);

  // the Payloads tab waits on an FTP listing after a console switch: a click meanwhile goes to the new console
  p._selectEntry('a');
  p._selectEntry('b');
  p._askSendPayload('ftp.bin');
  assert.equal(p._plConfirm.target.host, '192.0.2.121');
  p._plConfirm = null;

  // payload names, the folder and the status line are escaped
  p._payloads = ['a<b>.bin'];
  p._payloadDir = '/config/<ps4>';
  p._binStatus = 'Send failed: <x>';
  out = render();
  assert.match(out, /data-payload="a&lt;b&gt;\.bin"/);
  assert.match(out, /<div class="payload-name">a&lt;b&gt;\.bin<\/div>/);
  assert.match(out, /\/config\/&lt;ps4&gt;/);
  assert.match(out, /Send failed: &lt;x&gt;/);
  assert.doesNotMatch(out, /a<b>|<ps4>|<x>/);
  console.log('PASS: payload send asks first (firmware shown / unknown), Cancel sends nothing, Send anyway sends once');
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
