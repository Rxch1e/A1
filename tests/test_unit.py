import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.prize import Prize
from app.services.auth_service import AuthService
from app.services.prize_service import PrizeService
from app.services.volunteer_hours_service import VolunteerHoursService


class DummyUpload:
    """An in-memory upload used by submission validation tests."""

    filename = "proof.png"
    content_type = "image/png"

    async def read(self):
        return b"proof data"


def make_volunteer_log(status, hours, event_date, submitted_at=None):
    return SimpleNamespace(
        status=status,
        hours=hours,
        event_date=event_date,
        submitted_at=submitted_at or datetime(2026, 1, 1),
    )


def test_submit_rejects_zero_hours():
    service = VolunteerHoursService(MagicMock())

    with pytest.raises(ValueError, match=r"^Hours must be greater than 0\.$"):
        asyncio.run(
            service.submit_student_log(
                user_id=1,
                event_name="Campus clean-up",
                hours=0,
                event_date=date(2026, 11, 1),
                additional_info=None,
                proof=DummyUpload(),
            )
        )


def test_submit_rejects_future_date():
    service = VolunteerHoursService(MagicMock())

    with pytest.raises(ValueError, match=r"^Event date cannot be in the future\.$"):
        asyncio.run(
            service.submit_student_log(
                user_id=1,
                event_name="Campus clean-up",
                hours=2,
                event_date=date.today() + timedelta(days=1),
                additional_info=None,
                proof=DummyUpload(),
            )
        )


def test_reject_requires_reason():
    service = VolunteerHoursService(MagicMock())
    service.volunteer_log_repo = MagicMock()

    with pytest.raises(ValueError, match=r"^A rejection reason is required\.$"):
        service.reject_submission(log_id=7, rejection_reason="  ")

    service.volunteer_log_repo.update_status.assert_not_called()


def test_badge_thresholds():
    service = VolunteerHoursService(MagicMock())

    assert service._highest_badge(24) == "Not yet unlocked"
    assert service._highest_badge(25) == "Bronze"
    assert service._highest_badge(50) == "Silver"
    assert service._highest_badge(150) == "Gold"


def test_progress_counts_only_approved():
    service = VolunteerHoursService(MagicMock())
    service.volunteer_log_repo = MagicMock()
    service.volunteer_log_repo.get_by_user_id.return_value = [
        make_volunteer_log("approved", 30, date(2026, 1, 1)),
        make_volunteer_log("pending", 20, date(2026, 1, 2)),
        make_volunteer_log("rejected", 10, date(2026, 1, 3)),
    ]

    progress = service.get_progress(user_id=1)

    assert progress["approved_hours"] == 30
    bronze = next(item for item in progress["milestones"] if item["badge"] == "Bronze")
    silver = next(item for item in progress["milestones"] if item["badge"] == "Silver")
    assert bronze["achieved"] is True
    assert silver["remaining_hours"] == 20
    service.volunteer_log_repo.get_by_user_id.assert_called_once_with(1)


def test_leaderboard_excludes_admin():
    service = VolunteerHoursService(MagicMock())
    service.volunteer_log_repo = MagicMock()
    service.user_repo = MagicMock()
    service.volunteer_log_repo.get_approved.return_value = [
        make_volunteer_log("approved", 40, date(2026, 1, 1)),
        make_volunteer_log("approved", 10, date(2026, 1, 2)),
    ]
    service.volunteer_log_repo.get_approved.return_value[0].user_id = 1
    service.volunteer_log_repo.get_approved.return_value[1].user_id = 2
    service.user_repo.get_all_users.return_value = [
        SimpleNamespace(id=1, username="alice", role="student"),
        SimpleNamespace(id=2, username="bob", role="student"),
        SimpleNamespace(id=3, username="administrator", role="admin"),
    ]

    leaderboard = service.get_leaderboard(user_id=1, show_all=True)

    assert all(entry["username"] != "administrator" for entry in leaderboard["entries"])
    alice = next(entry for entry in leaderboard["entries"] if entry["username"] == "alice")
    assert alice["rank"] == 1
    service.volunteer_log_repo.get_approved.assert_called_once_with()
    service.user_repo.get_all_users.assert_called_once_with()


