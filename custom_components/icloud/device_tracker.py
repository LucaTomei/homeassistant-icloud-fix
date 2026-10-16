"""Support for tracking for iCloud devices."""

from datetime import timedelta
from typing import Any

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .account import IcloudAccount, IcloudConfigEntry, IcloudDevice
from .const import (
    DEVICE_LOCATION_HORIZONTAL_ACCURACY,
    DEVICE_LOCATION_LATITUDE,
    DEVICE_LOCATION_LONGITUDE,
    DOMAIN,
    STALE_CHECK_INTERVAL,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IcloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up device tracker for iCloud component."""
    account = entry.runtime_data
    tracked = set[str]()

    @callback
    def update_account():
        """Update the values of the account."""
        add_entities(account, async_add_entities, tracked)

    account.listeners.append(
        async_dispatcher_connect(hass, account.signal_device_new, update_account)
    )

    update_account()


@callback
def add_entities(account: IcloudAccount, async_add_entities, tracked):
    """Add new tracker entities from the account."""
    new_tracked = []

    for dev_id, device in account.devices.items():
        if dev_id in tracked or device.location is None:
            continue

        new_tracked.append(IcloudTrackerEntity(account, device))
        tracked.add(dev_id)

    async_add_entities(new_tracked, True)


class IcloudTrackerEntity(TrackerEntity):
    """Represent a tracked device."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, account: IcloudAccount, device: IcloudDevice) -> None:
        """Set up the iCloud tracker entity."""
        self._account = account
        self._device = device
        self._unsub_dispatcher: CALLBACK_TYPE | None = None
        self._unsub_stale: CALLBACK_TYPE | None = None
        self._was_stale = False
        self._attr_unique_id = device.unique_id

    # A location that is too old is not used: the tracker becomes "unknown"
    # (and the person falls back on their other trackers) instead of staying
    # where the device was last seen, e.g. at home.
    @property
    def location_accuracy(self) -> float:
        """Return the location accuracy of the device."""
        location = self._device.fresh_location
        return location[DEVICE_LOCATION_HORIZONTAL_ACCURACY] if location else 0

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        location = self._device.fresh_location
        return location[DEVICE_LOCATION_LATITUDE] if location else None

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        location = self._device.fresh_location
        return location[DEVICE_LOCATION_LONGITUDE] if location else None

    @property
    def icon(self) -> str:
        """Return the icon."""
        return icon_for_icloud_device(self._device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the device state attributes."""
        return {
            **self._device.extra_state_attributes,
            **self._device.location_attributes,
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return DeviceInfo(
            configuration_url="https://icloud.com/",
            identifiers={(DOMAIN, self._device.unique_id)},
            manufacturer="Apple",
            model=self._device.device_model,
            name=self._device.name,
        )

    async def async_added_to_hass(self) -> None:
        """Register state update callback."""
        self._was_stale = self._device.location_stale
        self._unsub_dispatcher = async_dispatcher_connect(
            self.hass, self._account.signal_device_update, self._async_updated
        )
        # A location gets old even when nothing new arrives from Apple.
        self._unsub_stale = async_track_time_interval(
            self.hass, self._async_check_stale, timedelta(minutes=STALE_CHECK_INTERVAL)
        )

    @callback
    def _async_updated(self) -> None:
        self._was_stale = self._device.location_stale
        self.async_write_ha_state()

    @callback
    def _async_check_stale(self, _now=None) -> None:
        if self._device.location_stale != self._was_stale:
            self._async_updated()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up after entity before removal."""
        if self._unsub_dispatcher:
            self._unsub_dispatcher()
        if self._unsub_stale:
            self._unsub_stale()


def icon_for_icloud_device(icloud_device: IcloudDevice) -> str:
    """Return an icon for the device."""
    switcher = {
        "iPad": "mdi:tablet",
        "iPhone": "mdi:cellphone",
        "iPod": "mdi:ipod",
        "iMac": "mdi:monitor",
        "MacBookPro": "mdi:laptop",
    }

    return switcher.get(icloud_device.device_class, "mdi:cellphone-link")
