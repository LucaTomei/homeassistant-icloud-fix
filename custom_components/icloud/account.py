"""iCloud account."""

from datetime import datetime, timedelta, timezone
import functools
import logging
import operator
import os
import re
import threading
from typing import Any

from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudException,
    PyiCloudFailedLoginException,
    PyiCloudNoDevicesException,
    PyiCloudServiceNotActivatedException,
    PyiCloudServiceUnavailable,
)
import requests
from pyicloud.services.findmyiphone import AppleDevice
from pyicloud.session import PyiCloudSession

from homeassistant.components.zone import async_active_zone
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.helpers.event import track_point_in_utc_time
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify
from homeassistant.util.async_ import run_callback_threadsafe
from homeassistant.util.dt import utcnow
from homeassistant.util.location import distance

from .const import (
    ATTR_ACCOUNT,
    ATTR_ACCOUNT_FETCH_INTERVAL,
    ATTR_BATTERY,
    ATTR_BATTERY_STATUS,
    ATTR_DEVICE_NAME,
    ATTR_DEVICE_STATUS,
    ATTR_LAST_LOCATED,
    ATTR_LOCATION_STALE,
    ATTR_LOW_POWER_MODE,
    ATTR_OWNER_NAME,
    DEVICE_BATTERY_LEVEL,
    DEVICE_BATTERY_STATUS,
    DEVICE_CLASS,
    DEVICE_DISPLAY_NAME,
    DEVICE_ID,
    DEVICE_LOCATION,
    DEVICE_LOCATION_HORIZONTAL_ACCURACY,
    DEVICE_LOCATION_LATITUDE,
    DEVICE_LOCATION_LONGITUDE,
    DEVICE_LOCATION_TIMESTAMP,
    DEVICE_LOST_MODE_CAPABLE,
    DEVICE_LOW_POWER_MODE,
    DEVICE_NAME,
    DEVICE_PERSON_ID,
    DEVICE_RAW_DEVICE_MODEL,
    DEVICE_STATUS,
    DEVICE_STATUS_CODES,
    DEVICE_STATUS_SET,
    DOMAIN,
    DEFAULT_MAX_LOCATION_AGE,
    EVENT_REAUTH_REQUIRED,
    STALE_CHECK_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Minutes to wait before retrying after a temporary (network / Apple-side) error.
RETRY_AFTER_TRANSIENT_ERROR = 5
# Failed device updates in a row before opening a new session.
MAX_FAILED_UPDATES = 3
# (connect, read) timeout in seconds for iCloud requests.
REQUEST_TIMEOUT = (10, 60)


def _with_default_timeout(method):
    """Give a pyicloud request a timeout when it has none."""

    @functools.wraps(method)
    def request(self, *args, timeout=None, **kwargs):
        if timeout is None:
            timeout = REQUEST_TIMEOUT
        return method(self, *args, timeout=timeout, **kwargs)

    request.icloud_default_timeout = True
    return request


# pyicloud never sets a timeout: a stalled connection to Apple would block the
# setup or the polling loop forever instead of being retried later.
for _name in ("request", "request_raw"):
    if not getattr(getattr(PyiCloudSession, _name), "icloud_default_timeout", False):
        setattr(
            PyiCloudSession, _name, _with_default_timeout(getattr(PyiCloudSession, _name))
        )


def is_transient_error(err: BaseException | None) -> bool:
    """Return True if an iCloud error is temporary (network, timeout, Apple 5xx/429).

    pyicloud wraps network failures (DNS not ready at boot, timeouts, Apple
    outages) into PyiCloudFailedLoginException. Those must NOT be treated as a
    wrong password, otherwise Home Assistant asks to re-authenticate after
    every restart where iCloud was not reachable for a moment.
    """
    seen: set[int] = set()
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        if isinstance(
            err,
            (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
                # Apple answered with an HTML page (maintenance, captive portal).
                requests.exceptions.InvalidJSONError,
                PyiCloudServiceUnavailable,
            ),
        ):
            return True
        if isinstance(err, PyiCloudAPIResponseException):
            code = err.code
            if isinstance(code, str) and code.isdigit():
                code = int(code)
            if isinstance(code, int):
                if code >= 500 or code == 429:
                    return True
                if 400 <= code < 500:
                    # A definitive answer from Apple (e.g. wrong password):
                    # do not look at older errors raised while handling it.
                    return False
        err = err.__cause__ or err.__context__
    return False


def apple_status_code(err: BaseException | None) -> int | None:
    """Return the first numeric status code Apple answered with, if any."""
    seen: set[int] = set()
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        if isinstance(err, PyiCloudAPIResponseException):
            code = err.code
            if isinstance(code, str) and code.isdigit():
                code = int(code)
            if isinstance(code, int):
                return code
        err = err.__cause__ or err.__context__
    return None


def is_wrong_password(err: BaseException | None) -> bool:
    """Return True if Apple rejected the Apple ID or the password (HTTP 401)."""
    return apple_status_code(err) == 401


def reset_session_files(cookie_directory: str, username: str) -> None:
    """Delete the saved iCloud session of one account, like a fresh install.

    Same file names as pyicloud (PyiCloudSession.session_path/cookiejar_path).
    """
    name = re.sub(r"\W", "", username)
    for suffix in (".session", ".cookiejar"):
        path = os.path.join(cookie_directory, name + suffix)
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as err:
            _LOGGER.error("Could not delete the iCloud session file %s: %s", path, err)
    _LOGGER.warning("Saved iCloud session of %s deleted", username)


type IcloudConfigEntry = ConfigEntry[IcloudAccount]


class IcloudAccount:
    """Representation of an iCloud account."""

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        icloud_dir: Store,
        with_family: bool,
        max_interval: int,
        gps_accuracy_threshold: int,
        config_entry: IcloudConfigEntry,
        max_location_age: int = DEFAULT_MAX_LOCATION_AGE,
        tracked_device_ids: set[str] | None = None,
    ) -> None:
        """Initialize an iCloud account."""
        self.hass = hass
        self._username = username
        self._password = password
        self._with_family = with_family
        self._fetch_interval: float = max_interval
        self._max_interval = max_interval
        self._gps_accuracy_threshold = gps_accuracy_threshold
        self._max_location_age = max_location_age
        # Devices that already have entities from this integration.
        self._tracked_device_ids = tracked_device_ids or set()

        self._icloud_dir = icloud_dir

        self.api: PyiCloudService | None = None
        self._owner_fullname: str | None = None
        self._family_members_fullname: dict[str, str] = {}
        self._devices: dict[str, IcloudDevice] = {}
        self._retried_fetch = False
        self._failed_updates = 0
        self._config_entry = config_entry
        self._reauth_pending = False
        self._shutdown = False
        self._unsub_fetch: CALLBACK_TYPE | None = None
        self._fetch_lock = threading.Lock()
        # Held while the session is used (updates) or replaced (reset,
        # shutdown), so a reset never runs in the middle of an update.
        self._api_lock = threading.RLock()

        self.listeners: list[CALLBACK_TYPE] = []

    def setup(self, raise_not_ready: bool = True) -> None:
        """Set up an iCloud account.

        raise_not_ready=True during Home Assistant setup (HA retries by itself);
        False when called from the polling loop (we reschedule ourselves).
        """
        try:
            self.api = PyiCloudService(
                self._username,
                self._password,
                self._icloud_dir.path,
                with_family=self._with_family,
                # Auto-accept updated Apple terms of service. Newer pyicloud
                # versions otherwise raise PyiCloudAcceptTermsException on login.
                accept_terms=True,
            )

            if self.api.requires_2fa and not self._try_restore_session():
                # The trusted session is really gone: the user must enter a code.
                raise PyiCloudFailedLoginException("2FA Required")  # noqa: TRY301

        except PyiCloudFailedLoginException as err:
            self._release_api()
            if is_transient_error(err):
                self._handle_transient_error(err, raise_not_ready)
                return
            # Login failed which means credentials need to be updated.
            _LOGGER.error(
                (
                    "Your password for '%s' is no longer working; Go to the "
                    "Integrations menu and click on Configure on the discovered Apple "
                    "iCloud card to login again"
                ),
                self._config_entry.data[CONF_USERNAME],
            )

            self._require_reauth()
            return
        except (
            PyiCloudAPIResponseException,
            PyiCloudServiceUnavailable,
            requests.exceptions.RequestException,
        ) as err:
            self._release_api()
            if is_transient_error(err):
                self._handle_transient_error(err, raise_not_ready)
                return
            raise

        self._reauth_pending = False

        try:
            # Gets device owners infos
            user_info = self.api.devices.user_info
            if user_info is None:
                raise PyiCloudNoDevicesException(  # noqa: TRY301
                    "No user info found in iCloud devices response"
                )
        except (PyiCloudException, requests.exceptions.RequestException) as err:
            # Find My not reachable yet (network, Apple outage) or no device:
            # retry later, never ask to re-authenticate for this.
            self._release_api()
            if isinstance(
                err,
                (
                    PyiCloudServiceNotActivatedException,
                    PyiCloudNoDevicesException,
                    PyiCloudServiceUnavailable,
                ),
            ):
                _LOGGER.error("No iCloud device found: %s", err)
            self._handle_transient_error(err, raise_not_ready)
            return

        self._owner_fullname = (
            f"{user_info.get('firstName')} {user_info.get('lastName')}"
        )

        self._family_members_fullname = {}
        if user_info.get("membersInfo") is not None:
            for prs_id, member in user_info.get("membersInfo").items():
                self._family_members_fullname[prs_id] = (
                    f"{member['firstName']} {member['lastName']}"
                )

        # Existing devices are kept (and updated) so that entities created
        # before a new session keep receiving updates.
        # The device list was just fetched (and located) by pyicloud.
        self.update_devices(refresh=False)

    def update_devices(self, refresh: bool = True) -> None:
        """Update iCloud devices."""
        if self.api is None:
            return
        _LOGGER.debug("Updating devices")

        if self.api.requires_2fa:
            try:
                restored = self._try_restore_session()
            except PyiCloudException as err:
                self._handle_transient_error(err, raise_not_ready=False)
                return
            if not restored:
                self._require_reauth()
                return

        api_devices = {}
        try:
            api_devices = self.api.devices
            if refresh:
                # pyicloud 2.5 caches the device list and refreshes it in the
                # background without locating the devices: ask Apple for fresh
                # locations, like the built-in integration does.
                api_devices.refresh(locate=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Unknown iCloud error: %s", err)
            self._failed_updates += 1
            if self._failed_updates >= MAX_FAILED_UPDATES:
                # The session looks stuck: open a new one (from the saved
                # trust token, no new code needed) on the next update.
                _LOGGER.warning(
                    "iCloud updates keep failing, opening a new session for %s",
                    self._username,
                )
                self._failed_updates = 0
                self._release_api()
            self._fetch_interval = 2
            dispatcher_send(self.hass, self.signal_device_update)
            self._schedule_next_fetch()
            return

        # Gets devices infos
        new_device = False
        for device in api_devices:
            status = device.status(DEVICE_STATUS_SET)
            device_id = status[DEVICE_ID]
            device_name = status[DEVICE_NAME]

            if (known_device := self._devices.get(device_id)) is not None:
                # A new session gives new device objects (used by the services).
                known_device.set_device(device)

            if (
                known_device is None
                and not _has_battery(status)
                and not (
                    _has_location(status) and device_id in self._tracked_device_ids
                )
            ):
                # Never tracked and no battery (a Mac, an old device...): skip
                # it, as before. Known devices are always updated, so that a
                # phone that stops reporting is not stuck on its last state; a
                # phone that is off while Home Assistant starts keeps its
                # entities (its old location then expires).
                continue

            if self._devices.get(device_id) is not None:
                # Seen device -> updating
                _LOGGER.debug("Updating iCloud device: %s", device_name)
                self._devices[device_id].update(status)
            else:
                # New device, should be unique
                _LOGGER.debug(
                    "Adding iCloud device: %s [model: %s]",
                    device_name,
                    status[DEVICE_RAW_DEVICE_MODEL],
                )
                self._devices[device_id] = IcloudDevice(self, device, status)
                self._devices[device_id].update(status)
                new_device = True

        self._failed_updates = 0
        if (
            DEVICE_STATUS_CODES.get(list(api_devices)[0][DEVICE_STATUS]) == "pending"
            and not self._retried_fetch
        ):
            _LOGGER.debug("Pending devices, trying again in 15s")
            self._fetch_interval = 0.25
            self._retried_fetch = True
        else:
            self._fetch_interval = self._determine_interval()
            self._retried_fetch = False

        dispatcher_send(self.hass, self.signal_device_update)
        if new_device:
            dispatcher_send(self.hass, self.signal_device_new)

        self._schedule_next_fetch()

    def _require_reauth(self):
        """Require the user to log in again."""
        if not self._reauth_pending:
            self.hass.add_job(self._async_fire_reauth_required)
        self._reauth_pending = True
        # Stop using the old session: it must not overwrite the session files
        # while the re-authentication flow saves the new trusted session.
        self._release_api()
        self.hass.add_job(self._config_entry.async_start_reauth, self.hass)

    @callback
    def _async_fire_reauth_required(self) -> None:
        """Let automations notify the user (e.g. on the phone).

        At boot automations are only listening once Home Assistant has started.
        """

        @callback
        def _fire(hass: HomeAssistant) -> None:
            hass.bus.async_fire(EVENT_REAUTH_REQUIRED, {ATTR_ACCOUNT: self._username})

        async_at_started(self.hass, _fire)

    def _release_api(self) -> None:
        """Drop the pyicloud session and stop its background refresh thread."""
        api, self.api = self.api, None
        if api is None:
            return
        # Use the private attribute: the public "devices" property would log in
        # to Find My again only to stop it.
        stop_event = getattr(getattr(api, "_devices", None), "stop_event", None)
        if stop_event is not None:
            stop_event.set()

    def reset_session(self) -> None:
        """Delete the saved session and ask the user to sign in again.

        Same as removing the files in .storage/icloud by hand, for when the
        session got stuck. Runs in the executor.
        """
        with self._api_lock:
            with self._fetch_lock:
                self._cancel_next_fetch()
            self._release_api()
            reset_session_files(self._icloud_dir.path, self._username)
            self._require_reauth()

    def shutdown(self) -> None:
        """Stop polling and release the iCloud session (run in the executor)."""
        with self._api_lock:
            with self._fetch_lock:
                self._shutdown = True
                self._cancel_next_fetch()
            self._release_api()

    def _try_restore_session(self) -> bool:
        """Try to restore a trusted session without asking for a new 2FA code.

        After a restart pyicloud may report "2FA required" even though a valid
        trust token is saved on disk (for example when the cached session token
        expired, or when the token-validation response does not carry the
        trusted-browser flag). Logging in again with the saved session/trust
        token restores the trusted session silently, exactly like a browser
        that was marked as "trusted".
        """
        api = self.api
        if api is None:
            return False
        session_data = api.session.data
        if not session_data.get("session_token") and not session_data.get(
            "trust_token"
        ):
            return False

        authenticate_with_token = getattr(api, "_authenticate_with_token", None)
        if authenticate_with_token is None:
            return False

        try:
            authenticate_with_token()
        except PyiCloudException as err:
            if is_transient_error(err):
                # Network / Apple-side problem: let the caller retry later
                # instead of asking the user for a new code.
                raise
            _LOGGER.debug("Could not restore the trusted iCloud session: %s", err)
            return False

        # The token login succeeded and Apple marked the session as trusted.
        if hasattr(api, "_requires_mfa"):
            api._requires_mfa = False  # noqa: SLF001

        restored = not api.requires_2fa and not api.requires_2sa
        if restored:
            _LOGGER.info(
                "iCloud session for %s restored without a new verification code",
                self._username,
            )
        return restored

    def _handle_transient_error(self, err: Exception, raise_not_ready: bool) -> None:
        """Handle a temporary iCloud error without asking to re-authenticate."""
        if raise_not_ready:
            raise ConfigEntryNotReady(
                f"iCloud temporarily unreachable, will retry: {err}"
            ) from err
        _LOGGER.warning(
            "iCloud temporarily unreachable (%s), retrying in %s minutes",
            err,
            RETRY_AFTER_TRANSIENT_ERROR,
        )
        self._fetch_interval = RETRY_AFTER_TRANSIENT_ERROR
        self._schedule_next_fetch()

    def _determine_interval(self) -> int:
        """Calculate new interval between two API fetch (in minutes)."""
        intervals = {"default": self._max_interval}
        for device in self._devices.values():
            location = device.fresh_location
            # Max interval if no (recent) location
            if location is None:
                continue

            current_zone = run_callback_threadsafe(
                self.hass.loop,
                async_active_zone,
                self.hass,
                location[DEVICE_LOCATION_LATITUDE],
                location[DEVICE_LOCATION_LONGITUDE],
                location[DEVICE_LOCATION_HORIZONTAL_ACCURACY],
            ).result()

            # Max interval if in zone
            if current_zone is not None:
                continue

            zones = (
                self.hass.states.get(entity_id)
                for entity_id in sorted(self.hass.states.entity_ids("zone"))
            )

            distances = []
            for zone_state in zones:
                if zone_state is None:
                    continue
                zone_state_lat = zone_state.attributes[DEVICE_LOCATION_LATITUDE]
                zone_state_long = zone_state.attributes[DEVICE_LOCATION_LONGITUDE]
                zone_distance = distance(
                    location[DEVICE_LOCATION_LATITUDE],
                    location[DEVICE_LOCATION_LONGITUDE],
                    zone_state_lat,
                    zone_state_long,
                )
                if zone_distance is not None:
                    distances.append(round(zone_distance / 1000, 1))

            # Max interval if no zone
            if not distances:
                continue
            mindistance = min(distances)

            # Calculate out how long it would take for the device to drive
            # to the nearest zone at 120 km/h:
            interval = round(mindistance / 2)

            # Never poll more than once per minute
            interval = max(interval, 1)

            if interval > 180:
                # Three hour drive?
                # This is far enough that they might be flying
                interval = self._max_interval

            if (
                device.battery_level is not None
                and device.battery_level <= 33
                and mindistance > 3
            ):
                # Low battery - let's check half as often
                interval = interval * 2

            intervals[device.name] = interval

        return max(
            int(min(intervals.items(), key=operator.itemgetter(1))[1]),
            self._max_interval,
        )

    def _schedule_next_fetch(self) -> None:
        with self._fetch_lock:
            # Only one pending update at a time (the "update" service and the
            # polling loop may both schedule one).
            self._cancel_next_fetch()
            if self._shutdown or self._config_entry.pref_disable_polling:
                return
            self._unsub_fetch = track_point_in_utc_time(
                self.hass,
                self.keep_alive,
                utcnow() + timedelta(minutes=self._fetch_interval),
            )

    def _cancel_next_fetch(self) -> None:
        if self._unsub_fetch is not None:
            self._unsub_fetch()
            self._unsub_fetch = None

    def keep_alive(self, now=None) -> None:
        """Keep the API alive."""
        with self._api_lock:
            if self._shutdown:
                return
            try:
                self._keep_alive()
            except Exception as err:
                # Never let the polling loop die (unexpected Apple responses,
                # pyicloud errors...): log it and try again later.
                _LOGGER.exception("Unexpected error while updating iCloud")
                self._handle_transient_error(err, raise_not_ready=False)

    def _keep_alive(self) -> None:
        if self.api is None:
            if self._reauth_pending:
                # Waiting for the user to re-authenticate: do not keep logging
                # in from scratch (that triggers Apple sign-in prompts).
                return
            self.setup(raise_not_ready=False)

        if self.api is None:
            return
        if self._shutdown:
            self._release_api()
            return

        try:
            self.api.authenticate()
        except PyiCloudException as err:
            if is_transient_error(err):
                self._handle_transient_error(err, raise_not_ready=False)
                return
            try:
                restored = self._try_restore_session()
            except PyiCloudException as restore_err:
                self._handle_transient_error(restore_err, raise_not_ready=False)
                return
            if not restored:
                _LOGGER.error("iCloud authentication failed: %s", err)
                self._require_reauth()
                return

        self.update_devices()

    def get_devices_with_name(self, name: str) -> list[Any]:
        """Get devices by name."""
        name_slug = slugify(name.replace(" ", "", 99))
        result = [
            device
            for device in self.devices.values()
            if slugify(device.name.replace(" ", "", 99)) == name_slug
        ]
        if not result:
            raise ValueError(f"No device with name {name}")
        return result

    @property
    def username(self) -> str:
        """Return the account username."""
        return self._username

    @property
    def owner_fullname(self) -> str | None:
        """Return the account owner fullname."""
        return self._owner_fullname

    @property
    def family_members_fullname(self) -> dict[str, str]:
        """Return the account family members fullname."""
        return self._family_members_fullname

    @property
    def max_location_age_option(self) -> int:
        """The max location age as configured."""
        return self._max_location_age

    @property
    def max_location_age(self) -> int:
        """Minutes after which a location is not used any more (0 = never).

        Never shorter than min_location_age(), otherwise a tracker would
        become "unknown" between two normal updates.
        """
        if not self._max_location_age:
            return 0
        return max(self._max_location_age, min_location_age(self._max_interval))

    @property
    def fetch_interval(self) -> float:
        """Return the account fetch interval."""
        return self._fetch_interval

    @property
    def devices(self) -> dict[str, Any]:
        """Return the account devices."""
        return self._devices

    @property
    def signal_device_new(self) -> str:
        """Event specific per Freebox entry to signal new device."""
        return f"{DOMAIN}-{self._username}-device-new"

    @property
    def signal_device_update(self) -> str:
        """Event specific per Freebox entry to signal updates in devices."""
        return f"{DOMAIN}-{self._username}-device-update"


class IcloudDevice:
    """Representation of a iCloud device."""

    _attr_attribution = "Data provided by Apple iCloud"

    def __init__(self, account: IcloudAccount, device: AppleDevice, status) -> None:
        """Initialize the iCloud device."""
        self._account = account

        self._device = device
        self._status = status

        self._name = self._status[DEVICE_NAME]
        self._device_id = self._status[DEVICE_ID]
        self._device_class = self._status[DEVICE_CLASS]
        self._device_model = self._status[DEVICE_DISPLAY_NAME]

        self._battery_level: int | None = None
        self._battery_status = None
        self._location = None
        self._located_at: datetime | None = None
        self._location_stamp: Any = None

        self._attrs = {
            ATTR_ACCOUNT_FETCH_INTERVAL: self._account.fetch_interval,
            ATTR_DEVICE_NAME: self._device_model,
            ATTR_DEVICE_STATUS: None,
        }
        if self._status[DEVICE_PERSON_ID]:
            self._attrs[ATTR_OWNER_NAME] = account.family_members_fullname[
                self._status[DEVICE_PERSON_ID]
            ]
        elif account.owner_fullname is not None:
            self._attrs[ATTR_OWNER_NAME] = account.owner_fullname

    def set_device(self, device: AppleDevice) -> None:
        """Use the device object of the current iCloud session."""
        self._device = device

    def update(self, status) -> None:
        """Update the iCloud device."""
        self._status = status

        self._status[ATTR_ACCOUNT_FETCH_INTERVAL] = self._account.fetch_interval

        device_status = DEVICE_STATUS_CODES.get(self._status[DEVICE_STATUS], "error")
        self._attrs[ATTR_DEVICE_STATUS] = device_status

        had_battery = self._battery_level is not None
        had_location = self._location is not None

        self._battery_status = self._status.get(DEVICE_BATTERY_STATUS)
        self._attrs[ATTR_BATTERY_STATUS] = self._battery_status
        if _has_battery(self._status):
            self._battery_level = int(self._status[DEVICE_BATTERY_LEVEL] * 100)
            self._attrs[ATTR_BATTERY] = self._battery_level
            self._attrs[ATTR_LOW_POWER_MODE] = self._status.get(DEVICE_LOW_POWER_MODE)
        else:
            # Apple does not know the battery (phone off, no network): do not
            # keep showing the last level as if it were current.
            self._battery_level = None
            self._attrs.pop(ATTR_BATTERY, None)
            self._attrs.pop(ATTR_LOW_POWER_MODE, None)

        # The location does not depend on the battery: Apple can still report
        # where a phone is when it does not know its battery.
        if _has_location(self._status):
            self._update_location(self._status[DEVICE_LOCATION])

        # A battery or a location seen for the first time may need a new entity
        # (battery sensor, tracker). Sent once the values are stored.
        if (not had_battery and self._battery_level is not None) or (
            not had_location and self._location is not None
        ):
            dispatcher_send(self._account.hass, self._account.signal_device_new)

    def _update_location(self, location: dict[str, Any]) -> None:
        """Keep the newest location reported by Apple."""
        stamp = location.get(DEVICE_LOCATION_TIMESTAMP)
        if stamp is not None and stamp == self._location_stamp:
            return  # the same location as before: it does not get younger
        self._location_stamp = stamp
        located_at = _location_time(location)
        if self._located_at is None or located_at >= self._located_at:
            self._location = location
            self._located_at = located_at

    def play_sound(self) -> None:
        """Play sound on the device."""
        if self._account.api is None:
            return

        self._account.api.authenticate()
        _LOGGER.debug("Playing sound for %s", self.name)
        self.device.play_sound()

    def display_message(self, message: str, sound: bool = False) -> None:
        """Display a message on the device."""
        if self._account.api is None:
            return

        self._account.api.authenticate()
        _LOGGER.debug("Displaying message for %s", self.name)
        self.device.display_message("Subject not working", message, sound)

    def lost_device(self, number: str, message: str) -> None:
        """Make the device in lost state."""
        if self._account.api is None:
            return

        self._account.api.authenticate()
        if self._status[DEVICE_LOST_MODE_CAPABLE]:
            _LOGGER.debug("Make device lost for %s", self.name)
            self.device.lost_device(number, message, None)
        else:
            _LOGGER.error("Cannot make device lost for %s", self.name)

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._device_id

    @property
    def name(self) -> str:
        """Return the Apple device name."""
        return self._name

    @property
    def device(self) -> AppleDevice:
        """Return the Apple device."""
        return self._device

    @property
    def device_class(self) -> str:
        """Return the Apple device class."""
        return self._device_class

    @property
    def device_model(self) -> str:
        """Return the Apple device model."""
        return self._device_model

    @property
    def battery_level(self) -> int | None:
        """Return the Apple device battery level."""
        return self._battery_level

    @property
    def battery_status(self) -> str | None:
        """Return the Apple device battery status."""
        return self._battery_status

    @property
    def location(self) -> dict[str, Any] | None:
        """Return the last location reported by Apple, however old."""
        return self._location

    @property
    def located_at(self) -> datetime | None:
        """When Apple located the device for the last time."""
        return self._located_at

    @property
    def location_stale(self) -> bool:
        """Whether the last location is too old to be used."""
        max_age = self._account.max_location_age
        if not max_age or self._located_at is None:
            return False
        return utcnow() - self._located_at > timedelta(minutes=max_age)

    @property
    def fresh_location(self) -> dict[str, Any] | None:
        """The location, or None when there is none or it is too old."""
        if self._location is None or self.location_stale:
            return None
        return self._location

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the attributes."""
        return dict(self._attrs)

    @property
    def location_attributes(self) -> dict[str, Any]:
        """Age of the location (tracker only: it re-checks it every few minutes)."""
        if self._located_at is None:
            return {}
        return {
            ATTR_LAST_LOCATED: self._located_at.isoformat(),
            ATTR_LOCATION_STALE: self.location_stale,
        }


def min_location_age(max_interval: float) -> int:
    """Shortest usable max location age: two update intervals plus a margin.

    Each update usually gets the location located during the previous one,
    and updates can be late (slow answers, retries after an error).
    """
    return int(2 * max_interval) + STALE_CHECK_INTERVAL


def _has_battery(status: dict[str, Any]) -> bool:
    """Whether Apple reports a battery level for the device."""
    return (
        status.get(DEVICE_BATTERY_STATUS) not in (None, "Unknown")
        and status.get(DEVICE_BATTERY_LEVEL) is not None
    )


def _has_location(status: dict[str, Any]) -> bool:
    """Whether Apple reports coordinates for the device."""
    location = status.get(DEVICE_LOCATION)
    return bool(
        location
        and location.get(DEVICE_LOCATION_LATITUDE) is not None
        and location.get(DEVICE_LOCATION_LONGITUDE) is not None
    )


def _location_time(location: dict[str, Any]) -> datetime:
    """When the location was taken (Apple's time stamp, or now if missing)."""
    stamp = location.get(DEVICE_LOCATION_TIMESTAMP)
    if isinstance(stamp, (int, float)) and stamp > 0:
        try:
            located = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return utcnow()
        # never in the future (clock differences)
        return min(located, utcnow())
    return utcnow()
