// switching console in the panel switches the BinLoader target too.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let Panel;
const sandbox = {
  HTMLElement: class { attachShadow() { this.shadowRoot = {querySelector:()=>null}; } },
  customElements: {define:(_name,cls)=>{Panel=cls;}},
  alert:()=>{}, requestAnimationFrame:()=>{}, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../custom_components/ps4_goldhen/frontend/ps4-goldhen-panel.js'),'utf8'),sandbox);
async function main(){
  const sent=[];
  const p=new Panel();
  p._render=()=>{};
  p._hass={
    callWS: async()=>({entries:[{entry_id:'bed',ps4_host:'192.0.2.189',binloader_port:9090},{entry_id:'pro2',ps4_host:'192.0.2.121',binloader_port:9021}]}),
    callService: async(domain,service,data)=>{sent.push(data);},
  };
  await p._loadEntries();
  assert.equal(p._payloadTarget(p._selectedEntry()).host,'192.0.2.189');
  p._selectEntry('pro2');
  assert.equal(p._selectedEntryId,'pro2');
  assert.equal(p._payloadTarget(p._selectedEntry()).host,'192.0.2.121');
  await p._sendPayload('ftp.bin');
  assert.equal(sent.length,1);
  assert.equal(sent[0].ps4_host,'192.0.2.121');
  assert.equal(sent[0].binloader_port,undefined);  // HA uses the console's own port
  p._selectEntry('bed');
  assert.equal(p._payloadTarget(p._selectedEntry()).host,'192.0.2.189');
  console.log('PASS: the BinLoader target follows the selected console');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
