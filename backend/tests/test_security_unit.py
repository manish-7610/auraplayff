"""
Pure unit tests for security module — no async, no DB, no marks needed.
"""
from __future__ import annotations

import pytest


def test_bcrypt_cost_factor():
    """Password hash must use bcrypt with cost ≥ 12."""
    from app.core.security import hash_password
    hashed = hash_password("TestPassword@123")
    parts = hashed.split("$")
    assert parts[1] in ("2b", "2a")
    cost = int(parts[2])
    assert cost >= 12


def test_password_verify_correct():
    from app.core.security import hash_password, verify_password
    pw = "MyPassword@123"
    hashed = hash_password(pw)
    assert verify_password(pw, hashed) is True


def test_password_verify_wrong():
    from app.core.security import hash_password, verify_password
    hashed = hash_password("CorrectPassword@123")
    assert verify_password("WrongPassword@123", hashed) is False


def test_referral_code_format():
    from app.core.security import generate_referral_code
    for _ in range(20):
        code = generate_referral_code()
        assert len(code) == 8
        assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in code)


def test_refresh_token_not_stored_raw():
    from app.core.security import generate_refresh_token
    raw, hashed = generate_refresh_token()
    assert raw != hashed
    assert len(hashed) == 64


def test_jwt_audience_separation():
    from jose import JWTError
    from app.core.security import create_access_token, decode_access_token
    import uuid

    player_token = create_access_token(
        subject=uuid.uuid4(), audience="player", token_version=0
    )
    staff_token = create_access_token(
        subject=uuid.uuid4(), audience="staff", token_version=0, role="agent"
    )

    with pytest.raises(JWTError):
        decode_access_token(player_token, "staff")

    with pytest.raises(JWTError):
        decode_access_token(staff_token, "player")

    decode_access_token(player_token, "player")
    decode_access_token(staff_token, "staff")


def test_password_hash_never_plaintext():
    from app.core.security import hash_password
    plain = "SomePlainPassword@123"
    hashed = hash_password(plain)
    assert plain not in hashed
    assert hashed != plain


def test_sha256_token_hash_length():
    from app.core.security import hash_token
    raw = "some-raw-token-value"
    hashed = hash_token(raw)
    assert len(hashed) == 64
    assert hashed != raw


def test_generate_password_reset_token():
    from app.core.security import generate_password_reset_token
    raw, hashed = generate_password_reset_token()
    assert raw != hashed
    assert len(hashed) == 64
    assert len(raw) > 20  # urlsafe 32 bytes


def test_token_version_embedded_in_jwt():
    from app.core.security import create_access_token, get_token_version_from_payload, decode_access_token
    import uuid

    token = create_access_token(
        subject=uuid.uuid4(), audience="player", token_version=7
    )
    payload = decode_access_token(token, "player")
    assert get_token_version_from_payload(payload) == 7
