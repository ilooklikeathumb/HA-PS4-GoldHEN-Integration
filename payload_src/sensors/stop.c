// SPDX-License-Identifier: GPL-3.0-or-later
/* PS4 GoldHEN integration: stops the live sensors payload. One shot.
 *
 * Deletes /data/ps4_goldhen/sensors.run. The running ps4-sensors.elf checks that file every 5 s and exits when it's
 * gone (main.c). Home Assistant sends this when the "Live temperatures" switch is turned off.
 */
#include <errno.h>
#include <unistd.h>

#include <ps4/klog.h>

#define RUN_FILE "/data/ps4_goldhen/sensors.run"

int
main(void) {
  if(unlink(RUN_FILE) && errno != ENOENT) {
    klog_perror("ps4_goldhen sensors: state=stop-failed unlink " RUN_FILE);
    return 1;
  }
  klog_puts("ps4_goldhen sensors: state=stop-requested");
  return 0;
}
