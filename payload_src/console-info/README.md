# console-info payload

Reads the console's hardware model and serial number and prints them as one klog line:

```
ps4_goldhen console-info: model=CUH-7215B B01X;serial=<serial>
```

Home Assistant sends the built ELF (`custom_components/ps4_goldhen/info_payload/console-info.elf`) to GoldHEN's
BinLoader once per console, plus whenever the "Read console info" button is pressed (`console_info.py`). The klog
listener picks the line up for the device page and drops it. The serial never reaches the Klog tab or the recorder.
Fields the console doesn't return are left out. The payload runs once and nothing stays resident.

Calls: `sceKernelGetHwModelName` and `sceKernelGetHwSerialNumber`, both in `libkernel_sys` (linked with
`-lkernel_sys`). The PS5 SDK's hwinfo sample uses the same calls. Tested live on two PS4 Pros (FW 11.00
and 12.00): `CUH-7215B B01X` and each console's own serial.

No Console ID (IDPS). A payload process only gets a masked one: `sceKernelGetIdPs` and `sysctl machdep.idps`
returned the same placeholder. Orbis Toolbox gets the real one because it runs inside the system UI.

## Build

Same SDK and steps as [`../rest-mode/README.md`](../rest-mode/README.md) (ps4-payload-dev/sdk **v0.9**, zip sha256
`3190e882…`), then:

```console
make -C payload_src/console-info PS4_PAYLOAD_SDK=$PWD/.toolchain/sdk/ps4-payload-sdk install
```

Built with clang 22.1.8: `console-info.elf` 73 768 bytes, sha256
`e3dd54ab25bef74927d07c45d0add8ed4d6c3c625a229c53abdc88be89701b96` (a rebuild gave the same hash).

## Firmware

As with rest-mode, HA only sends it to firmware the SDK README marks as confirmed (`rest.CONFIRMED_FIRMWARE`, checked
against the console's DDP `system-version`).

## License

`main.c` and `Makefile` are MIT like the rest of this repo. The built ELF links the SDK's GPL-3+ runtime, so the binary
is GPL-3+. Its full source is this folder plus the SDK release named above.
