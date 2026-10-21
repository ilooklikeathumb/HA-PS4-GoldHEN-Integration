// SPDX-License-Identifier: GPL-3.0-or-later
/* PS4 GoldHEN integration: sets the fan threshold. One shot.
 *
 * The threshold is the temperature the console's own fan controller works to hold: lower = the fan spins up sooner
 * and harder. Home Assistant writes the wanted °C into `target` below (it replaces the 16-byte marker in the ELF) and
 * sends the ELF to GoldHEN's BinLoader (TCP 9090) when Fan control sends a target (fan.py). The payload prints
 *   ps4_goldhen fan: state=set;thr=72;was=83
 * or state=failed;step=<target|open|get|set|verify>;errno=<n>[;thr=<what the console holds>], then exits.
 *
 * /dev/icc_fan, channel 0 (the one that follows the CPU and drives the fan). The ioctl numbers encode a 28-byte object
 * (0xC01C8F07 = 0x1c bytes); byte 2 = channel, byte 5 = threshold °C. GET it, change byte 5 only, SET, GET again and
 * compare (drakmor/fan_target's rule). Older PS4 fan payloads SET from a zeroed 10-byte buffer, which wipes the
 * channel's other fields and hands the kernel 18 bytes of stack.
 *
 * Fan probe (PS4 Pro CUH-7215B, FW 12.00): channels 0-2 answer (83 / 72 / 66 °C, stock), 3 = EIO; the kernel copies
 * exactly 28 bytes. A written value holds through game launch and exit (PS5 resets it there; the PS4 doesn't), and
 * survives reboots until the console is unplugged (Zer0xFF).
 */
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include <ps4/klog.h>

#define MARKER            "ps4_goldhen fan: "
#define FAN_DEVICE        "/dev/icc_fan"
#define FAN_SET_AUTOSERVO 0xC01C8F07UL
#define FAN_GET_AUTOSERVO 0xC01C8F08UL
#define OBJECT_SIZE       28
#define THRESHOLD_BYTE    5
#define MIN_THRESHOLD     50  // the lowest allowed; idle runs 45-70 °C; Zer0xFF's 0 sent the fan to violent speeds
#define MAX_THRESHOLD     83  // the highest factory target (CUH-72xx Pros); HA caps each model at its own

// HA replaces this marker with the wanted threshold as ASCII digits, NUL-padded. Volatile so the compiler never folds
// the marker into the code.
volatile char target[16] = "PS4GH_FAN_THR__";

static void
say(const char *line) {
  char buf[160];
  int fd, len;

  len = snprintf(buf, sizeof(buf), "%s\n", line);
  if((fd=open("/dev/console", O_WRONLY | O_NOCTTY | O_NONBLOCK)) >= 0) {
    if(write(fd, buf, len) == len) {
      close(fd);
      return;
    }
    close(fd);
  }
  klog_puts(line);
}

static int
failed(const char *step, int err, int held) {
  char line[128];

  if(held >= 0) {
    snprintf(line, sizeof(line), MARKER "state=failed;step=%s;errno=%d;thr=%d", step, err, held);
  } else {
    snprintf(line, sizeof(line), MARKER "state=failed;step=%s;errno=%d", step, err);
  }
  say(line);
  return 1;
}

static int
wanted_threshold(void) {
  int value = 0, digits = 0;

  for(int i=0; i<(int)sizeof(target) && target[i]; i++) {
    if(target[i] < '0' || target[i] > '9' || ++digits > 3) {
      return -1;
    }
    value = value * 10 + (target[i] - '0');
  }
  return digits ? value : -1;
}

static int
get_object(int fd, uint8_t obj[32]) {
  memset(obj, 0, 32);  // channel 0; the kernel writes 28 bytes
  return ioctl(fd, FAN_GET_AUTOSERVO, obj);
}

int
main(void) {
  uint8_t before[32], wanted[32], after[32];
  char line[128];
  int thr, fd;

  thr = wanted_threshold();
  if(thr < MIN_THRESHOLD || thr > MAX_THRESHOLD) {
    return failed("target", EINVAL, -1);
  }
  if((fd=open(FAN_DEVICE, O_RDONLY, 0)) < 0) {
    return failed("open", errno, -1);
  }
  if(get_object(fd, before)) {
    close(fd);
    return failed("get", errno, -1);
  }

  memcpy(wanted, before, sizeof(wanted));
  wanted[THRESHOLD_BYTE] = thr;
  if(ioctl(fd, FAN_SET_AUTOSERVO, wanted)) {
    close(fd);
    return failed("set", errno, before[THRESHOLD_BYTE]);
  }
  if(get_object(fd, after)) {
    close(fd);
    return failed("verify", errno, -1);
  }
  close(fd);
  if(memcmp(after + 2, wanted + 2, OBJECT_SIZE - 2)) {
    return failed("verify", 0, after[THRESHOLD_BYTE]);
  }

  snprintf(line, sizeof(line), MARKER "state=set;thr=%d;was=%d", after[THRESHOLD_BYTE], before[THRESHOLD_BYTE]);
  say(line);
  return 0;
}
