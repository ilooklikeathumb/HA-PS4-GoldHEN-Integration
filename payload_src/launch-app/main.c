// SPDX-License-Identifier: GPL-3.0-or-later
/* PS4 GoldHEN integration: launch-app payload.
 *
 * Starts one installed game or app by title ID, the way the home screen does. Home Assistant writes the title ID into
 * `title_id` below (it replaces the 16-byte marker in the ELF) and sends the ELF to GoldHEN's BinLoader (TCP 9090).
 * One shot: nothing stays resident. The result goes to the console log as one line:
 *   ps4_goldhen launch: <title ID> -> 0x<code>
 * A code with the top bit set is an error. 0x80940010 = another game is running: the console itself then asks "The
 * following application will close" and starts this one after OK (seen live: one game → another).
 * 0x8094000c = this title is already running.
 *
 * sceLncUtilLaunchApp + LncAppParam as Itemzflow calls them (foreground user, SkipSystemUpdateCheck).
 * The app starts for the foreground user (else the first logged-in one); with nobody logged in (the "who is
 * playing?" screen) nothing starts.
 *
 * Resume: the game that was running before rest (or before the PS button) stays suspended / in the background as the
 * "big app". When the picked title is that app, it is brought back instead of closed and started fresh, so no progress
 * is lost. A resume-only marker brings back whatever game is the big app (HA sends it after a wake from rest).
 * The call is sceLncUtilLaunchApp on that same title, what pressing its tile does: it answers 0x8094000c (already
 * running) and ShellCore resumes the existing process and gives it the screen (klog `resumeApp() appId={same id}`,
 * `GameWillResume`, `AppFocusChanged [NPXS20001] -> [CUSA…]`). Tested on a PS4 Pro (FW 12.00), after an HA rest
 * + wake + Remote Play login: a game resumed at the same spot, twice. Lead from PS5SX2's disc_launch.c notes,
 * which also found SetAppFocus(id, 0) takes the pad away, so it isn't used.
 *   ps4_goldhen launch: resume <title ID> 0x6000520c -> 0x8094000c (user 0x10000001)
 *
 * Close one title: a title ID with the close-only flag closes that title, game or app, in front or in the
 * background. An app is never the big app, so the big-app close leaves it open. The title's app ID comes from
 * sceLncUtilGetAppId; the kill is the same call as the big-app close, then sceLncUtilKillApp if that one refuses.
 * Tested on FW 9.03: an app in front with a game suspended behind it (only the app closed, the game resumed in its
 * own process), an app in the background behind a game, a game in front, and a title that isn't running.
 *   ps4_goldhen launch: close <title ID> 0x<app> -> 0x<code> (lnc 0x<code>), closed
 *   ps4_goldhen launch: close <title ID> -> not running (0xffffffff)
 */
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include <ps4/klog.h>

typedef struct {
  uint32_t sz;
  uint32_t user_id;
  uint32_t app_opt;
  uint64_t crash_report;
  uint64_t check_flag;
} LncAppParam;

typedef struct {
  int32_t user_id[4];
} UserServiceLoginUserIdList;

int sceLncUtilLaunchApp(const char *, const char **, LncAppParam *);
int sceLncUtilGetAppTitleId(int, char *); // 16-byte buffer, as the Itemzflow daemon calls it
int sceLncUtilGetAppId(const char *);
int sceLncUtilKillApp(uint32_t);
int sceSystemServiceGetAppIdOfBigApp(void);
int sceSystemServiceKillApp(uint32_t, int, int, int);
int sceUserServiceInitialize(void *);
int sceUserServiceGetLoginUserIdList(UserServiceLoginUserIdList *);
int sceUserServiceGetForegroundUser(int32_t *);

// HA replaces this marker: bytes 0-9 = title ID + NUL (all NUL = no title), byte 10 = 'K' to close the running game
// first (never the picked one, see above), 'R' (no title) = resume the suspended game only, or 'C' = close this
// title only (nothing launched). Volatile so the compiler never folds the marker into the code.
volatile char title_id[16] = "PS4GH_TITLE_ID_";

#define KILL_NEEDED 0x80940010     // another game is running
#define ALREADY_RUNNING 0x8094000c // this title is running (suspended or in the background counts)

// Through /dev/console like the sensors payload: klog_puts lines can lag minutes at idle.
static void
say(const char *fmt, ...) {
  char buf[512];
  va_list ap;
  int fd, len;

  va_start(ap, fmt);
  len = vsnprintf(buf, sizeof(buf) - 1, fmt, ap);
  va_end(ap);
  if(len < 0) {
    return;
  }
  if(len > (int)sizeof(buf) - 2) {
    len = sizeof(buf) - 2;
  }
  buf[len++] = '\n';
  buf[len] = '\0';
  if((fd=open("/dev/console", O_WRONLY | O_NOCTTY | O_NONBLOCK)) >= 0) {
    if(write(fd, buf, len) == len) {
      close(fd);
      return;
    }
    close(fd);
  }
  klog_puts(buf);
}

static int
is_game(int app) {
  return (app & ~0xFFFFFF) == 0x60000000;
}

// Close the running game (Itemzflow's "Close game"), then wait up to 10 s for it to go. Unsaved progress is lost: HA
// asks for this from the Close game button, or when the user picks another game while one runs.
static void
close_running_game(void) {
  int app = sceSystemServiceGetAppIdOfBigApp();
  if(!is_game(app)) {
    say("ps4_goldhen launch: close -> no game running");
    return;
  }
  int ret = sceSystemServiceKillApp(app, -1, 0, 0);
  say("ps4_goldhen launch: close 0x%08x -> 0x%08x", app, ret);
  for(int i = 0; i < 40 && sceSystemServiceGetAppIdOfBigApp() == app; i++) {
    usleep(250000);
  }
}

