# Using PS4 GoldHEN

Install and setup are in the [README](../README.md). This page covers the rest: the entities, the panel, payloads,
keeping GoldHEN healthy and the known issues.

## Entities

Per console, on its device page:

| Entity | What it is |
|---|---|
| Media player | Main control: state, game, cover, turn on / off, start a game. The source list and the media browser list the console's games and installed apps (Apollo, Itemzflow…; built-in apps such as the Internet Browser aren't listed); the media browser also has the signed-in user's home-screen folders. Picking one starts it; from rest mode HA wakes the console and signs in the paired user first. A game closes the running game first; an app never does (the console asks on the TV if it has to). `off` covers rest and fully off; the `power_state` attribute says which. Covers are each console's own icons, as its home screen shows them (custom or restyled icons included). HA reads the game list and the covers it doesn't have about 90 s after the console wakes (once someone is signed in) and every hour while it's on, and fetches an icon again when it changed on the console. |
| Status | Playing / Home screen / Rest mode / Off / Not responding. Use this in automations. Not responding = the console is on, but its log has been silent for 5 minutes and it hasn't answered two DDP checks a minute apart: it froze. The media player then reads unknown (its `not_responding` attribute is true) and play time stops. It clears when the console answers again or is seen off. |
| Current game | The running game's name as plain text. |
| Game cover | The running game's cover as an image entity. |
| Close application | Closes the game or app on screen; at the home screen, the game suspended behind it. Unsaved progress is lost. The Internet Browser can't be closed from HA: close it on the PS4. |
| Play time today / last 7 days / last 30 days, Current session | Hours, counted by HA from the console's log. |
| Live temperatures (switch) | Streams CPU temperature, SoC temperature and Fan duty while on. HA starts it about a minute after GoldHEN loads, on the home screen (not while a game runs); turning the switch on starts it at once. |
| Fan control (switch), Fan target temperature | Sets the fan's target temperature; HA re-sends it if the console resets it. Off puts back the target the console had before. |
| Read console info | Fills the device page: model, hardware, firmware, serial. |
| Back up saves now, Save backup status, Last save backup | Appear when the Saves tab is on for that console. |
| Latest screenshot, Sync latest screenshot | Appear when the Screenshots tab is on for that console. |
| Rest mode (button) | Disabled by default; the media player's Turn off does the same. |
| Firmware version, Hardware model | Diagnostic. |
| FTP status | Diagnostic, disabled by default. Always reads `not_polled`: the FTP poll it reports on is off. |
| SoC / CPU / GPU / Total power, Console ID | Disabled, and stay empty: they need Tech-Morph's PS4StateJSON plugin, which isn't finished and isn't included. Don't install it. |

