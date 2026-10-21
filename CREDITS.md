# Credits

This integration stands on a lot of other people's work. Thank you all.

## GoldHEN: SiSTRo

First and most of all, **[SiSTRo](https://github.com/SiSTR0)** and the GoldHEN contributors.
**[GoldHEN](https://github.com/GoldHEN/GoldHEN)** is what makes any of this possible: the FTP server, BinLoader and
klog server the integration talks to, rest-mode support that keeps it loaded through rest, and the Remote Play
Enabler that lets a console without PSN pair with Remote Play at all.

## chiaki and chiaki-ng

[chiaki](https://git.sr.ht/~thestr4ng3r/chiaki), created by Florian Märkl, was the first open-source PlayStation
Remote Play client; [chiaki-ng](https://github.com/chiaki-ng/chiaki-ng) carries it on. Both are licensed
AGPL-3.0-only (with an OpenSSL exception).

- **No chiaki code is used.** The pairing and login code is a Python port of playactor (ISC), not of chiaki.
- **The Remote Play key tables are chiaki's bytes.** The tables and 16-byte constants in `rp_keys.py` and
  `rp_regist.py` (and the PS5 tables in `tests/rp_ps5_keys.py`) are byte-for-byte the same as chiaki's
  `lib/src/rpcrypt.c`. They reached this project through playactor. They are Sony's key material: any Remote Play
  client has to use exactly these bytes to talk to the console. chiaki published them first (October 2020, PS4
  firmware 8.0 support); playactor added them in March 2021, crediting chiaki's work in that change.
- chiaki-ng was the reference for the discovery (DDP) wake and status packets and for the Remote Play retry timing,
  and one pairing test vector was generated with chiaki.

## Code this integration uses

| Who | What | How it's used |
|---|---|---|
| [Tech-Morph](https://github.com/Tech-Morph/HA-PS4-GoldHEN-Integration) | The original integration (MIT) | This is a fork of it: the FTP browser, payload sender, klog listener, game names from `app.db`, and the bundled payload collection. |
| [Daniel Leong (playactor)](https://github.com/dhleong/playactor) | PS4 Remote Play client (ISC) | Remote Play pairing, keys and login are ported from it. Notice in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). |
| [John Törnblom (ps4-payload-dev SDK)](https://github.com/ps4-payload-dev/sdk) | PS4 payload SDK (GPL-3.0-or-later) | Builds the integration's own payloads in `payload_src/`. |

## Research the code follows (no code copied)

- [Bucanero (Apollo Save Tool)](https://github.com/bucanero/apollo-ps4): account IDs for consoles without PSN, and
  the Fake USB folder that save restore uses.
- [LightningMods](https://github.com/LightningMods): [Itemzflow](https://github.com/LightningMods/Itemzflow) (how
  to launch a game) and [PS4-daemon-writeup](https://github.com/LightningMods/PS4-daemon-writeup) (the rest mode
  calls).
- [OpenOrbis PS4 Toolchain](https://github.com/OpenOrbis/OpenOrbis-PS4-Toolchain): system state names.
- PS5SX2's `disc_launch.c` notes: resuming a suspended game instead of restarting it.
- [drakmor (fan_target)](https://github.com/drakmor/fan_target): the fan controller map and the read → change →
  write → verify rule.
- [Zer0xFF](https://gist.github.com/Zer0xFF/4aa38d836a5696ed1b6486bb8e782b4a): the PS4 Pro fan threshold and how
  long a set value lasts.
- [Marice (ps5-exporter)](https://github.com/Marice/ps5-exporter): the kernel calls for temperatures, model and
  serial.
- [PS4 Developer Wiki](https://www.psdevwiki.com/ps4/): file formats and console internals.

## Bundled payloads

The payloads in `custom_components/ps4_goldhen/bundled_payloads/` came with Tech-Morph's integration and were written
by many PS4 homebrew authors. They're passed along unchanged; credit belongs to them.

Missing someone? Please open an issue or a pull request.