// Close one title by title ID, game or app; wait up to 10 s for it to go. Unsaved progress is lost.
// Never a system title: the Internet Browser (NPXS20102) runs inside the home screen's app (NPXS20001), so its
// app ID is the home screen's.
static int
close_title(const char *tid) {
  if(!strncmp(tid, "NPXS", 4)) {
    say("ps4_goldhen launch: close %s -> system title, not closed", tid);
    return 1;
  }
  int app = sceLncUtilGetAppId(tid);
  if(!is_game(app)) { // games and apps both have 0x60… app IDs
    say("ps4_goldhen launch: close %s -> not running (0x%08x)", tid, app);
    return 0;
  }
  int ret = sceSystemServiceKillApp(app, -1, 0, 0);
  int lnc = ret ? sceLncUtilKillApp(app) : 0;
  for(int i = 0; i < 40 && sceLncUtilGetAppId(tid) == app; i++) {
    usleep(250000);
  }
  int gone = sceLncUtilGetAppId(tid) != app;
  say("ps4_goldhen launch: close %s 0x%08x -> 0x%08x (lnc 0x%08x), %s", tid, app, ret, lnc,
      gone ? "closed" : "still running");
  return !gone;
}

static int
valid_title_id(const char *s) {
  for(int i = 0; i < 9; i++) {
    if(i < 4 ? (s[i] < 'A' || s[i] > 'Z') : (s[i] < '0' || s[i] > '9')) {
      return 0;
    }
  }
  return s[9] == '\0';
}

// The foreground user, else the first logged-in one; -1 = nobody (logged with the user-service results)
static int32_t
launch_user(const char *what) {
  UserServiceLoginUserIdList users;
  struct { int32_t priority; } init = {700}; // SCE_KERNEL_PRIO_FIFO_DEFAULT
  int init_ret = sceUserServiceInitialize(&init); // fails harmlessly if the process already initialized it
  memset(&users, 0xff, sizeof(users));
  int list_ret = sceUserServiceGetLoginUserIdList(&users);
  int32_t fg = -1;
  int fg_ret = sceUserServiceGetForegroundUser(&fg);
  int32_t user = fg_ret ? -1 : fg;
  for(int i = 0; i < 4 && user == -1 && !list_ret; i++) {
    user = users.user_id[i];
  }
  if(user == -1) {
    say("ps4_goldhen launch: %s -> no user logged in (init 0x%08x, list 0x%08x %d %d %d %d, fg 0x%08x %d)",
        what, init_ret, list_ret, users.user_id[0], users.user_id[1], users.user_id[2], users.user_id[3], fg_ret, fg);
  }
  return user;
}

static int
launch_title(const char *tid, int32_t user) {
  LncAppParam param;
  memset(&param, 0, sizeof(param));
  param.sz = sizeof(param);
  param.user_id = user;
  // SkipSystemUpdateCheck, as Itemzflow does.
  param.check_flag = 2;
  return sceLncUtilLaunchApp(tid, 0, &param);
}

// bring the big app (suspended by rest, or in the background) back to the screen without restarting it
static int
resume_app(int app, const char *tid) {
  int32_t user = launch_user(tid);
  if(user == -1) {
    return 1;
  }
  int ret = launch_title(tid, user);
  say("ps4_goldhen launch: resume %s 0x%08x -> 0x%08x (user 0x%08x)", tid, app, ret, user);
  return ret == 0 || (unsigned)ret == ALREADY_RUNNING ? 0 : 1;
}

int
main(void) {
  char tid[16];
  char big_tid[16];

  for(int i = 0; i < 16; i++) {
    tid[i] = title_id[i];
  }
  tid[15] = '\0';
  int close_first = tid[10] == 'K';
  int resume_only = tid[10] == 'R';
  int close_only = tid[10] == 'C';

  int big = sceSystemServiceGetAppIdOfBigApp();
  memset(big_tid, 0, sizeof(big_tid));
  int big_known = is_game(big) && sceLncUtilGetAppTitleId(big, big_tid) == 0 && valid_title_id(big_tid);

  if(!valid_title_id(tid)) {
    if(close_first && !tid[0]) {
      close_running_game();
      return 0;
    }
    if(resume_only && !tid[0]) {
      if(!big_known) {
        say("ps4_goldhen launch: resume -> no game suspended (big app 0x%08x)", big);
        return 0;
      }
      return resume_app(big, big_tid);
    }
    say("ps4_goldhen launch: no title ID in the payload, nothing launched");
    return 1;
  }
  if(close_only) {
    return close_title(tid);
  }
  if(big_known && !strcmp(big_tid, tid)) {
    return resume_app(big, tid); // the picked game is the suspended one: never close it
  }
  if(close_first) {
    close_running_game();
  }

  int32_t user = launch_user(tid);
  if(user == -1) {
    return 1;
  }
  int ret = launch_title(tid, user);
  for(int i = 0; i < 5 && close_first && (unsigned)ret == KILL_NEEDED; i++) { // the closed game may still be exiting
    sleep(1);
    ret = launch_title(tid, user);
  }
  say("ps4_goldhen launch: %s -> 0x%08x (user 0x%08x)", tid, ret, user);
  return ret < 0;
}
