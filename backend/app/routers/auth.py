"""Authentication endpoints (Architecture.md §4.0)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core import csrf, rate_limit, security
from app.core.config import settings
from app.core.dependencies import CurrentUser, DbSession
from app.models.enums import AuditAction, AuditTargetType
from app.schemas.auth import (
    CsrfOut,
    LoginRequest,
    SessionOut,
    SignupRequest,
    UserOut,
    WorkspaceSummary,
)
from app.services import audit, auth, invitations

router = APIRouter(prefix=f"{settings.api_v1_prefix}/auth", tags=["auth"])

LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 15 * 60

# One deliberately generic message for every failure mode, so a caller cannot tell
# "no such account" from "wrong password" (Architecture.md §4.0).
INVALID_CREDENTIALS = "Incorrect email or password."


async def _session_payload(db, user) -> tuple[SessionOut, str]:
    """Build the session body and mint an access token for it."""
    workspaces = await auth.list_workspaces_for_user(db, user.id)
    access_token, expires_at = security.create_access_token(user.id)
    return SessionOut(
        user=UserOut.model_validate(user),
        workspaces=[
            WorkspaceSummary(
                id=workspace.id,
                name=workspace.name,
                is_personal=workspace.is_personal,
                role=role,
            )
            for workspace, role in workspaces
        ],
        access_token_expires_at=expires_at,
    ), access_token


@router.post("/signup", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    db: DbSession,
) -> SessionOut:
    """Create an account plus its personal workspace, in one transaction.

    When an invitation token is supplied the email comes from the invitation, not from
    the request body — otherwise a token holder could register any address they liked
    and still consume the invitation.
    """
    invitation = None
    email = payload.email.lower()

    if payload.invitation_token:
        invitation = await invitations.get_by_token(db, payload.invitation_token)
        if invitation is None or not invitations.is_usable(invitation):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This invitation is no longer valid.")
        email = invitation.email.lower()

    if await auth.get_user_by_email(db, email) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An account with that email already exists.",
        )

    user = await auth.create_user(
        db,
        email=email,
        password=payload.password,
        full_name=payload.full_name,
        # Holding the invitation token proves control of the address.
        email_verified=invitation is not None,
    )
    await auth.create_personal_workspace(db, user)

    if invitation is not None:
        await invitations.accept(db, invitation=invitation, user=user)
        await audit.record(
            db,
            action=AuditAction.INVITATION_ACCEPTED,
            target_type=AuditTargetType.WORKSPACE_INVITATION,
            target_id=invitation.id,
            workspace_id=invitation.workspace_id,
            actor_id=user.id,
            request=request,
            metadata={
                "label": invitation.email,
                "role": invitation.role.value,
                "via": "signup",
            },
        )

    await audit.record(
        db,
        action=AuditAction.LOGIN,
        target_type=AuditTargetType.USER,
        target_id=user.id,
        actor_id=user.id,
        request=request,
        metadata={"via": "signup"},
    )

    session, access_token = await _session_payload(db, user)
    refresh_token = await auth.issue_refresh_token(db, user, request=request)
    await db.commit()

    security.set_auth_cookies(response, access_token, refresh_token)
    # A new identity gets a new token: one obtained before signing in is worthless after.
    session.csrf_token = csrf.rotate(response)
    return session


@router.post("/login", response_model=SessionOut)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
) -> SessionOut:
    email = payload.email.lower()
    ip = audit.client_ip(request) or "unknown"

    # Throttle by IP and by account: the first blunts spraying across many accounts,
    # the second blunts a distributed attack on one account.
    ip_ok, _ = await rate_limit.hit(
        f"login:ip:{ip}", limit=LOGIN_MAX_ATTEMPTS * 5, window_seconds=LOGIN_WINDOW_SECONDS
    )
    email_ok, _ = await rate_limit.hit(
        f"login:email:{email}", limit=LOGIN_MAX_ATTEMPTS, window_seconds=LOGIN_WINDOW_SECONDS
    )
    if not (ip_ok and email_ok):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many sign-in attempts. Try again later.",
        )

    user = await auth.authenticate(db, email=email, password=payload.password)
    if user is None:
        await audit.record(
            db,
            action=AuditAction.LOGIN_FAILED,
            target_type=AuditTargetType.USER,
            request=request,
            # The attempted address is recorded; the password never is.
            metadata={"label": email, "email": email},
        )
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_CREDENTIALS)

    await rate_limit.reset(f"login:email:{email}")
    await audit.record(
        db,
        action=AuditAction.LOGIN,
        target_type=AuditTargetType.USER,
        target_id=user.id,
        actor_id=user.id,
        request=request,
    )

    session, access_token = await _session_payload(db, user)
    refresh_token = await auth.issue_refresh_token(db, user, request=request)
    await db.commit()

    security.set_auth_cookies(response, access_token, refresh_token)
    session.csrf_token = csrf.rotate(response)
    return session


@router.post("/refresh", response_model=SessionOut)
async def refresh(request: Request, response: Response, db: DbSession) -> SessionOut:
    raw = request.cookies.get(security.REFRESH_COOKIE_NAME)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No refresh token.")

    try:
        user, new_refresh = await auth.rotate_refresh_token(db, raw, request=request)
    except auth.RefreshTokenReuse as exc:
        await db.commit()  # persist the mass revocation
        security.clear_auth_cookies(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except auth.AuthError as exc:
        security.clear_auth_cookies(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    session, access_token = await _session_payload(db, user)
    await db.commit()

    security.set_auth_cookies(response, access_token, new_refresh)
    # Same person, same token: rotating here would break a request another part of the
    # page sent with the token it already had.
    session.csrf_token = csrf.current_or_new(request, response)
    return session


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, db: DbSession) -> Response:
    """Revoke the refresh token, not just the cookie.

    Clearing a cookie while the token stays valid for 14 days is not a logout.
    """
    raw = request.cookies.get(security.REFRESH_COOKIE_NAME)
    actor_id = None
    access = request.cookies.get(security.ACCESS_COOKIE_NAME)
    if access:
        try:
            actor_id = security.decode_access_token(access)
        except security.TokenError:
            actor_id = None

    if raw:
        await auth.revoke_refresh_token(db, raw)
    if actor_id:
        await audit.record(
            db,
            action=AuditAction.LOGOUT,
            target_type=AuditTargetType.USER,
            target_id=actor_id,
            actor_id=actor_id,
            request=request,
        )
    await db.commit()

    security.clear_auth_cookies(response)
    csrf.rotate(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    request: Request, response: Response, user: CurrentUser, db: DbSession
) -> Response:
    """Revoke every session for this user — the "someone else has my password" path."""
    revoked = await auth.revoke_all_for_user(db, user.id)
    await audit.record(
        db,
        action=AuditAction.LOGOUT,
        target_type=AuditTargetType.USER,
        target_id=user.id,
        actor_id=user.id,
        request=request,
        metadata={"scope": "all_sessions", "revoked": revoked},
    )
    await db.commit()

    security.clear_auth_cookies(response)
    csrf.rotate(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/csrf", response_model=CsrfOut)
async def csrf_token(request: Request, response: Response) -> CsrfOut:
    """The CSRF token for this browser, issuing one if it has none.

    Needs no session, because sign-in and sign-up are protected too (login CSRF). Safe as
    a GET: it changes nothing an attacker can use, and CORS keeps any other origin from
    reading the value it returns.
    """
    return CsrfOut(csrf_token=csrf.current_or_new(request, response))


@router.get("/me", response_model=SessionOut)
async def me(request: Request, user: CurrentUser, db: DbSession) -> SessionOut:
    """Current session. Reports the expiry of the token actually in use — minting a
    fresh one here would report an expiry for a token the client never receives."""
    workspaces = await auth.list_workspaces_for_user(db, user.id)
    expires_at = None
    token = request.cookies.get(security.ACCESS_COOKIE_NAME)
    if token:
        expires_at = security.access_token_expiry(token)
    return SessionOut(
        user=UserOut.model_validate(user),
        workspaces=[
            WorkspaceSummary(
                id=workspace.id,
                name=workspace.name,
                is_personal=workspace.is_personal,
                role=role,
            )
            for workspace, role in workspaces
        ],
        access_token_expires_at=expires_at,
    )
