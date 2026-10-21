# Developer reference

How the integration works inside, for anyone changing it. Using it: [README](../README.md). What changed since
Tech-Morph's 0.9.0: [CHANGELOG](CHANGELOG.md).

## How it talks to the PS4

| Port | Service | Used for | Rule |
|---|---|---|---|
| TCP 3232 | GoldHEN klog | One long-lived connection per console. Almost all state comes from it. | One client at a time. Never close it on a timer. |
| TCP 2121 | GoldHEN FTP | app.db (game list), covers, screenshots, saves, the panel's FTP tab | HA uses one session at a time (a precaution). Never poll. |
| TCP 9090 | GoldHEN BinLoader | Payload sends | Connect only to send a real payload. |
| UDP 987 | Sony DDP | Status (on / rest), firmware, MAC, running title; wake from rest | Answers in rest too, no GoldHEN needed. |
| UDP + TCP 9295 | Sony Remote Play | Pairing; signing the paired user in after a wake | No video stream is ever opened. |

Nothing else leaves the LAN, apart from game covers that app.db lists as a Sony CDN URL (fetched by HA's own picture
proxy).

## Where the state comes from

```
klog 3232 ─► _klog_listener_task ─┬─► console_info / live_sensors   (payload lines, stop here)
                                  ├─► signal_klog_line(entry)       (panel Klog tab only)
                                  └─► KlogStateMachine ─► coordinator data ─► media player, Status, Current game
                                                                          └─► game_session ─► play_time, save_backup
DDP 987 (setup, each klog connect, stale check) ─► KlogStateMachine
```

- **`KlogStateMachine`** (`__init__.py`) reads power from SceSystemStateMgr lines (`State Change`, the
  `Keeping MAIN_ON_STANDBY` line rest logs every 60 s, `Power Mode Change`), the game in focus from
  `[SL] AppFocusChanged`, launches and closes from the launch / BGFT / `Game Close detected` lines, and the
  choose-user screen from `[UserSelectScene]` and the logged-in user count. A game that loses focus without a close line is kept as `suspended_title_id`.
- **Power without a line:** klog connected for 90 s without a rest heartbeat = on. A connect that fails with
  `EHOSTUNREACH` = fully off (a console in rest still answers and refuses).
