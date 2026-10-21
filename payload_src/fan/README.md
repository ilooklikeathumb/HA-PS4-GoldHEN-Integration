# Fan threshold payload

`ps4-fan.elf` sets the temperature the console's own fan controller works to hold, then exits. Lower = the fan spins
up sooner and harder. Home Assistant writes the wanted °C (ASCII digits) over the 16-byte marker `PS4GH_FAN_THR__`
and sends the ELF to GoldHEN's BinLoader whenever Fan control sends a target (`fan.py`). It prints one line:

```
ps4_goldhen fan: state=set;thr=72;was=83
ps4_goldhen fan: state=failed;step=<target|open|get|set|verify>;errno=<n>[;thr=<what the console holds>]
```

- `/dev/icc_fan`, channel 0 (follows the CPU, drives the fan). GET `0xC01C8F08` → change byte 5 only → SET
  `0xC01C8F07` → GET and compare bytes 2–27. The ioctls encode a 28-byte object; the buffers are 32.
- 50–83 °C only (83 = the CUH-72xx Pros' factory target). An unreplaced marker fails at `target`.
- The value holds through game switches and survives reboots until the console is unplugged.

## Build

Same SDK and steps as [`../rest-mode/README.md`](../rest-mode/README.md) (ps4-payload-dev/sdk **v0.9**), then:

```console
make -C payload_src/fan PS4_PAYLOAD_SDK=$PWD/.toolchain/sdk/ps4-payload-sdk install
```

Built with clang 22.1.8: `ps4-fan.elf` 89 968 bytes, sha256
`7c6d664fcdb4f3ff685c77a2102fcca6f8266fcd545f1bc2e2725fd68610597a`. Imports `open`, `ioctl`, `close`, `snprintf`,
`write`.

## License

`main.c` and `Makefile` are MIT like the rest of this repo. The built ELF links the SDK's GPL-3+ runtime, so the
binary is GPL-3+. Its full source is this folder plus the SDK release.
