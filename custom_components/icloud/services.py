"""The iCloud component."""

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from .account import IcloudAccount, IcloudConfigEntry, reset_session_files
from .const import (
    ATTR_ACCOUNT,
    ATTR_DEVICE_NAME,
    ATTR_LOST_DEVICE_MESSAGE,
    ATTR_LOST_DEVICE_NUMBER,
    ATTR_LOST_DEVICE_SOUND,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)

# services
SERVICE_ICLOUD_PLAY_SOUND = "play_sound"
SERVICE_ICLOUD_DISPLAY_MESSAGE = "display_message"
SERVICE_ICLOUD_LOST_DEVICE = "lost_device"
SERVICE_ICLOUD_UPDATE = "update"
SERVICE_ICLOUD_RESET_SESSION = "reset_session"

SERVICE_SCHEMA = vol.Schema({vol.Optional(ATTR_ACCOUNT): cv.string})

SERVICE_SCHEMA_RESET_SESSION = vol.Schema({vol.Required(ATTR_ACCOUNT): cv.string})

SERVICE_SCHEMA_PLAY_SOUND = vol.Schema(
    {vol.Required(ATTR_ACCOUNT): cv.string, vol.Required(ATTR_DEVICE_NAME): cv.string}
)

SERVICE_SCHEMA_DISPLAY_MESSAGE = vol.Schema(
    {
        vol.Required(ATTR_ACCOUNT): cv.string,
        vol.Required(ATTR_DEVICE_NAME): cv.string,
        vol.Required(ATTR_LOST_DEVICE_MESSAGE): cv.string,
        vol.Optional(ATTR_LOST_DEVICE_SOUND): cv.boolean,
    }
)

SERVICE_SCHEMA_LOST_DEVICE = vol.Schema(
    {
        vol.Required(ATTR_ACCOUNT): cv.string,
        vol.Required(ATTR_DEVICE_NAME): cv.string,
        vol.Required(ATTR_LOST_DEVICE_NUMBER): cv.string,
        vol.Required(ATTR_LOST_DEVICE_MESSAGE): cv.string,
    }
)


def play_sound(service: ServiceCall) -> None:
    """Play sound on the device."""
    account = service.data[ATTR_ACCOUNT]
    device_name: str = service.data[ATTR_DEVICE_NAME]
    device_name = slugify(device_name.replace(" ", "", 99))

    for device in _get_account(service.hass, account).get_devices_with_name(
        device_name
    ):
        device.play_sound()


def display_message(service: ServiceCall) -> None:
    """Display a message on the device."""
    account = service.data[ATTR_ACCOUNT]
    device_name: str = service.data[ATTR_DEVICE_NAME]
    device_name = slugify(device_name.replace(" ", "", 99))
    message = service.data.get(ATTR_LOST_DEVICE_MESSAGE)
    sound = service.data.get(ATTR_LOST_DEVICE_SOUND, False)

    for device in _get_account(service.hass, account).get_devices_with_name(
        device_name
    ):
        device.display_message(message, sound)


def lost_device(service: ServiceCall) -> None:
    """Make the device in lost state."""
    account = service.data[ATTR_ACCOUNT]
    device_name: str = service.data[ATTR_DEVICE_NAME]
    device_name = slugify(device_name.replace(" ", "", 99))
    number = service.data.get(ATTR_LOST_DEVICE_NUMBER)
    message = service.data.get(ATTR_LOST_DEVICE_MESSAGE)

    for device in _get_account(service.hass, account).get_devices_with_name(
        device_name
    ):
        device.lost_device(number, message)


def update_account(service: ServiceCall) -> None:
    """Call the update function of an iCloud account."""
    if (account := service.data.get(ATTR_ACCOUNT)) is None:
        # Update all accounts when no specific account is provided
        entry: IcloudConfigEntry
        for entry in service.hass.config_entries.async_loaded_entries(DOMAIN):
            entry.runtime_data.keep_alive()
    else:
        _get_account(service.hass, account).keep_alive()


async def async_reset_session(service: ServiceCall) -> None:
    """Delete the saved session of an account and ask to sign in again.

    Also works when the account could not be loaded (e.g. stuck at startup).
    """
    hass = service.hass
    username = service.data[ATTR_ACCOUNT]
    entry: IcloudConfigEntry
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data[CONF_USERNAME] != username:
            continue
        # An open sign-in would keep using the old session: close it, a new
        # one is started below.
        for flow in entry.async_get_active_flows(hass, {SOURCE_REAUTH}):
            hass.config_entries.flow.async_abort(flow["flow_id"])
        if entry.state is ConfigEntryState.LOADED:
            await hass.async_add_executor_job(entry.runtime_data.reset_session)
        else:
            await hass.async_add_executor_job(
                reset_session_files,
                Store(hass, STORAGE_VERSION, STORAGE_KEY).path,
                username,
            )
            entry.async_start_reauth(hass)
        return

    raise ValueError(f"No iCloud account with username or name {username}")


def _get_account(hass: HomeAssistant, account_identifier: str) -> IcloudAccount:
    if account_identifier is None:
        return None

    entry: IcloudConfigEntry
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.runtime_data.username == account_identifier:
            return entry.runtime_data

    raise ValueError(f"No iCloud account with username or name {account_identifier}")


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register iCloud services."""

    hass.services.async_register(
        DOMAIN, SERVICE_ICLOUD_PLAY_SOUND, play_sound, schema=SERVICE_SCHEMA_PLAY_SOUND
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ICLOUD_DISPLAY_MESSAGE,
        display_message,
        schema=SERVICE_SCHEMA_DISPLAY_MESSAGE,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ICLOUD_LOST_DEVICE,
        lost_device,
        schema=SERVICE_SCHEMA_LOST_DEVICE,
    )

    hass.services.async_register(
        DOMAIN, SERVICE_ICLOUD_UPDATE, update_account, schema=SERVICE_SCHEMA
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ICLOUD_RESET_SESSION,
        async_reset_session,
        schema=SERVICE_SCHEMA_RESET_SESSION,
    )
