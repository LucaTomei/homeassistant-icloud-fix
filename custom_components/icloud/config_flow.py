"""Config flow to configure the iCloud integration."""

from collections.abc import Mapping
from functools import partial
import logging
import os
from typing import TYPE_CHECKING, Any

from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloudException,
    PyiCloudNoDevicesException,
    PyiCloudNoTrustedNumberAvailable,
    PyiCloudServiceNotActivatedException,
    PyiCloudServiceUnavailable,
)
import requests
import voluptuous as vol

from homeassistant.config_entries import SOURCE_USER, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.storage import Store

from .account import is_transient_error
from .const import (
    CONF_GPS_ACCURACY_THRESHOLD,
    CONF_MAX_INTERVAL,
    CONF_WITH_FAMILY,
    DEFAULT_GPS_ACCURACY_THRESHOLD,
    DEFAULT_MAX_INTERVAL,
    DEFAULT_WITH_FAMILY,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)

CONF_TRUSTED_DEVICE = "trusted_device"
CONF_VERIFICATION_CODE = "verification_code"
CONF_REQUEST_NEW_CODE = "request_new_code"
CONF_USE_SMS = "use_sms"

# Short status texts shown in the verification form (placeholders are not
# translated by Home Assistant, so they are localised here).
_STATUS_TEXT = {
    "it": {
        "trusted_device": "Codice inviato ai tuoi dispositivi Apple.",
        "sms": "Codice inviato via SMS al numero di fiducia.",
        "new_code": "Nuovo codice richiesto.",
        "unknown": "Apple sta inviando il codice.",
    },
    "de": {
        "trusted_device": "Code an deine Apple-Geräte gesendet.",
        "sms": "Code per SMS an die vertrauenswürdige Nummer gesendet.",
        "new_code": "Neuer Code angefordert.",
        "unknown": "Apple sendet den Code.",
    },
    "fr": {
        "trusted_device": "Code envoyé sur vos appareils Apple.",
        "sms": "Code envoyé par SMS au numéro de confiance.",
        "new_code": "Nouveau code demandé.",
        "unknown": "Apple envoie le code.",
    },
    "en": {
        "trusted_device": "Code sent to your Apple devices.",
        "sms": "Code sent by SMS to your trusted phone number.",
        "new_code": "New code requested.",
        "unknown": "Apple is sending the code.",
    },
}

_LOGGER = logging.getLogger(__name__)


class IcloudFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle an iCloud config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize iCloud config flow."""
        self.api = None
        self._username = None
        self._password = None
        self._with_family = None
        self._max_interval = None
        self._gps_accuracy_threshold = None

        self._trusted_device = None
        self._verification_code = None

        self._existing_entry_data: dict[str, Any] | None = None
        self._description_placeholders: dict[str, str] | None = None

        # Track 2FA code requests so redisplaying the form does not repeatedly
        # request new Apple verification codes.
        self._2fa_code_requested: bool = False
        # True once the 2FA code has been verified: the same, already trusted
        # session is reused instead of logging in again from scratch.
        self._verified: bool = False
        self._status_key: str | None = None

    def _show_setup_form(self, user_input=None, errors=None, step_id="user"):
        """Show the setup form to the user."""

        if user_input is None:
            user_input = {}

        if step_id == "user":
            schema = {
                vol.Required(
                    CONF_USERNAME, default=user_input.get(CONF_USERNAME, "")
                ): str,
                vol.Required(
                    CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, "")
                ): str,
                vol.Optional(
                    CONF_WITH_FAMILY,
                    default=user_input.get(CONF_WITH_FAMILY, DEFAULT_WITH_FAMILY),
                ): bool,
            }
        else:
            schema = {
                vol.Required(
                    CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, "")
                ): str,
            }

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(schema),
            errors=errors or {},
            description_placeholders=self._description_placeholders,
        )

    async def _create_icloud_api(self) -> PyiCloudService:
        """Create a pyicloud API object in the executor."""
        # accept_terms=True auto-accepts updated Apple terms of service. Newer
        # pyicloud versions otherwise raise PyiCloudAcceptTermsException during
        # login, and a config flow has no way to display the terms interactively.
        return await self.hass.async_add_executor_job(
            partial(
                PyiCloudService,
                self._username,
                self._password,
                cookie_directory=Store(self.hass, STORAGE_VERSION, STORAGE_KEY).path,
                with_family=self._with_family,
                accept_terms=True,
            )
        )

    async def _request_2fa_code_if_supported(
        self,
        errors: dict[str, str] | None = None,
        force: bool = False,
        use_sms: bool = False,
    ) -> dict[str, str]:
        """Request an Apple 2FA code when pyicloud supports that flow.

        Older pyicloud versions may not expose request_2fa_code().
        Newer pyicloud versions do, and Apple may not send/show a code until
        this is called.
        """
        if errors is None:
            errors = {}

        if self._2fa_code_requested and not force:
            return errors

        if TYPE_CHECKING:
            assert self.api is not None

        if use_sms:
            request_sms = getattr(self.api, "_request_sms_2fa_code", None)
            if request_sms is None:
                errors["base"] = "no_sms"
                return errors
            try:
                await self.hass.async_add_executor_job(request_sms)
            except PyiCloudNoTrustedNumberAvailable:
                errors["base"] = "no_sms"
                return errors
            except PyiCloudException as error:
                _LOGGER.error("Failed to request iCloud SMS code: %s", error)
                errors["base"] = "send_verification_code"
                return errors
            self._2fa_code_requested = True
            self._status_key = "sms"
            return errors

        request_2fa_code = getattr(self.api, "request_2fa_code", None)

        if request_2fa_code is None:
            _LOGGER.warning(
                "PyiCloud does not expose request_2fa_code(); showing verification "
                "form without explicitly requesting a 2FA code"
            )
            self._2fa_code_requested = True
            return errors

        try:
            result = await self.hass.async_add_executor_job(request_2fa_code)
        except PyiCloudException as error:
            _LOGGER.error("Failed to request iCloud 2FA verification code: %s", error)
            errors["base"] = "send_verification_code"
            return errors
        except Exception:
            _LOGGER.exception(
                "Unexpected error requesting iCloud 2FA verification code"
            )
            errors["base"] = "send_verification_code"
            return errors

        if result is False:
            _LOGGER.error("PyiCloud request_2fa_code returned False")
            errors["base"] = "send_verification_code"
            return errors

        self._2fa_code_requested = True
        method = getattr(self.api, "two_factor_delivery_method", "unknown")
        self._status_key = method if method in ("trusted_device", "sms") else "unknown"
        _LOGGER.debug("Requested iCloud 2FA verification code (%s)", method)
        return errors

    async def _validate_and_create_entry(self, user_input, step_id):
        """Check if config is valid and create entry if so."""
        self._password = user_input[CONF_PASSWORD]

        extra_inputs = user_input

        # If an existing entry was found, meaning this is a password update attempt,
        # use those to get config values that aren't changing.
        if self._existing_entry_data:
            extra_inputs = self._existing_entry_data

        self._username = extra_inputs[CONF_USERNAME]
        self._with_family = extra_inputs.get(CONF_WITH_FAMILY, DEFAULT_WITH_FAMILY)
        self._max_interval = extra_inputs.get(CONF_MAX_INTERVAL, DEFAULT_MAX_INTERVAL)
        self._gps_accuracy_threshold = extra_inputs.get(
            CONF_GPS_ACCURACY_THRESHOLD, DEFAULT_GPS_ACCURACY_THRESHOLD
        )

        # Check if already configured.
        if self.unique_id is None:
            await self.async_set_unique_id(self._username)
            self._abort_if_unique_id_configured()

        if not (self._verified and self.api is not None):
            try:
                self.api = await self._create_icloud_api()
                if self.api.requires_2fa and not getattr(self.api, "_auth_data", None):
                    # A saved session token was still accepted, so pyicloud did
                    # not sign in with the password and has no 2FA challenge:
                    # Apple would never send a code. Sign in with the password
                    # to start a real 2FA challenge.
                    await self.hass.async_add_executor_job(
                        partial(self.api.authenticate, force_refresh=True)
                    )
            except (PyiCloudException, requests.exceptions.RequestException) as error:
                self.api = None
                if is_transient_error(error):
                    _LOGGER.warning("iCloud temporarily unreachable: %s", error)
                    errors = {"base": "cannot_connect"}
                else:
                    _LOGGER.error("Error logging into iCloud service: %s", error)
                    errors = {CONF_PASSWORD: "invalid_auth"}
                return self._show_setup_form(user_input, errors, step_id)

        if self.api.requires_2fa:
            self._2fa_code_requested = False
            return await self.async_step_verification_code(request_code=True)

        if self.api.requires_2sa:
            return await self.async_step_trusted_device()

        try:
            devices = await self.hass.async_add_executor_job(
                getattr, self.api, "devices"
            )
            if not devices:
                raise PyiCloudNoDevicesException  # noqa: TRY301
        except (
            PyiCloudServiceNotActivatedException,
            PyiCloudNoDevicesException,
            PyiCloudServiceUnavailable,
        ):
            _LOGGER.error("No device found in the iCloud account: %s", self._username)
            self.api = None
            return self.async_abort(reason="no_device")
        except (PyiCloudException, requests.exceptions.RequestException) as error:
            _LOGGER.warning("iCloud temporarily unreachable: %s", error)
            return self._show_setup_form(user_input, {"base": "cannot_connect"}, step_id)

        # The device list was only needed to check the account: stop pyicloud's
        # background refresh so this session does not keep rewriting the
        # session files used by the config entry.
        stop_event = getattr(devices, "stop_event", None)
        if stop_event is not None:
            stop_event.set()

        data = {
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
            CONF_WITH_FAMILY: self._with_family,
            CONF_MAX_INTERVAL: self._max_interval,
            CONF_GPS_ACCURACY_THRESHOLD: self._gps_accuracy_threshold,
        }

        # If this is a password update attempt, don't try creating a new one.
        if self.source == SOURCE_USER:
            return self.async_create_entry(title=self._username, data=data)

        entry = await self.async_set_unique_id(self.unique_id)
        self.hass.config_entries.async_update_entry(entry, data=data)
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason="reauth_successful")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by the user."""
        errors: dict[str, str] = {}

        icloud_dir = Store[Any](self.hass, STORAGE_VERSION, STORAGE_KEY)

        if not os.path.exists(icloud_dir.path):
            await self.hass.async_add_executor_job(os.makedirs, icloud_dir.path)

        if user_input is None:
            return self._show_setup_form(user_input, errors)

        return await self._validate_and_create_entry(user_input, "user")

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Initialise re-authentication."""
        # Store existing entry data so it can be used later and set unique ID
        # so existing config entry can be updated.
        await self.async_set_unique_id(self.context["unique_id"])
        self._existing_entry_data = {**entry_data}
        self._description_placeholders = {"username": entry_data[CONF_USERNAME]}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update password for a config entry that can't authenticate."""
        if user_input is None:
            return self._show_setup_form(step_id="reauth_confirm")

        return await self._validate_and_create_entry(user_input, "reauth_confirm")

    async def async_step_trusted_device(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """We need a trusted device."""
        if errors is None:
            errors = {}

        if TYPE_CHECKING:
            assert self.api is not None

        trusted_devices = await self.hass.async_add_executor_job(
            getattr, self.api, "trusted_devices"
        )
        trusted_devices_for_form = {}
        for i, device in enumerate(trusted_devices):
            trusted_devices_for_form[i] = device.get(
                "deviceName", f"SMS to {device.get('phoneNumber')}"
            )

        if user_input is None:
            return await self._show_trusted_device_form(
                trusted_devices_for_form, errors
            )

        self._trusted_device = trusted_devices[int(user_input[CONF_TRUSTED_DEVICE])]

        if not await self.hass.async_add_executor_job(
            self.api.send_verification_code, self._trusted_device
        ):
            _LOGGER.error("Failed to send verification code")
            self._trusted_device = None
            errors[CONF_TRUSTED_DEVICE] = "send_verification_code"

            return await self._show_trusted_device_form(
                trusted_devices_for_form, errors
            )

        return await self.async_step_verification_code()

    async def _show_trusted_device_form(
        self, trusted_devices, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show the trusted_device form to the user."""

        return self.async_show_form(
            step_id="trusted_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRUSTED_DEVICE): vol.All(
                        vol.Coerce(int), vol.In(trusted_devices)
                    )
                }
            ),
            errors=errors or {},
        )

    async def async_step_verification_code(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
        request_code: bool = False,
    ) -> ConfigFlowResult:
        """Ask the verification code to the user.

        The form also offers "request a new code" (optionally by SMS) for when
        the code got lost or expired.
        """
        if errors is None:
            errors = {}

        if user_input is None:
            if request_code:
                errors = await self._request_2fa_code_if_supported(errors)
            return await self._show_verification_code_form(errors)

        if TYPE_CHECKING:
            assert self.api is not None

        # "Request a new code" ticked: send a new one and show the form again.
        if user_input.get(CONF_REQUEST_NEW_CODE):
            if not self.api.requires_2fa:
                # Legacy 2SA: the new code is sent by choosing the device again.
                return await self.async_step_trusted_device()
            errors = await self._request_2fa_code_if_supported(
                errors, force=True, use_sms=bool(user_input.get(CONF_USE_SMS))
            )
            if not errors and self._status_key != "sms":
                self._status_key = "new_code"
            return await self._show_verification_code_form(errors)

        self._verification_code = (user_input.get(CONF_VERIFICATION_CODE) or "").strip()
        if not self._verification_code:
            errors[CONF_VERIFICATION_CODE] = "code_required"
            return await self._show_verification_code_form(errors)

        try:
            if self.api.requires_2fa:
                if not await self.hass.async_add_executor_job(
                    self.api.validate_2fa_code, self._verification_code
                ):
                    raise PyiCloudException("The code you entered is not valid.")  # noqa: TRY301
            elif not await self.hass.async_add_executor_job(
                self.api.validate_verification_code,
                self._trusted_device,
                self._verification_code,
            ):
                raise PyiCloudException("The code you entered is not valid.")  # noqa: TRY301
        except PyiCloudException as error:
            # Redisplay the verification form after a failed verification attempt.
            # For 2FA, do not request a new Apple verification code on every bad
            # user entry. The original code may still be valid, and repeatedly
            # requesting new codes can invalidate prior codes or trigger rate limits.
            _LOGGER.error("Failed to verify verification code: %s", error)
            self._verification_code = None
            errors["base"] = (
                "cannot_connect"
                if is_transient_error(error)
                else "validate_verification_code"
            )

            if self.api.requires_2fa:
                return await self.async_step_verification_code(
                    None, errors, request_code=False
                )

            self._trusted_device = None
            return await self.async_step_trusted_device(None, errors)

        # Make sure Apple marked this session as trusted, so the next Home
        # Assistant restart does not ask for a code again.
        if not getattr(self.api, "is_trusted_session", True):
            await self.hass.async_add_executor_job(self.api.trust_session)

        self._verified = True
        return await self.async_step_user(
            {
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_WITH_FAMILY: self._with_family,
                CONF_MAX_INTERVAL: self._max_interval,
                CONF_GPS_ACCURACY_THRESHOLD: self._gps_accuracy_threshold,
            }
        )

    async def _show_verification_code_form(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show the verification_code form to the user."""

        language = (self.hass.config.language or "en").split("-")[0]
        texts = _STATUS_TEXT.get(language, _STATUS_TEXT["en"])
        status = texts.get(self._status_key or "", "") if self._status_key else ""

        return self.async_show_form(
            step_id="verification_code",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_VERIFICATION_CODE, default=""): str,
                    vol.Optional(CONF_REQUEST_NEW_CODE, default=False): bool,
                    vol.Optional(CONF_USE_SMS, default=False): bool,
                }
            ),
            errors=errors or {},
            description_placeholders={"status": status},
        )
