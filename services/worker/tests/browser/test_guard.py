import pytest

from worker.browser.errors import UnsafeDestinationError
from worker.browser.guard import DestinationGuard


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8100/healthz",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata service
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http:///nohost",
    ],
)
async def test_internal_and_non_web_destinations_are_refused(url: str) -> None:
    with pytest.raises(UnsafeDestinationError):
        await DestinationGuard().check(url)


async def test_a_public_address_is_allowed() -> None:
    await DestinationGuard().check("https://93.184.216.34/")  # an IP literal: no DNS needed


async def test_private_hosts_can_be_allowed_for_local_testing() -> None:
    await DestinationGuard(allow_private=True).check("http://127.0.0.1:9/")