def test_spendable_balance():
    service = PrizeService(MagicMock())
    service.volunteer_log_repo = MagicMock()
    service.redemption_repo = MagicMock()
    service.prize_repo = MagicMock()
    service.volunteer_log_repo.get_by_user_id.return_value = [
        make_volunteer_log("approved", 42, date(2026, 1, 1)),
    ]
    service.redemption_repo.get_redeemed_hours.return_value = 10
    service.redemption_repo.get_by_user_id.return_value = []
    service.prize_repo.get_all.return_value = [
        Prize(prize_id=1, name="Campus T-shirt", hours_cost=10, stock=5),
    ]

    catalog = service.get_catalog(user_id=1)

    assert catalog["spendable_balance"] == 32
    service.redemption_repo.get_redeemed_hours.assert_called_once_with(1)


def test_redeem_insufficient_balance():
    service = PrizeService(MagicMock())
    service.prize_repo = MagicMock()
    service.redemption_repo = MagicMock()
    service.volunteer_log_repo = MagicMock()
    prize = Prize(
        prize_id=1,
        name="Campus T-shirt",
        image_url="img/prize-shirt.svg",
        hours_cost=10,
        stock=5,
    )
    service.prize_repo.get_all.return_value = [prize]
    service.prize_repo.get_by_id.return_value = prize
    service.volunteer_log_repo.get_by_user_id.return_value = []
    service.redemption_repo.get_redeemed_hours.return_value = 0
    service.redemption_repo.get_by_user_id.return_value = []

    with pytest.raises(ValueError, match=r"^You do not have enough spendable hours\.$"):
        service.redeem(user_id=1, prize_id=1)

    service.prize_repo.save.assert_not_called()
    service.redemption_repo.create.assert_not_called()


def test_redeem_instant_vs_manual():
    service = PrizeService(MagicMock())
    service.prize_repo = MagicMock()
    service.redemption_repo = MagicMock()
    instant_prize = Prize(
        prize_id=1,
        name="Recognition Certificate",
        hours_cost=10,
        stock=2,
        delivery_type="instant",
    )
    manual_prize = Prize(
        prize_id=2,
        name="Campus T-shirt",
        hours_cost=10,
        stock=2,
        delivery_type="manual",
    )
    service._ensure_catalog = MagicMock(return_value=[instant_prize, manual_prize])
    service.get_catalog = MagicMock(return_value={"spendable_balance": 100})
    service.prize_repo.get_by_id.side_effect = [instant_prize, manual_prize]
    service.redemption_repo.create.side_effect = lambda redemption: redemption

    instant_redemption = service.redeem(user_id=1, prize_id=1)
    manual_redemption = service.redeem(user_id=1, prize_id=2)

    assert instant_redemption.status == "fulfilled"
    assert instant_redemption.fulfilled_at is not None
    assert manual_redemption.status == "pending"
    assert manual_redemption.fulfilled_at is None
    assert instant_prize.stock == 1
    assert manual_prize.stock == 1
    assert service.redemption_repo.create.call_count == 2


def test_authenticate_user():
    user_repo = MagicMock()
    service = AuthService(user_repo)
    user = SimpleNamespace(id=4, role="student", password="stored-password")
    user_repo.get_by_username.return_value = user

    with patch("app.services.auth_service.verify_password") as verify_password:
        verify_password.return_value = False
        assert service.authenticate_user("alice", "wrong") is None
        verify_password.assert_called_once_with(
            plaintext_password="wrong",
            encrypted_password="stored-password",
        )

    with (
        patch("app.services.auth_service.verify_password", return_value=True) as verify_password,
        patch(
            "app.services.auth_service.create_access_token",
            return_value="token-value",
        ) as create_access_token,
    ):
        token = service.authenticate_user("alice", "correct")

    assert isinstance(token, str)
    assert token == "token-value"
    verify_password.assert_called_once_with(
        plaintext_password="correct",
        encrypted_password="stored-password",
    )
    create_access_token.assert_called_once_with(data={"sub": "4", "role": "student"})
