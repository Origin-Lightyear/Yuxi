import pytest

from yuxi.utils.crypto_utils import decrypt_secret, encrypt_secret


def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setenv("IM_CREDENTIAL_ENCRYPTION_KEY", "unit-test-key")
    token = encrypt_secret("secret-value")
    assert token != "secret-value"
    assert decrypt_secret(token) == "secret-value"


def test_encrypt_is_randomized(monkeypatch):
    monkeypatch.setenv("IM_CREDENTIAL_ENCRYPTION_KEY", "unit-test-key")
    assert encrypt_secret("x") != encrypt_secret("x")


def test_decrypt_rejects_tampered_token(monkeypatch):
    monkeypatch.setenv("IM_CREDENTIAL_ENCRYPTION_KEY", "unit-test-key")
    with pytest.raises(Exception):
        decrypt_secret("not-a-valid-token")
