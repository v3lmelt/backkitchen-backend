import pytest
from fastapi import HTTPException

from app.security import (
    create_access_token,
    hash_password,
    verify_password,
    _decode_token,
)


def test_hash_and_verify_password():
    hashed = hash_password("my_secure_pass")
    assert hashed.startswith("pbkdf2_sha256$")
    assert verify_password("my_secure_pass", hashed)
    assert not verify_password("wrong_pass", hashed)


def test_verify_password_with_none():
    assert verify_password("anything", None) is False


def test_verify_password_with_malformed_hash():
    assert verify_password("anything", "not-a-valid-hash") is False
    assert verify_password("anything", "pbkdf2_sha256$bad") is False


def test_different_hashes_for_same_password():
    h1 = hash_password("samepass")
    h2 = hash_password("samepass")
    assert h1 != h2
    assert verify_password("samepass", h1)
    assert verify_password("samepass", h2)


def test_create_and_decode_token(factory):
    user = factory.user(username="tokenuser")
    token = create_access_token(user)
    payload = _decode_token(token)
    assert payload["sub"] == user.id
    assert payload["type"] == "access"


def test_decode_invalid_token():
    with pytest.raises(HTTPException) as exc:
        _decode_token("not.a.valid.token")
    assert exc.value.status_code == 401


def test_decode_token_no_dot():
    with pytest.raises(HTTPException) as exc:
        _decode_token("nodothere")
    assert exc.value.status_code == 401


def test_decode_tampered_token(factory):
    user = factory.user()
    token = create_access_token(user)
    parts = token.split(".")
    tampered = parts[0] + "X." + parts[1]
    with pytest.raises(HTTPException) as exc:
        _decode_token(tampered)
    assert exc.value.status_code == 401


def test_token_expiry(factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", -1)

    user = factory.user()
    token = create_access_token(user)

    with pytest.raises(HTTPException) as exc:
        _decode_token(token)
    assert exc.value.status_code == 401
