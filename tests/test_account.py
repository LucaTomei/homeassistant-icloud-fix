"""Tests for session restore and transient error handling."""

from unittest.mock import patch

import pytest
import requests
from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudFailedLoginException,
)

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.icloud.account import is_transient_error
from custom_components.icloud.const import DOMAIN

from .fakes import FakeICloud

DATA = {
    "username": "luca@example.com",
    "password": "pw",
    "with_family": False,
    "max_interval": 30,
    "gps_accuracy_threshold": 500,
}


@pytest.fixture(autouse=True)
def fake_icloud():
    FakeICloud.reset()
    with patch("custom_components.icloud.account.PyiCloudService", FakeICloud):
        yield FakeICloud


def _network_login_error():
    """Exactly how pyicloud reports 'iCloud not reachable' at login."""
    try:
        try:
            try:
                raise requests.exceptions.ConnectionError("dns not ready")
            except requests.exceptions.ConnectionError as err:
                raise PyiCloudAPIResponseException("Request failed to iCloud") from err
        except PyiCloudAPIResponseException as err:
            raise PyiCloudFailedLoginException(
                "Failed to initiate srp authentication.", err
            ) from err
    except PyiCloudFailedLoginException as err:
        return err


def test_transient_detection():
    assert is_transient_error(_network_login_error())
    assert is_transient_error(PyiCloudAPIResponseException("busy", 503))
    assert is_transient_error(PyiCloudAPIResponseException("slow down", "429"))
    assert not is_transient_error(PyiCloudFailedLoginException("Invalid email/password combination."))
    assert not is_transient_error(PyiCloudAPIResponseException("bad", 401))


async def _setup(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=DATA["username"])
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _reauth_flows(hass):
    return [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"]


async def test_normal_restart_no_reauth(hass, fake_icloud):
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert not _reauth_flows(hass)


async def test_restart_restores_trusted_session(hass, fake_icloud):
    """After restart pyicloud says '2FA required' but the trust token is valid."""
    fake_icloud.start_requires_2fa = True
    fake_icloud.token_login_trusts = True
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert not _reauth_flows(hass), "should restore the session silently"


async def test_restart_really_untrusted_asks_reauth(hass, fake_icloud):
    fake_icloud.start_requires_2fa = True
    fake_icloud.token_login_trusts = False
    await _setup(hass)
    assert len(_reauth_flows(hass)) == 1


async def test_network_error_at_boot_retries_not_reauth(hass, fake_icloud):
    fake_icloud.init_error = _network_login_error()
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not _reauth_flows(hass), "network problems must not ask for a password"


async def test_wrong_password_asks_reauth(hass, fake_icloud):
    fake_icloud.init_error = PyiCloudFailedLoginException("Invalid email/password combination.")
    await _setup(hass)
    assert len(_reauth_flows(hass)) == 1


async def test_keep_alive_network_error_keeps_polling(hass, fake_icloud):
    entry = await _setup(hass)
    account = entry.runtime_data
    account.api.authenticate.side_effect = _network_login_error()
    with patch.object(account, "_schedule_next_fetch") as sched:
        await hass.async_add_executor_job(account.keep_alive)
    sched.assert_called_once()
    assert account._fetch_interval == 5
    assert not _reauth_flows(hass)
