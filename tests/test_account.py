"""Tests for session restore and transient error handling."""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest
import requests
from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudFailedLoginException,
)

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.icloud.account import (
    REQUEST_TIMEOUT,
    is_transient_error,
    is_wrong_password,
    reset_session_files,
)
from custom_components.icloud.const import DOMAIN, EVENT_REAUTH_REQUIRED

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
    assert is_transient_error(requests.exceptions.JSONDecodeError("html", "<html>", 0))


def test_wrong_password_after_network_error_is_not_transient():
    """pyicloud signs in with the password while handling a token error."""
    try:
        try:
            raise _network_login_error()
        except PyiCloudFailedLoginException:
            try:
                raise PyiCloudAPIResponseException("Unauthorized", 401)
            except PyiCloudAPIResponseException as err:
                raise PyiCloudFailedLoginException(
                    "Invalid email/password combination."
                ) from err
    except PyiCloudFailedLoginException as err:
        assert not is_transient_error(err)


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


async def test_find_my_network_error_at_boot_retries(hass, fake_icloud):
    """Login works but Find My is not reachable yet: retry, no password."""
    fake_icloud.devices_error = _network_login_error().__cause__
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not _reauth_flows(hass)


async def test_keep_alive_unexpected_error_keeps_polling(hass, fake_icloud):
    entry = await _setup(hass)
    account = entry.runtime_data
    account.api.authenticate.side_effect = ValueError("unexpected html")
    with patch.object(account, "_schedule_next_fetch") as sched:
        await hass.async_add_executor_job(account.keep_alive)
    sched.assert_called_once()
    assert not _reauth_flows(hass)


async def test_keep_alive_refreshes_locations(hass, fake_icloud):
    entry = await _setup(hass)
    account = entry.runtime_data
    devices = account.api.devices
    devices.refresh.assert_not_called()  # just fetched at setup
    with patch.object(account, "_schedule_next_fetch"):
        await hass.async_add_executor_job(account.keep_alive)
    devices.refresh.assert_called_once_with(locate=True)


async def test_unload_stops_polling_and_session(hass, fake_icloud):
    entry = await _setup(hass)
    account = entry.runtime_data
    devices = account.api._devices
    assert account._unsub_fetch is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert account._unsub_fetch is None
    assert account.api is None
    devices.stop_event.set.assert_called()
    # A late timer or service call does nothing after unload.
    await hass.async_add_executor_job(account.keep_alive)
    assert account.api is None
    assert len(fake_icloud.instances) == 1


async def test_reauth_releases_old_session(hass, fake_icloud):
    entry = await _setup(hass)
    account = entry.runtime_data
    devices = account.api._devices
    account.api._requires_mfa = True
    fake_icloud.token_login_trusts = False
    with patch.object(account, "_schedule_next_fetch"):
        await hass.async_add_executor_job(account.keep_alive)
    await hass.async_block_till_done()
    assert len(_reauth_flows(hass)) == 1
    assert account.api is None
    devices.stop_event.set.assert_called()


def test_wrong_password_detection():
    try:
        try:
            raise PyiCloudAPIResponseException("Unauthorized", 401)
        except PyiCloudAPIResponseException as err:
            raise PyiCloudFailedLoginException(
                "Invalid email/password combination."
            ) from err
    except PyiCloudFailedLoginException as err:
        assert is_wrong_password(err)
    assert not is_wrong_password(_network_login_error())
    assert not is_wrong_password(PyiCloudAPIResponseException("expired", 421))
    assert not is_wrong_password(PyiCloudFailedLoginException("No session token"))


def test_reset_session_files_only_touches_one_account(tmp_path):
    for name in ("lucaexamplecom", "otherexamplecom"):
        for suffix in (".session", ".cookiejar"):
            (tmp_path / f"{name}{suffix}").write_text("x")
    reset_session_files(str(tmp_path), "luca@example.com")
    assert sorted(f.name for f in tmp_path.iterdir()) == [
        "otherexamplecom.cookiejar",
        "otherexamplecom.session",
    ]
    reset_session_files(str(tmp_path), "luca@example.com")  # nothing left: no error


def test_requests_get_a_default_timeout():
    from pyicloud.session import PyiCloudSession

    session = MagicMock()
    PyiCloudSession.request(session, "GET", "https://example.com")
    assert session._request.call_args.kwargs["timeout"] == REQUEST_TIMEOUT
    PyiCloudSession.request(session, "GET", "https://example.com", timeout=3)
    assert session._request.call_args.kwargs["timeout"] == 3
    PyiCloudSession.request_raw(session, "GET", "https://example.com")
    assert session._request_raw.call_args.kwargs["timeout"] == REQUEST_TIMEOUT


async def test_reauth_fires_event_once(hass, fake_icloud):
    events = async_capture_events(hass, EVENT_REAUTH_REQUIRED)
    entry = await _setup(hass)
    account = entry.runtime_data
    account.api._requires_mfa = True
    fake_icloud.token_login_trusts = False
    with patch.object(account, "_schedule_next_fetch"):
        await hass.async_add_executor_job(account.keep_alive)
        await hass.async_add_executor_job(account.keep_alive)
    await hass.async_block_till_done()
    assert [e.data for e in events] == [{"account": DATA["username"]}]


