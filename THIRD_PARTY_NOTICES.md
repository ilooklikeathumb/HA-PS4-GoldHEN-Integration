# Third-party notices

The integration itself is MIT (see [LICENSE](LICENSE)). It includes or is built with the following.

## playactor (ISC)

`custom_components/ps4_goldhen/rp_regist.py`, `rp_keys.py` and `rp_session.py` (Remote Play pairing, keys and login)
are ported from [playactor](https://github.com/dhleong/playactor) at commit `93ee087`. `tests/rp_ps5_keys.py` holds
key tables from it. playactor declares the ISC license in its `package.json` and has no separate license file; this is
the standard ISC text with its author:

```
ISC License

Copyright (c) Daniel Leong

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

## chiaki / chiaki-ng (key tables only, no code)

The Remote Play key tables and constants in `custom_components/ps4_goldhen/rp_keys.py` and `rp_regist.py` (and the
PS5 tables in `tests/rp_ps5_keys.py`) are byte-for-byte the same as those in chiaki's `lib/src/rpcrypt.c`
([chiaki](https://git.sr.ht/~thestr4ng3r/chiaki) by Florian Märkl, continued as
[chiaki-ng](https://github.com/chiaki-ng/chiaki-ng); AGPL-3.0-only with an OpenSSL exception). They came to this
project through playactor (above), which added them in March 2021 building on chiaki's work; chiaki published them
in October 2020. They are Sony's Remote Play key material, which every compatible client has to use unchanged. No
chiaki or chiaki-ng source code is used.

## ps4-payload-dev SDK (GPL-3.0-or-later)

The integration's own payloads (`rest_payload/`, `launch_payload/`, `sensors_payload/`, `fan_payload/`,
`info_payload/` under `custom_components/ps4_goldhen/`) are built with John Törnblom's
[ps4-payload-dev SDK](https://github.com/ps4-payload-dev/sdk) and are distributed under the GNU General Public License,
version 3 or later. Their complete source and build steps are in [`payload_src/`](payload_src); the license text is
[`payload_src/COPYING`](payload_src/COPYING).

## Bundled payloads

The files in `custom_components/ps4_goldhen/bundled_payloads/` come from Tech-Morph's original integration and were
written by various PS4 homebrew authors. They are passed along unchanged; see that folder's README.

## References (no code copied)

See [CREDITS.md](CREDITS.md).
