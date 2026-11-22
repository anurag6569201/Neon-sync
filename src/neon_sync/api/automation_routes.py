"""Neon Sync Automations REST API routes (/v1/automations/*)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request, status

from neon_sync.automation import (
    AUTOMATION_TEMPLATES,
    AutomationCreateRequest,
    AutomationRule,
    AutomationRunRecord,
    AutomationTemplate,
    AutomationUpdateRequest,
)
from neon_sync.security import UrlValidationError, validate_and_canonicalize_url

from .auth_routes import resolve_request_auth
from .branding import brand_error_detail, sanitize_public_text

if TYPE_CHECKING:
    from .app import AppContainer

automation_router = APIRouter(prefix="/automations", tags=["automations"])


def _container(request: Request) -> AppContainer:
    return request.app.state.container


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _owner_id(request: Request) -> str:
    ctx = resolve_request_auth(request)
    return ctx.user.id if ctx.user is not None else "workspace"


@automation_router.get("/templates", response_model=list[AutomationTemplate])
async def list_automation_templates() -> list[AutomationTemplate]:
    """Return curated one-click Neon Sync AI & media automation templates."""
    return AUTOMATION_TEMPLATES


@automation_router.get("", response_model=list[AutomationRule])
async def list_automations(request: Request) -> list[AutomationRule]:
    """List saved Neon Sync automations for the current user/workspace."""
    return _container(request).automations.store.list_all(owner_id=_owner_id(request))


@automation_router.post("", response_model=AutomationRule, status_code=status.HTTP_201_CREATED)
async def create_automation(body: AutomationCreateRequest, request: Request) -> AutomationRule:
    """Create a persistent Neon Sync scheduled or on-demand media automation."""
    c = _container(request)
    try:
        validate_and_canonicalize_url(
            body.target_url,
            allow_private=c.settings.allow_private_addresses,
            max_length=c.settings.max_url_length,
        )
        if body.webhook_url:
            validate_and_canonicalize_url(
                body.webhook_url,
                allow_private=c.settings.allow_private_addresses,
                max_length=c.settings.max_url_length,
            )
    except UrlValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN if exc.code.value == "ERR_SSRF_BLOCKED" else status.HTTP_400_BAD_REQUEST,
            detail=brand_error_detail(exc.code.value, exc.message, request_id=_request_id(request)),
        ) from exc

    if body.trigger_type == "interval" and not body.interval_minutes:
        body.interval_minutes = 60

    return c.automations.store.create(body, owner_id=_owner_id(request))


@automation_router.patch("/{automation_id}", response_model=AutomationRule)
async def update_automation(
    automation_id: str,
    body: AutomationUpdateRequest,
    request: Request,
) -> AutomationRule:
    """Enable, disable, or update a Neon Sync automation rule."""
    c = _container(request)
    if body.webhook_url:
        try:
            validate_and_canonicalize_url(
                body.webhook_url,
                allow_private=c.settings.allow_private_addresses,
                max_length=c.settings.max_url_length,
            )
        except UrlValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=brand_error_detail(exc.code.value, exc.message, request_id=_request_id(request)),
            ) from exc
    updated = c.automations.store.update(automation_id, body)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "Automation not found", request_id=_request_id(request)),
        )
    return updated


@automation_router.delete("/{automation_id}")
async def delete_automation(automation_id: str, request: Request) -> dict:
    """Delete a saved Neon Sync automation rule and its run history."""
    ok = _container(request).automations.store.delete(automation_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "Automation not found", request_id=_request_id(request)),
        )
    return {"status": "ok", "service": "neon-sync", "deleted": automation_id}


@automation_router.post("/{automation_id}/run", response_model=AutomationRunRecord)
async def run_automation_now(automation_id: str, request: Request) -> AutomationRunRecord:
    """Trigger an immediate run of a Neon Sync automation workflow."""
    c = _container(request)
    auth_ctx = resolve_request_auth(request)
    pro_key = request.headers.get("x-neon-pro-key")
    byok_key = request.headers.get("x-gemini-api-key")
    client_key = f"user:{auth_ctx.user.id}" if auth_ctx.user is not None else "workspace"
    account_tier = auth_ctx.user.tier if auth_ctx.user is not None else "pro"
    try:
        run = await c.automations.execute_rule(
            automation_id,
            trigger="manual",
            client_key=client_key,
            pro_key=pro_key,
            byok_gemini_key=byok_key,
            account_tier=account_tier,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "Automation not found", request_id=_request_id(request)),
        ) from exc
    run.summary = sanitize_public_text(run.summary)
    if run.error:
        run.error = sanitize_public_text(run.error)
    return run


@automation_router.get("/{automation_id}/runs", response_model=list[AutomationRunRecord])
async def list_automation_runs(
    automation_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[AutomationRunRecord]:
    """List execution history for a Neon Sync automation rule."""
    c = _container(request)
    if c.automations.store.get(automation_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "Automation not found", request_id=_request_id(request)),
        )
    return c.automations.store.list_runs(automation_id, limit=limit)
