# Bundled payloads

Third-party payloads that came with Tech-Morph's integration, passed along unchanged. At setup HA copies them to
`/config/ps4/payloads`, skipping any name that's already there; removing one here doesn't remove HA's copy. Nothing
runs on its own: a payload is sent only from the Payloads tab or the `send_payload` action.

**Before you send one:** their authors, sources and supported firmwares aren't listed here yet. Many payloads only
work on the firmwares they were built for, and a wrong one can freeze the console until you power it off by hand.
Check the payload's own project first; the file name alone isn't enough. The panel asks before every send and shows
the console's firmware.

A successful send means BinLoader (port 9090) received the file, not that the payload ran.

The integration doesn't need any of these: rest mode, launch / close, temperatures, fan and console info use its own
payloads (main README → Payloads). To add your own, put a `.bin` or `.elf` in `/config/ps4/payloads`.

[Back to the main README](../../../README.md)
