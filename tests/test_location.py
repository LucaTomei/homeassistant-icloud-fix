"""Location age: an old location must never keep a device "at home"."""

from datetime import timedelta
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.icloud.const import CONF_MAX_LOCATION_AGE, DOMAIN

from .fakes import FakeICloud

DATA = {
    "username": "luca@example.com",
    "password": "pw",
    "with_family": False,
    "max_interval": 30,
    "gps_accuracy_threshold": 500,
}
# Home of the test Home Assistant (pytest-homeassistant-custom-component)
HOME = (32.87336, -117.22743)
AWAY = (45.0, 9.0)


@pytest.fixture(autouse=True)
def fake_icloud():
    FakeICloud.reset()
    with patch("custom_components.icloud.account.PyiCloudService", FakeICloud):
        yield FakeICloud


def _location(where, minutes_ago=0, stamp=True):
    loc = {"latitude": where[0], "longitude": where[1], "horizontalAccuracy": 10}
    if stamp:
        loc["timeStamp"] = int((utcnow() - timedelta(minutes=minutes_ago)).timestamp() * 1000)
    return loc


async def _setup(hass: HomeAssistant, options=None, tracked=False):
    entry = MockConfigEntry(
        domain=DOMAIN, data=DATA, unique_id=DATA["username"], options=options or {}
    )
    entry.add_to_hass(hass)
    if tracked:
        # The device already has a tracker from an earlier run.
        er.async_get(hass).async_get_or_create(
            "device_tracker", DOMAIN, "dev1", config_entry=entry
        )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _tracker(hass):
    trackers = hass.states.async_all("device_tracker")
    assert len(trackers) == 1
    return trackers[0]


async def _apple_reports(hass, entry, **status):
    """Next poll: Apple reports these values for the device."""
    account = entry.runtime_data
    account.api.devices[0].status.return_value.update(status)
    with patch.object(account, "_schedule_next_fetch"):
        await hass.async_add_executor_job(account.keep_alive)
    await hass.async_block_till_done()


async def test_fresh_location_is_used(hass, fake_icloud):
    fake_icloud.location = _location(HOME, minutes_ago=5)
    await _setup(hass)
    state = _tracker(hass)
    assert state.state == "home"
    assert state.attributes["location_stale"] is False
    assert "last_located" in state.attributes


async def test_old_location_is_unknown_not_home(hass, fake_icloud):
    """Apple only has a location from 3 hours ago: not "home" any more."""
    fake_icloud.location = _location(HOME, minutes_ago=180)
    await _setup(hass)
    state = _tracker(hass)
    assert state.state == "unknown"
    assert state.attributes["location_stale"] is True
    assert "latitude" not in state.attributes


async def test_location_gets_old_without_new_data(hass, fake_icloud, freezer):
    """No update from Apple at all: the tracker still stops saying "home"."""
    fake_icloud.location = _location(HOME, minutes_ago=0)
    entry = await _setup(hass)
    assert _tracker(hass).state == "home"
    account = entry.runtime_data
    with patch.object(account, "keep_alive"):  # Apple silent
        freezer.tick(timedelta(minutes=119))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert _tracker(hass).state == "home"
        freezer.tick(timedelta(minutes=6))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert _tracker(hass).state == "unknown"


async def test_new_location_makes_it_fresh_again(hass, fake_icloud):
    fake_icloud.location = _location(HOME, minutes_ago=300)
    entry = await _setup(hass)
    assert _tracker(hass).state == "unknown"
    await _apple_reports(hass, entry, location=_location(AWAY, minutes_ago=1))
    state = _tracker(hass)
    assert state.state == "not_home"
    assert state.attributes["location_stale"] is False


async def test_older_location_does_not_replace_newer(hass, fake_icloud):
    fake_icloud.location = _location(AWAY, minutes_ago=1)
    entry = await _setup(hass)
    await _apple_reports(hass, entry, location=_location(HOME, minutes_ago=30))
    assert _tracker(hass).state == "not_home"


async def test_zero_keeps_last_location_forever(hass, fake_icloud):
    fake_icloud.location = _location(HOME, minutes_ago=600)
    await _setup(hass, options={CONF_MAX_LOCATION_AGE: 0})
    state = _tracker(hass)
    assert state.state == "home"
    assert state.attributes["location_stale"] is False


async def test_location_without_time_stamp(hass, fake_icloud):
    fake_icloud.location = _location(HOME, stamp=False)
    await _setup(hass)
    assert _tracker(hass).state == "home"


async def test_future_time_stamp_is_not_trusted(hass, fake_icloud, freezer):
    loc = _location(HOME)
    loc["timeStamp"] = int((utcnow() + timedelta(days=1)).timestamp() * 1000)
    fake_icloud.location = loc
    entry = await _setup(hass)
    with patch.object(entry.runtime_data, "keep_alive"):
        freezer.tick(timedelta(minutes=125))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert _tracker(hass).state == "unknown"


