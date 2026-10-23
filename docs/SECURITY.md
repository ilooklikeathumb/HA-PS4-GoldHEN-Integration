# Security and privacy

- Everything talks to the PS4 directly on your LAN; nothing goes to the internet. Game covers are the console's own
  icons, read over FTP.
- The panel, its WebSocket commands, the file download / upload URLs and the `send_payload` / `close_title` actions are
  for HA admins only. Automations can still call the actions.
- `send_payload` only takes files from `/config/ps4/payloads` and only sends to consoles set up in this integration.
- Remote Play keys and the account ID are stored only in HA's config entry (and so in HA's backups). They aren't
  written to HA's log.
- Klog lines don't go into HA's event bus, so the recorder doesn't store the console's log.

[Back to the main README](../README.md)
