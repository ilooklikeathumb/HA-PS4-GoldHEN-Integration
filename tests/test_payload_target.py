"""`send_payload` goes to one of the integration's own consoles, never a guess or a stranger."""
import ast
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ps4_payload_target", ROOT / "custom_components/ps4_goldhen/payload_target.py")
pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pt)

CONSOLE_B = {"ps4_host": "192.0.2.189", "binloader_port": 9090}
SECOND = {"ps4_host": "192.0.2.121", "binloader_port": 9021}


def target(consoles, host=None):
    return pt.payload_target(consoles, host, host_key="ps4_host", port_key="binloader_port", default_port=9090)


class PayloadTargetTests(unittest.TestCase):
    def test_named_console_wins_with_its_own_port(self):
        self.assertEqual(target([CONSOLE_B, SECOND], "192.0.2.121"), ("192.0.2.121", 9021))
        self.assertEqual(target([CONSOLE_B, SECOND], " 192.0.2.189 "), ("192.0.2.189", 9090))

    def test_single_console_needs_no_host(self):
        self.assertEqual(target([SECOND]), ("192.0.2.121", 9021))
        self.assertEqual(target([{"ps4_host": "192.0.2.5"}]), ("192.0.2.5", 9090))  # port default

    def test_several_consoles_and_no_host_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Several PS4s"):
            target([CONSOLE_B, SECOND])

    def test_unknown_host_refused(self):
        for host in ("192.0.2.99", "example.com", "192.0.2.12"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "not a PS4 set up"):
                    target([CONSOLE_B, SECOND], host)

    def test_nothing_set_up(self):
        with self.assertRaises(ValueError):
            target([])


class ButtonsNameTheirConsoleTests(unittest.TestCase):
    """A button that calls send_payload must say which PS4 — without ps4_host the call fails once 2 consoles are set up.

    No button does today: the Restart button is gone and Rest mode sends its own payload to its own entry.
    This guards the next one.
    """

    def test_every_button_send_payload_call_passes_its_own_host(self):
        tree = ast.parse((ROOT / "custom_components/ps4_goldhen/button.py").read_text())
        calls = [
            c for c in ast.walk(tree)
            if isinstance(c, ast.Call) and any(isinstance(a, ast.Constant) and a.value == "send_payload" for a in c.args)
        ]
        for c in calls:
            data = next(a for a in c.args if isinstance(a, ast.Dict))
            keys = {k.value for k in data.keys if isinstance(k, ast.Constant)}
            with self.subTest(line=c.lineno):
                self.assertIn("ps4_host", keys)
                self.assertIn("self._entry.data[CONF_PS4_HOST]", ast.unparse(data))


if __name__ == "__main__":
    unittest.main()
