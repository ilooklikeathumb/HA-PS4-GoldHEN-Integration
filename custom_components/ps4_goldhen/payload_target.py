"""Which PS4 and which file a `send_payload` call uses (no HA imports, for the tests).

The call may name a host, but only one of this integration's own consoles; the BinLoader port comes from that
console's entry unless the call gives one. No host + one console = that console; no host + several = an error, never a
guess. The file must sit directly inside the payloads folder.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import os


def payload_target(
    consoles: Iterable[Mapping], requested_host: str | None, *, host_key: str, port_key: str, default_port: int
) -> tuple[str, int]:
    """(host, BinLoader port) for the call; ValueError with a user-facing message if there's no single answer."""
    targets: dict[str, int] = {}
    for data in consoles:
        if host := (data.get(host_key) or "").strip():
            targets[host] = int(data.get(port_key, default_port))
    if not targets:
        raise ValueError("No PS4 GoldHEN console is set up")
    if requested := (requested_host or "").strip():
        if requested not in targets:
            raise ValueError(f"{requested} is not a PS4 set up in PS4 GoldHEN (set up: {', '.join(sorted(targets))})")
        return requested, targets[requested]
    if len(targets) > 1:
        raise ValueError(f"Several PS4s are set up ({', '.join(sorted(targets))}): set ps4_host to pick one")
    return next(iter(targets.items()))


def payload_path(payload_dir: str, payload_file: str) -> str:
    """Full path of a file directly inside payload_dir, given by its name or its full path; ValueError for any other
    path, `..` or a link that leads outside."""
    name = (payload_file or "").strip()
    if os.path.isabs(name):
        path = os.path.realpath(name)
    elif name and name not in (".", "..") and os.path.basename(name) == name:
        path = os.path.realpath(os.path.join(payload_dir, name))
    else:
        path = None
    if path is None or os.path.dirname(path) != os.path.realpath(payload_dir):
        raise ValueError(f"payload_file must be a file name in {payload_dir} (move the file there and pass its name)")
    return path
