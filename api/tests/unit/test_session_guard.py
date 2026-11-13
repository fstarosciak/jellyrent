import uuid
from datetime import UTC, datetime, timedelta

from app.jellyfin_client import SessionInfo
from app.models import ViolationReason
from app.session_guard import TrackedCopy, decide

ALICE_JF = uuid.uuid4()
BOB_JF = uuid.uuid4()
ITEM = uuid.uuid4()
OTHER_ITEM = uuid.uuid4()
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def session(sid: str, user: uuid.UUID | None, item: uuid.UUID | None = ITEM) -> SessionInfo:
    return SessionInfo(
        id=sid, user_id=user, user_name=None, device_name=f"device-{sid}", client="web",
        now_playing_item_id=item,
    )


def test_renter_with_one_session_is_fine() -> None:
    copies = {ITEM: TrackedCopy(copy_id=1, renter_user_id=10, renter_jf_user_id=ALICE_JF)}
    assert decide([session("a", ALICE_JF)], copies, {}) == []


def test_untracked_items_and_idle_sessions_are_ignored() -> None:
    copies = {ITEM: TrackedCopy(1, 10, ALICE_JF)}
    sessions = [session("a", BOB_JF, OTHER_ITEM), session("b", BOB_JF, None)]
    assert decide(sessions, copies, {}) == []


def test_viewer_without_rental_is_stopped() -> None:
    copies = {ITEM: TrackedCopy(1, 10, ALICE_JF)}
    (enforcement,) = decide([session("b", BOB_JF)], copies, {}, accounts={BOB_JF: 20})
    assert enforcement.reason is ViolationReason.NO_RENTAL
    assert enforcement.session_id == "b"
    assert enforcement.user_id == 20


def test_unrented_copy_cannot_be_played_by_anyone() -> None:
    copies = {ITEM: TrackedCopy(1, None, None)}
    (enforcement,) = decide([session("x", ALICE_JF)], copies, {})
    assert enforcement.reason is ViolationReason.NO_RENTAL


def test_second_connection_of_renter_is_stopped_oldest_wins() -> None:
    copies = {ITEM: TrackedCopy(1, 10, ALICE_JF)}
    first_seen = {("phone", 1): T0, ("tv", 1): T0 + timedelta(seconds=30)}
    result = decide([session("tv", ALICE_JF), session("phone", ALICE_JF)], copies, first_seen)
    assert [(e.session_id, e.reason) for e in result] == [
        ("tv", ViolationReason.CONCURRENT_SESSION)
    ]


def test_unknown_first_seen_loses_against_known() -> None:
    copies = {ITEM: TrackedCopy(1, 10, ALICE_JF)}
    result = decide(
        [session("new", ALICE_JF), session("old", ALICE_JF)], copies, {("old", 1): T0}
    )
    assert [e.session_id for e in result] == ["new"]


def test_mixed_violations() -> None:
    copies = {ITEM: TrackedCopy(1, 10, ALICE_JF)}
    result = decide(
        [session("a1", ALICE_JF), session("a2", ALICE_JF), session("b", BOB_JF)],
        copies,
        {("a1", 1): T0, ("a2", 1): T0 + timedelta(seconds=1)},
    )
    assert {(e.session_id, e.reason) for e in result} == {
        ("b", ViolationReason.NO_RENTAL),
        ("a2", ViolationReason.CONCURRENT_SESSION),
    }
