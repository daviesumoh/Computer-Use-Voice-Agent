"""Encrypts saved browser state (cookies, local storage) before it reaches the database."""

import json
import os
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12


class StateDecryptionError(Exception):
    """The blob is corrupt, was encrypted under another key, or belongs to another account."""


def encrypt_state(key: bytes, account_id: UUID, state: dict[str, Any]) -> bytes:
    """AES-256-GCM. The account id is authenticated, so a blob cannot be moved between accounts."""
    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(state, separators=(",", ":")).encode()
    return nonce + AESGCM(key).encrypt(nonce, plaintext, account_id.bytes)


def decrypt_state(key: bytes, account_id: UUID, blob: bytes) -> dict[str, Any]:
    try:
        plaintext = AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], account_id.bytes)
    except InvalidTag as bad:
        raise StateDecryptionError("saved session cannot be decrypted") from bad
    state: dict[str, Any] = json.loads(plaintext)
    return state
