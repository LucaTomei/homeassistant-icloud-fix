"""Tests for the verification-code step (new code / SMS / session reuse)."""

from unittest.mock import patch

import pytest

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.icloud.const import DOMAIN

from .fakes import FakeICloud

USER = {"username": "luca@example.com", "password": "pw", "with_family": False}


@pytest.fixture(autouse=True)
def fake_icloud():
    FakeICloud.reset()
    FakeICloud.start_requires_2fa = True
    with (
        patch("custom_components.icloud.config_flow.PyiCloudService", FakeICloud),
        patch("custom_components.icloud.account.PyiCloudService", FakeICloud),
    ):
        yield FakeICloud


async def _to_code_form(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "verification_code"
    return result


async def test_form_has_new_code_options(hass):
    result = await _to_code_form(hass)
    fields = [str(k) for k in result["data_schema"].schema]
    assert {"verification_code", "request_new_code", "use_sms"} <= set(fields)
    assert FakeICloud.instances[0].request_2fa_code.call_count == 1
    assert "Apple" in result["description_placeholders"]["status"]


async def test_request_new_code(hass):
    result = await _to_code_form(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"request_new_code": True})
    assert result["step_id"] == "verification_code"
    assert not result["errors"]
    assert FakeICloud.instances[0].request_2fa_code.call_count == 2


async def test_request_new_code_by_sms(hass):
    result = await _to_code_form(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"request_new_code": True, "use_sms": True})
    assert result["step_id"] == "verification_code"
    assert FakeICloud.instances[0]._request_sms_2fa_code.call_count == 1
    assert "SMS" in result["description_placeholders"]["status"]


async def test_empty_code_error(hass):
    result = await _to_code_form(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"verification_code": ""})
    assert result["errors"] == {"verification_code": "code_required"}


async def test_wrong_then_right_code_reuses_session(hass):
    result = await _to_code_form(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"verification_code": "000000"})
    assert result["errors"] == {"base": "validate_verification_code"}
    # a wrong code must not trigger a new Apple code automatically
    assert FakeICloud.instances[0].request_2fa_code.call_count == 1
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"verification_code": "123456"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # only one login during the whole flow (+1 when the entry is loaded)
    assert FakeICloud.instances[0].validate_2fa_code.call_count == 2
    assert len(FakeICloud.instances) <= 2, f"created {len(FakeICloud.instances)} sessions"


async def test_saved_token_without_trust_still_sends_code(hass):
    """The saved token is accepted but untrusted: a real code must be sent."""
    FakeICloud.no_2fa_challenge = True
    result = await _to_code_form(hass)
    api = FakeICloud.instances[0]
    api.authenticate.assert_called_once_with(force_refresh=True)
    assert not result["errors"]
    assert api.request_2fa_code.call_count == 1


async def test_flow_stops_its_background_refresh(hass):
    result = await _to_code_form(hass)
    api = FakeICloud.instances[0]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"verification_code": "123456"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    api.devices.stop_event.set.assert_called()
