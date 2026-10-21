from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import TYPE_CHECKING

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    DOMAIN,
    CONF_PS4_HOST,
    CONF_BINLOADER_PORT,
    CONF_CONSOLE_NAME,
    CONF_FTP_PORT,
    CONF_REGIST_KEY,
    CONF_WAKE_ENTITY,
    CONF_RP_KEY,
    CONF_ACCOUNT_ID,
    CONF_PIN,
    DEFAULT_PS4_HOST,
    DEFAULT_BINLOADER_PORT,
    DEFAULT_FTP_PORT,
    TCP_PROBE_TIMEOUT,
)
from .ddp import async_search, parse_regist_key
from .rp_regist import RegistError, async_register, parse_account_id, parse_pin

if TYPE_CHECKING:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

_LOGGER = logging.getLogger(__name__)

# RegistError reasons with their own message in strings.json (others → pair_failed)
_PAIR_ERRORS = {"no_console", "resting", "no_search_reply", "wrong_pin", "invalid_account", "in_use", "no_reply"}


def _schema(ps4_host: str = DEFAULT_PS4_HOST) -> vol.Schema:
    """Return the config/options schema with the current host pre-filled.

    No port fields: GoldHEN's FTP (2121) and BinLoader (9090) ports are fixed. An entry keeps the ports stored in it.
    """
    return vol.Schema({vol.Required(CONF_PS4_HOST, default=ps4_host): str})


def _wake_schema(options: dict) -> vol.Schema:
    """How media_player turn_on wakes the console. Both optional; current values pre-filled."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_REGIST_KEY, description={"suggested_value": options.get(CONF_REGIST_KEY)}
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Optional(
                CONF_WAKE_ENTITY, description={"suggested_value": options.get(CONF_WAKE_ENTITY)}
            ): EntitySelector(EntitySelectorConfig(domain=["button", "input_button", "script"])),
        }
    )


class _PairSteps:
    """Remote Play pairing in two steps, shared by setup and Configure.

    1. `account`: a user's account ID (Apollo / PSN help). It comes first because finding it means using the console,
       and leaving the PS4's Add Device screen cancels the pairing.
    2. `pin`: the 8-digit number from Add Device; submitting it pairs (rp_regist.async_register).
    """

    _account_id: str | None = None

    def _pair_host(self) -> str:
        raise NotImplementedError

    def _pair_defaults(self) -> dict:
        """Saved options to pre-fill from (Configure); empty during setup."""
        return {}

    def _pair_done(self, paired: dict) -> FlowResult:
        raise NotImplementedError

    async def async_step_account(self, user_input=None, errors: dict | None = None) -> FlowResult:
        errors = dict(errors or {})
        if user_input is not None:
            try:
                self._account_id = parse_account_id(user_input.get(CONF_ACCOUNT_ID, ""))
            except ValueError:
                errors[CONF_ACCOUNT_ID] = "invalid_account_id"
            else:
                return await self.async_step_pin()
        suggested = (
            (user_input or {}).get(CONF_ACCOUNT_ID) or self._account_id or self._pair_defaults().get(CONF_ACCOUNT_ID)
        )
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT_ID, description={"suggested_value": suggested}): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"host": self._pair_host()},
        )

    async def async_step_pin(self, user_input=None) -> FlowResult:
        if self._account_id is None:
            return await self.async_step_account()
        host = self._pair_host()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                pin = parse_pin(user_input.get(CONF_PIN, ""))
            except ValueError:
                errors[CONF_PIN] = "invalid_pin"
            else:
                try:
                    registration = await async_register(host, pin, self._account_id)
                except RegistError as err:
                    _LOGGER.warning("Remote Play pairing with %s failed: %s", host, err)
                    if err.reason == "invalid_account":
                        return await self.async_step_account(errors={"base": "pair_invalid_account"})
                    errors["base"] = f"pair_{err.reason}" if err.reason in _PAIR_ERRORS else "pair_failed"
                except OSError as err:
                    _LOGGER.warning("Remote Play pairing with %s failed: %s", host, err)
                    errors["base"] = "pair_no_console"
                else:
                    _LOGGER.info(
                        "Paired with %s (%s) for Remote Play; regist key saved", registration.nickname or "the PS4", host
                    )
                    return self._pair_done(
                        {
                            CONF_REGIST_KEY: registration.regist_key,
                            CONF_RP_KEY: registration.rp_key,
                            CONF_ACCOUNT_ID: self._account_id,
                        }
                    )
        return self.async_show_form(
            step_id="pin",
            data_schema=vol.Schema({vol.Required(CONF_PIN): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))}),
            errors=errors,
            description_placeholders={"host": host},
        )


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def _tcp_reachable(host: str, port: int, timeout: float = TCP_PROBE_TIMEOUT) -> bool:
    """Bound the setup probe and socket cleanup; never swallow cancellation."""
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        return True
    except (OSError, TimeoutError, ValueError):
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=0.15)
            except (OSError, TimeoutError):
                pass


async def _console_name(host: str) -> str | None:
    """The name set on the console (Settings → System → System Information), from DDP. It answers in rest too.
    None if it doesn't answer: the device is then called "PS4"."""
    try:
        reply = await async_search(host)
    except OSError:
        return None
    name = (reply.headers.get("host-name") or "").strip() if reply is not None else ""
    return name[:64] or None


