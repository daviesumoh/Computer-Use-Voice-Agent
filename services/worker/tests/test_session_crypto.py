import os
from uuid import uuid4

import pytest

from worker.session_crypto import StateDecryptionError, decrypt_state, encrypt_state

STATE = {"cookies": [{"name": "sid", "value": "s3cret-session-token", "domain": "shop.example"}]}


def test_state_round_trips_and_the_blob_hides_its_contents() -> None:
    key, account = os.urandom(32), uuid4()
    blob = encrypt_state(key, account, STATE)
    assert b"s3cret-session-token" not in blob
    assert decrypt_state(key, account, blob) == STATE


def test_every_encryption_uses_a_fresh_nonce() -> None:
    key, account = os.urandom(32), uuid4()
    assert encrypt_state(key, account, STATE) != encrypt_state(key, account, STATE)


@pytest.mark.parametrize("tamper", ["wrong_key", "other_account", "flipped_bit", "truncated"])
def test_anything_but_the_exact_key_account_and_bytes_is_rejected(tamper: str) -> None:
    key, account = os.urandom(32), uuid4()
    blob = encrypt_state(key, account, STATE)
    if tamper == "wrong_key":
        key = os.urandom(32)
    elif tamper == "other_account":
        account = uuid4()  # a blob copied onto another account's row
    elif tamper == "flipped_bit":
        blob = blob[:-1] + bytes([blob[-1] ^ 1])
    else:
        blob = blob[:20]
    with pytest.raises(StateDecryptionError):
        decrypt_state(key, account, blob)
