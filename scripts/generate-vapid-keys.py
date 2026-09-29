"""Prints a new VAPID key pair for web push.

    cd services/worker && uv run python ../../scripts/generate-vapid-keys.py

The private key goes in WORKER_VAPID_PRIVATE_KEY, the public key in the dashboard's
NEXT_PUBLIC_VAPID_PUBLIC_KEY. Changing the pair later turns notifications off on every device
(each has to turn them on again).
"""

import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


vapid = Vapid()
vapid.generate_keys()
private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
public = vapid.public_key.public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
)
print(f"WORKER_VAPID_PRIVATE_KEY={b64url(private)}")
print(f"NEXT_PUBLIC_VAPID_PUBLIC_KEY={b64url(public)}")
