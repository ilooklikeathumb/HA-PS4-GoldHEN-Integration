// SPDX-License-Identifier: GPL-3.0-or-later
/* PS4 GoldHEN integration: rest-mode payload.
 *
 * Puts the console into rest mode, the same as Power → Enter Rest Mode on the PS4. Home Assistant sends this ELF to
 * GoldHEN's BinLoader (TCP 9090) for media_player.turn_off and the "Rest mode" button. One shot: nothing stays
 * resident. GoldHEN stays loaded through rest, so it's still loaded after a wake.
 *
 * sceSystemStateMgr* live in libSceSystemService.sprx. Signatures follow LightningMods/PS4-daemon-writeup
 * (Daemon-SystemState.h): EnterStandby takes an int (other headers declare it void — passing 0 is harmless either
 * way), IsStandbyModeEnabled returns a bool (only %al is defined, hence _Bool and not int).
 */
#include <unistd.h>

#include <ps4/klog.h>

_Bool sceSystemStateMgrIsStandbyModeEnabled(void);
int sceSystemStateMgrEnterStandby(int);

int
main(void) {
  int ret;

  if(!sceSystemStateMgrIsStandbyModeEnabled()) {
    // entering standby here could power the console fully off, which unloads GoldHEN
    klog_puts("ps4_goldhen rest-mode: rest mode is not enabled on this console, staying on");
    return 1;
  }

  klog_puts("ps4_goldhen rest-mode: entering rest mode");
  sleep(1); // let the line reach the klog stream before the state change

  if((ret=sceSystemStateMgrEnterStandby(0))) {
    klog_printf("ps4_goldhen rest-mode: sceSystemStateMgrEnterStandby failed: 0x%08x\n", ret);
  }

  return ret;
}
