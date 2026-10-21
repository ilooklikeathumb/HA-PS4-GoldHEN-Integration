"""PS4 GoldHEN media player: power state, running game + cover, wake and rest, game list + launch.

HA's standard entity for a game console (core ps4, playstation_network and xbox use it too). State comes from klog
lines: off (rest mode or fully off) / idle (home screen) / playing (a game). HA deprecated STANDBY, so rest
reads `off`; the `power_state` attribute (on / rest / off) tells rest from fully off.
Turn on sends Sony's DDP WAKEUP with the Remote Play regist key from the options, or presses the optional wake entity
(e.g. a Bluetooth wake button) when the console is fully off or no key is set. Turn off puts the console into rest
mode with the bundled rest payload (`rest.py`).

Rest logs every user out, so after a wake from rest the console sits on the choose-user screen. When the
pairing's RP-Key is saved, turn on then logs the paired user in with a short Remote Play session
(`rp_session.py`), so the console reaches the home screen and HA can launch games. Also when the console is already on
but klog shows the choose-user screen. Skipped when DDP says the console is on with someone logged in, and after a
wake entity press (fully off: GoldHEN isn't running until someone loads it again).

The console's games (from its app.db, `db.game_list`) are the source list and the media browser (a cover grid
in HA's Media panel: "Recently played" + "All games"). Picking one starts it with the bundled launch payload
(`launch.py`), closing the running game first.

After turn on logged the user in, the game suspended by rest comes back (`launch.async_resume_suspended`),
as it does after a controller wake. Picking a game while the console rests (or sits on the choose-user screen) wakes
it and logs in first, then launches. If the picked game is the suspended one, the payload resumes it instead of
closing it, so no progress is lost (also while on: a game left with the PS button reads as the home screen here).
Fully off can't: GoldHEN has to be loaded again first.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import _read_or_fetch_cover
from . import ftp_session
from .const import (
    DOMAIN,
    CONF_PS4_HOST,
    CONF_REGIST_KEY,
    CONF_RP_KEY,
    CONF_WAKE_ENTITY,
    SENSOR_TITLE_ID,
    SENSOR_GAME_NAME,
    POWER_ON,
    POWER_REST,
    POWER_OFF,
)
from .db import game_list
from .ddp import async_search, async_wakeup
from .entity import ps4_device_info
from .launch import async_launch_title, async_resume_suspended
from .rest import async_enter_rest
from .rp_session import RpSessionError, async_login_after_wake

_LOGGER = logging.getLogger(__name__)

_PRESS_DOMAINS = ("button", "input_button")  # other wake entities (scripts) are turned on

_BROWSE_RECENT = "recent"
_BROWSE_ALL = "all"
_RECENT_COUNT = 12

# RpSessionError reason → why the console stays on the choose-user screen
_LOGIN_FAILURES = {
    "not_on": "it didn't come on within a minute",
    "not_listening": "its Remote Play service didn't start within 45 s",
    "in_use": "Remote Play is in use (another Remote Play app connected?)",
    "passcode": "the paired user has a login passcode, which isn't supported",
    "rp_version": "this firmware's Remote Play is too old (FW 8.00 or later needed)",
    "refused": "the console refused the session (pair again in the options)",
}
# Turn on when neither klog nor DDP sees the console
WAKE_UNREACHABLE = (
    "Wake sent, but the PS4 isn't on the network. Either it's fully off (a network wake only works from rest mode), "
    "or rest mode turns its network off: on the PS4, Settings → Power Saving Settings → Set Features Available in "
    "Rest Mode → turn on Stay Connected to the Internet and Enable Turning On PS4 from Network"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([PS4MediaPlayer(hass, coordinator, entry)])


class PS4MediaPlayer(CoordinatorEntity, MediaPlayerEntity):
    _attr_has_entity_name = True
    _attr_name = None  # takes the device's name → media_player.ps4
    _attr_icon = "mdi:sony-playstation"

    def __init__(self, hass: HomeAssistant, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_media_player"
        self._attr_device_info = ps4_device_info(entry)
        self._entry_data = hass.data[DOMAIN][entry.entry_id]  # updated in place (game_map refreshes)

    def _power(self) -> str | None:
        return (self.coordinator.data or {}).get("power_state")

    def _tid(self) -> str | None:
        """Title ID of the running game; None on the home screen, in rest or off."""
        if self._power() in (POWER_REST, POWER_OFF):
            return None
        tid = (self.coordinator.data or {}).get(SENSOR_TITLE_ID)
        return tid.strip().upper() if tid else None

    def _cdn_cover(self, tid: str) -> str | None:
        url = self._entry_data.get("game_map", {}).get(tid, {}).get("cdn_cover")
        return url if url and url.startswith("http") else None

    def _games(self) -> list[tuple[str, str]]:
        """[(title ID, name)] last played first."""
        return game_list(self._entry_data.get("game_map", {}))

    async def _async_cover(self, tid: str) -> tuple[bytes | None, str | None]:
        """The console's icon0.png for a title: disk cache, else FTP (one fetch at a time)."""
        data = self._entry_data
        cover_path = data.get("game_map", {}).get(tid, {}).get("cover") or f"/user/appmeta/{tid}/icon0.png"
        try:  # under the console's FTP lock (one HA FTP session per console)
            content = await ftp_session.run(
                self.hass, data, _read_or_fetch_cover, data["host"], int(data["ftp_port"]), tid, cover_path
            )
        except Exception as err:  # noqa: BLE001 — no cover is a normal outcome (e.g. FTP off)
            _LOGGER.debug("Cover for %s unavailable: %s", tid, err)
            return None, None
        return content, "image/png"

    @property
    def state(self) -> MediaPlayerState | None:
        power = self._power()
        if power in (POWER_OFF, POWER_REST):
            return MediaPlayerState.OFF
        if self._tid():
            return MediaPlayerState.PLAYING
        if power == POWER_ON:
            return MediaPlayerState.IDLE
        return None  # unknown until klog tells (≤ 90 s after it connects)

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"power_state": self._power()}  # rest and fully off both read `off`

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        features = MediaPlayerEntityFeature.TURN_OFF  # rest payload, any GoldHEN console
        options = self._entry.options
        if options.get(CONF_REGIST_KEY) or options.get(CONF_WAKE_ENTITY):
            features |= MediaPlayerEntityFeature.TURN_ON
        if self._games():  # game list + launch, once the console's app.db has been read
            features |= (
                MediaPlayerEntityFeature.SELECT_SOURCE
                | MediaPlayerEntityFeature.BROWSE_MEDIA
                | MediaPlayerEntityFeature.PLAY_MEDIA
            )
        return features

    @property
    def source_list(self) -> list[str] | None:
        return [name for _, name in self._games()] or None

    @property
    def source(self) -> str | None:
        tid = self._tid()
        return next((name for game_tid, name in self._games() if game_tid == tid), None) if tid else None

    @property
    def media_content_type(self) -> MediaType | None:
        return MediaType.GAME if self._tid() else None

    @property
    def media_content_id(self) -> str | None:
        return self._tid()

    @property
    def media_title(self) -> str | None:
        tid = self._tid()
        if not tid:
            return None
        name = (self.coordinator.data or {}).get(SENSOR_GAME_NAME)
        return name.strip() if name and name.strip() else tid

    @property
    def media_image_url(self) -> str | None:
        tid = self._tid()
        return self._cdn_cover(tid) if tid else None

    @property
    def media_image_hash(self) -> str | None:
        # no CDN cover → the console's own icon0.png, served by async_get_media_image
        tid = self._tid()
        if tid and not self._cdn_cover(tid):
            return tid
        return super().media_image_hash

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        tid = self._tid()
        if tid is None:
            return None, None
        if self._cdn_cover(tid):
            return await super().async_get_media_image()
        return await self._async_cover(tid)

    async def async_select_source(self, source: str) -> None:
        """Start the game picked from the source list."""
        tid = next((game_tid for game_tid, name in self._games() if name == source), None)
        if tid is None:
            raise HomeAssistantError(f"No game named {source!r} on this PS4")
        await self._async_launch(tid)

    async def async_play_media(self, media_type: MediaType | str, media_id: str, **kwargs) -> None:
        """Start a game by title ID: the media browser, or `media_player.play_media` with type `game`."""
        if media_type != MediaType.GAME:
            raise HomeAssistantError(f"Only games can be played on the PS4 (got {media_type})")
        await self._async_launch(media_id)

    async def _async_launch(self, tid: str) -> None:
        """In rest (or on the choose-user screen) wake + log in first; the payload resumes the picked game
        if it's the suspended one."""
        power = self._power()
        if power == POWER_OFF:
            raise HomeAssistantError(
                "The PS4 is fully off: games start from HA in rest mode or when on (GoldHEN has to be loaded again)"
            )
        if power != POWER_REST and not self._choose_user_screen():
            await async_launch_title(self.hass, self._entry, tid)
            return
        options = self._entry.options
        if not options.get(CONF_REGIST_KEY) or not options.get(CONF_RP_KEY):
            raise HomeAssistantError(
                "Starting a game from rest needs HA's pairing with this PS4 (PS4 GoldHEN options: Configure → Pair with the PS4)"
            )
        await self._async_wake()
        await async_launch_title(self.hass, self._entry, tid, after_wake=True)

    async def async_browse_media(
        self, media_content_type: MediaType | str | None = None, media_content_id: str | None = None
    ) -> BrowseMedia:
        """The console's games as a cover grid: "Recently played" + "All games"."""
        games = self._games()
        if media_content_id in (None, ""):
            folders = [self._folder(_BROWSE_RECENT, "Recently played"), self._folder(_BROWSE_ALL, "All games")]
            return BrowseMedia(
                media_class=MediaClass.DIRECTORY, media_content_id="", media_content_type="",
                title="Games", can_play=False, can_expand=True,
                children=folders, children_media_class=MediaClass.DIRECTORY,
            )
        if media_content_id == _BROWSE_RECENT:
            picked = games[:_RECENT_COUNT]
        elif media_content_id == _BROWSE_ALL:
            picked = sorted(games, key=lambda game: game[1].casefold())
        else:
            raise BrowseError(f"Unknown PS4 folder: {media_content_id}")
        folder = self._folder(media_content_id, "Recently played" if media_content_id == _BROWSE_RECENT else "All games")
        folder.children = [
            BrowseMedia(
                media_class=MediaClass.GAME, media_content_id=tid, media_content_type=MediaType.GAME, title=name,
                can_play=True, can_expand=False, thumbnail=self.get_browse_image_url(MediaType.GAME, tid),
            )
            for tid, name in picked
        ]
        return folder

    @staticmethod
    def _folder(content_id: str, title: str) -> BrowseMedia:
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY, media_content_id=content_id, media_content_type="",
            title=title, can_play=False, can_expand=True, children_media_class=MediaClass.GAME,
        )

    async def async_get_browse_image(
        self, media_content_type: str, media_content_id: str, media_image_id: str | None = None
    ) -> tuple[bytes | None, str | None]:
        """A game tile's cover: the console's icon0.png (only titles in this console's app.db)."""
        tid = media_content_id.strip().upper()
        if tid not in self._entry_data.get("game_map", {}):
            return None, None
        return await self._async_cover(tid)

    async def async_turn_on(self) -> None:
        if not await self._async_wake():
            return
        # logged in after a wake → bring back the game rest suspended, like a controller wake does
        try:
            await async_resume_suspended(self.hass, self._entry)
        except HomeAssistantError as err:
            _LOGGER.warning("PS4 %s is on and logged in, but bringing back the suspended game failed: %s",
                            self._entry_data["host"], err)

    async def _async_wake(self) -> bool:
        """Wake (DDP, or the wake entity) and, with the RP-Key saved, log in. True = this call logged the user in."""
        options = self._entry.options
        key = options.get(CONF_REGIST_KEY)
        wake_entity = options.get(CONF_WAKE_ENTITY)
        if wake_entity and (not key or self._power() == POWER_OFF):
            domain = wake_entity.split(".", 1)[0]
            await self.hass.services.async_call(
                domain,
                "press" if domain in _PRESS_DOMAINS else "turn_on",
                {"entity_id": wake_entity},
                blocking=True,
                context=self._context,
            )
            return False
        if not key:
            raise HomeAssistantError(
                "No wake method set: pair with the PS4, or add a regist key or wake entity, in the PS4 GoldHEN options"
            )
        host = self._entry_data["host"]
        # HA says fully off and DDP gets no answer either: fully off, or resting with its network off (a console
        # with "Stay Connected to the Internet" off looks fully off and never wakes). The packet still goes out,
        # then the message says what to check.
        unreachable = self._power() == POWER_OFF and await self._async_ddp_reply(host) is None
        rp_key = options.get(CONF_RP_KEY)
        # log in after a wake from rest, or when the console is on but sits on the choose-user screen
        log_in = not unreachable and bool(rp_key) and (self._choose_user_screen() or not await self._async_ddp_on(host))
        try:
            await async_wakeup(host, key)
        except (OSError, ValueError) as err:
            raise HomeAssistantError(f"Wake packet to the PS4 failed: {err}") from err
        if unreachable:
            raise HomeAssistantError(WAKE_UNREACHABLE)
        return log_in and await self._async_log_in(host, key, rp_key)

    def _choose_user_screen(self) -> bool:
        data = self.coordinator.data or {}
        return data.get("power_state") == POWER_ON and bool(data.get("no_user_logged_in"))

    @staticmethod
    async def _async_ddp_on(host: str) -> bool:
        """DDP's answer (200 on / 620 rest), not klog's, which can lag after a deep rest."""
        reply = await PS4MediaPlayer._async_ddp_reply(host)
        return reply is not None and reply.code == 200

    @staticmethod
    async def _async_ddp_reply(host: str):
        """One DDP search (Sony's own service, UDP 987); None = no answer."""
        try:
            return await async_search(host, timeout=1.5)
        except OSError:
            return None

    async def _async_log_in(self, host: str, key: str, rp_key: str) -> bool:
        """Log the paired user in once the console is on (≈ 15-25 s after the wake). False = another
        call is already logging in (that one carries on)."""
        lock: asyncio.Lock = self._entry_data.setdefault("rp_login_lock", asyncio.Lock())
        if lock.locked():
            _LOGGER.debug("Remote Play login already running for %s", host)
            return False
        async with lock:
            try:
                took = await async_login_after_wake(host, key, rp_key)
            except RpSessionError as err:
                _LOGGER.warning("PS4 %s woke up, but the Remote Play login failed: %s", host, err)
                why = _LOGIN_FAILURES.get(err.reason, "the Remote Play login failed")
                raise HomeAssistantError(
                    f"The PS4 woke up but stays on the choose-user screen: {why}. Pick the user with a controller."
                ) from err
        _LOGGER.info("PS4 %s: paired user logged in via Remote Play, %.0f s after the wake", host, took)
        return True

    async def async_turn_off(self) -> None:
        """Rest mode; the state follows once klog reports MAIN_ON_STANDBY."""
        await async_enter_rest(self.hass, self._entry)
