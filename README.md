# PS4 GoldHEN for Home Assistant

Control a GoldHEN PS4 from Home Assistant: see what's playing, wake it from rest mode, start and close
games, put it to rest, read temperatures, set the fan, back up saves, pull screenshots and track play time. Everything
runs on your LAN; no PSN account or cloud is needed.

This is a fork of [Tech-Morph/HA-PS4-GoldHEN-Integration](https://github.com/Tech-Morph/HA-PS4-GoldHEN-Integration)
(MIT). Tech-Morph's integration gave HA the FTP browser, payload sender and klog listener; this fork adds the features
above plus a set of fixes. See [Credits](#credits).

> [!WARNING]
> **Beta.** Tested on FW 9.03, 10.01, 11.00 and 12.00: Pro, Slim and original consoles.

Tried it on another model or firmware? Please open an issue with the result, good or bad.

## What it does

- **Media player** per console: off / idle / playing, the game's name and cover, **Turn on** (wake from rest),
  **Turn off** (rest mode), and a game list: pick a game in the source list or the media browser to start it. From rest
  mode HA wakes the console, signs in the paired user and starts the game.
- **Status** sensor: Playing / Home screen / Rest mode / Off, read live from the console's log (klog).
- **Close game** button.
- **Play time**: today, last 7 days, last 30 days and the current session, per console, plus a Play time tab.
- **Temperatures and fan**: a *Live temperatures* switch streams CPU / SoC temperature and fan duty while it's on. *Fan
  control* + *Fan target temperature* (50 °C up to the model's factory target) set the temperature the fan aims for.
- **Device page**: model, hardware revision, firmware and serial (*Read console info* button).
- **Screenshots**: view the console's screenshots in HA, sync them to HA, delete them on the PS4.
- **Save backup**: copy game saves to HA (newest copy + older versions), and send a save back to the PS4 for restoring.
- **Sidebar panel** (admins only): FTP browser, payload sender, live klog, Saves, Screenshots, Play time.
- Follows the console to a new IP address when HA's DHCP watcher sees it move.

## Requirements

- A PS4 running **GoldHEN 2.4b18.10 or newer** with its **FTP server** (port 2121), **BinLoader** (9090) and **klog
  server** (3232) on.
  FTP and BinLoader: GoldHEN menu → **Server Settings** → **Enable FTP** / **Enable BinLoader**. The klog server
  starts on its own; GoldHEN's on-screen klog option isn't needed. Rest mode keeps
  GoldHEN loaded; a full power-off unloads it until you load it again.
- Home Assistant on the same LAN (tested with 2026.10). If HA and the PS4 are on different VLANs, allow HA → PS4 on
  TCP 2121 (plus FTP's passive data ports), 3232, 9090 and 9295, and UDP 987 and 9295.
- For **wake from rest**: on the PS4, *Settings → Power Saving Settings → Set Features Available in Rest Mode* → turn on
  **Stay Connected to the Internet** and **Enable Turning On PS4 from Network**.
- For **pairing without PSN**: [Apollo Save Tool](https://github.com/bucanero/apollo-ps4) on the PS4 (it shows the
  user's account ID and can activate an offline account).
- For **restoring saves**: Apollo Save Tool as well (restores from its Fake USB folder).

## Install

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories** → paste this repository's URL, type **Integration** → Add.
2. Download **PS4 GoldHEN**, then restart Home Assistant.

### Manual

Copy `custom_components/ps4_goldhen` into your HA config's `custom_components` folder and restart Home Assistant.

### Coming from Tech-Morph's version

Both use the domain `ps4_goldhen`, so this one replaces Tech-Morph's in place.
- Your config entry and entity IDs carry over.
- 0.9.0's FTP Status, power and Console ID sensors stop getting data, and its Restart button shows as unavailable.
  Disable or delete them on the device page.
- If the entry won't load, delete it and add the console again.

## Add a console

1. Turn the PS4 on and load GoldHEN with FTP enabled.
2. **Settings → Devices & services → Add integration → PS4 GoldHEN.**
3. Enter the PS4's IP address. Setup only checks FTP.
4. HA asks **Wake the PS4 from Home Assistant?** Choose **Pair now** (next section) or **Skip, finish setup**. You can
   pair later.
5. The last dialog names the device after the console (*Settings → System → System Information → Name* on the PS4)
   and asks for an area. Rename it there if you like: the entity IDs follow that name.

Give the PS4 a fixed IP (a DHCP reservation in your router) if you can. Repeat for each console.

## Pair for wake from rest (optional)

Pairing makes HA a Remote Play device of the PS4, the same as the Remote Play app or chiaki-ng, but no other app is
needed. After pairing, the media player's **Turn on** wakes the PS4 from rest mode, and games can start from rest.

**Before you start:**
- The PS4 is **on** (not in rest mode) and GoldHEN is running.
- The rest mode settings from [Requirements](#requirements) are turned on.
- You have the **account ID** of a user on that PS4. Look it up first: the PS4's pairing screen has to stay open until
  pairing finishes.
  - **No PSN (typical with GoldHEN):** open Apollo Save Tool → **User Tools → Activate PS4 Accounts**. Each user shows
    as `USERNAME (account ID)`; the ID is the 16 characters in brackets. If the user isn't activated yet, activate it
    there (offline account). Don't use the all-zero ID (`0000000000000000`) Apollo shows prefilled.
  - **PSN user:** not tested yet. HA also accepts a PSN account ID as base64 (12 characters ending in `=`) or as the
    decimal number.

**Steps:**
1. In HA: **Settings → Devices & services → PS4 GoldHEN → Configure → Pair with the PS4** (or **Pair now** during
   setup).
2. Enter the account ID and submit. HA remembers it for next time.
3. On the PS4: **Settings → Remote Play Connection Settings** → turn on **Enable Remote Play** → **Add Device**. The
   PS4 shows an 8-digit number.
4. Type the number into HA and submit **while Add Device is still open**. Leaving that screen, or waiting more than 5
   minutes, cancels the pairing.
5. Done. The media player now has **Turn on**. Test it: put the PS4 in rest mode, wait for the media player to show
   off, then turn it on from HA.

The keys from pairing are stored in the config entry's options in HA, nowhere else.

**If pairing fails:**

| HA says | Do this |
|---|---|
| No answer from the PS4 | The PS4 must be on (not in rest) and reachable from HA. Check the IP and the VLAN rules above. |
| The PS4 is in rest mode | Turn it on fully, open Add Device, try again. |
| The PS4 refused the number | The number changes every time Add Device opens. Use the one on screen now. |
| The PS4 doesn't know this account ID | The ID must belong to a user on this PS4: signed in to PSN, or activated in Apollo. |
| Remote Play is busy | Close other Remote Play sessions (PS Remote Play app, chiaki-ng) and try again. |
| Pairing failed | The HA log has the PS4's reason code. Please include it in an issue. |

**Turn on does nothing?** If HA says the PS4 isn't on the network, check the rest mode settings from
[Requirements](#requirements): with *Stay Connected to the Internet* off, a resting PS4 drops off the network and looks
fully off.

**Fully off, not rest?** Network wake only works from rest mode. *Wake entity* (same settings page) can point at a
button or script that wakes the PS4 another way, for example an ESP32 Bluetooth wake. After a full power-off you still
have to load GoldHEN again before its features come back.

## Entities

Per console, on its device page:

| Entity | What it is |
|---|---|
| Media player | Main control: state, game, cover, turn on / off, start a game. `off` covers rest and fully off; the `power_state` attribute says which. |
| Status | Playing / Home screen / Rest mode / Off. Use this in automations. |
| Current game | The running game's name as plain text. |
| Game cover | The running game's cover as an image entity. |
| Close game | Closes the running game. |
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

## The panel

**PS4 GoldHEN** in the sidebar, admins only. Pick the console at the top.

| Tab | What it does |
|---|---|
| FTP | Browse the PS4's files: upload, download, rename, delete, edit text files. Uploads over ~16 MB fail for now. |
| Payloads | Send a `.bin` / `.elf` from `/config/ps4/payloads` on the HA host. |
| Klog | The console's live log. |
| Saves | **On PS4** (what the console has, compared with HA) and **In HA** (HA's copies + older versions). Back up, send back to the PS4, download, delete from HA. Copies go to `/share/PS4 Saves/<console>/<user>/`. Saves of a game that is open are never copied. |
| Screenshots | View, sync to HA, delete on the PS4 (asks first). |
| Play time | Today, last 7 / 30 days and all time, plus a per-game table. Counts while a game is on screen. |

Saves and Screenshots are **Off** per console until you turn them on, and nothing runs on a schedule: HA reads or
copies only when you press a button. Write an automation if you want it automatic. Each has a size limit for HA's
copies (defaults: 5 GB saves, 2 GB screenshots).

**Restoring a save:** In HA → tick the save → **Send to PS4**. HA puts it in Apollo's Fake USB folder; on the PS4 open
Apollo Save Tool → the USB saves list → **Copy save game to HDD**.

## Payloads

**The integration's own payloads** do rest mode, game launch / close, live temperatures, fan control and console info.
HA sends them itself; you never pick them. They are built from the C source in [`payload_src/`](payload_src) with
John Törnblom's [ps4-payload-dev SDK](https://github.com/ps4-payload-dev/sdk) and are GPL-3.0-or-later. Each folder's
README has the build steps and the SHA256 of the shipped binary.

**Bundled payloads** (from Tech-Morph's version, listed in
[`bundled_payloads/README.md`](custom_components/ps4_goldhen/bundled_payloads/README.md)) are copied to
`/config/ps4/payloads` at setup and only run when you send one from the Payloads tab. Their sources and supported
firmwares aren't documented here. Many payloads only work on the firmwares they were built for, so only send one you
know supports your firmware. ymmv.

The `ps4_goldhen.send_payload` action sends a file from `/config/ps4/payloads` to one of your consoles (other hosts
are refused):

```yaml
action: ps4_goldhen.send_payload
data:
  payload_file: your-payload.bin   # a file name inside /config/ps4/payloads, no paths
  ps4_host: 192.0.2.10             # needed when you have more than one console
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
- A console that freezes still shows as playing, and its play time keeps counting until HA notices it's gone.
- Going to rest from a game shows Home screen for about 13 seconds before Rest mode.
- The panel's FTP upload fails above ~16 MB.

## Security and privacy

- Everything talks to the PS4 directly on your LAN. The only outside traffic is game cover art when the console's
  database lists a Sony CDN URL for it (HA's picture proxy fetches it).
- The panel, its WebSocket commands and the file download / upload URLs are for HA admins only.
- `send_payload` only takes files from `/config/ps4/payloads` and only sends to consoles set up in this integration.
- Remote Play keys and the account ID are stored only in HA's config entry (and so in HA's backups). They aren't
  written to HA's log.
- Klog lines don't go into HA's event bus, so the recorder doesn't store the console's log.

## Development

How it works inside, and the rules for not knocking GoldHEN over: [docs/PROJECT.md](docs/PROJECT.md). What changed
since Tech-Morph's 0.9.0: [docs/CHANGELOG.md](docs/CHANGELOG.md).

Tests are plain scripts, no HA install needed. They need Python 3.13+ with `cryptography` (HA ships it; the pairing
code imports it) and Node 24:

```sh
python3 -m venv .venv && . .venv/bin/activate && pip install cryptography
for t in tests/test_*.py;  do python -B "$t" || break; done
for t in tests/test_*.cjs; do node "$t"      || break; done
```

Issues and pull requests are welcome. For bug reports, include the console model, firmware, GoldHEN version, HA version
and the relevant HA log lines (remove keys and account IDs first). Taking this further, a fork, a rewrite, a "third
stack" on top: please do, that's why it's MIT.

Most of this fork's code was written by Claude Code (Anthropic's AI) from prompts, and tested on real consoles. Review
it like any other code. Much of the time spent was correcting issues it created (I swear it's by design). If you spot
anything that's redundant or a result of corrections, please point it out.

Full disclosure: I'm not a prompt engineer, I'm not a dev, I'm not even a professional. I'm just a person with an
Anthropic sub; anyone could have done this with some time, patience and the right attitude. Better or worse? At this
point the variables are the calendar date and the model. If you want to build on it with the same method, by all means,
please do so. Just make sure you prompt it to credit the ones who went before you, the real ones. Without their source
data, this isn't possible. So thanks, genuinely. GG.

## Credits

- **[SiSTRo](https://github.com/SiSTR0) and the [GoldHEN](https://github.com/GoldHEN/GoldHEN) contributors**, first
  and most of all: GoldHEN's FTP, BinLoader and klog servers are what this integration talks to, and its Remote Play
  Enabler is why a console without PSN can pair at all.
- **[Tech-Morph](https://github.com/Tech-Morph/HA-PS4-GoldHEN-Integration)**: the original integration this fork
  builds on (MIT), and the bundled payload collection.
- **[Daniel Leong (playactor)](https://github.com/dhleong/playactor)** (ISC): the Remote Play pairing and login are
  ported from it.
- **[John Törnblom (ps4-payload-dev SDK)](https://github.com/ps4-payload-dev/sdk)** (GPL-3.0-or-later): builds this
  integration's payloads.
- **[Bucanero (Apollo Save Tool)](https://github.com/bucanero/apollo-ps4)**: offline accounts, Fake USB restore, save
  names.
- **[chiaki](https://git.sr.ht/~thestr4ng3r/chiaki)** by Florian Märkl and
  **[chiaki-ng](https://github.com/chiaki-ng/chiaki-ng)** (AGPL-3.0): the open Remote Play clients this pairing and
  wake were checked against. No chiaki code is used, but the Remote Play key tables are the same bytes as chiaki's.
- **[LightningMods](https://github.com/LightningMods)** (Itemzflow), **[drakmor](https://github.com/drakmor/fan_target)**,
  **Zer0xFF**, **Marice**, OpenOrbis, PS5SX2 and the PS4 Developer Wiki: research the code follows, no code copied.

What each one contributed is in [CREDITS.md](CREDITS.md).

## License

MIT for the integration: see [LICENSE](LICENSE) (Tech-Morph's copyright kept). The payloads built from `payload_src/`
are GPL-3.0-or-later, and the Remote Play code ported from playactor is ISC: see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Not affiliated with or endorsed by Sony Interactive Entertainment.
