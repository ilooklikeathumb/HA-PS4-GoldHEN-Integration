# console-info.elf

Prints the console's hardware model and serial number as one klog line, then exits. Sent by the "Read console info"
button; the device page shows the result (`console_info.py`).

- **Source:** [`payload_src/console-info/`](../../../payload_src/console-info/): C source, Makefile, build steps and the
  SHA256 of the shipped ELF.
- **Built with:** John Törnblom's [ps4-payload-dev SDK](https://github.com/ps4-payload-dev/sdk) v0.9.
- **License:** GPL-3.0-or-later. The source files are MIT like the rest of this repo, but the built ELF links the SDK's
  GPL-3+ runtime, so the binary is GPL-3+. Its complete source is that folder plus the SDK release above. License text:
  [`payload_src/COPYING`](../../../payload_src/COPYING).
