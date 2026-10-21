"""Setup + Configure pairing (account step → PIN step), against minimal HA stand-ins.
The DHCP step that follows a console to a new IP (DhcpFollowTests).

Needs `cryptography` (rp_regist imports it): `uv run --no-project --with cryptography python -m unittest
tests/test_config_flow_pair.py`.
"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_config_flow_pair_test_pkg"


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class _Key(str):
    def __new__(cls, key, default=None, description=None):
        obj = str.__new__(cls, key)
        obj.default = default
        obj.description = description
        return obj


class Schema:
    def __init__(self, schema):
        self.schema = schema

    def extend(self, other):
        return Schema({**self.schema, **other})


def _require_steps(flow, *step_ids):
    """HA raises UnknownStep ("Invalid flow specified" in the UI) for a step ID without an async_step_<id>."""
    for step_id in step_ids:
        if not callable(getattr(flow, f"async_step_{step_id}", None)):
            raise AssertionError(f"{type(flow).__name__} doesn't support step {step_id}")


class ConfigFlow:
    hass = None

    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)

    def _async_current_entries(self, include_ignore=None):
        return list(self.hass.config_entries.entries)

    def async_abort(self, reason):
        return {"type": "abort", "reason": reason}

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        pass

    def async_show_menu(self, step_id, menu_options):
        _require_steps(self, step_id, *menu_options)
        return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

    def async_show_form(self, step_id, data_schema=None, errors=None, description_placeholders=None):
        _require_steps(self, step_id)
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors or {},
                "description_placeholders": description_placeholders}

    def async_create_entry(self, title, data, options=None):
        return {"type": "create_entry", "title": title, "data": data, "options": options}


class OptionsFlow:
    hass = None

    def async_show_menu(self, step_id, menu_options):
        _require_steps(self, step_id, *menu_options)
        return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

    def async_show_form(self, step_id, data_schema=None, errors=None, description_placeholders=None):
        _require_steps(self, step_id)
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors or {},
                "description_placeholders": description_placeholders}

    def async_create_entry(self, title, data):
        return {"type": "create_entry", "title": title, "data": data}


class _Selector:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


_module("voluptuous", Schema=Schema, Required=_Key, Optional=_Key, All=lambda *a: a, Coerce=lambda t: t,
        Range=lambda **kw: kw)
_module("homeassistant")
_module("homeassistant.config_entries", ConfigFlow=ConfigFlow, OptionsFlow=OptionsFlow, ConfigEntry=object)
sys.modules["homeassistant"].config_entries = sys.modules["homeassistant.config_entries"]
_module("homeassistant.core", callback=lambda f: f)
_module("homeassistant.data_entry_flow", FlowResult=dict)
_module("homeassistant.helpers")


class DeviceRegistry:
    """Devices keyed by their (domain, entry_id) identifier, like ps4_device_info registers them. No async_get_device:
    HA 2026.10 deprecated it (gone in 2027.8), so a call to it fails here."""

    def __init__(self):
        self.devices = {}


def _entries_for_config_entry(registry, entry_id):
    return [d for (_domain, eid), d in registry.devices.items() if eid == entry_id]


REGISTRY = DeviceRegistry()


def _format_mac(mac):  # HA's rule for the bare 12-hex form DHCP uses
    mac = mac.lower()
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2)) if len(mac) == 12 else mac


sys.modules["homeassistant.helpers"].device_registry = _module(
    "homeassistant.helpers.device_registry", async_get=lambda hass: REGISTRY, format_mac=_format_mac,
    CONNECTION_NETWORK_MAC="mac", async_entries_for_config_entry=_entries_for_config_entry,
)
_module("homeassistant.helpers.selector", EntitySelector=_Selector, EntitySelectorConfig=_Selector,
        TextSelector=_Selector, TextSelectorConfig=_Selector,
        TextSelectorType=types.SimpleNamespace(PASSWORD="password", TEXT="text"))

_pkg = types.ModuleType(PKG)
_pkg.__path__ = [str(COMP)]
sys.modules[PKG] = _pkg
_spec = importlib.util.spec_from_file_location(f"{PKG}.config_flow", COMP / "config_flow.py")
cf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cf
_spec.loader.exec_module(cf)
rp = sys.modules[f"{PKG}.rp_regist"]

# made-up values, never a real console's
OLD_KEY = "0a0b0c0d"
NEW_KEY = "1a2b3c4d"
RP_KEY = "00112233445566778899aabbccddeeff"
ACCOUNT_HEX = "0123456789abcdef"
ACCOUNT_B64 = rp.parse_account_id(ACCOUNT_HEX)
HOST = "192.0.2.10"


class Entry:
    def __init__(self, options=None):
        self.data = {"ps4_host": HOST, "binloader_port": 9090, "ftp_port": 2121}
        self.options = dict(options or {})


class ConfigEntries:
    def __init__(self):
        self.updates = []

    def async_update_entry(self, entry, **kwargs):
        self.updates.append(kwargs)


def make_flow(options=None):
    flow = cf.PS4GoldHENOptionsFlow(Entry(options))
    flow.hass = types.SimpleNamespace(config_entries=ConfigEntries())
    return flow


def run(coro):
    return asyncio.run(coro)


class FakeRegister:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    async def __call__(self, host, pin, account_id):
        self.calls.append((host, pin, account_id))
        if self.exc:
            raise self.exc
        return self.result


def _keys(result):
    return {str(k): k for k in result["data_schema"].schema}


class PairStepsTests(unittest.TestCase):
    """Configure → Pair: account step, then PIN step (shared _PairSteps)."""

    def setUp(self):
        self._orig = cf.async_register

    def tearDown(self):
        cf.async_register = self._orig

    def _at_pin(self, options=None, account=ACCOUNT_HEX, **fake):
        cf.async_register = register = FakeRegister(**fake)
        flow = make_flow(options)
        result = run(flow.async_step_account({"account_id": account}))
        self.assertEqual((result["type"], result["step_id"], result["errors"]), ("form", "pin", {}))
        self.assertEqual(set(_keys(result)), {"pin"})
        return flow, register

    def test_menu(self):
        result = run(make_flow().async_step_init())
        self.assertEqual(result, {"type": "menu", "step_id": "init", "menu_options": ["settings", "account"]})

    def test_account_form_prefilled_from_options(self):
        result = run(make_flow({"account_id": ACCOUNT_B64}).async_step_account())
        self.assertEqual((result["type"], result["step_id"]), ("form", "account"))
        self.assertEqual(result["description_placeholders"], {"host": HOST})
        self.assertEqual(set(_keys(result)), {"account_id"})
        self.assertEqual(_keys(result)["account_id"].description, {"suggested_value": ACCOUNT_B64})

    def test_bad_account_stays_on_account(self):
        cf.async_register = register = FakeRegister()
        result = run(make_flow().async_step_account({"account_id": "nope"}))
        self.assertEqual((result["step_id"], result["errors"]), ("account", {"account_id": "invalid_account_id"}))
        self.assertEqual(_keys(result)["account_id"].description, {"suggested_value": "nope"})
        self.assertEqual(register.calls, [])

    def test_pin_without_account_goes_to_account(self):
        result = run(make_flow().async_step_pin({"pin": "12345678"}))
        self.assertEqual(result["step_id"], "account")

    def test_success_saves_keys_keeps_other_options(self):
        reg = rp.Registration(NEW_KEY, RP_KEY, "aabbccddeeff", "PS4-test", "2")
        flow, register = self._at_pin({"regist_key": OLD_KEY, "wake_entity": "button.wake"}, result=reg)
        result = run(flow.async_step_pin({"pin": "1234 5678"}))
        self.assertEqual(register.calls, [(HOST, "12345678", ACCOUNT_B64)])
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {"regist_key": NEW_KEY, "rp_key": RP_KEY, "account_id": ACCOUNT_B64,
                                          "wake_entity": "button.wake"})

    def test_bad_pin_never_contacts_the_console(self):
        flow, register = self._at_pin()
        result = run(flow.async_step_pin({"pin": "1234"}))
        self.assertEqual((result["step_id"], result["errors"]), ("pin", {"pin": "invalid_pin"}))
        self.assertEqual(register.calls, [])

    def test_errors_stay_on_pin(self):
        for exc, error in (
            (rp.RegistError("wrong_pin"), "pair_wrong_pin"),
            (rp.RegistError("resting"), "pair_resting"),
            (rp.RegistError("no_search_reply"), "pair_no_search_reply"),
            (rp.RegistError("no_reply"), "pair_no_reply"),
            (rp.RegistError("crash"), "pair_failed"),
            (rp.RegistError("invalid_response"), "pair_failed"),
            (OSError("unreachable"), "pair_no_console"),
        ):
            with self.subTest(exc=exc):
                flow, _ = self._at_pin({"regist_key": OLD_KEY}, exc=exc)
                result = run(flow.async_step_pin({"pin": "12345678"}))
                self.assertEqual((result["type"], result["step_id"], result["errors"]), ("form", "pin", {"base": error}))

    def test_unknown_account_goes_back_to_account(self):
        flow, _ = self._at_pin(exc=rp.RegistError("invalid_account"))
        result = run(flow.async_step_pin({"pin": "12345678"}))
        self.assertEqual((result["step_id"], result["errors"]), ("account", {"base": "pair_invalid_account"}))
        self.assertEqual(_keys(result)["account_id"].description, {"suggested_value": ACCOUNT_B64})

    def test_every_error_has_text(self):
        import json
        strings = json.loads((COMP / "strings.json").read_text())
        for flow in ("config", "options"):
            errors = strings[flow]["error"]
            for reason in cf._PAIR_ERRORS | {"failed"}:
                self.assertIn(f"pair_{reason}", errors, flow)
            for key in ("invalid_pin", "invalid_account_id"):
                self.assertIn(key, errors, flow)
            self.assertEqual({"account", "pin"} - set(strings[flow]["step"]), set(), flow)
            self.assertNotIn("pair", strings[flow]["step"], flow)
        self.assertEqual(set(strings["options"]["step"]["init"]["menu_options"]), {"settings", "account"})


class SetupFlowTests(unittest.TestCase):
    """First-time setup: connection step → wake menu → account → PIN, or finish. Nothing leaves the process."""

    DATA = {"ps4_host": HOST, "binloader_port": 9090, "ftp_port": 2121}

    def setUp(self):
        self._orig = (cf.async_register, cf._tcp_reachable, cf.async_search)
        self.probed = []
        self.ddp = None  # the DDP reply: None = no answer, an exception = raised

        async def reachable(host, port, timeout=3.0):
            self.probed.append(port)
            return True

        async def search(host):
            self.probed.append(("ddp", host))
            if isinstance(self.ddp, Exception):
                raise self.ddp
            return self.ddp

        cf._tcp_reachable = reachable
        cf.async_search = search

    def tearDown(self):
        cf.async_register, cf._tcp_reachable, cf.async_search = self._orig

    def _flow_at_menu(self):
        flow = cf.PS4GoldHENConfigFlow()
        result = run(flow.async_step_user({"ps4_host": f" {HOST} "}))
        self.assertEqual(result, {"type": "menu", "step_id": "wake", "menu_options": ["account", "finish"]})
        # FTP + one DDP search (UDP 987, Sony's own service) — BinLoader (9090) is never probed
        self.assertEqual(self.probed, [2121, ("ddp", HOST)])
        return flow

    def test_form_asks_only_the_host(self):
        # GoldHEN's ports are fixed: a new entry stores 2121 / 9090 (DATA)
        result = run(cf.PS4GoldHENConfigFlow().async_step_user())
        self.assertEqual(set(_keys(result)), {"ps4_host"})

    def test_device_named_after_the_console(self):
        # without the console's own name, every console comes in as "PS4"
        self.ddp = types.SimpleNamespace(code=620, headers={"host-name": " PS4 Slim A ", "host-type": "PS4"})
        result = run(self._flow_at_menu().async_step_finish())
        self.assertEqual(result["data"], {**self.DATA, "console_name": "PS4 Slim A"})
        self.assertEqual(result["title"], f"PS4 GoldHEN ({HOST})")  # the entry title stays (DHCP follow renames it)

    def test_no_name_without_a_ddp_answer(self):
        for reply in (None, OSError("unreachable"), types.SimpleNamespace(code=200, headers={"host-name": "  "})):
            with self.subTest(reply=reply):
                self.ddp, self.probed = reply, []
                self.assertEqual(run(self._flow_at_menu().async_step_finish())["data"], self.DATA)

    def test_long_name_cut(self):
        self.ddp = types.SimpleNamespace(code=200, headers={"host-name": "x" * 100})
        self.assertEqual(run(self._flow_at_menu().async_step_finish())["data"]["console_name"], "x" * 64)

    def test_skip_pairing(self):
        result = run(self._flow_at_menu().async_step_finish())
        self.assertEqual(result, {"type": "create_entry", "title": f"PS4 GoldHEN ({HOST})", "data": self.DATA,
                                  "options": None})

    def test_pair_during_setup(self):
        flow = self._flow_at_menu()
        form = run(flow.async_step_account())
        self.assertEqual((form["step_id"], form["description_placeholders"]), ("account", {"host": HOST}))
        self.assertEqual(_keys(form)["account_id"].description, {"suggested_value": None})
        self.assertEqual(run(flow.async_step_account({"account_id": ACCOUNT_HEX}))["step_id"], "pin")
        cf.async_register = register = FakeRegister(result=rp.Registration(NEW_KEY, RP_KEY, "aabbccddeeff"))
        result = run(flow.async_step_pin({"pin": "12345678"}))
        self.assertEqual(register.calls, [(HOST, "12345678", ACCOUNT_B64)])
        self.assertEqual(result, {"type": "create_entry", "title": f"PS4 GoldHEN ({HOST})", "data": self.DATA,
                                  "options": {"regist_key": NEW_KEY, "rp_key": RP_KEY, "account_id": ACCOUNT_B64}})

    def test_retry_pin_keeps_account(self):
        flow = self._flow_at_menu()
        run(flow.async_step_account({"account_id": ACCOUNT_HEX}))
        cf.async_register = FakeRegister(exc=rp.RegistError("no_search_reply"))
        self.assertEqual(run(flow.async_step_pin({"pin": "12345678"}))["errors"], {"base": "pair_no_search_reply"})
        cf.async_register = register = FakeRegister(result=rp.Registration(NEW_KEY, RP_KEY, "aabbccddeeff"))
        self.assertEqual(run(flow.async_step_pin({"pin": "87654321"}))["type"], "create_entry")
        self.assertEqual(register.calls, [(HOST, "87654321", ACCOUNT_B64)])

    def test_setup_strings(self):
        import json
        config = json.loads((COMP / "strings.json").read_text())["config"]
        self.assertEqual(set(config["step"]["wake"]["menu_options"]), {"account", "finish"})
        self.assertIn("{host}", config["step"]["account"]["description"])


class SettingsStepTests(unittest.TestCase):
    INPUT = {"ps4_host": HOST}

    def _save(self, options, user_input):
        flow = make_flow(options)
        result = run(flow.async_step_settings({**self.INPUT, **user_input}))
        return result, flow.hass.config_entries.updates

    def test_form_step_id(self):
        result = run(make_flow().async_step_settings())
        self.assertEqual((result["type"], result["step_id"]), ("form", "settings"))
        self.assertEqual(set(_keys(result)), {"ps4_host", "regist_key", "wake_entity"})  # no port fields

    def test_stored_ports_kept(self):
        # an entry set up with other ports keeps them; the form doesn't show them
        flow = make_flow()
        flow._config_entry.data = {**flow._config_entry.data, "binloader_port": 9091, "ftp_port": 2122}
        run(flow.async_step_settings(dict(self.INPUT)))
        self.assertEqual(flow.hass.config_entries.updates[0]["data"],
                         {"ps4_host": HOST, "binloader_port": 9091, "ftp_port": 2122})

    def test_same_key_keeps_rp_key(self):
        result, updates = self._save({"regist_key": NEW_KEY, "rp_key": RP_KEY, "account_id": ACCOUNT_B64},
                                     {"regist_key": NEW_KEY})
        self.assertEqual(result["data"], {"regist_key": NEW_KEY, "rp_key": RP_KEY, "account_id": ACCOUNT_B64})
        self.assertEqual(updates[0]["options"], result["data"])

    def test_pasted_key_drops_rp_key(self):
        result, _ = self._save({"regist_key": NEW_KEY, "rp_key": RP_KEY, "account_id": ACCOUNT_B64},
                               {"regist_key": OLD_KEY})
        self.assertEqual(result["data"], {"regist_key": OLD_KEY, "account_id": ACCOUNT_B64})

    def test_cleared_key_drops_rp_key(self):
        result, _ = self._save({"regist_key": NEW_KEY, "rp_key": RP_KEY}, {})
        self.assertEqual(result["data"], {})

    def test_console_name_kept(self):
        flow = make_flow()
        flow._config_entry.data = {**flow._config_entry.data, "console_name": "PS4 Slim A", "old_key": 1}
        run(flow.async_step_settings(dict(self.INPUT)))
        self.assertEqual(flow.hass.config_entries.updates[0]["data"],
                         {**Entry().data, "console_name": "PS4 Slim A"})


# — follow a console to its new IP ———————————————————————————————————————————————————————————

CONSOLE_B_MAC = "00:00:5e:00:53:01"
CONSOLE_A_MAC = "00:00:5e:00:53:02"


class DhcpEntry:
    def __init__(self, entry_id, host, mac, title=None, unique_id=None):
        self.entry_id = entry_id
        self.data = {"ps4_host": host, "binloader_port": 9090, "ftp_port": 2121}
        self.title = title or f"PS4 GoldHEN ({host})"
        self.unique_id = unique_id or host
        if mac:
            REGISTRY.devices[("ps4_goldhen", entry_id)] = types.SimpleNamespace(
                identifiers={("ps4_goldhen", entry_id)}, connections={("mac", mac)})


class DhcpFollowTests(unittest.TestCase):
    """HA's DHCP watcher saw a set-up console's MAC (manifest registered_devices); MACs are the two Pros'."""

    def setUp(self):
        REGISTRY.devices.clear()
        self.console_b = DhcpEntry("B", "192.0.2.189", CONSOLE_B_MAC)
        self.console_a = DhcpEntry("O", "192.0.2.121", CONSOLE_A_MAC)

    def dhcp(self, ip, mac, entries=None):
        config_entries = ConfigEntries()
        config_entries.entries = entries if entries is not None else [self.console_b, self.console_a]
        flow = cf.PS4GoldHENConfigFlow()
        flow.hass = types.SimpleNamespace(config_entries=config_entries)
        info = types.SimpleNamespace(ip=ip, hostname="ps4-pro-b", macaddress=mac.replace(":", ""))
        return run(flow.async_step_dhcp(info)), config_entries.updates

    def test_moved_console_follows(self):
        result, updates = self.dhcp("192.0.2.150", CONSOLE_B_MAC)
        self.assertEqual(result, {"type": "abort", "reason": "ip_updated"})
        self.assertEqual(updates, [{
            "data": {"ps4_host": "192.0.2.150", "binloader_port": 9090, "ftp_port": 2121},
            "unique_id": "192.0.2.150",
            "title": "PS4 GoldHEN (192.0.2.150)",
        }])

    def test_same_ip_is_a_quiet_abort(self):
        # what every HA start sends: no update, no reload
        self.assertEqual(self.dhcp("192.0.2.121", CONSOLE_A_MAC), ({"type": "abort", "reason": "already_configured"}, []))

    def test_unknown_mac(self):
        result, updates = self.dhcp("192.0.2.150", "aa:bb:cc:dd:ee:ff")
        self.assertEqual((result["reason"], updates), ("not_ps4_goldhen_console", []))

    def test_device_without_mac_yet(self):
        REGISTRY.devices.clear()
        result, updates = self.dhcp("192.0.2.150", CONSOLE_B_MAC)
        self.assertEqual((result["reason"], updates), ("not_ps4_goldhen_console", []))

    def test_hostname_left_alone(self):
        named = DhcpEntry("B", "ps4-b.lan", CONSOLE_B_MAC)
        result, updates = self.dhcp("192.0.2.150", CONSOLE_B_MAC, [named, self.console_a])
        self.assertEqual((result["reason"], updates), ("already_configured", []))

    def test_renamed_title_and_custom_unique_id_kept(self):
        renamed = DhcpEntry("B", "192.0.2.189", CONSOLE_B_MAC, title="Console B PS4", unique_id="something-else")
        result, updates = self.dhcp("192.0.2.150", CONSOLE_B_MAC, [renamed, self.console_a])
        self.assertEqual(result["reason"], "ip_updated")
        self.assertEqual(updates, [{"data": {"ps4_host": "192.0.2.150", "binloader_port": 9090, "ftp_port": 2121}}])

    def test_ip_held_by_the_other_console(self):
        result, updates = self.dhcp("192.0.2.121", CONSOLE_B_MAC)
        self.assertEqual((result["reason"], updates), ("already_configured", []))

    def test_wake_key_and_pairing_survive(self):
        result, updates = self.dhcp("192.0.2.150", CONSOLE_A_MAC)
        self.assertNotIn("options", updates[0])  # options (regist key, RP-Key, account) untouched

    def test_manifest_and_strings(self):
        import json
        manifest = json.loads((COMP / "manifest.json").read_text())
        self.assertEqual(manifest["dhcp"], [{"registered_devices": True}])  # registered consoles only, no OUI match
        for name in ("strings.json", "translations/en.json"):
            aborts = json.loads((COMP / name).read_text())["config"]["abort"]
            for reason in ("already_configured", "not_ps4_goldhen_console", "ip_updated"):
                with self.subTest(file=name, reason=reason):
                    self.assertTrue(aborts.get(reason))


if __name__ == "__main__":
    unittest.main()
