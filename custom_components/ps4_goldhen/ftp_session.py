"""One HA FTP session at a time per console.

A precaution, not a proven GoldHEN limit (a second FTP client alongside HA worked in testing). Every FTP session HA
opens to a console takes that console's FTP lock: screenshots, save backup, the cover fetch, the app.db pull and the panel's file
browser, upload and download. A panel action waits at most FTP_BUSY_WAIT for it, then reports FTP as busy.
capture_files.connect still retries a refused connect: an FTP client outside HA (on a PC) may be using the console.
"""
from __future__ import annotations

import asyncio
import ftplib
from typing import Any, Callable

from homeassistant.core import HomeAssistant

from . import capture_files as cf
from .const import DOMAIN

FTP_BUSY_WAIT = 60  # s a panel action waits for the lock before it gives up


class FTPBusy(Exception):
    """The console's FTP lock stayed taken for the whole wait."""


def lock(data: dict[str, Any]) -> asyncio.Lock:
    """The console's FTP lock (`data` = the entry's hass.data)."""
    return data.setdefault("ftp_lock", asyncio.Lock())


async def run(hass: HomeAssistant, data: dict[str, Any], func: Callable[..., Any], *args: Any,
              wait: float | None = None) -> Any:
    """func(*args) in the executor under the console's FTP lock. With `wait`: FTPBusy after that many seconds.

    The lock is released when func returns, not when the caller is cancelled: the cancelled caller's FTP thread runs on,
    and the next session must not open beside it.
    """
    ftp_lock = lock(data)
    try:
        await asyncio.wait_for(ftp_lock.acquire(), wait)
    except TimeoutError:
        raise FTPBusy(
            "The PS4's FTP is busy with another transfer from HA (a save backup, screenshot sync or the game list). "
            "Try again when it's done."
        ) from None
    try:
        job = asyncio.ensure_future(hass.async_add_executor_job(func, *args))
    except BaseException:
        ftp_lock.release()
        raise
    try:
        result = await asyncio.shield(job)
    except asyncio.CancelledError:
        job.add_done_callback(lambda _job: ftp_lock.release())  # the FTP thread runs on: release when it ends
        raise
    except BaseException:
        ftp_lock.release()
        raise
    ftp_lock.release()
    return result


async def session(hass: HomeAssistant, entry_id: str, work: Callable[[ftplib.FTP], Any]) -> Any:
    """Run work(ftp) in one FTP session under the console's FTP lock."""
    data = hass.data[DOMAIN][entry_id]
    host, port = data["host"], int(data["ftp_port"])

    def _run() -> Any:
        with cf.connect(host, port) as ftp:
            return work(ftp)

    return await run(hass, data, _run)