async def test_unknown_battery_still_updates_location(hass, fake_icloud):
    """Apple does not know the battery: the location is still read."""
    fake_icloud.location = _location(HOME, minutes_ago=1)
    entry = await _setup(hass)
    assert _tracker(hass).attributes["battery"] == 50
    await _apple_reports(
        hass, entry, batteryStatus="Unknown", location=_location(AWAY, minutes_ago=0)
    )
    state = _tracker(hass)
    assert state.state == "not_home"
    assert "battery" not in state.attributes
    assert state.attributes["battery_status"] == "Unknown"


async def test_never_tracked_device_without_battery_is_skipped(hass, fake_icloud):
    """A Mac or an old device (no battery) does not get new entities."""
    fake_icloud.unknown_battery = True
    fake_icloud.location = _location(AWAY, minutes_ago=1)
    entry = await _setup(hass)
    assert entry.runtime_data.devices == {}
    assert hass.states.async_all("device_tracker") == []


async def test_tracked_phone_off_at_startup_keeps_its_tracker(hass, fake_icloud):
    """Phone off while Home Assistant starts: "unknown", never "unavailable"."""
    fake_icloud.unknown_battery = True
    fake_icloud.location = _location(HOME, minutes_ago=300)
    await _setup(hass, tracked=True)
    state = _tracker(hass)
    assert state.state == "unknown"
    assert state.attributes["location_stale"] is True
    assert hass.states.async_all("sensor") == []  # no battery, no battery sensor


async def test_device_without_battery_and_location_is_skipped(hass, fake_icloud):
    fake_icloud.unknown_battery = True
    entry = await _setup(hass)
    assert entry.runtime_data.devices == {}
    assert hass.states.async_all("device_tracker") == []


async def test_options_flow_changes_age_and_reloads(hass, fake_icloud):
    fake_icloud.location = _location(HOME, minutes_ago=90)
    entry = await _setup(hass)
    assert _tracker(hass).state == "home"
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAX_LOCATION_AGE: 70}
    )
    await hass.async_block_till_done()
    assert entry.options == {CONF_MAX_LOCATION_AGE: 70}
    assert entry.runtime_data.max_location_age == 70
    assert _tracker(hass).state == "unknown"


async def test_reauth_does_not_reload_twice(hass, fake_icloud):
    """Only an options change reloads: a re-authentication reloads by itself."""
    fake_icloud.location = _location(HOME, minutes_ago=1)
    entry = await _setup(hass)
    account = entry.runtime_data
    hass.config_entries.async_update_entry(entry, data={**DATA, "password": "new"})
    await hass.async_block_till_done()
    assert entry.runtime_data is account
    assert len(fake_icloud.instances) == 1


async def test_options_flow_rejects_too_short_age(hass, fake_icloud):
    fake_icloud.location = _location(HOME, minutes_ago=1)
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAX_LOCATION_AGE: 15}
    )
    assert result["errors"] == {CONF_MAX_LOCATION_AGE: "age_too_short"}
    assert result["description_placeholders"] == {"min": "65"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAX_LOCATION_AGE: 0}
    )
    await hass.async_block_till_done()
    assert entry.options == {CONF_MAX_LOCATION_AGE: 0}


async def test_too_short_age_is_never_used(hass, fake_icloud):
    """A too short age (set elsewhere) is raised to the shortest usable one."""
    fake_icloud.location = _location(HOME, minutes_ago=45)
    entry = await _setup(hass, options={CONF_MAX_LOCATION_AGE: 15})
    assert entry.runtime_data.max_location_age == 65
    assert _tracker(hass).state == "home"


async def test_battery_sensor_appears_when_battery_comes_back(hass, fake_icloud):
    """Phone off at startup: its battery sensor is added once Apple reports it."""
    fake_icloud.unknown_battery = True
    fake_icloud.location = _location(HOME, minutes_ago=300)
    entry = await _setup(hass, tracked=True)
    assert hass.states.async_all("sensor") == []
    await _apple_reports(
        hass, entry, batteryStatus="Charging", location=_location(HOME, minutes_ago=0)
    )
    [battery] = hass.states.async_all("sensor")
    assert battery.state == "50"
    assert _tracker(hass).state == "home"


async def test_tracker_appears_when_location_arrives(hass, fake_icloud):
    """A battery-only device (location not shared yet) gets its tracker later."""
    entry = await _setup(hass)
    assert hass.states.async_all("device_tracker") == []
    await _apple_reports(hass, entry, location=_location(AWAY, minutes_ago=0))
    assert _tracker(hass).state == "not_home"


async def test_location_age_only_on_tracker(hass, fake_icloud):
    """The battery sensor does not re-check the age: no stale value there."""
    fake_icloud.location = _location(HOME, minutes_ago=1)
    await _setup(hass)
    [battery] = hass.states.async_all("sensor")
    assert "last_located" not in battery.attributes
    assert "location_stale" not in battery.attributes
    assert "last_located" in _tracker(hass).attributes
