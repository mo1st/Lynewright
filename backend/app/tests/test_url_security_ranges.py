from __future__ import annotations

import pytest

from app.services.url_security import is_disallowed_address


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.5", "172.17.0.1", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "100.127.255.254", "0.0.0.0", "::1", "fd00::1", "fe80::1",
    "::ffff:127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe",
])
def test_internal_and_special_purpose_addresses_are_refused(address: str) -> None:
    assert is_disallowed_address(address) is True


@pytest.mark.parametrize("address", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111", "::ffff:93.184.216.34"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_disallowed_address(address) is False
