"""
Phase 11 — Free Fire display name (ff_name) tests.

Covers:
  1. Syntax / import sanity — new schemas and endpoint importable.
  2. validate_ff_name unit tests — valid names, empty rejection,
     invalid chars, whitespace-only, consecutive spaces, length limits.
  3. GET /api/users/me — returns ff_name field (None when not set).
  4. PATCH /api/users/me — successful ff_name update.
  5. PATCH /api/users/me — empty / whitespace-only rejected (422).
  6. PATCH /api/users/me — invalid character rejected (422).
  7. PATCH /api/users/me — too-short / too-long rejected (422).
  8. PATCH /api/users/me — consecutive spaces rejected (422).
  9. PATCH /api/users/me — cannot change ff_uid.
  10. PATCH /api/users/me — cannot change username.
  11. PATCH /api/users/me — login still works after ff_name change
      (username is unchanged, token is re-issued cleanly).
  12. Registration with optional ff_name — persisted correctly.
  13. Registration without ff_name — ff_name is None in /me response.
  14. Unauthenticated PATCH → 401/403.
  15. ff_name same-value no-op returns 200 with success message.
  16. Existing player/auth tests are not broken (smoke run).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import login_player, login_staff, register_player


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _register_and_login(
    client: AsyncClient,
    username: str = "testff",
    ff_uid: str = "1234567890",
    ff_name: str | None = None,
) -> str:
    """Register a player, log in, and return the access token."""
    payload = {
        "username": username,
        "password": "TestPass@123",
        "confirm_password": "TestPass@123",
        "ff_uid": ff_uid,
    }
    if ff_name is not None:
        payload["ff_name"] = ff_name

    reg = await client.post("/api/auth/register", json=payload)
    assert reg.status_code == 201, f"Registration failed: {reg.text}"

    login_resp = await client.post(
        "/api/auth/login",
        json={"username": username, "password": "TestPass@123"},
    )
    assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
    return login_resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Syntax / import sanity
# ─────────────────────────────────────────────────────────────────────────────

def test_validate_ff_name_importable():
    from app.schemas.user import validate_ff_name
    assert callable(validate_ff_name)


def test_update_profile_request_importable():
    from app.schemas.user import UpdateProfileRequest, UpdateProfileResponse
    assert UpdateProfileRequest is not None
    assert UpdateProfileResponse is not None


def test_player_me_response_has_ff_name():
    from app.schemas.user import PlayerMeResponse
    import inspect
    fields = PlayerMeResponse.model_fields
    assert "ff_name" in fields


def test_patch_endpoint_registered():
    """PATCH /api/users/me must be present in the users router."""
    from app.routers.users import router
    patch_routes = [
        r for r in router.routes
        if getattr(r, "path", None) == "/api/users/me"
        and "PATCH" in (getattr(r, "methods", None) or set())
    ]
    assert patch_routes, "PATCH /api/users/me route not found in users router"


# ─────────────────────────────────────────────────────────────────────────────
# 2. validate_ff_name unit tests (pure — no DB/network)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    # Original ASCII names — still valid
    "FireKing",
    "fire king",
    "AK47_Pro",
    "FF-Player",
    "Pro.Gamer",
    "A",            # single char — min is now 1
    "AB",
    "A" * 15,       # exactly 15 chars (new max)
    "player 1",
    "fire_king99",
    # Free Fire stylish/Unicode names — must all be accepted
    "MANISH*7610*",
    "꧁MANISH꧂",
    "★MANISH★",
    "ᴍᴀɴɪsʜ",
    "MANISH⚡",
    "a  b",         # consecutive spaces now allowed
    "bad@name",     # @ symbol now allowed
    "bad#name",     # # symbol now allowed
    "hello!",       # ! now allowed
    "emoji🔥",       # emoji now allowed
])
def test_valid_ff_names(name):
    from app.schemas.user import validate_ff_name
    result = validate_ff_name(name)
    assert result == name.strip()


@pytest.mark.parametrize("bad_name,expected_fragment", [
    ("",          "empty"),
    ("  ",        "empty"),
    ("A" * 16,    "15 characters"),   # 16 chars > new max of 15
    ("<script>",  "angle brackets"),  # HTML injection blocked
    (">attack",   "angle brackets"),
])
def test_invalid_ff_names(bad_name, expected_fragment):
    from app.schemas.user import validate_ff_name
    import pytest as _pytest
    with _pytest.raises(ValueError, match=expected_fragment):
        validate_ff_name(bad_name)


# ─────────────────────────────────────────────────────────────────────────────
# 3. GET /api/users/me — ff_name field present
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_me_returns_ff_name_none_when_not_set(client: AsyncClient):
    """Fresh account with no ff_name → /me returns ff_name = null."""
    token = await _register_and_login(client, username="metest1", ff_uid="1111111111")
    resp = await client.get("/api/users/me", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert "ff_name" in body
    assert body["ff_name"] is None


@pytest.mark.asyncio
async def test_me_returns_ff_name_when_set_at_registration(client: AsyncClient):
    """Account created with ff_name → /me returns that ff_name."""
    token = await _register_and_login(
        client, username="metest2", ff_uid="2222222222", ff_name="FireKing99"
    )
    resp = await client.get("/api/users/me", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ff_name"] == "FireKing99"


# ─────────────────────────────────────────────────────────────────────────────
# 4. PATCH /api/users/me — successful update
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_me_sets_ff_name(client: AsyncClient):
    """PATCH /api/users/me with valid ff_name → 200 and persisted."""
    token = await _register_and_login(client, username="patchtest1", ff_uid="3333333333")

    resp = await client.patch(
        "/api/users/me",
        json={"ff_name": "AK47 Pro"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ff_name"] == "AK47 Pro"
    assert "updated" in body["message"].lower() or "success" in body["message"].lower()

    # Verify persisted via GET
    me = await client.get("/api/users/me", headers=_auth(token))
    assert me.json()["ff_name"] == "AK47 Pro"


@pytest.mark.asyncio
async def test_patch_me_updates_ff_name_again(client: AsyncClient):
    """Player can change ff_name multiple times."""
    token = await _register_and_login(client, username="patchtest2", ff_uid="4444444444")

    await client.patch("/api/users/me", json={"ff_name": "FirstName"}, headers=_auth(token))
    resp = await client.patch("/api/users/me", json={"ff_name": "SecondName"}, headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ff_name"] == "SecondName"

    me = await client.get("/api/users/me", headers=_auth(token))
    assert me.json()["ff_name"] == "SecondName"


# ─────────────────────────────────────────────────────────────────────────────
# 5. PATCH — empty / whitespace-only rejected
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_me_rejects_empty_ff_name(client: AsyncClient):
    """Empty string must be rejected with 422."""
    token = await _register_and_login(client, username="validtest1", ff_uid="5555555555")
    resp = await client.patch("/api/users/me", json={"ff_name": ""}, headers=_auth(token))
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_patch_me_rejects_whitespace_only(client: AsyncClient):
    """Whitespace-only string must be rejected with 422."""
    token = await _register_and_login(client, username="validtest2", ff_uid="6666666666")
    resp = await client.patch("/api/users/me", json={"ff_name": "   "}, headers=_auth(token))
    assert resp.status_code == 422, resp.text


# ─────────────────────────────────────────────────────────────────────────────
# 6. PATCH — invalid characters rejected
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_name", ["<script>alert(1)</script>", ">xss", "a<b"])
async def test_patch_me_rejects_invalid_chars(client: AsyncClient, bad_name: str):
    token = await _register_and_login(client, username="validtest3", ff_uid="7777777777")
    resp = await client.patch("/api/users/me", json={"ff_name": bad_name}, headers=_auth(token))
    assert resp.status_code == 422, f"Expected 422 for {bad_name!r}, got {resp.status_code}: {resp.text}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. PATCH — length limits enforced
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_me_rejects_empty_after_strip(client: AsyncClient):
    """Whitespace-only after stripping must be rejected with 422."""
    token = await _register_and_login(client, username="lentest1", ff_uid="8888888881")
    resp = await client.patch("/api/users/me", json={"ff_name": "   "}, headers=_auth(token))
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_patch_me_rejects_too_long(client: AsyncClient):
    token = await _register_and_login(client, username="lentest2", ff_uid="8888888882")
    resp = await client.patch("/api/users/me", json={"ff_name": "A" * 16}, headers=_auth(token))
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_patch_me_accepts_boundary_lengths(client: AsyncClient):
    """Single char and exactly 15 chars must be accepted."""
    token = await _register_and_login(client, username="lentest3", ff_uid="8888888883")
    r1 = await client.patch("/api/users/me", json={"ff_name": "A"}, headers=_auth(token))
    assert r1.status_code == 200

    r15 = await client.patch("/api/users/me", json={"ff_name": "A" * 15}, headers=_auth(token))
    assert r15.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# 8. PATCH — consecutive spaces rejected
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_me_accepts_consecutive_spaces(client: AsyncClient):
    """Consecutive spaces are now allowed (Free Fire names may include them)."""
    token = await _register_and_login(client, username="spacetest1", ff_uid="9999999991")
    resp = await client.patch("/api/users/me", json={"ff_name": "a  b"}, headers=_auth(token))
    assert resp.status_code == 200, resp.text


# ─────────────────────────────────────────────────────────────────────────────
# 9. PATCH — cannot change ff_uid
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_me_cannot_change_ff_uid(client: AsyncClient):
    """
    Supplying ff_uid in the PATCH body must not change the player's UID.
    The endpoint only accepts ff_name; unknown fields are ignored by Pydantic.
    The original ff_uid must remain unchanged after the call.
    """
    token = await _register_and_login(
        client, username="uidtest1", ff_uid="1234500001"
    )

    # Attempt to pass ff_uid alongside ff_name
    resp = await client.patch(
        "/api/users/me",
        json={"ff_name": "GoodName", "ff_uid": "9999999999"},
        headers=_auth(token),
    )
    # Must succeed (ff_uid extra field is silently ignored)
    assert resp.status_code == 200

    # Verify the ff_uid was NOT changed
    me = await client.get("/api/users/me", headers=_auth(token))
    assert me.json()["ff_uid"] == "1234500001", (
        f"ff_uid was changed! Got {me.json()['ff_uid']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 10. PATCH — cannot change username
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_me_cannot_change_username(client: AsyncClient):
    """
    Supplying username in the PATCH body must not change the login identifier.
    """
    token = await _register_and_login(
        client, username="nametest1", ff_uid="1234500002"
    )

    resp = await client.patch(
        "/api/users/me",
        json={"ff_name": "GoodName", "username": "hackedname"},
        headers=_auth(token),
    )
    assert resp.status_code == 200

    me = await client.get("/api/users/me", headers=_auth(token))
    assert me.json()["username"] == "nametest1", (
        f"username was changed! Got {me.json()['username']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 11. Login still works after ff_name change
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_works_after_ff_name_change(client: AsyncClient):
    """
    Changing ff_name must not affect the player's ability to log in.
    Login uses `username` (never `ff_name`).
    """
    token = await _register_and_login(
        client, username="logintest1", ff_uid="1234500003"
    )

    # Change the ff_name
    patch = await client.patch(
        "/api/users/me",
        json={"ff_name": "NewDisplayName"},
        headers=_auth(token),
    )
    assert patch.status_code == 200

    # Login with the original username and password — must still work
    login_resp = await client.post(
        "/api/auth/login",
        json={"username": "logintest1", "password": "TestPass@123"},
    )
    assert login_resp.status_code == 200, (
        f"Login failed after ff_name change: {login_resp.text}"
    )
    new_token = login_resp.json()["access_token"]
    assert new_token  # non-empty

    # New token must work for /me and return the updated ff_name
    me = await client.get("/api/users/me", headers=_auth(new_token))
    assert me.status_code == 200
    assert me.json()["ff_name"] == "NewDisplayName"
    assert me.json()["username"] == "logintest1"  # login key unchanged


# ─────────────────────────────────────────────────────────────────────────────
# 12. Registration with ff_name persists correctly
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_registration_with_ff_name(client: AsyncClient):
    """ff_name supplied at registration must be stored and returned by /me."""
    token = await _register_and_login(
        client, username="regfftest1", ff_uid="1234500004", ff_name="RegPlayer"
    )
    me = await client.get("/api/users/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["ff_name"] == "RegPlayer"


@pytest.mark.asyncio
async def test_registration_response_includes_ff_name(client: AsyncClient):
    """Registration response must include ff_name field."""
    resp = await client.post("/api/auth/register", json={
        "username": "regfftest2",
        "password": "TestPass@123",
        "confirm_password": "TestPass@123",
        "ff_uid": "1234500005",
        "ff_name": "RegDisplay",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert "ff_name" in body
    assert body["ff_name"] == "RegDisplay"


# ─────────────────────────────────────────────────────────────────────────────
# 13. Registration without ff_name → ff_name is None
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_registration_without_ff_name(client: AsyncClient):
    """ff_name is optional at registration. Omitting it stores NULL."""
    resp = await client.post("/api/auth/register", json={
        "username": "regfftest3",
        "password": "TestPass@123",
        "confirm_password": "TestPass@123",
        "ff_uid": "1234500006",
    })
    assert resp.status_code == 201

    login_resp = await client.post(
        "/api/auth/login",
        json={"username": "regfftest3", "password": "TestPass@123"},
    )
    token = login_resp.json()["access_token"]
    me = await client.get("/api/users/me", headers=_auth(token))
    assert me.json()["ff_name"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 14. Unauthenticated PATCH → 401/403
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_me_requires_auth(client: AsyncClient):
    """PATCH /api/users/me without a token must return 401 or 403."""
    resp = await client.patch("/api/users/me", json={"ff_name": "Hacker"})
    assert resp.status_code in (401, 403), (
        f"Expected 401/403, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_patch_me_rejects_staff_token(client: AsyncClient):
    """Staff (admin) JWT must not work on the player /api/users/me endpoint."""
    staff_resp = await login_staff(client)
    staff_token = staff_resp.json()["access_token"]

    resp = await client.patch(
        "/api/users/me",
        json={"ff_name": "AdminHijack"},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    # Staff token carries audience "staff" — player endpoint rejects it
    assert resp.status_code in (401, 403), (
        f"Staff token was accepted on player endpoint: {resp.status_code}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 15. Same-value no-op returns 200
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_me_same_value_is_noop(client: AsyncClient):
    """Setting ff_name to the same value as current returns 200 with a clear message."""
    token = await _register_and_login(
        client, username="nooptest1", ff_uid="1234500007", ff_name="SameName"
    )

    resp = await client.patch(
        "/api/users/me",
        json={"ff_name": "SameName"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ff_name"] == "SameName"
    # Should indicate no change was needed (not an error)
    assert body.get("message")


# ─────────────────────────────────────────────────────────────────────────────
# 16. Regression — existing player auth tests not broken
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_without_ff_name_still_works(client: AsyncClient):
    """Existing registration flow (no ff_name field) must be unchanged."""
    resp = await client.post("/api/auth/register", json={
        "username": "legacyuser1",
        "password": "LegacyPass@1",
        "confirm_password": "LegacyPass@1",
        "ff_uid": "9876543210",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "legacyuser1"
    assert body["ff_uid"] == "9876543210"


@pytest.mark.asyncio
async def test_username_is_still_login_key(client: AsyncClient):
    """Login still uses username, not ff_name, as the authentication key."""
    # Register with a distinctive ff_name
    await client.post("/api/auth/register", json={
        "username": "loginkey1",
        "password": "TestPass@123",
        "confirm_password": "TestPass@123",
        "ff_uid": "1234500008",
        "ff_name": "DisplayOnly",
    })

    # Login with username — must work
    ok = await client.post(
        "/api/auth/login",
        json={"username": "loginkey1", "password": "TestPass@123"},
    )
    assert ok.status_code == 200

    # Attempting to login with ff_name as username — must fail (wrong identifier)
    fail = await client.post(
        "/api/auth/login",
        json={"username": "DisplayOnly", "password": "TestPass@123"},
    )
    assert fail.status_code in (401, 422), (
        f"Logging in with ff_name as username should fail, got {fail.status_code}"
    )


@pytest.mark.asyncio
async def test_ff_uid_remains_read_only_throughout(client: AsyncClient):
    """
    The ff_uid field is never writable by the player.
    It must stay at the value set at registration regardless of PATCH calls.
    """
    original_uid = "1234500009"
    token = await _register_and_login(
        client, username="uidreadonly1", ff_uid=original_uid
    )

    # Multiple PATCH calls trying to sneak in different ff_uid values
    for attempt_uid in ["9999999999", "0000000000", original_uid + "1"]:
        await client.patch(
            "/api/users/me",
            json={"ff_name": "AnyName", "ff_uid": attempt_uid},
            headers=_auth(token),
        )

    me = await client.get("/api/users/me", headers=_auth(token))
    assert me.json()["ff_uid"] == original_uid, (
        f"ff_uid was mutated! Expected {original_uid}, got {me.json()['ff_uid']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Free Fire Unicode / stylish name acceptance tests (new rules)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name,uid_suffix", [
    ("MANISH*7610*",  "01"),   # asterisks allowed
    ("꧁MANISH꧂",     "02"),   # decorative brackets
    ("★MANISH★",      "03"),   # Unicode stars
    ("ᴍᴀɴɪsʜ",       "04"),   # small-caps Unicode
    ("MANISH⚡",       "05"),   # lightning bolt
    ("★Fire★King★",   "06"),   # multiple Unicode symbols within 15
    ("AK47@Pro!",     "07"),   # @ and ! allowed
    ("❖Hero❖",        "08"),   # decorative diamond
    ("༄FireKing",     "09"),   # Tibetan mark
])
async def test_patch_me_accepts_stylish_ff_names(
    client: AsyncClient, name: str, uid_suffix: str
):
    """Unicode/stylish Free Fire names within 15 chars must be accepted."""
    assert len(name) <= 15, f"Test name {name!r} itself exceeds 15 chars — fix the test"
    uname  = f"ffstyle{uid_suffix}"
    ff_uid = f"150000{uid_suffix.zfill(4)}"
    token = await _register_and_login(client, username=uname, ff_uid=ff_uid)
    resp = await client.patch("/api/users/me", json={"ff_name": name}, headers=_auth(token))
    assert resp.status_code == 200, (
        f"Expected 200 for stylish name {name!r}, got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["ff_name"] == name


@pytest.mark.asyncio
async def test_patch_me_rejects_name_over_15_unicode_chars(client: AsyncClient):
    """A name of 16 Unicode characters must be rejected regardless of byte length."""
    # "★" is 3 UTF-8 bytes but 1 Unicode char; 16 of them = 16 chars, 48 bytes
    name_16 = "★" * 16
    assert len(name_16) == 16
    token = await _register_and_login(client, username="fflen16", ff_uid="1500000099")
    resp = await client.patch("/api/users/me", json={"ff_name": name_16}, headers=_auth(token))
    assert resp.status_code == 422, f"16-char name should be rejected: {resp.text}"


@pytest.mark.asyncio
async def test_patch_me_accepts_name_exactly_15_unicode_chars(client: AsyncClient):
    """Exactly 15 Unicode characters (even multi-byte) must be accepted."""
    name_15 = "★" * 15
    assert len(name_15) == 15
    token = await _register_and_login(client, username="fflen15", ff_uid="1500000098")
    resp = await client.patch("/api/users/me", json={"ff_name": name_15}, headers=_auth(token))
    assert resp.status_code == 200, f"15-char Unicode name should be accepted: {resp.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("xss_name", [
    "<script>alert(1)</script>",
    "<b>bold</b>",
    ">injected",
    "name<evil",
])
async def test_patch_me_rejects_html_injection(
    client: AsyncClient, xss_name: str
):
    """Names containing < or > must be rejected to prevent HTML injection."""
    token = await _register_and_login(client, username="ffxss1", ff_uid="1600000001")
    resp = await client.patch("/api/users/me", json={"ff_name": xss_name}, headers=_auth(token))
    assert resp.status_code == 422, (
        f"HTML injection name {xss_name!r} should be rejected, got {resp.status_code}"
    )
