"""A fake pyicloud service that simulates Apple's behaviour."""

from unittest.mock import MagicMock

from pyicloud.exceptions import PyiCloud2FARequiredException


class FakeSession:
    def __init__(self, data):
        self.data = data


class FakeICloud:
    """Configurable stand-in for pyicloud.PyiCloudService."""

    instances: list["FakeICloud"] = []
    # behaviour knobs (set by tests before creating instances)
    start_requires_2fa = False
    token_login_trusts = True
    session_data = {"session_token": "tok", "trust_token": "trust"}
    init_error: Exception | None = None

    def __init__(self, username, password, cookie_directory=None, **kwargs):
        if FakeICloud.init_error is not None:
            raise FakeICloud.init_error
        FakeICloud.instances.append(self)
        self.session = FakeSession(dict(FakeICloud.session_data))
        self._requires_mfa = FakeICloud.start_requires_2fa
        self.requires_2sa = False
        self.is_trusted_session = not FakeICloud.start_requires_2fa
        self.two_factor_delivery_method = "unknown"
        self.request_2fa_code = MagicMock(side_effect=self._request_code)
        self._request_sms_2fa_code = MagicMock(side_effect=self._request_sms)
        self.validate_2fa_code = MagicMock(side_effect=self._validate)
        self.trust_session = MagicMock(return_value=True)
        self.authenticate = MagicMock()
        device = MagicMock()
        device.status.return_value = {
            "id": "dev1",
            "name": "iPhone",
            "deviceStatus": "200",
            "batteryStatus": "Charging",
            "batteryLevel": 0.5,
            "deviceDisplayName": "iPhone",
            "rawDeviceModel": "iPhone15,2",
            "prsId": None,
            "lostModeCapable": True,
            "lowPowerMode": False,
            "location": None,
            "deviceClass": "iPhone",
        }
        devices = MagicMock()
        devices.__iter__ = lambda s: iter([device])
        devices.__getitem__ = lambda s, i: device
        devices.__bool__ = lambda s: True
        devices.user_info = {"firstName": "Luca", "lastName": "T", "membersInfo": None}
        self.devices = devices

    @property
    def requires_2fa(self):
        return self._requires_mfa

    def _authenticate_with_token(self):
        if not FakeICloud.token_login_trusts:
            raise PyiCloud2FARequiredException("x", None)
        self.is_trusted_session = True

    def _request_code(self):
        self.two_factor_delivery_method = "trusted_device"
        return True

    def _request_sms(self):
        self.two_factor_delivery_method = "sms"
        return True

    def _validate(self, code):
        if code == "123456":
            self._requires_mfa = False
            self.is_trusted_session = True
            return True
        return False

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.start_requires_2fa = False
        cls.token_login_trusts = True
        cls.session_data = {"session_token": "tok", "trust_token": "trust"}
        cls.init_error = None
