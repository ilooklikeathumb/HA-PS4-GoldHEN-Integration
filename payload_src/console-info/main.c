// SPDX-License-Identifier: GPL-3.0-or-later
/* PS4 GoldHEN integration: console-info payload.
 *
 * Prints one klog line with the console's hardware model and serial number:
 *   ps4_goldhen console-info: model=CUH-7215B B01X;serial=<serial>
 * HA's klog listener reads it, fills in the device page and keeps the line out of the Klog tab and the recorder. A
 * field the call didn't return is left out. Home Assistant sends this ELF to GoldHEN's BinLoader (TCP 9090) once per
 * console and from the "Read console info" button. One shot: nothing stays resident.
 *
 * Both calls are in libkernel_sys (hence -lkernel_sys). Tested live on two PS4 Pros (FW 11.00, 12.00).
 * No Console ID (IDPS): a payload process gets a masked one, the same placeholder, so it isn't read.
 */
#include <stdio.h>
#include <string.h>

#include <ps4/klog.h>

int sceKernelGetHwModelName(char *);
int sceKernelGetHwSerialNumber(char *);

// ';' and '=' separate the fields; anything unprintable would break the line
static void
clean(char *s) {
  for(; *s; s++) {
    if(*s < 0x20 || *s > 0x7e || *s == ';' || *s == '=') {
      *s = '_';
    }
  }
}

int
main(void) {
  char line[512] = "ps4_goldhen console-info: ";
  char model[256], serial[256];
  const char *sep = "";

  memset(model, 0, sizeof(model));
  if(!sceKernelGetHwModelName(model) && model[0]) {
    clean(model);
    snprintf(line + strlen(line), sizeof(line) - strlen(line), "%smodel=%.63s", sep, model);
    sep = ";";
  }

  memset(serial, 0, sizeof(serial));
  if(!sceKernelGetHwSerialNumber(serial) && serial[0]) {
    clean(serial);
    snprintf(line + strlen(line), sizeof(line) - strlen(line), "%sserial=%.63s", sep, serial);
  }

  klog_puts(line);
  return 0;
}
