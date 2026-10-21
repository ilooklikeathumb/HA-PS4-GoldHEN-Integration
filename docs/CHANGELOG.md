# Changelog

## 1.0.0

The first release of this fork. Everything below is new or changed since Tech-Morph's 0.9.0. An existing 0.9.0
config entry carries over in place.

### Security

- The sidebar panel, every WebSocket command and the FTP download / upload URLs are for HA admins only. Before, every
  HA user had them, including kiosk and TV logins.
- `send_payload` only sends a file from `/config/ps4/payloads`, and only to a console set up in this integration. It
  used to accept any file path on the HA host and any host and port.
- With two consoles, every payload went to the first console set up, whatever the call or the panel said. The call's
  `ps4_host` now picks one of the integration's consoles, and the panel switches its target with the console picker.
- Removed the game cover URL that worked without an HA login. Each request to it opened an FTP session to the console.

### Reliability

- No FTP polling. The 5 s poll (about 17,000 logins a day) looked for a file only the unfinished PS4StateJSON plugin
  would write; it's off. The game list (app.db) refreshes hourly, skips consoles in rest / off, and an on-demand pull
  for an unknown game runs one at a time, at most once per game per 5 minutes.
- klog: TCP keepalive finds a connection that died without closing. Before, the state froze while still saying
  connected.
- klog: a connection that dies when a console loses power no longer spins HA's event loop until it runs out of memory.
- klog: no reconnect on silence (closing klog from HA's side wedged older GoldHEN builds). A connection left stale
  after a wake from deep rest is found with a DDP check and reopened.
- klog lines no longer go on HA's event bus, so the recorder stops storing every console log line. Attributes that
  change more often than their state (Current game's klog details, Save backup status's progress) aren't recorded
  either.
- One game list cache per console. A shared one showed one console's games on the other when a pull failed.
- The running game shows right after an HA restart (from DDP) instead of waiting for the next game switch.
- Every FTP session HA opens to a console shares one lock: screenshots, saves, covers, the game list, the FTP tab and
  file download / upload. The FTP tab waits its turn up to 60 s, then says FTP is busy. Screenshots and saves retry a
  refused connect (another FTP client may be using the console).
- Payload sends go one at a time per console, 3 s apart.

### New

- **One device per console**, named after the console (its own name, read at setup), with model, hardware revision,
  firmware, serial and MAC. HA follows the console to a new IP address when its DHCP watcher sees it move.
- **Media player:** off / idle / playing with the game's name and cover. Turn on wakes from rest (DDP) or presses a
  wake entity you choose; turn off puts the console in rest mode. The source list and media browser list the
  installed titles: pick one to start it, also from rest (HA wakes the console and signs the user in first). After a
  wake, the title left suspended comes back where it was.
- **Pairing from HA** (Remote Play registration with the PS4's 8-digit number; no chiaki-ng or PSN needed) and
  signing the paired user in after a wake.
- **Status** sensor: Playing / Home screen / Rest mode / Off.
- **Close game** and **Read console info** buttons. Upstream's Standby button is now **Rest mode** and works.
- **Game cover** image entity.
- **Live temperatures** (CPU, SoC, fan duty) and **Fan control** with a target temperature (50 °C up to the model's
  factory target), using the integration's own payloads. Turning Fan control off puts back the console's own target.
- **Play time:** today, last 7 / 30 days and the current session, plus a Play time tab.
- **Screenshots** tab: view the console's screenshots, sync them to HA's media folder, delete them on the PS4. Latest
  screenshot + Sync latest on the device page.
- **Saves** tab: back up saves to `/share` with older versions kept, compare with what's on the PS4, send a save back
  for Apollo Save Tool to restore, download zips, delete HA's copies (with a warning when the last copy would go). A
  progress bar while it backs up; Select all in both lists.
- The panel asks before every payload send and shows the console's firmware.
- The panel works on phones, and shows errors in the tab instead of pop-ups.
- The integration's own payloads ship with their C source (`payload_src/`, GPL-3.0-or-later) and SHA256 sums, and
  rebuild byte for byte from it with SDK v0.9 and clang 22.1.8.

### Changed

- The media player reads `off` in rest mode (HA deprecated `standby`); its `power_state` attribute says rest or off.
- Current game shows Rest Mode / Off, has no picture, and lost its `cover_url` attribute (`cdn_cover` stays).
- CPU temperature, SoC temperature, Fan duty, Firmware version and Hardware model are on by default and read real
  values. The power sensors and Console ID still need the unfinished plugin and are off by default.
- FTP status is a diagnostic sensor, off by default, and reads `not_polled`.
- GoldHEN's ports are fixed: setup, options and the Payloads tab no longer ask for them (existing entries keep theirs).
- Turn on, when the console can't be seen on the network, says so and names the PS4's rest mode network settings.
- A klog connection error is logged once per outage, not every 20 s.
- `manifest.json`: documentation, issue tracker and `codeowners` point at this fork.

### Removed

- The Restart button. Its `restart.bin` was never shipped, so pressing it only raised an error.
- The `ps4_goldhen_klog_stream` and `ps4_goldhen_klog_event` events. The Klog tab gets the lines directly; for game and
  power automations use the media player or Status.
- The bundled `fan-threshold.bin`. It wrote the fan settings from a zeroed buffer and wiped the channel's other values;
  Fan control replaces it.
- The unauthenticated cover URL (see Security).
- `title_resolver.py`, a remote title-name lookup nothing imported. Game names come from the console's app.db.

Known issues: [README](../README.md#known-issues).

## 0.9.0 (Tech-Morph)

The base of this fork: the FTP browser, payload sender, klog listener and Current game sensor. Its notes:
[Tech-Morph's changelog](https://github.com/Tech-Morph/HA-PS4-GoldHEN-Integration/blob/main/docs/CHANGELOG.md).
