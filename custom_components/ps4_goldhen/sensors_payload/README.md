# ps4-sensors.elf, ps4-sensors-stop.elf

`ps4-sensors.elf` stays running and prints CPU / SoC temperature and fan duty to klog every 30 s while the "Live
temperatures" switch is on. `ps4-sensors-stop.elf` ends it (`live_sensors.py`).

- **Source:** [`payload_src/sensors/`](../../../payload_src/sensors/): C source, Makefile, build steps and the SHA256 of
  each shipped ELF.
- **Built with:** John Törnblom's [ps4-payload-dev SDK](https://github.com/ps4-payload-dev/sdk) v0.9.
- **License:** GPL-3.0-or-later. The source files are MIT like the rest of this repo, but the built ELFs link the SDK's
  GPL-3+ runtime, so the binaries are GPL-3+. Their complete source is that folder plus the SDK release above. License
  text: [`payload_src/COPYING`](../../../payload_src/COPYING).
