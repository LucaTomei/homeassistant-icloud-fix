"""The iCloud component."""

from typing import Any

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .account import IcloudAccount, IcloudConfigEntry
from .const import (
    CONF_GPS_ACCURACY_THRESHOLD,
    CONF_MAX_INTERVAL,
    CONF_MAX_LOCATION_AGE,
    CONF_WITH_FAMILY,
    DEFAULT_MAX_LOCATION_AGE,
    DOMAIN,
    PLATFORMS,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up iCloud integration."""

    async_setup_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: IcloudConfigEntry) -> bool:
    """Set up an iCloud account from a config entry."""

    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    with_family = entry.data[CONF_WITH_FAMILY]
    max_interval = entry.data[CONF_MAX_INTERVAL]
    gps_accuracy_threshold = entry.data[CONF_GPS_ACCURACY_THRESHOLD]
    max_location_age = entry.options.get(CONF_MAX_LOCATION_AGE, DEFAULT_MAX_LOCATION_AGE)

    # For backwards compat
    if entry.unique_id is None:
        hass.config_entries.async_update_entry(entry, unique_id=username)

    icloud_dir = Store[Any](hass, STORAGE_VERSION, STORAGE_KEY)

    # Devices that already have entities (tracker: device id, sensor: id_battery).
    tracked_device_ids = {
        reg.unique_id.removesuffix("_battery")
        for reg in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }

    account = IcloudAccount(
        hass,
        username,
        password,
        icloud_dir,
        with_family,
        max_interval,
        gps_accuracy_threshold,
        entry,
        max_location_age,
        tracked_device_ids,
    )
    await hass.async_add_executor_job(account.setup)

    entry.runtime_data = account

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: IcloudConfigEntry) -> None:
    """Options changed: reload (the saved session is reused, no new code).

    Only for the options: a re-authentication updates the entry data and
    reloads the entry by itself.
    """
    max_location_age = entry.options.get(CONF_MAX_LOCATION_AGE, DEFAULT_MAX_LOCATION_AGE)
    if max_location_age != entry.runtime_data.max_location_age_option:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: IcloudConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Stop polling and pyicloud's background thread, otherwise the old
        # session keeps running (and saving its tokens) after a reload.
        await hass.async_add_executor_job(entry.runtime_data.shutdown)
    return unload_ok
