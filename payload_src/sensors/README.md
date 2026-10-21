# Live sensors payloads

`ps4-sensors.elf` stays running on the console and prints one klog line every 30 s:

```
ps4_goldhen sensors: cpu=68;soc=88;soc0=82;duty=482;thr=83;ver=2
```

- `cpu`: `sceKernelGetCpuTemperature`, °C.
- `soc`: the hottest of the 16 `sceKernelGetSocSensorTemperature` sensors, °C. `soc0` is sensor 0 alone.
- `duty`: fan duty from `sceKernelGetCurrentFanDuty`, out of 1024. There's no RPM; the kernel only reports duty.
- `thr`: the fan threshold the console holds, °C: `/dev/icc_fan` channel 0, byte 5 of the 28-byte object
  (read-only GET `0xC01C8F08`). The Fan target temperature slider shows it; `payload_src/fan/` sets it.
- `ver`: this build (2). HA sends a new copy when a line carries an older one (build 1 had no `ver`).

A value the console doesn't give is left out. HA's klog listener reads the line for the CPU temperature, SoC
temperature and Fan duty sensors and drops it, so it never reaches the Klog tab or the recorder (`live_sensors.py`).

`ps4-sensors-stop.elf` (one shot) deletes `/data/ps4_goldhen/sensors.run`. The running copy checks that file every
5 s and exits when it's gone or holds another copy's token. So a new send replaces a running copy, and the stop ELF
ends it. HA sends the stop when the "Live temperatures" switch goes off.

On a PS4 Pro (FW 11.00) every call returned 0, on the home screen and in games. CPU 68 → 88 °C, all 16 SoC sensors
real, duty 154 → 504.

Lines go out through `/dev/console`, which klog delivers at once (`klog_puts` lines can sit for minutes); `klog_puts` is
the fallback.

## Build

Same SDK and steps as [`../rest-mode/README.md`](../rest-mode/README.md) (ps4-payload-dev/sdk **v0.9**, zip sha256
`3190e882…`), then:

```console
make -C payload_src/sensors PS4_PAYLOAD_SDK=$PWD/.toolchain/sdk/ps4-payload-sdk install
```

Built with clang 22.1.8 (a rebuild gave the same hashes):
- `ps4-sensors.elf` 74 200 bytes, sha256 `25ad529f6a336b68bef7c8361941993098ed237be68c143491ffa770c408ca42`
- `ps4-sensors-stop.elf` 73 640 bytes, sha256 `2aef17ce1c0c71689d9d4655285871952abd6a4efcb24f7a5999f4c466cb34b2`

## Firmware

As with rest-mode, HA only sends these to firmware the SDK README marks as confirmed (`rest.CONFIRMED_FIRMWARE`,
checked against the console's DDP `system-version`).

## License

`main.c`, `stop.c` and `Makefile` are MIT like the rest of this repo. The built ELFs link the SDK's GPL-3+ runtime,
so the binaries are GPL-3+. Their full source is this folder plus the SDK release named above.
