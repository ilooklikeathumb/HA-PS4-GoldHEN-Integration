# launch-app payload

Starts or closes a title on the PS4. Home Assistant writes the title ID into the ELF and sends it to GoldHEN's
BinLoader (`launch.py`): for `media_player.select_source`, `media_player.play_media` (type `game` or `app`), a tile
in the media browser, the "Close application" button, the `ps4_goldhen.close_title` service, and after HA's wake +
login to bring back the suspended game.
One shot: nothing stays resident.

- **The slot:** the ELF holds `PS4GH_TITLE_ID_` + NUL (16 bytes, exactly once). HA replaces it: bytes 0-9 = title ID +
  NUL (all NUL = no title), byte 10 = `K` = close the running game first, `R` (no title) = resume only, or `C` =
  close this title only.
- **Resume:** if the big app (`sceSystemServiceGetAppIdOfBigApp`) is a game and its title ID
  (`sceLncUtilGetAppTitleId`) is the picked one, nothing is closed: `sceLncUtilLaunchApp` on it answers `0x8094000c`
  and ShellCore resumes that process and gives it the screen (a tile press). `R` does the same for whatever game is
  the big app; none → `resume -> no game suspended`.
- **Close:** `sceSystemServiceKillApp(sceSystemServiceGetAppIdOfBigApp(), -1, 0, 0)`, then up to 10 s for the old app
  to go. Itemzflow's "Close game" makes the same call.
- **Close one title (`C`):** `sceLncUtilGetAppId(titleId)` → its app ID (`0xffffffff` = not running), then the same
  `sceSystemServiceKillApp(appId, -1, 0, 0)`, `sceLncUtilKillApp(appId)` if that one refuses, then up to 10 s for it
  to go. Works on apps too: an app is never the big app, so the big-app close leaves it open. System titles (`NPXS…`)
  are refused: the Internet Browser runs inside the home screen's app (NPXS20001), so its app ID is the home screen's.
- **Launch:** `sceLncUtilLaunchApp(titleId, NULL, &param)` with `user_id` = foreground user (else the first logged-in
  one), `check_flag` = 2 (SkipSystemUpdateCheck), as Itemzflow does. On `0x80940010` (another app still running)
  after a close, it retries 5 × 1 s.
- **klog** (through `/dev/console`, falling back to klog_puts): `ps4_goldhen launch: <title ID> -> 0x<code> (user
  0x<id>)`, `ps4_goldhen launch: close 0x<app> -> 0x<code>`, `ps4_goldhen launch: close <title ID> 0x<app> ->
  0x<code> (lnc 0x<code>), closed` (or `-> not running (0x<code>)`), `ps4_goldhen launch: resume <title ID> 0x<app> ->
  0x<code> (user 0x<id>)`, or `… -> no user logged in (…)` with the user-service results.

Codes seen live: `0x6000xxxx` = started (the app ID); `0x80940010` = another game is running (without the
close, the console then asks "The following application will close" and starts it after OK); `0x8094000c` = this
title is already running (a suspended or background one is then resumed and brought to the screen).

## Build

Same SDK and steps as [`../rest-mode/README.md`](../rest-mode/README.md) (ps4-payload-dev/sdk **v0.9**), then:

```console
make -C payload_src/launch-app PS4_PAYLOAD_SDK=$PWD/.toolchain/sdk/ps4-payload-sdk install
```

Build output: `launch-app.elf` 90 496 bytes, sha256
`f59e444db45300d99e96bbb47d9cfdb5b66cc7b26fea354325d4cd735631c22f`.
NEEDED `libSceSystemService.sprx`, `libSceUserService.sprx`; imports `sceLncUtilLaunchApp`, `sceLncUtilGetAppTitleId`,
`sceLncUtilGetAppId`, `sceLncUtilKillApp`, `sceSystemServiceGetAppIdOfBigApp`, `sceSystemServiceKillApp`,
`sceUserServiceInitialize`, `sceUserServiceGetLoginUserIdList`, `sceUserServiceGetForegroundUser`, `open`, `write`,
`close`, `vsnprintf`, `strcmp`, `sleep`, `usleep`.

## License

`main.c` and `Makefile` are MIT like the rest of this repo. The built ELF links the SDK's GPL-3+ runtime, so the binary
is GPL-3+. Its full source is this folder plus the SDK release named in `../rest-mode/README.md`.
