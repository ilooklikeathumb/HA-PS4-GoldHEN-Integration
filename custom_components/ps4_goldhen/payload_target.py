"""Which PS4 a `send_payload` call goes to (no HA imports, for the tests).

The call may name a host, but only one of this integration's own consoles; the BinLoader port always comes from that
console's entry. No host + one console = that console; no host + several = an error, never a guess.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


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