- **DDP fills the gaps.** At entry setup and on every klog connect, a DDP search that answers `200` sets the power on and,
  if klog has no game yet, the running title (`running-app-titleid`). It never overrides what klog saw. While the
  state says rest / off and klog has been silent for 60 s, one search a minute checks for a stale socket (a wake from
  deep rest that HA didn't see): `200` closes the socket and reconnects after 1 s.
- **No polling.** The listener pushes data with `coordinator.async_set_updated_data`. The coordinator's FTP poll
  (upstream's telemetry for its unfinished PS4StateJSON plugin) is off (`_FTP_TELEMETRY_ENABLED = False`).
- **Payload lines** (`ps4_goldhen console-info:`, `ps4_goldhen sensors:`, `ps4_goldhen fan:`) are claimed by
  `console_info` / `live_sensors` before anything else, so the serial and the 30 s sensor lines never reach the panel,
  the state machine or the recorder.

## Files

| File | Job |
|---|---|
| `__init__.py` | Entry setup / unload, klog listener + `KlogStateMachine`, app.db refresh task, panel + static paths, FTP download / upload views, `send_payload` service |
| `config_flow.py` | Setup (host, FTP check), wake / pair menu, options (connection and wake settings, pairing), DHCP IP follow |
| `entity.py` | One device per config entry |
| `media_player.py` | State, game + cover, turn on (DDP wake or wake entity, then sign-in), turn off (rest), game list + launch |
| `sensor.py` | Status, Current game, FTP status, play time sensors, temperatures, the disabled plugin sensors |
| `image.py`, `button.py`, `switch.py`, `number.py` | Game cover; Rest mode, Read console info, Close game; Live temperatures, Fan control; Fan target temperature |
| `ddp.py` | DDP `SRCH` (status, firmware, MAC, title) and `WAKEUP` |
| `rest.py` | Rest mode + `async_payload_preflight`, the checks before any of the integration's own payloads |
| `launch.py` | Launch / close / resume through the launch payload |
| `console_info.py` | Firmware, model, serial, MAC on the device page |
| `live_sensors.py`, `fan.py` | Live sensors payload (start, stop, readings); fan target set + drift re-send |
| `game_session.py`, `play_time.py` | Game started / left signals; play time counting, sensors, panel data |
| `screenshots.py`, `screenshot_entities.py`, `capture_files.py` | Screenshots: settings, sync, viewer, delete; device-page entities; console layout + FTP walk |
| `save_backup.py`, `save_backup_entities.py`, `save_files.py` | Save backup: settings, passes, send to PS4, zips, delete; entities; console + HA layouts |
| `rp_regist.py`, `rp_session.py`, `rp_keys.py` | Remote Play pairing with a PIN, the sign-in session, the key tables (ported from playactor, see [CREDITS](../CREDITS.md)) |
| `db.py` | app.db pull, per-console cache, game map + game list |
| `ftp_session.py`, `ftp_listing.py`, `payload_target.py`, `dynamic_entities.py` | Per-console FTP lock; MLSD / LIST listings; which console a `send_payload` call goes to; entities that come and go with a setting |
| `websocket.py` | FTP tab commands, Klog tab subscription |
| `ftp_telemetry.py` | Upstream's telemetry poll for its PS4State plugin (unfinished); off |
| `frontend/ps4-goldhen-panel.js` | The sidebar panel (FTP, Payloads, Klog, Saves, Screenshots, Play time) |
| `*_payload/` | The integration's own ELFs, each with a README (source, SHA256, license) |
| `bundled_payloads/` | Third-party payloads from upstream, copied to `/config/ps4/payloads` |

## The integration's own payloads

| ELF | Does | HA knows it worked from |
|---|---|---|
| `rest_payload/rest-mode.elf` | `sceSystemStateMgrEnterStandby` (Power → Enter Rest Mode) | klog power lines |
| `launch_payload/launch-app.elf` | Close / launch / resume a game. HA writes a 16-byte marker: title ID in bytes 0–9, flag in byte 10 (`K` = close the running game first, `R` with no title = bring back the suspended game). | `ps4_goldhen launch: <title ID> -> 0x<code>`, then AppFocusChanged |
| `info_payload/console-info.elf` | Model + serial | one `ps4_goldhen console-info:` line |
| `sensors_payload/ps4-sensors.elf` (+ `-stop`) | Stays running, prints CPU / SoC temperature, fan duty and fan target every 30 s; a run file on the console keeps one copy | `ps4_goldhen sensors:` lines |
| `fan_payload/ps4-fan.elf` | Sets the fan target (°C written into a marker); changes one byte of the fan channel's settings and writes the rest back as read | `ps4_goldhen fan: state=set;thr=…;was=…` |

Source and build steps: [`payload_src/`](../payload_src), built with the ps4-payload-dev SDK, GPL-3.0-or-later. The
SDK's startup code patches the kernel from a per-firmware table, so `async_payload_preflight` only sends when klog
doesn't say rest / off, DDP answers `200`, and DDP's firmware is in `CONFIRMED_FIRMWARE` (the SDK's confirmed list).
Right after an HA wake klog can still say rest, so those sends check DDP only. These ELFs aren't in
`bundled_payloads/`, so the panel can't send them around the checks.

**Bundled payloads:** copied to `/config/ps4/payloads` at setup (missing files only: a file removed from the bundle
stays in HA). The panel asks before every send and shows the console's firmware. The `send_payload` service doesn't
ask; it takes a file name inside `/config/ps4/payloads` only, and a host only if it is one of the integration's
consoles (the BinLoader port always comes from that console's entry).

## Rules learned on real consoles

GoldHEN's servers are small. Breaking one of these rules can freeze or wedge the console until it restarts.

- **Never TCP-connect to BinLoader 9090 to check it.** A connect without a payload can kill BinLoader within ~30 s
  (3 of 3 tries on GoldHEN 2.4b18.4, and again on 2.4b18.12). To ask "is it on?", use DDP or klog. Retrying a
  *refused* connect right after a wake is fine (nothing is listening yet).
- **HA's FTP work takes turns.** A precaution, not a proven limit (a second FTP client next to HA worked in testing).
  Run FTP work through `ftp_session.run` / `ftp_session.session` (the console's FTP lock), keep sessions short and
  few, and never poll. Every FTP session HA opens takes the lock: screenshots, saves, cover / icon fetches, the app.db
  pull, the FTP tab and file download / upload (those three wait up to 60 s, then report FTP as busy). A cancelled
  caller keeps the lock until its FTP thread ends. `capture_files.connect` retries a refused connect (3×, 2 s): an FTP
  client outside HA may be using the console.
- **klog: one client, and HA keeps it open.** A new client on 3232 drops the one before it (2 of 2 on GoldHEN
  2.4b18.4, 4 of 4 on 2.4b18.12), and HA's reconnect 10 s later drops that client in turn. Silence is normal (an
  idle console logs little), so a dead peer is found by TCP keepalive (30 s idle, then 10 s × 3), never by a silence
  timeout. HA closes it only in the stale-socket case above.
- **One payload at a time, with a gap.** Every payload HA sends goes through `_send_bin_bytes`, which queues sends per
  console and keeps 3 s from the end of one to the start of the next. The hard freezes seen so far came with
  overlapping FTP clients or payload sends close together. A payload built for other firmware can also freeze the console.
- **Rest keeps GoldHEN loaded; a full power-off unloads it.** DDP `WAKEUP` (with the Remote Play regist key) only
  wakes from rest. Rest also signs every user out, so after a wake the console sits on the choose-user screen until
  the Remote Play session signs the paired user in. Port 9295 refuses connections for the first ~11 s after a wake
  and briefly after the session init; `rp_session.py` has the timings and retries.
- **A wedged GoldHEN** (FTP and BinLoader dead, the GoldHEN menu can't turn them back on): restart the PS4 fully (not
  rest) and load GoldHEN again. HA can't fix it from outside.

## Rules for HA's side

- **The klog read loop must yield.** A keepalive timeout raises `TimeoutError` with an errno, the same class as
  `asyncio.wait_for`'s timeout. Treating it as silence re-read a dead socket without yielding: the event loop spun and
  memory grew ~1.5 GB a minute until the kernel killed HA. An errno means a dead socket: leave it.
- **No HA event per klog line.** Upstream fired two per line, and the recorder stored them all (a console waking up
  and starting a game logs hundreds of lines a minute). Lines go to the panel by dispatcher only.
- **Attributes that change more often than the state go in `_unrecorded_attributes`** (Current game's
  `state_signal_line`, `state_reason`, `pending_title_id`, `power_trigger`; Save backup status's `progress`).
  Otherwise the recorder keeps a new state row each time.
- **Bounded everything:** unfinished klog line 64 KB, a line to the panel 8 KB, recent lines 250, a screenshot read for
  the viewer 32 MB, the console's savedata.db 8 MB. Blocking FTP runs in the executor.
- **Rate limits instead of loops:** stale-klog DDP check ≤ 1 a minute; automatic live-sensor start ≤ 1 per 10 min;
  fan re-send ≤ 1 per 10 min, never mid-game; app.db hourly, skipped in rest / off, an on-demand pull ≤ 1 per title per
  5 min, one at a time.
- **Settings that change at runtime live in `.storage`, not in the entry options.** Saving options reloads the entry,
  and a reload drops klog.
- **Copies run only when asked.** Screenshots and saves never run on a schedule (an automation can press the
  buttons). Over FTP, HA writes to a console only for Send to PS4, the panel's deletes (they ask first) and whatever
  is done in the FTP tab.

## Storage and paths

| Where | What |
|---|---|
| `/config/ps4/payloads` | Bundled + your own payloads (the Payloads tab, `send_payload`) |
| `/config/ps4/db/<host>/app.db` | That console's app.db cache |
| `/config/ps4/covers` | Game icons by title ID |
| `/config/ps4/screenshots/<entry>/thumbs` | Screenshot thumbnails, ≤ 200 per console |
| `<media>/PS4 Screenshots/<console>/<game>/` | Synced screenshots |
| `/share/PS4 Saves/<console>/<user>/Latest`, `History` | Save copies in Apollo's USB layout (`PS4/SAVEDATA/<16 hex>/<title ID>/`) |
| `.storage/ps4_goldhen.{console_info,play_time,screenshots,saves}.<entry>` | Per-console data (console info is private) |
| Entry options | `regist_key`, `rp_key`, `account_id`, `wake_entity` |

On the console HA reads `/system_data/priv/mms/app.db`, `/user/av_contents/` (screenshots),
`/user/home/<user>/savedata/` and the console's savedata.db (save names). Send to PS4 writes
`/data/fakeusb/PS4/SAVEDATA/0000000000000000/`. The live sensors payload keeps its run file in `/data/ps4_goldhen/`.

## HA interfaces

- **Service:** `ps4_goldhen.send_payload` (`payload_file`, optional `ps4_host` and `timeout`; `binloader_port` is
  accepted and ignored).
- **WebSocket commands** (all admin only): `ps4_goldhen/list_entries`, `list_payloads`, `ftp_list_dir`,
  `ftp_delete`, `ftp_rename`, `ftp_mkdir`, `ftp_get_text`, `ftp_put_text`, `klog_subscribe`, `play_time`,
  `screenshots/{get,set,list,plan,sync,import,delete}`, `saves/{get,set,list,console,backup,keep,zip,send,delete,clear_sent}`.
- **HTTP views** (all need an HA login; non-admins get 403): `/api/ps4_goldhen/ftp/download`, `/ftp/upload`,
  `/screenshots/<entry>/full`, `/saves/<entry>/download`.
- **Dispatcher signals** per entry: `signal_klog_line` (`const.py`), `signal_game_started` / `signal_game_left`
  (`game_session.py`; reasons `switched`, `closed`, `home_screen`, `rest`, `off`, `klog_lost`).

## Tests and CI

The tests are plain scripts with small HA stand-ins; no HA install and no console needed. Fake servers on loopback
stand in for FTP, DDP and the Remote Play control channel. 28 Python suites (`tests/test_*.py`) and 7 Node suites
(`tests/test_*.cjs`, the panel in a sandbox). Commands: [README → Development](../README.md#development).

GitHub Actions: `regression.yml` checks Python / JSON / JS syntax and runs every suite on Python 3.13 and 3.14 (with
`cryptography`) and Node 24. `hassfest.yaml` and `validate.yml` run hassfest and the HACS check. `release.yml` zips
the integration for a GitHub release.

What the tests don't cover: a real HA runtime, real browsers, and real consoles. Anything that sends to a console
gets tried on one before a release.

## Comments

A comment says what the code does and why, in the present tense. Who decided something, when, and how the code got
here belong in git and the [CHANGELOG](CHANGELOG.md). A console finding stays as a plain fact with what it was seen
on ("a bare connect kills BinLoader within 30 s", "on a PS4 Pro, FW 12.00"). Don't keep a story of one incident,
and don't name a cause that wasn't proven. Test data keeps its timestamps.

## Open work

The user-facing list is in [README → Known issues](../README.md#known-issues). For contributors:

- A frozen console still reads as playing until HA notices: a DDP check while klog is silent could catch it.
- FTP upload above ~16 MB fails (HA's request size limit, and the whole file is held in memory).
- Bundled payloads have no firmware-support list yet; with one, the panel could skip its question for known pairs.
