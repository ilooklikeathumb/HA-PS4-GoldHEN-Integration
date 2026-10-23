# Changelog

## 1.1.0

### Security

- The `send_payload` and `close_title` actions are for HA admins only, like the panel. HA refuses a non-admin user's
  call (Unauthorized); automations still run them.
- `send_payload` no longer swaps a path for a file of the same name: `sub/x.bin` or `/elsewhere/x.bin` used to send
  `/config/ps4/payloads/x.bin`. A path is now refused unless it leads straight to a file in `/config/ps4/payloads`;
  a full path there works like the file name.

### New

- **Wake / Rest** in the panel's top bar, next to the console picker: the console's power state and one button that
  wakes it or puts it in rest mode (the media player's turn on / off). Rest asks first while a game runs.
- **Stop** for a save backup, in its progress bar. The backup ends after the save being copied; what it copied stays
  and the next backup copies the rest. Save backup status reads **Stopped**.
- **Apps in the media player:** the source list and the media browser list the console's installed apps (Apollo,
  Itemzflow, Spotify…) next to its games, with a new **Apps** folder in the browser. Picking an app never closes the
  running game; when the app can't run next to it, the console asks on the TV. `media_player.play_media` takes type
  `app` as well as `game`. The console's built-in titles (Internet Browser, Library, PlayStation Store, THE
  PLAYROOM…) aren't listed anywhere, and `play_media` refuses them: HA can't close them.
- **`ps4_goldhen.close_title`** action: closes one game or app by title ID, on screen or in the background.
- **Folders in the media browser:** a new **Folders** entry shows the signed-in user's home-screen folders as the
  console has them (names, order, the titles inside), then the titles outside a folder. It shows when that user has
  folders. Until someone signs in after an HA restart, it shows the folders of whoever played last. Built-in titles
  HA doesn't list stay out.
- **Content tab** in the panel, first and where the panel opens: the console's games and apps as covers, with what's
  running on top. A cover opens that game's page: its details from the console's app list (version, size, region,
  install date, folder), play time with the last 30 days, HA's copies of its saves and its screenshots on the PS4,
  plus **Launch** / **Close**, **Back up saves** and **Sync screenshots** for that one game. Launch asks first when it
  would close a game (on screen or in the background). Opening the tab reads HA only; a game's page reads that game's
  screenshot folders on the PS4 while it's on.

### Changed

- The README is shorter: what it does, install, adding a console, pairing and credits. Buttons open the repository in
  HACS and start the setup. The rest moved to `docs/`: [USAGE](USAGE.md) (entities, panel, payloads, keeping GoldHEN
  healthy, known issues), [TROUBLESHOOTING](TROUBLESHOOTING.md) (pairing, wake), [SECURITY](SECURITY.md) and
  [CONTRIBUTING](CONTRIBUTING.md).
- `send_payload` uses the call's `binloader_port` again (1.0.0 ignored it). Without one, the port comes from the
  console's entry.
- The panel's tabs are now Content, Saves, Screenshots, Play time, FTP, Payloads, Klog. The FTP tab lists the PS4 when
  it's opened, not when the panel opens.

- **Close game is now Close application:** it closes the game or app on screen. Before, it only closed games, and
  with an app on screen it closed the game behind the app. At the home screen it still closes the suspended game.
  The entity ID stays the same.

- Screenshots no longer connect to a console in rest mode or off. The tab shows what HA read last (and when), and
  Sync, Refresh and PS4 deletes wait until the console is on. HA's copies can still be deleted.
- The FTP tab doesn't connect to a console in rest mode or off either: it says so and its buttons wait.
- The panel's FTP, Saves and Screenshots tabs follow the console's power state live: waking it turns their buttons
  back on.
- Every button, tab and toggle in the panel has the same shape and height; only status badges are round.
- The panel's Reload button is gone: the browser's own refresh (or pull down on a phone) reloads the panel.
- Over the storage limit, an older version that makes room for a new copy is deleted right before that copy, not at
  the start of the backup. A backup that stops or fails partway keeps the versions it didn't need.
- **Covers come only from the console** and each console shows its own icons. Before, HA kept one cover per title for
  all consoles, so a title's icon from one console (for example one restyled by a homebrew) showed on every console,
  and an icon changed on the console never reached HA. HA now keeps each console's icons apart and checks them for
  changes after each game-list read. The covers HA saved before are removed once and read again from each console.
- **The game list is read again after a wake**, about 90 s after the console comes on with someone signed in (not
  sooner: nothing from HA in a wake's first minute), as well as every hour while it's on. Each read also fetches the
  covers HA doesn't have yet, 25 per FTP session, so the Content tab and the media browser open with their covers
  instead of one FTP session per cover.
- The Sony CDN cover link from the console's app list isn't used any more, and Current game's `cdn_cover` attribute
  is gone: the integration makes no internet requests.

### Fixed

- **Fan control off survives an HA restart.** Turned off while the console rests (or after a send that failed), Fan
  control puts back the console's own target when the console reports in. An HA restart before that lost it, and the
  console kept HA's target until it was unplugged. It's now kept in HA's storage until it lands.
- **A live sensors line that lacks a reading no longer leaves the old value showing.** Every line counted as current
  for all four values, so a field missing from one line (or out of range) kept its last value as if it were new. That
  sensor now reads unavailable until a line has it again; the others stay.
- **A save backup cut short by an HA stop or crash no longer leaves half a copy.** Each copy is two files (the save and
  its key), moved into place one after the other. Stopped in between, HA could keep one file without the other: an
  older version in History that couldn't be restored, or a stray file in the newest copies. At start-up and before
  each backup, HA now puts such a pair back together when both halves are still there (the older version comes back
  whole) and otherwise removes the lone file, so the next backup copies that save again.
- **Remove from PS4 and Send to PS4 delete only the files HA sent.** They cleared every save-shaped file in HA's
  account folder of Apollo's Fake USB folder, including ones HA never put there. HA now records each file it uploads
  (before the upload starts) and deletes only those. A send that would write over a file HA didn't send stops before
  writing anything and names the file. A send made before this update is still removed.
- Going to rest from a game no longer shows Home screen for about 13 s before Rest mode (the console logs a
  power-mode line right after it enters rest).
- A frozen console no longer reads Playing for hours: Status reads **Not responding** about 6 minutes after it goes
  silent, the media player reads unknown (`not_responding` attribute), Current game reads Not responding, play time
  stops and the panel's top bar says so. HA asks the console's DDP once a minute while klog is silent for 5 minutes
  or more; two no-replies in a row mean not responding. The game-left signal's reason is `not_responding`.

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

Known issues: [USAGE](USAGE.md#known-issues).

## 0.9.0 (Tech-Morph)

The base of this fork: the FTP browser, payload sender, klog listener and Current game sensor. Its notes:
[Tech-Morph's changelog](https://github.com/Tech-Morph/HA-PS4-GoldHEN-Integration/blob/main/docs/CHANGELOG.md).