class PS4GoldHENConfigFlow(_PairSteps, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow for PS4 GoldHEN."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "PS4GoldHENOptionsFlow":
        """Expose the options (re-configure) flow."""
        return PS4GoldHENOptionsFlow(config_entry)

    _data: dict

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Connection step of the setup wizard (then the wake menu)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_PS4_HOST].strip()
            ftp_port = DEFAULT_FTP_PORT
            binloader_port = DEFAULT_BINLOADER_PORT

            # Validate: probe FTP (safe to poke). BinLoader is NOT probed on
            # setup because repeated connections can destabilise GoldHEN.
            if not await _tcp_reachable(host, ftp_port):
                errors["base"] = "ftp_not_reachable"

            if not errors:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                self._data = {
                    CONF_PS4_HOST: host,
                    CONF_BINLOADER_PORT: binloader_port,
                    CONF_FTP_PORT: ftp_port,
                }
                # the console's own name, so several consoles don't all come out as "PS4"; HA's last setup dialog
                # can still rename it
                if name := await _console_name(host):
                    self._data[CONF_CONSOLE_NAME] = name
                # offer Remote Play pairing (wake) before creating the entry
                return await self.async_step_wake()

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(),
            errors=errors,
        )

    async def async_step_wake(self, user_input=None) -> FlowResult:
        """Menu — pair now or finish (HA needs a step method for every shown step ID)."""
        return self.async_show_menu(step_id="wake", menu_options=["account", "finish"])

    async def async_step_finish(self, user_input=None) -> FlowResult:
        """Skip pairing (Configure → Pair later)."""
        return self.async_create_entry(title=f"PS4 GoldHEN ({self._data[CONF_PS4_HOST]})", data=self._data)

    def _pair_host(self) -> str:
        return self._data[CONF_PS4_HOST]

    def _pair_done(self, paired: dict) -> FlowResult:
        return self.async_create_entry(title=f"PS4 GoldHEN ({self._pair_host()})", data=self._data, options=paired)

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> FlowResult:
        """A set-up console showed up at a new IP → move its entry there.

        HA's DHCP watcher calls this for consoles whose device carries their MAC (manifest `registered_devices`;
        console_info puts the MAC there from DDP). It runs for every sighting, each HA start included, so the
        usual outcome is a quiet abort with no network traffic. Hostnames typed in setup are left alone.
        """
        mac = dr.format_mac(discovery_info.macaddress)
        ip = discovery_info.ip
        registry = dr.async_get(self.hass)
        entries = self._async_current_entries(include_ignore=False)
        # Not async_get_device(identifiers=…): deprecated in HA 2026.10, gone in 2027.8
        entry = next(
            (
                e for e in entries
                if any(
                    (DOMAIN, e.entry_id) in device.identifiers and (dr.CONNECTION_NETWORK_MAC, mac) in device.connections
                    for device in dr.async_entries_for_config_entry(registry, e.entry_id)
                )
            ),
            None,
        )
        if entry is None:
            return self.async_abort(reason="not_ps4_goldhen_console")
        old = entry.data.get(CONF_PS4_HOST, "")
        if old == ip or not _is_ip(old) or any(e.data.get(CONF_PS4_HOST) == ip for e in entries):
            return self.async_abort(reason="already_configured")

        _LOGGER.info("PS4 %s moved from %s to %s (DHCP); updating its entry", mac, old, ip)
        updates = {}
        if entry.unique_id == old:
            updates["unique_id"] = ip
        if entry.title == f"PS4 GoldHEN ({old})":  # a renamed entry keeps its name
            updates["title"] = f"PS4 GoldHEN ({ip})"
        # one update = one reload, through the entry's update listener (like the options flow)
        self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PS4_HOST: ip}, **updates)
        return self.async_abort(reason="ip_updated")