**Fan target:** the slider goes from 50 °C up to your model's factory target, so HA never holds the console hotter
than it left the factory: 83 °C on the later Pros (CUH-72xx), 79 °C on the first Pros (CUH-70xx / 71xx), 80 °C on
the Slims and originals in the [community table](https://gbatemp.net/threads/default-fan-threshold-payload.623315/),
78 °C on the CUH-12xx originals (one reading). Until HA has read the model (*Read console info*), and for a model
not in that table, the top is 78 °C.

Starting a game from an automation:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.ps4
data:
  media_content_type: game
  media_content_id: CUSA00000   # the game's title ID; the media browser shows every installed game
```

Closing one game or app, also one running in the background (nothing happens if it isn't running):

```yaml
action: ps4_goldhen.close_title
data:
  title_id: CUSA00000   # while it's on screen, the media player's media_content_id
  ps4_host: 192.0.2.10  # needed when you have more than one console
```

## The panel

**PS4 GoldHEN** in the sidebar, admins only. Pick the console at the top. Next to it: a **Wake** / **Rest** button
(the media player's turn on / off; Rest asks first while a game runs) and the console's power state.

| Tab | What it does |
|---|---|
| Content | The console's games and apps as covers (search, Games / Apps / All, sort by last played, most played or name), with what's running on top. A cover opens that game's page: version, size, region, install date, its home-screen folder; play time (totals + the last 30 days); HA's copies of its saves; its screenshots on the PS4. **Launch** / **Close**, **Back up saves** and **Sync screenshots** for that game. Launch asks first when it would close a game. |
| Saves | **On PS4** (what the console has, compared with HA) and **In HA** (HA's copies + older versions). Back up (**Stop** ends it after the save being copied), send back to the PS4, download, delete from HA. Copies go to `/share/PS4 Saves/<console>/<user>/`. Saves of a game that is open are never copied. |
| Screenshots | View, sync to HA, delete on the PS4 (asks first). |
| Play time | Today, last 7 / 30 days and all time, plus a per-game table. Counts while a game is on screen. |
| FTP | Browse the PS4's files: upload, download, rename, delete, edit text files. Uploads over ~16 MB fail for now. |
| Payloads | Send a `.bin` / `.elf` from `/config/ps4/payloads` on the HA host. |
| Klog | The console's live log. |

Saves and Screenshots are **Off** per console until you turn them on, and nothing runs on a schedule: HA reads or
copies only when you press a button. Write an automation if you want it automatic. Each has a size limit for HA's
copies (defaults: 5 GB saves, 2 GB screenshots). While the console is in rest mode or off, both tabs show what HA read
last and don't connect to it; the FTP tab waits until it's on.

**Restoring a save:** In HA → tick the save → **Send to PS4**. HA puts it in Apollo's Fake USB folder; on the PS4 open
Apollo Save Tool → the USB saves list → **Copy save game to HDD**. **Remove from PS4** then deletes what HA sent, and
only that: files you or Apollo put in the folder stay. If the folder already holds a file of the same save that HA
didn't send, Send to PS4 stops and names it.

## Payloads

**The integration's own payloads** do rest mode, game launch / close, live temperatures, fan control and console info.
HA sends them itself; you never pick them. They are built from the C source in [`payload_src/`](../payload_src) with
John Törnblom's [ps4-payload-dev SDK](https://github.com/ps4-payload-dev/sdk) and are GPL-3.0-or-later. Each folder's
README has the build steps and the SHA256 of the shipped binary.

**Bundled payloads** (from Tech-Morph's version, listed in
[`bundled_payloads/README.md`](../custom_components/ps4_goldhen/bundled_payloads/README.md)) are copied to
`/config/ps4/payloads` at setup and only run when you send one from the Payloads tab. Their sources and supported
firmwares aren't documented here. Many payloads only work on the firmwares they were built for, so only send one you
know supports your firmware. ymmv.

The `ps4_goldhen.send_payload` action sends a file from `/config/ps4/payloads` to one of your consoles (other hosts
are refused). Like `close_title`, it's for admin users; automations can call both:

```yaml
action: ps4_goldhen.send_payload
data:
  payload_file: your-payload.bin   # a file in /config/ps4/payloads: its name, or its full path there
  ps4_host: 192.0.2.10             # needed when you have more than one console
  binloader_port: 9090             # optional: without it, the console's own BinLoader port
```

## Keep GoldHEN healthy

GoldHEN's servers are small and easy to knock over. The integration follows these rules; your own tools should too.

- **Never "check" BinLoader (9090) with a bare connect** (port scanners, `nc -z`, uptime monitors). An empty connection
  can kill BinLoader within seconds. Only connect to send a real payload.
- **HA's FTP work takes turns.** HA's own FTP work (saves, screenshots, covers, the game list, the FTP tab) uses one
  session at a time per console. A second FTP client alongside it worked in testing; still, avoid FileZilla or another
  FTP client while HA is backing up saves or syncing screenshots.
- **One klog client at a time.** A new connection to port 3232 drops the one before it (GoldHEN 2.4b18.4 and
  2.4b18.12). HA reconnects 10 s later and drops the other client in turn, missing log lines in each gap, so close
  other klog viewers.
- HA sends payloads one at a time, 3 s apart. Sending from a PC as well? Give the console a moment between them.
- **When GoldHEN wedges** (FTP and BinLoader dead, the GoldHEN menu can't turn them back on): restart the PS4 fully
  (not rest mode) and load GoldHEN again.

## Known issues

- The bundled payloads weren't part of any test.
- Not seen on a console yet, only in tests: Fan control off putting back the earlier target; HA re-sending the fan
  target after the console resets it.
- A frozen console reads Not responding about 6 minutes after it went silent; its play time counts until then.
- The panel's FTP upload fails above ~16 MB.

[Back to the main README](../README.md)
