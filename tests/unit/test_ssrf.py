from __future__ import annotations

import pytest

from cosmic.sources.http import UnsafeUrlError, check_public_url


def resolver_for(ips: list[str]):  # type: ignore[no-untyped-def]
    return lambda _host: ips


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",
        "file:///etc/passwd",
        "ftp://example.com",
        "https://user:pw@example.com",
        "https://example.com:8443/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://169.254.169.254/latest",
        "https://10.0.0.5/",
        "https://192.168.1.1/",
    ],
)
def test_rejects_unsafe(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        check_public_url(url, resolver=resolver_for(["93.184.216.34"]))


@pytest.mark.parametrize("ips", [["10.1.2.3"], ["93.184.216.34", "127.0.0.1"], ["fd00::1"], []])
def test_rejects_private_resolution(ips: list[str]) -> None:
    with pytest.raises(UnsafeUrlError):
        check_public_url("https://rebind.example/", resolver=resolver_for(ips))


def test_accepts_public() -> None:
    assert (
        check_public_url("https://example.com/doc", resolver=resolver_for(["93.184.216.34"]))
        == "example.com"
    )