class PS4GoldHENOptionsFlow(_PairSteps, config_entries.OptionsFlow):
    """Let the user change settings after the integration is set up."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        """Menu — the settings form, or pair with the PS4 for Remote Play wake."""
        return self.async_show_menu(step_id="init", menu_options=["settings", "account"])

    async def async_step_settings(self, user_input=None) -> FlowResult:
        """Show the options form (mirrors the setup form)."""
        errors: dict[str, str] = {}
        current = self._config_entry.data

        if user_input is not None:
            host = user_input[CONF_PS4_HOST].strip()
            ftp_port = current.get(CONF_FTP_PORT, DEFAULT_FTP_PORT)
            binloader_port = current.get(CONF_BINLOADER_PORT, DEFAULT_BINLOADER_PORT)

            # probe FTP only when the target changed — saving a wake key while the PS4 rests must work
            moved = (host, ftp_port) != (current.get(CONF_PS4_HOST), current.get(CONF_FTP_PORT, DEFAULT_FTP_PORT))
            if moved and not await _tcp_reachable(host, ftp_port):
                errors["base"] = "ftp_not_reachable"

            options = {k: v for k, v in self._config_entry.options.items() if k not in (CONF_REGIST_KEY, CONF_WAKE_ENTITY)}
            if raw_key := (user_input.get(CONF_REGIST_KEY) or "").strip():
                try:
                    options[CONF_REGIST_KEY] = parse_regist_key(raw_key)
                except ValueError:
                    errors[CONF_REGIST_KEY] = "invalid_regist_key"
            if wake_entity := user_input.get(CONF_WAKE_ENTITY):
                options[CONF_WAKE_ENTITY] = wake_entity
            # the RP-Key belongs to the pairing that issued the stored key — drop it with that key
            if options.get(CONF_REGIST_KEY) != self._config_entry.options.get(CONF_REGIST_KEY):
                options.pop(CONF_RP_KEY, None)

            if not errors:
                # data + options in ONE update = one reload
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    title=f"PS4 GoldHEN ({host})",
                    data={
                        CONF_PS4_HOST: host,
                        CONF_BINLOADER_PORT: binloader_port,
                        CONF_FTP_PORT: ftp_port,
                        # the console's name from setup names the device
                        **{k: current[k] for k in (CONF_CONSOLE_NAME,) if k in current},
                    },
                    options=options,
                )
                return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="settings",
            data_schema=_schema(ps4_host=current.get(CONF_PS4_HOST, DEFAULT_PS4_HOST)).extend(_wake_schema(self._config_entry.options).schema),
            errors=errors,
        )

    def _pair_host(self) -> str:
        return self._config_entry.data.get(CONF_PS4_HOST, "")

    def _pair_defaults(self) -> dict:
        return dict(self._config_entry.options)

    def _pair_done(self, paired: dict) -> FlowResult:
        return self.async_create_entry(title="", data={**self._config_entry.options, **paired})
