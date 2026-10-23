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

- **See what's playing:** a media player per console shows the game's name and cover. A Status sensor reads Playing,
  Home screen, Rest mode, Off or Not responding (frozen).
- **Wake, rest and launch:** wake the console from rest mode, put it to rest, and start or close games and apps.
  Starting one from rest wakes the console first. Waking needs [pairing](#pair-for-wake-from-rest-optional).
- **Play time:** today, the last 7 and 30 days, and the current session.
- **Temperatures and fan:** live CPU / SoC temperature and fan duty, and a fan target temperature you choose, up to
  the model's factory target.
- **Save backup:** copy game saves to HA, older versions included, and send one back to the PS4 to restore it.
- **Screenshots:** view them in HA, sync them to HA, delete them on the PS4.
- **Sidebar panel** (admins only): your games as covers with a page per game, plus saves, screenshots, play time, an
  FTP browser, a payload sender and the live log.
- **Device page:** model, hardware revision, firmware and serial.

Every entity and panel tab in detail: [Using it](docs/USAGE.md).

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
- For **pairing**: [Apollo Save Tool](https://github.com/bucanero/apollo-ps4) on the PS4 (it shows the
  user's account ID and can activate an offline account).
- For **restoring saves**: Apollo Save Tool as well (restores from its Fake USB folder).

## Install

### HACS (custom repository)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ilooklikeathumb&repository=HA-PS4-GoldHEN-Integration&category=integration)

1. Click the button above, or add it by hand: HACS → ⋮ → **Custom repositories** → paste this repository's URL, type
   **Integration** → Add.
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

[![Open your Home Assistant instance and start setting up PS4 GoldHEN.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ps4_goldhen)

1. Turn the PS4 on and load GoldHEN with FTP enabled.
2. Click the button above, or in HA: **Settings → Devices & services → Add integration → PS4 GoldHEN.**
3. Enter the PS4's IP address. Setup only checks FTP.
4. HA asks **Wake the PS4 from Home Assistant?** Choose **Pair now** (next section) or **Skip, finish setup**. You can
   pair later.
5. The last dialog names the device after the console (*Settings → System → System Information → Name* on the PS4)
   and asks for an area. Rename it there if you like: the entity IDs follow that name.

Give the PS4 a fixed IP (a DHCP reservation in your router) if you can. Repeat for each console.

## Pair for wake from rest (optional)

Pairing makes HA a Remote Play device of the PS4, the same as the Remote Play app or chiaki-ng, but no other app is
needed. After pairing, the media player's **Turn on** wakes the PS4 from rest mode, and games can start from rest.

1. Find the **account ID** of the user to pair: on the PS4, open Apollo Save Tool → **User Tools → Activate PS4
   Accounts**. Each user shows as `USERNAME (account ID)`; the ID is the 16 characters in brackets. If the user isn't
   activated, activate it there (offline account). Ignore the all-zero ID (`0000000000000000`) Apollo shows
   prefilled. Note the ID now: leaving the PS4's pairing screen in step 4 cancels the pairing.
2. In HA: **Settings → Devices & services → PS4 GoldHEN → Configure → Pair with the PS4** (or **Pair now** during
   setup).
3. Enter the account ID and submit. HA remembers it for next time.
4. On the PS4: **Settings → Remote Play Connection Settings** → turn on **Enable Remote Play** → **Add Device**. The
   PS4 shows an 8-digit number.
5. Type the number into HA and submit **while Add Device is still open**. Leaving that screen, or waiting more than 5
   minutes, cancels the pairing.
6. Done. The media player now has **Turn on**. Test it: put the PS4 in rest mode, wait for the media player to show
   off, then turn it on from HA.

Pairing failed, or Turn on does nothing? See [Troubleshooting](docs/TROUBLESHOOTING.md).

## More

- [Using it](docs/USAGE.md): the entities, the panel, payloads,
  [keeping GoldHEN healthy](docs/USAGE.md#keep-goldhen-healthy) and [known issues](docs/USAGE.md#known-issues)
- [Troubleshooting](docs/TROUBLESHOOTING.md): pairing and wake from rest
- [Security and privacy](docs/SECURITY.md)
- [Development](docs/CONTRIBUTING.md), [how it works inside](docs/PROJECT.md), [changelog](docs/CHANGELOG.md)

## Credits

- **[SiSTRo](https://github.com/SiSTR0) and the [GoldHEN](https://github.com/GoldHEN/GoldHEN) contributors**, first
  and most of all: GoldHEN's FTP, BinLoader and klog servers are what this integration talks to, and its Remote Play
  Enabler is why a console without PSN can pair at all.
- **[Tech-Morph](https://github.com/Tech-Morph/HA-PS4-GoldHEN-Integration)**: the original integration this fork
  builds on (MIT), and the bundled payload collection.
- **[Daniel Leong (playactor)](https://github.com/dhleong/playactor)** (ISC): HA's Remote Play pairing and login are
  a Python port of it, so HA pairs with a PS4 without another Remote Play app.
- **[John Törnblom (ps4-payload-dev SDK)](https://github.com/ps4-payload-dev/sdk)** (GPL-3.0-or-later): builds this
  integration's payloads.
- **[Bucanero (Apollo Save Tool)](https://github.com/bucanero/apollo-ps4)**: offline accounts, Fake USB restore, save
  names.
- **[chiaki](https://git.sr.ht/~thestr4ng3r/chiaki)** by Florian Märkl and
  **[chiaki-ng](https://github.com/chiaki-ng/chiaki-ng)** (AGPL-3.0): the first open Remote Play client and its
  continuation, which playactor and this integration build on; pairing and wake were checked against chiaki-ng. The key tables match
  chiaki's (through playactor); no chiaki code is used.
- **[LightningMods](https://github.com/LightningMods)** (Itemzflow), **[drakmor](https://github.com/drakmor/fan_target)**,
  **Zer0xFF**, **Marice**, OpenOrbis, PS5SX2 and the PS4 Developer Wiki: research the code follows, no code copied.

What each one contributed is in [CREDITS.md](CREDITS.md).

## License

MIT for the integration: see [LICENSE](LICENSE) (Tech-Morph's copyright kept). The payloads built from `payload_src/`
are GPL-3.0-or-later, and the Remote Play code ported from playactor is ISC: see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Not affiliated with or endorsed by Sony Interactive Entertainment.
