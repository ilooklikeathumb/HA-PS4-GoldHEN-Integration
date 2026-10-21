# Payload icons

Put payload icon images in this folder. The panel serves them from:

```text
/api/ps4_goldhen/frontend/payload_icons/<filename>
```

The mapping is in `_payloadIconUrl()` in `ps4-goldhen-panel.js`. It uses the normalized payload name to choose an image. Use the filenames and case below.

## Current filenames

- `Linux-1gb.png`
- `Linux-2gb.png`
- `Linux-3gb.png`
- `Linux-4gb.png`
- `WebRTE.png`
- `app-dumper.png`
- `app2usb.png`
- `backup.png`
- `disable-aslr.png`
- `disable-updates.png`
- `enable-browser.png`
- `enable-updates.png`
- `exit-idu.png`
- `ftp.png`
- `history-blocker.png`
- `kernel-clock.png`
- `kernel-dumper.png`
- `module-dumper.png`
- `permanent-uart.png`
- `ps4-debug_v1.1.16.png`
- `ps4-sflash0-dumper.png`
- `pup-decrypt.png`
- `restore.png`
- `rif-renamer.png`
- `todex.png`

If a payload has no mapping, the panel uses the GoldHEN logo. If it has a mapping but the image is missing, you can still get a broken image. The panel adds a cache token to icon URLs.

This folder is for images only. Payload binaries belong in `/config/ps4/payloads` on the HA host.

[Back to the main README](../../../../README.md)
