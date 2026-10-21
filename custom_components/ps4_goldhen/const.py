from __future__ import annotations

DOMAIN = "ps4_goldhen"

# — Config entry keys ———————————————————————————————————————————————————————
CONF_PS4_HOST       = "ps4_host"
CONF_BINLOADER_PORT = "binloader_port"
CONF_FTP_PORT       = "ftp_port"
CONF_RPI_PORT       = "rpi_port"
CONF_KLOG_PORT      = "klog_port"
CONF_CONSOLE_NAME   = "console_name"  # the console's own name at setup (DDP host-name): the device's default name
# options — wake the console (media_player turn_on)
CONF_REGIST_KEY     = "regist_key"   # Remote Play regist key (8 hex) → DDP WAKEUP from rest
CONF_WAKE_ENTITY    = "wake_entity"  # optional button / script that wakes it another way (e.g. Bluetooth)
# options — set by the Pair step (Remote Play registration with a PIN)
CONF_RP_KEY         = "rp_key"       # 32 hex, Remote Play session key from the same pairing (login)
CONF_ACCOUNT_ID     = "account_id"   # base64 account ID of the pairing (pre-fills the next pairing)
CONF_PIN            = "pin"          # Pair form field only, never stored

# — Defaults ————————————————————————————————————————————————————————————————
DEFAULT_PS4_HOST        = "192.168.x.x"
DEFAULT_BINLOADER_PORT  = 9090
DEFAULT_FTP_PORT        = 2121
DEFAULT_RPI_PORT        = 12800
DEFAULT_KLOG_PORT       = 3232

# — Directory layout (/config/ps4/) —————————————————————————————————————————
PS4_CONFIG_DIR    = "/config/ps4"
PAYLOAD_DIR       = f"{PS4_CONFIG_DIR}/payloads"
DB_CACHE_DIR      = f"{PS4_CONFIG_DIR}/db"
COVER_CACHE_DIR   = f"{PS4_CONFIG_DIR}/covers"

TCP_PROBE_TIMEOUT = 3.0
PLATFORMS: list[str] = ["sensor", "button", "image", "media_player", "switch", "number"]

# — Sensor data keys ————————————————————————————————————————————————————————
SENSOR_CURRENT_GAME   = "current_game"
SENSOR_CPU_TEMP       = "cpu_temp"
SENSOR_SOC_TEMP       = "soc_temp"
SENSOR_TITLE_ID       = "title_id"
SENSOR_GAME_NAME      = "game_name"
SENSOR_GAME_COVER     = "cover"
SENSOR_KLOG_LAST_LINE = "klog_last_line"

# — Power sensor keys (watts from PS4StateJSON) —————————————————————————————
SENSOR_SOC_POWER   = "soc_power_w"
SENSOR_CPU_POWER   = "cpu_power_w"
SENSOR_GPU_POWER   = "gpu_power_w"
SENSOR_TOTAL_POWER = "total_power_w"

# — New PS4StateJSON v14 keys ————————————————————————————————————————————————
SENSOR_FAN_DUTY   = "fan_duty"
SENSOR_FAN_THRESHOLD = "fan_threshold"  # °C the fan works to hold (live sensors payload's `thr`)
SENSOR_FW_VERSION = "fw_version"
SENSOR_HW_MODEL   = "hw_model"
SENSOR_CONSOLE_ID = "console_id"

HOME_SCREEN     = "PlayStation Home Screen"


def signal_klog_line(entry_id: str) -> str:
    """Raw klog lines go to the panel by dispatcher, not the event bus (the recorder would store every line)."""
    return f"{DOMAIN}_klog_line_{entry_id}"

# console power state parsed from klog (SceSystemStateMgr)
POWER_ON   = "on"
POWER_REST = "rest"
POWER_OFF  = "off"

APP_DB_REMOTE        = "/system_data/priv/mms/app.db"
APP_DB_LOCAL         = f"{DB_CACHE_DIR}/{{host}}/app.db"  # one per console
DB_REFRESH_INTERVAL  = 3600  # s between FTP app.db pulls (1 h)
