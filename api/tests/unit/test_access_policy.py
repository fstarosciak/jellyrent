from app.access import ACCESS_TAG_PREFIX, access_tag, apply_access_policy, is_managed_tag

SENTINEL = "jellyrent-none"


def policy(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "IsAdministrator": False,
        "AllowedTags": [],
        "BlockedTags": [],
        "MaxActiveSessions": 0,
        "EnableContentDeletion": True,
        "EnableContentDownloading": True,
        "AuthenticationProviderId": "Default",
        "PasswordResetProviderId": "Default",
    }
    return {**base, **overrides}


def test_access_tag_format_matches_sql_function() -> None:
    # db/init/03_functions.sql: access_tag(c copy) = 'rent-' || c.id
    assert access_tag(42) == "rent-42"
    assert ACCESS_TAG_PREFIX == "rent-"


def test_new_account_sees_nothing() -> None:
    """An empty AllowedTags list means 'no restriction' in Jellyfin."""
    result = apply_access_policy(policy(), set(), SENTINEL)
    assert result["AllowedTags"] == [SENTINEL]


def test_grants_exactly_the_desired_tags() -> None:
    result = apply_access_policy(
        policy(AllowedTags=[SENTINEL, "rent-1"]), {"rent-2", "rent-3"}, SENTINEL
    )
    assert result["AllowedTags"] == [SENTINEL, "rent-2", "rent-3"]


def test_preserves_tags_not_managed_by_jellyrent() -> None:
    result = apply_access_policy(policy(AllowedTags=["kids", "rent-9"]), set(), SENTINEL)
    assert result["AllowedTags"] == ["kids", SENTINEL]


def test_enforces_account_restrictions_and_keeps_unknown_fields() -> None:
    result = apply_access_policy(policy(SomeFutureField=123), {"rent-1"}, SENTINEL)
    assert result["MaxActiveSessions"] == 1
    assert result["EnableContentDeletion"] is False
    assert result["EnableContentDownloading"] is False
    assert result["SomeFutureField"] == 123
    assert result["AuthenticationProviderId"] == "Default"


def test_is_idempotent() -> None:
    once = apply_access_policy(policy(), {"rent-5"}, SENTINEL)
    assert apply_access_policy(once, {"rent-5"}, SENTINEL) == once


def test_does_not_mutate_input() -> None:
    original = policy(AllowedTags=["rent-1"])
    apply_access_policy(original, set(), SENTINEL)
    assert original["AllowedTags"] == ["rent-1"]


def test_managed_tag_detection() -> None:
    assert is_managed_tag("rent-12", SENTINEL)
    assert is_managed_tag(SENTINEL, SENTINEL)
    assert not is_managed_tag("rental", SENTINEL)
