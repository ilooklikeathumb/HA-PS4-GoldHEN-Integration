# rest-mode payload

Puts the PS4 into rest mode: `sceSystemStateMgrEnterStandby`, the same system call that Power → Enter Rest Mode makes.
Home Assistant sends the built ELF (`custom_components/ps4_goldhen/rest_payload/rest-mode.elf`) to GoldHEN's BinLoader
for `media_player.turn_off` and the "Rest mode" button (`rest.py`). The payload runs once and nothing stays resident.

- If `sceSystemStateMgrIsStandbyModeEnabled()` is false, it logs to klog and exits, so the console is never powered
  fully off (that would unload GoldHEN).
- It logs `ps4_goldhen rest-mode: entering rest mode` to klog, waits 1 s, then makes the call.

## Build

SDK: [ps4-payload-dev/sdk](https://github.com/ps4-payload-dev/sdk) **v0.9** (John Törnblom, GPL-3+),
`ps4-payload-sdk.zip` sha256 `3190e88255a72161e5ce2f22a8ae8f638a2c5a9d1ff1e0209ed692d36b4d9ec5` (GitHub release
digest, checked). It needs the host's clang + lld. The hashes below come from clang + lld 22.1.8; another version
gives different hashes.

```console
gh release download v0.9 --repo ps4-payload-dev/sdk --pattern ps4-payload-sdk.zip -D .toolchain/sdk-dl   # or download it from the release page
sha256sum .toolchain/sdk-dl/ps4-payload-sdk.zip      # must match the digest above
unzip -q .toolchain/sdk-dl/ps4-payload-sdk.zip -d .toolchain/sdk
make -C payload_src/rest-mode PS4_PAYLOAD_SDK=$PWD/.toolchain/sdk/ps4-payload-sdk install
```

Build output: `rest-mode.elf` 73 736 bytes, sha256
`4519490a7d7cf29900eb5acb61c51f30500001855c263ab5e12f14df85c3af54`. NEEDED `libSceSystemService.sprx`; imports
`sceSystemStateMgrEnterStandby`, `sceSystemStateMgrIsStandbyModeEnabled`, `sleep`.

## Firmware

At start-up the SDK's runtime patches 3 kernel addresses (mmap of SELF files) from a per-firmware table. On a
firmware it doesn't know, it skips the patches. `rest.py` only sends to firmware the SDK README marks as confirmed
(9.03, 10.00, 11.00, 12.00, 12.02 among them), read from the console's DDP `system-version`. Everything else is
refused. GoldHEN runs ELF payloads from 2.4b18.5 on; this integration needs 2.4b18.10+, which fixed ELF payloads that
use the user service (launch-app does).

## License

`main.c` and `Makefile` are MIT like the rest of this repo. The built ELF links the SDK's GPL-3+ runtime, so the binary
is GPL-3+. Its full source is this folder plus the SDK release named above.
