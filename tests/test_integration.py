import asyncio
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.models.user import User
from app.repositories.user import UserRepository
from app.services.auth_service import AuthService
from app.services.prize_service import PrizeService
from app.services.volunteer_hours_service import VolunteerHoursService
from app.schemas.user import AdminCreate
from app.utilities.security import encrypt_password


class TestUpload:
    filename = "proof.png"
    content_type = "image/png"

    async def read(self):
        return b"integration-test-proof"


@pytest.fixture
def db_session(monkeypatch, tmp_path: Path):
    from app.services import volunteer_hours_service

    upload_dir = tmp_path / "uploads"
    real_path = volunteer_hours_service.Path

    def test_path(value):
        if value == "app/static/uploads":
            return upload_dir
        return real_path(value)

    monkeypatch.setattr(volunteer_hours_service, "Path", test_path)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


def register_user(session: Session, username: str, email: str) -> User:
    service = AuthService(UserRepository(session))
    return service.register_user(
        username=username,
        email=email,
        password="correct-password",
    )


def submit_log(
    service: VolunteerHoursService,
    user_id: int,
    hours: float = 2,
    proof: TestUpload | None = None,
):
    return asyncio.run(
        service.submit_student_log(
            user_id=user_id,
            event_name="Campus clean-up",
            hours=hours,
            event_date=date(2026, 1, 15),
            additional_info="Integration test submission",
            proof=proof or TestUpload(),
        )
    )


def test_register_and_authenticate(db_session: Session):
    user = register_user(db_session, "alice", "alice@example.com")
    auth_service = AuthService(UserRepository(db_session))

    stored_user = UserRepository(db_session).get_by_username("alice")

    assert stored_user is not None
    assert stored_user.id == user.id
    assert auth_service.authenticate_user("alice", "correct-password")
    assert auth_service.authenticate_user("alice", "wrong-password") is None


def test_submit_log_saved(db_session: Session, tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user = register_user(db_session, "student", "student@example.com")
    service = VolunteerHoursService(db_session)
    log = submit_log(service, user.id)
    logs = service.get_student_logs(user.id)

    assert len(logs) == 1
    assert logs[0].log_id == log.log_id
    assert logs[0].user_id == user.id
    assert logs[0].status == "pending"
    assert (upload_dir / Path(log.proof).name).is_file()


def test_approve_updates_progress(db_session: Session):
    user = register_user(db_session, "progress", "progress@example.com")
    service = VolunteerHoursService(db_session)
    log = submit_log(service, user.id, hours=25)

    service.approve_submission(log.log_id)
    progress = service.get_progress(user.id)
    bronze = next(item for item in progress["milestones"] if item["badge"] == "Bronze")

    assert progress["approved_hours"] == 25
    assert bronze["achieved"] is True
    assert bronze["state"] == "Unlocked"


def test_reject_stores_reason(db_session: Session):
    user = register_user(db_session, "rejected", "rejected@example.com")
    service = VolunteerHoursService(db_session)
    log = submit_log(service, user.id, hours=3)

    service.reject_submission(log.log_id, "Proof was unclear")
    summary = service.get_student_submission_summary(user.id)

    assert len(summary["rejected"]) == 1
    assert summary["rejected"][0].rejection_reason == "Proof was unclear"
    assert summary["rejected"][0].status == "rejected"


def test_leaderboard_ranks_approved_hours(db_session: Session):
    first = register_user(db_session, "first", "first@example.com")
    second = register_user(db_session, "second", "second@example.com")
    admin = UserRepository(db_session).create(
        AdminCreate(
            username="administrator",
            email="administrator@example.com",
            password=encrypt_password("admin-password"),
        )
    )
    service = VolunteerHoursService(db_session)

    first_log = submit_log(service, first.id, hours=10)
    second_approved = submit_log(service, second.id, hours=20)
    second_pending = submit_log(service, second.id, hours=100)
    admin_log = submit_log(service, admin.id, hours=200)
    service.approve_submission(first_log.log_id)
    service.approve_submission(second_approved.log_id)
    service.approve_submission(admin_log.log_id)

    leaderboard = service.get_leaderboard(first.id, show_all=True)
    entries = leaderboard["entries"]

    assert [entry["username"] for entry in entries] == ["second", "first"]
    assert entries[0]["hours"] == 20
    assert entries[1]["hours"] == 10
    assert all(entry["username"] != "administrator" for entry in entries)
    assert all(entry["hours"] != 120 for entry in entries)
    assert second_pending.status == "pending"


def test_redeem_prize_updates_balance_and_stock(db_session: Session):
    user = register_user(db_session, "redeemer", "redeemer@example.com")
    hours_service = VolunteerHoursService(db_session)
    approved_log = submit_log(hours_service, user.id, hours=25)
    hours_service.approve_submission(approved_log.log_id)
    prize_service = PrizeService(db_session)

    catalog_before = prize_service.get_catalog(user.id)
    prize = next(
        prize for prize in catalog_before["prizes"] if prize.name == "Campus T-shirt"
    )
    initial_stock = prize.stock
    redemption = prize_service.redeem(user.id, prize.prize_id)
    catalog_after = prize_service.get_catalog(user.id)
    redeemed_prize = next(
        item for item in catalog_after["prizes"] if item.prize_id == prize.prize_id
    )

    assert catalog_after["spendable_balance"] == 15
    assert redeemed_prize.stock == initial_stock - 1
    assert redemption.user_id == user.id
    assert redemption.prize_id == prize.prize_id
    assert redemption in catalog_after["redemptions"]


def test_manual_prize_fulfilment(db_session: Session):
    user = register_user(db_session, "fulfilment", "fulfilment@example.com")
    hours_service = VolunteerHoursService(db_session)
    approved_log = submit_log(hours_service, user.id, hours=25)
    hours_service.approve_submission(approved_log.log_id)
    prize_service = PrizeService(db_session)
    prize = next(
        prize
        for prize in prize_service.get_catalog(user.id)["prizes"]
        if prize.delivery_type == "manual"
    )

    redemption = prize_service.redeem(user.id, prize.prize_id)
    pending = prize_service.get_pending_fulfilments()

    assert redemption.status == "pending"
    assert any(item["redemption"].redemption_id == redemption.redemption_id for item in pending)

    prize_service.fulfill(redemption.redemption_id)

    assert prize_service.get_pending_fulfilments() == []
    assert redemption.status == "fulfilled"
    assert redemption.fulfilled_at is not None