async def test_stuck_updates_open_a_new_session(hass, fake_icloud):
    """Updates keep failing: a new session is opened, entities keep working."""
    entry = await _setup(hass)
    account = entry.runtime_data
    device = account.devices["dev1"]
    first_api = account.api
    first_api._fmip.refresh.side_effect = PyiCloudAPIResponseException("stuck", 450)
    with patch.object(account, "_schedule_next_fetch"):
        for _ in range(3):
            await hass.async_add_executor_job(account.keep_alive)
        assert account.api is None
        first_api._fmip.stop_event.set.assert_called()
        await hass.async_add_executor_job(account.keep_alive)
    assert account.api is not None and account.api is not first_api
    # Same device object (used by the entities), now bound to the new session.
    assert account.devices["dev1"] is device
    assert device.device is not None
    assert not _reauth_flows(hass)


async def test_reset_session_service(hass, fake_icloud):
    events = async_capture_events(hass, EVENT_REAUTH_REQUIRED)
    entry = await _setup(hass)
    account = entry.runtime_data
    with patch("custom_components.icloud.account.reset_session_files") as reset:
        await hass.services.async_call(
            DOMAIN, "reset_session", {"account": DATA["username"]}, blocking=True
        )
        await hass.async_block_till_done()
    reset.assert_called_once()
    assert reset.call_args.args[1] == DATA["username"]
    assert account.api is None
    assert account._unsub_fetch is None
    assert len(_reauth_flows(hass)) == 1
    assert len(events) == 1


async def test_reset_session_service_when_not_loaded(hass, fake_icloud):
    fake_icloud.init_error = _network_login_error()
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    with patch("custom_components.icloud.services.reset_session_files") as reset:
        await hass.services.async_call(
            DOMAIN, "reset_session", {"account": DATA["username"]}, blocking=True
        )
        await hass.async_block_till_done()
    reset.assert_called_once()
    assert len(_reauth_flows(hass)) == 1


def test_reset_session_files_never_raises(tmp_path):
    (tmp_path / "lucaexamplecom.session").mkdir()  # cannot be removed as a file
    (tmp_path / "lucaexamplecom.cookiejar").write_text("x")
    reset_session_files(str(tmp_path), "luca@example.com")
    assert not (tmp_path / "lucaexamplecom.cookiejar").exists()


async def test_reauth_event_at_boot_waits_for_start(hass, fake_icloud):
    """At boot automations only listen once Home Assistant has started."""
    events = async_capture_events(hass, EVENT_REAUTH_REQUIRED)
    hass.set_state(CoreState.not_running)
    fake_icloud.start_requires_2fa = True
    fake_icloud.token_login_trusts = False
    await _setup(hass)
    assert not events
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert [e.data for e in events] == [{"account": DATA["username"]}]


async def test_reset_waits_for_running_update(hass, fake_icloud):
    """A reset never happens in the middle of an update."""
    entry = await _setup(hass)
    account = entry.runtime_data
    started, release = threading.Event(), threading.Event()

    def slow_refresh(**kwargs):
        started.set()
        release.wait(5)

    account.api._fmip.refresh.side_effect = slow_refresh
    update = hass.async_add_executor_job(account.keep_alive)
    await hass.async_add_executor_job(started.wait, 5)
    with patch("custom_components.icloud.account.reset_session_files") as reset_files:
        reset = hass.async_add_executor_job(account.reset_session)
        await asyncio.sleep(0.1)
        assert not reset.done()
        reset_files.assert_not_called()
        release.set()
        await update
        await reset
    reset_files.assert_called_once()
    assert account.api is None
    assert account._unsub_fetch is None
    # A late timer does not open a session while the sign-in is pending.
    await hass.async_add_executor_job(account.keep_alive)
    assert account.api is None
    assert len(fake_icloud.instances) == 1


async def test_reset_service_replaces_open_sign_in(hass, fake_icloud):
    entry = await _setup(hass)
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    [old_flow] = _reauth_flows(hass)
    with patch("custom_components.icloud.account.reset_session_files"):
        await hass.services.async_call(
            DOMAIN, "reset_session", {"account": DATA["username"]}, blocking=True
        )
        await hass.async_block_till_done()
    [new_flow] = _reauth_flows(hass)
    assert new_flow["flow_id"] != old_flow["flow_id"]


async def test_skipped_device_follows_new_session(hass, fake_icloud):
    """Devices without battery info must not keep the old session's object."""
    entry = await _setup(hass)
    account = entry.runtime_data
    device = account.devices["dev1"]
    old_object = device.device
    account._release_api()
    fake_icloud.unknown_battery = True
    with patch.object(account, "_schedule_next_fetch"):
        await hass.async_add_executor_job(account.keep_alive)
    assert account.devices["dev1"] is device
    assert device.device is not old_object


async def test_battery_only_in_battery_sensor(hass, fake_icloud):
    """The tracker no longer has a battery attribute (deprecated by HA)."""
    fake_icloud.location = {
        "latitude": 45.0,
        "longitude": 9.0,
        "horizontalAccuracy": 10,
    }
    await _setup(hass)
    [tracker] = hass.states.async_all("device_tracker")
    assert "battery_level" not in tracker.attributes
    [battery] = hass.states.async_all("sensor")
    assert battery.state == "50"
