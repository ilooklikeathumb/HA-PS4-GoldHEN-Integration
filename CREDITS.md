# Credits

This integration stands on a lot of other people's work. Thank you all.

## GoldHEN: SiSTRo

First and most of all, **[SiSTRo](https://github.com/SiSTR0)** and the GoldHEN contributors.
**[GoldHEN](https://github.com/GoldHEN/GoldHEN)** is what makes any of this possible: the FTP server, BinLoader and
klog server the integration talks to, rest-mode support that keeps it loaded through rest, and the Remote Play
Enabler that lets a console without PSN pair with Remote Play at all.

## playactor: Daniel Leong

HA's Remote Play pairing and login are a Python port of Daniel Leong's
[playactor](https://github.com/dhleong/playactor) (ISC), key handling included. It's what lets HA pair with a PS4 by
itself, without another Remote Play app. License notice: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## chiaki and chiaki-ng

[chiaki](https://git.sr.ht/~thestr4ng3r/chiaki) by Florian Märkl was the first open-source PlayStation Remote Play
client, and [chiaki-ng](https://github.com/chiaki-ng/chiaki-ng) carries it on (AGPL-3.0-only, with an OpenSSL
exception). playactor, and so this integration, builds on what they figured out.

- The DDP wake and status packets, the Remote Play retry timing and one pairing test were checked against chiaki-ng.
- The Remote Play key tables (`rp_keys.py`, `rp_regist.py`, `tests/rp_ps5_keys.py`) are the same bytes as chiaki's
  `lib/src/rpcrypt.c`. chiaki published them first (October 2020); playactor added them in March 2021, crediting
  chiaki, and they came here through playactor. The console accepts only these exact values, so every Remote Play
  client carries the same ones. No chiaki code is used.

## Code this integration uses

| Who | What | How it's used |
|---|---|---|
| [Tech-Morph](https://github.com/Tech-Morph/HA-PS4-GoldHEN-Integration) | The original integration (MIT) | This is a fork of it: the FTP browser, payload sender, klog listener, game names from `app.db`, and the bundled payload collection. |
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
