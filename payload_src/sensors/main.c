// SPDX-License-Identifier: GPL-3.0-or-later
/* PS4 GoldHEN integration: live sensors payload. Resident.
 *
 * Prints one klog line every 30 s with the CPU temperature, the hottest SoC sensor, SoC sensor 0, the fan duty and
 * the fan threshold, plus this build's version:
 *   ps4_goldhen sensors: cpu=68;soc=88;soc0=82;duty=482;thr=83;ver=2
 * HA's klog listener reads it for the CPU temperature / SoC temperature / Fan duty sensors and the Fan target
 * temperature slider, and drops the line, so it never reaches the Klog tab or the recorder. A field the console
 * didn't give is left out. Duty is out of 1024. `thr` = /dev/icc_fan channel 0, byte 5 of its 28-byte object
 * (read-only GET; the fan payload in payload_src/fan/ sets it). `ver` lets HA replace a copy from an older build
 * (no `ver` = build 1).
 *
 * Home Assistant sends this ELF to GoldHEN's BinLoader (TCP 9090) when the "Live temperatures" switch is on and the
 * console has come up. It keeps running through rest mode; a power-off ends it.
 *
 * Run file: at start it writes its own token to /data/ps4_goldhen/sensors.run, and every 5 s it checks the file
 * still holds that token. A newer copy overwrites the token (so only one copy runs), and ps4-sensors-stop.elf deletes
 * the file (the switch's off). Either way this copy prints state=stopped and exits.
 *
 * Probe (PS4 Pro, FW 11.00): every call returned 0 on the home screen and in games. CPU 68 -> 88 °C, all 16 SoC
 * sensors real (54 -> 88 °C), duty 154 -> 504. The duty call writes 2 + 8 bytes: (uint16_t *duty, uint64_t
 * *chassis), the same as PS5 homebrew. All three are in libkernel_sys (-lkernel_sys).
 *
 * Lines go out through /dev/console, which klog delivers at once (klog_puts lines can sit for minutes); klog_puts is
 * the fallback.
 */
#include <fcntl.h>
#include <stdint.h>
#include <sys/ioctl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <ps4/klog.h>

#define RUN_DIR     "/data/ps4_goldhen"
#define RUN_FILE    RUN_DIR "/sensors.run"
#define MARKER      "ps4_goldhen sensors: "
#define INTERVAL    30   // s between klog lines
#define CHECK       5    // s between run-file checks: how soon a stop or a newer copy takes over
#define SOC_SENSORS 16
#define DUTY_SCALE  1024
#define VERSION     2    // + thr. HA resends when a line carries an older one
#define FAN_DEVICE        "/dev/icc_fan"
#define FAN_GET_AUTOSERVO 0xC01C8F08UL  // 28-byte object, byte 2 = channel, byte 5 = threshold °C

int sceKernelGetCpuTemperature(int *);
int sceKernelGetSocSensorTemperature(int, int *);
int sceKernelGetCurrentFanDuty(uint16_t *, uint64_t *);

static void
say(const char *line) {
  char buf[192];
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
sane(int celsius) {
  return celsius > 0 && celsius < 125;
}

static int
fan_threshold(void) {
  uint8_t obj[32];  // the kernel writes exactly 28 bytes (measured)
  int fd, rc;

  if((fd=open(FAN_DEVICE, O_RDONLY, 0)) < 0) {
    return -1;
  }
  memset(obj, 0, sizeof(obj));  // channel 0
  rc = ioctl(fd, FAN_GET_AUTOSERVO, obj);
  close(fd);
  return rc ? -1 : obj[5];
}

static int
still_current(const char *token) {
  char buf[64];
  size_t n;
  FILE *f;

  if(!(f=fopen(RUN_FILE, "r"))) {
    return 0;
  }
  n = fread(buf, 1, sizeof(buf) - 1, f);
  fclose(f);
  buf[n] = 0;
  return !strcmp(buf, token);
}

static void
report(void) {
  char line[160] = MARKER;
  const char *sep = "";
  int t, hottest = -1, first = -1;
  uint16_t duty[8];    // the kernel writes 2 bytes here (measured); room to spare
  uint64_t chassis[2]; // and 8 here

  t = 0;
  if(!sceKernelGetCpuTemperature(&t) && sane(t)) {
    snprintf(line + strlen(line), sizeof(line) - strlen(line), "%scpu=%d", sep, t);
    sep = ";";
  }

  for(int s=0; s<SOC_SENSORS; s++) {
    t = 0;
    if(!sceKernelGetSocSensorTemperature(s, &t) && sane(t)) {
      if(t > hottest) {
        hottest = t;
      }
      if(!s) {
        first = t;
      }
    }
  }
  if(hottest > 0) {
    snprintf(line + strlen(line), sizeof(line) - strlen(line), "%ssoc=%d", sep, hottest);
    sep = ";";
  }
  if(first > 0) {
    snprintf(line + strlen(line), sizeof(line) - strlen(line), "%ssoc0=%d", sep, first);
    sep = ";";
  }

  memset(duty, 0, sizeof(duty));
  memset(chassis, 0, sizeof(chassis));
  if(!sceKernelGetCurrentFanDuty(duty, chassis) && duty[0] <= DUTY_SCALE) {
    snprintf(line + strlen(line), sizeof(line) - strlen(line), "%sduty=%u", sep, duty[0]);
    sep = ";";
  }
  if(sane(t=fan_threshold())) {
    snprintf(line + strlen(line), sizeof(line) - strlen(line), "%sthr=%d", sep, t);
    sep = ";";
  }
  snprintf(line + strlen(line), sizeof(line) - strlen(line), "%sver=%d", sep, VERSION);

  say(line);
}

int
main(void) {
  char token[64];
  struct timespec now;
  FILE *f;

  clock_gettime(CLOCK_REALTIME, &now);
  snprintf(token, sizeof(token), "%ld.%09ld", (long)now.tv_sec, (long)now.tv_nsec);

  mkdir(RUN_DIR, 0777);
  if(!(f=fopen(RUN_FILE, "w"))) {
    klog_perror(MARKER "state=failed fopen " RUN_FILE);
    return 1;
  }
  fputs(token, f);
  fclose(f);
  say(MARKER "state=started");

  for(;;) {
    report();
    for(int waited=0; waited<INTERVAL; waited+=CHECK) {
      sleep(CHECK);
      if(!still_current(token)) {
        say(MARKER "state=stopped");
        return 0;
      }
    }
  }
}
