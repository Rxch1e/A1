from typing import Annotated, Literal

import jwt
from fastapi import Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, EmailStr

from app.config import get_settings
from app.dependencies.session import SessionDep
from app.models.user import User
from app.repositories.user import UserRepository
from app.services.auth_service import AuthService
from app.services.prize_service import PrizeService
from app.services.volunteer_hours_service import VolunteerHoursService
from . import api_router


class SignupPayload(BaseModel):
    username: str
    email: EmailStr
    password: str


class LoginPayload(BaseModel):
    username: str
    password: str


class VolunteerHoursPayload(BaseModel):
    event_name: str
    hours: float
    event_date: str
    additional_info: str | None = None


class VolunteerHoursStatusUpdate(BaseModel):
    status: Literal["approved", "rejected"]
    rejection_reason: str | None = None


class PrizeRedemptionPayload(BaseModel):
    prize_id: int


class RedemptionStatusUpdate(BaseModel):
    status: Literal["fulfilled"]


async def get_current_api_user(request: Request, db: SessionDep) -> User:
    auth_header = request.headers.get("authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )

    try:
        payload = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=[get_settings().jwt_algorithm],
        )
        user_id = payload.get("sub")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )

    repo = UserRepository(db)
    user = repo.get_by_id(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )
    return user


APIUserDep = Annotated[User, Depends(get_current_api_user)]


async def require_student(user: User = Depends(get_current_api_user)):
    if user.role == "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Student access required.",
        )
    return user


async def require_admin(user: User = Depends(get_current_api_user)):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return user


@api_router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup_api(payload: SignupPayload, db: SessionDep):
    user_repo = UserRepository(db)
    auth_service = AuthService(user_repo)
    try:
        user = auth_service.register_user(payload.username, payload.email, payload.password)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already exists",
        )
    return {"id": user.id, "username": user.username, "email": user.email}


@api_router.post("/login")
async def login_api(payload: LoginPayload, db: SessionDep):
    user_repo = UserRepository(db)
    auth_service = AuthService(user_repo)
    token = auth_service.authenticate_user(payload.username, payload.password)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    return {"access_token": token, "token_type": "bearer"}


@api_router.post("/volunteer-hours", status_code=status.HTTP_201_CREATED)
async def create_volunteer_hours_api(
    db: SessionDep,
    user: User = Depends(require_student),
    event_name: str = Form(...),
    hours: float = Form(...),
    event_date: str = Form(...),
    additional_info: str | None = Form(default=None),
    proof: UploadFile = File(...),
):
    service = VolunteerHoursService(db)
    try:
        log = await service.submit_student_log(
            user_id=user.id,
            event_name=event_name,
            hours=hours,
            event_date=__import__("datetime").date.fromisoformat(event_date),
            additional_info=additional_info,
            proof=proof,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return log.model_dump()


@api_router.get("/volunteer-hours")
async def list_volunteer_hours_api(
    db: SessionDep,
    user: User = Depends(require_student),
):
    logs = VolunteerHoursService(db).get_student_logs(user.id)
    return [log.model_dump() for log in logs]


@api_router.patch("/volunteer-hours/{log_id}")
async def update_volunteer_hours_api(
    log_id: int,
    payload: VolunteerHoursStatusUpdate,
    db: SessionDep,
    user: User = Depends(require_admin),
):
    service = VolunteerHoursService(db)
    try:
        if payload.status == "rejected":
            reason = (payload.rejection_reason or "").strip()
            if not reason:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="A rejection reason is required.",
                )
            service.reject_submission(log_id, reason)
            updated_status = "rejected"
        else:
            service.approve_submission(log_id)
            updated_status = "approved"
    except HTTPException:
        raise
    except ValueError as exc:
        message = str(exc)
        if message == "Volunteer log not found.":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message) from exc
    return {"log_id": log_id, "status": updated_status}


@api_router.get("/progress")
async def progress_api(
    db: SessionDep,
    user: User = Depends(require_student),
):
    return VolunteerHoursService(db).get_progress(user.id)


@api_router.get("/leaderboard")
async def leaderboard_api(
    db: SessionDep,
    user: User = Depends(require_student),
    badge: str | None = None,
    letter: str | None = None,
    show_all: bool = False,
):
    return VolunteerHoursService(db).get_leaderboard(
        user.id,
        badge_filter=badge or "",
        letter_filter=letter or "",
        show_all=show_all,
    )


@api_router.get("/prizes")
async def prizes_api(
    db: SessionDep,
    user: User = Depends(require_student),
):
    catalog = PrizeService(db).get_catalog(user.id)
    return {
        "prizes": catalog["prizes"],
        "approved_hours": catalog["approved_hours"],
        "redeemed_hours": catalog["redeemed_hours"],
        "spendable_balance": catalog["spendable_balance"],
    }


@api_router.post("/redemptions", status_code=status.HTTP_201_CREATED)
async def create_redemption_api(
    payload: PrizeRedemptionPayload,
    db: SessionDep,
    user: User = Depends(require_student),
):
    try:
        redemption = PrizeService(db).redeem(user.id, payload.prize_id)
    except ValueError as exc:
        message = str(exc)
        if message == "Prize not found.":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message) from exc
    return redemption.model_dump()


@api_router.patch("/redemptions/{redemption_id}")
async def update_redemption_api(
    redemption_id: int,
    payload: RedemptionStatusUpdate,
    db: SessionDep,
    user: User = Depends(require_admin),
):
    try:
        PrizeService(db).fulfill(redemption_id)
    except ValueError as exc:
        message = str(exc)
        if message == "Redemption not found.":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message) from exc
    return {"redemption_id": redemption_id, "status": payload.status}
