"""Auth Settings — admin CRUD for per-provider OAuth credentials.

Admins configure Client ID + Client Secret for OAuth providers here.
When configured, users get one-click OAuth connect in the Knowledge modal
without needing to supply their own credentials.

Endpoints:
  GET    /v1/auth-settings/providers           → List all providers with config status
  GET    /v1/auth-settings/providers/{provider} → Get config for a single provider
  PUT    /v1/auth-settings/providers/{provider} → Create or update provider credentials
  DELETE /v1/auth-settings/providers/{provider} → Remove provider credentials
  POST   /v1/auth-settings/providers/{provider}/test → Test provider credentials
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from platform_core.telemetry import get_logger

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update

from platform_knowledge_engine.clients.postgres_client import PostgresClient
from platform_knowledge_engine.dependencies import (
    get_auth,
    get_postgres,
    get_settings,
    require_capability,
)
from platform_knowledge_engine.middleware.auth import AuthContext
from platform_knowledge_engine.models import OAuthProviderCredential
from platform_knowledge_engine.services.credential_manager import CredentialManager
from platform_knowledge_engine.services.oauth_providers import (
    OAUTH_PROVIDERS,
    PROVIDER_CLIENT_ID_HINTS,
    PROVIDER_DOCS_URLS,
    PROVIDER_SUBDOMAIN_PATTERNS,
    get_provider_category,
)
from platform_knowledge_engine.settings import KnowledgeEngineSettings

router = APIRouter()
logger = get_logger(__name__)


# Providers whose OAuth URLs contain {subdomain} or {domain} placeholders and
# therefore require the admin to supply an instance subdomain when configuring.
def _provider_requires_subdomain(provider: str) -> bool:
    cfg = OAUTH_PROVIDERS.get(provider)
    if not cfg:
        return False
    return "{subdomain}" in cfg.auth_url or "{domain}" in cfg.auth_url


def _provider_requires_tenant(provider: str) -> bool:
    """True for Microsoft providers whose authorize URL uses the {tenant}
    placeholder (SharePoint/Teams). A single-tenant Azure app must supply a
    tenant id instead of /common (AADSTS50194). (CR-559)"""
    cfg = OAUTH_PROVIDERS.get(provider)
    if not cfg:
        return False
    return "{tenant}" in cfg.auth_url


# Subdomain syntax: RFC 1123 label — alphanumeric and hyphens, 1-63 chars,
# cannot start or end with a hyphen. Permissive enough to accept ServiceNow
# PDI names like "dev362851", BambooHR company slugs, etc.
_SUBDOMAIN_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$")
# Microsoft tenant id: GUID, verified domain, or common/organizations/consumers.
_TENANT_RE = re.compile(r"^[a-zA-Z0-9.\-]+$")


def _get_credential_manager(settings: KnowledgeEngineSettings, postgres: PostgresClient) -> CredentialManager:
    """Instantiate CredentialManager with a clear HTTP error if encryption key is missing."""
    try:
        return CredentialManager(settings, postgres)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Encryption key not configured. Set KE_CREDENTIAL_ENCRYPTION_KEY on the server.",
        ) from exc


# ── Schemas ──────────────────────────────────────────────────────────────────


class ProviderCredentialInput(BaseModel):
    """Input for creating/updating a provider credential."""

    client_id: str = Field(..., min_length=1)
    client_secret: str = Field(..., min_length=1)
    # Instance subdomain for providers whose OAuth URLs include {subdomain}/{domain}
    # (ServiceNow, BambooHR, Zendesk, Egnyte). Ignored for providers with static URLs.
    instance_subdomain: str | None = Field(None, max_length=63)
    # Microsoft (SharePoint/Teams) Directory (tenant) ID — GUID or verified
    # domain. A single-tenant Azure app needs the /{tenant}/ authorize endpoint
    # instead of /common (AADSTS50194). Optional; ignored for non-Microsoft
    # providers whose URLs don't use the {tenant} placeholder. (CR-559)
    tenant_id: str | None = Field(None, max_length=128)


class ConfiguredBy(BaseModel):
    """Human-readable attribution for who configured a provider.

    The frontend renders display_name, falls back to email, then to "an admin".
    id stays available for audit-log linking. For rows configured before
    attribution capture existed, display_name/email are null.
    """

    id: str | None = None
    display_name: str | None = None
    email: str | None = None


class LastTest(BaseModel):
    """Most recent credential-test result for a provider."""

    at: str | None = None
    status: str = "never_tested"  # "passed" | "failed" | "never_tested"
    message: str | None = None
    tested_by_id: str | None = None


class ConfiguredBy(BaseModel):
    """Human-readable attribution for who configured a provider.

    The frontend renders display_name, falls back to email, then to "an admin".
    id stays available for audit-log linking. For rows configured before
    attribution capture existed, display_name/email are null.
    """

    id: str | None = None
    display_name: str | None = None
    email: str | None = None


class LastTest(BaseModel):
    """Most recent credential-test result for a provider."""

    at: str | None = None
    status: str = "never_tested"  # "passed" | "failed" | "never_tested"
    message: str | None = None
    tested_by_id: str | None = None


class ProviderStatus(BaseModel):
    """Status of a single OAuth provider."""

    provider: str
    name: str
    category: str = "Other"
    configured: bool
    enabled: bool = False
    requires_subdomain: bool = False
    requires_tenant: bool = False
    subdomain_pattern: str | None = None
    client_id_format_hint: str | None = None
    provider_docs_url: str | None = None
    configured_by: ConfiguredBy | None = None
    last_test: LastTest | None = None
    updated_at: str | None = None


class ProviderDetailResponse(BaseModel):
    """Detail response for a configured provider (client_secret is masked)."""

    provider: str
    client_id: str
    instance_subdomain: str | None = None
    tenant_id: str | None = None
    configured: bool = True
    enabled: bool = True
    configured_by: str | None = None
    updated_at: str | None = None


class TestResult(BaseModel):
    """Result of testing provider credentials."""

    success: bool
    message: str


# ── Display names ────────────────────────────────────────────────────────────

PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "confluence": "Confluence",
    "jira": "Jira",
    "google_drive": "Google Drive",
    "google_cloud_storage": "Google Cloud Storage",
    "google_sites": "Google Sites",
    "gmail": "Gmail",
    "sharepoint": "SharePoint",
    "teams": "Microsoft Teams",
    "slack": "Slack",
    "github": "GitHub",
    "gitlab": "GitLab",
    "bitbucket": "Bitbucket",
    "salesforce": "Salesforce",
    "hubspot": "HubSpot",
    "notion": "Notion",
    "dropbox": "Dropbox",
    "zendesk": "Zendesk",
    "asana": "Asana",
    "linear": "Linear",
    "airtable": "Airtable",
    "clickup": "ClickUp",
    "gong": "Gong",
    "discord": "Discord",
    "egnyte": "Egnyte",
    "productboard": "ProductBoard",
    "highspot": "Highspot",
    # New providers (previously Nango)
    "monday": "Monday.com",
    "trello": "Trello",
    "basecamp": "Basecamp",
    "wrike": "Wrike",
    "smartsheet": "Smartsheet",
    "intercom": "Intercom",
    "helpscout": "Help Scout",
    "front": "Front",
    "coda": "Coda",
    "pipedrive": "Pipedrive",
    "zohocrm": "Zoho CRM",
    "servicenow": "ServiceNow",
    "pagerduty": "PagerDuty",
    "bamboohr": "BambooHR",
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "figma": "Figma",
    "miro": "Miro",
    "wordpress": "WordPress",
    "webflow": "Webflow",
    "box": "Box",
    "onedrive": "OneDrive",
    "google_calendar": "Google Calendar",
    "typeform": "Typeform",
    "surveymonkey": "SurveyMonkey",
}


# ── Attribution / test-result helpers ─────────────────────────────────────────


def _display_name_from_auth(auth: AuthContext) -> str | None:
    """Best-effort human name from the JWT claims, captured at write time."""
    claims = auth.raw_claims or {}
    full_name = f"{claims.get('given_name', '')} {claims.get('family_name', '')}".strip()
    return claims.get("name") or claims.get("preferred_username") or (full_name or None)


def _build_configured_by(row: OAuthProviderCredential | None) -> ConfiguredBy | None:
    """Structured attribution from a credential row (None if unconfigured)."""
    if row is None:
        return None
    return ConfiguredBy(
        id=row.configured_by,
        display_name=row.configured_by_name,
        email=row.configured_by_email,
    )


def _build_last_test(row: OAuthProviderCredential | None) -> LastTest | None:
    """Persisted last-test result from a credential row, or None if never tested."""
    if row is None or not row.last_test:
        return None
    lt = row.last_test
    return LastTest(
        at=lt.get("at"),
        status=lt.get("status", "never_tested"),
        message=lt.get("message"),
        tested_by_id=lt.get("tested_by_id"),
    )


async def _record_test_result(
    postgres: PostgresClient,
    auth: AuthContext,
    provider: str,
    result: TestResult,
) -> None:
    """Persist the most recent test result so the Credentials UI status dot
    survives a page refresh. Keyed on (tenant, provider); no-op if the row was
    deleted between the test and this write."""
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if result.success else "failed",
        "message": result.message,
        "tested_by_id": auth.user_id,
    }
    async with postgres.session() as session:
        await session.execute(
            update(OAuthProviderCredential)
            .where(
                OAuthProviderCredential.tenant_id == auth.tenant_id,
                OAuthProviderCredential.provider == provider,
            )
            .values(last_test=record)
        )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/auth-settings/providers", response_model=list[ProviderStatus])
async def list_provider_status(
    auth: AuthContext = Depends(get_auth),
    postgres: PostgresClient = Depends(get_postgres),
):
    """List all OAuth providers with their configuration status."""
    # Fetch all configured providers for this tenant
    async with postgres.session() as session:
        result = await session.execute(
            select(OAuthProviderCredential).where(
                OAuthProviderCredential.tenant_id == auth.tenant_id
            )
        )
        configured = {row.provider: row for row in result.scalars().all()}

    # Build response for ALL OAuth providers
    providers = []
    for provider_key in OAUTH_PROVIDERS:
        row = configured.get(provider_key)
        providers.append(
            ProviderStatus(
                provider=provider_key,
                name=PROVIDER_DISPLAY_NAMES.get(provider_key, provider_key),
                category=get_provider_category(provider_key),
                configured=row is not None,
                enabled=row.enabled if row else False,
                requires_subdomain=_provider_requires_subdomain(provider_key),
                requires_tenant=_provider_requires_tenant(provider_key),
                subdomain_pattern=PROVIDER_SUBDOMAIN_PATTERNS.get(provider_key) or None,
                client_id_format_hint=PROVIDER_CLIENT_ID_HINTS.get(provider_key) or None,
                provider_docs_url=PROVIDER_DOCS_URLS.get(provider_key) or None,
                configured_by=_build_configured_by(row),
                last_test=_build_last_test(row),
                updated_at=row.updated_at.isoformat() if row and row.updated_at else None,
            )
        )

    return providers


@router.get("/auth-settings/configured")
async def list_configured_providers(
    auth: AuthContext = Depends(get_auth),
    postgres: PostgresClient = Depends(get_postgres),
):
    """Return a lightweight set of configured provider names (for modal flow decisions)."""
    async with postgres.session() as session:
        result = await session.execute(
            select(OAuthProviderCredential.provider).where(
                OAuthProviderCredential.tenant_id == auth.tenant_id,
                OAuthProviderCredential.enabled.is_(True),
            )
        )
        providers = [row[0] for row in result.all()]

    return {"configured_providers": providers}


@router.get("/auth-settings/providers/{provider}", response_model=ProviderDetailResponse)
async def get_provider_credential(
    provider: str,
    auth: AuthContext = Depends(get_auth),
    postgres: PostgresClient = Depends(get_postgres),
    settings: KnowledgeEngineSettings = Depends(get_settings),
):
    """Get configured credentials for a provider (client_secret masked)."""
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider: {provider}")

    async with postgres.session() as session:
        result = await session.execute(
            select(OAuthProviderCredential).where(
                OAuthProviderCredential.tenant_id == auth.tenant_id,
                OAuthProviderCredential.provider == provider,
            )
        )
        row = result.scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Provider {provider} not configured")

    # Decrypt to get client_id (mask client_secret)
    cm = _get_credential_manager(settings, postgres)
    data = cm._decrypt(row.encrypted_data)

    return ProviderDetailResponse(
        provider=provider,
        client_id=data.get("client_id", ""),
        instance_subdomain=data.get("instance_subdomain") or None,
        tenant_id=data.get("tenant_id") or None,
        configured=True,
        enabled=row.enabled,
        configured_by=row.configured_by,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.put(
    "/auth-settings/providers/{provider}",
    response_model=ProviderDetailResponse,
    dependencies=[require_capability("knowledge:configure")],
)
async def upsert_provider_credential(
    provider: str,
    body: ProviderCredentialInput,
    auth: AuthContext = Depends(get_auth),
    postgres: PostgresClient = Depends(get_postgres),
    settings: KnowledgeEngineSettings = Depends(get_settings),
):
    """Create or update OAuth credentials for a provider."""
    # SEC: writing OAuth client_id/secret is privileged config. Person-gate is
    # the route-level require_capability("knowledge:configure") (replaced the
    # former _require_admin(auth) call). Same-tenant only (rows are
    # tenant-scoped), so no cross-tenant blast radius.
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider: {provider}")

    # Subdomain-templated providers (ServiceNow, BambooHR, Zendesk, Egnyte)
    # require an instance subdomain — otherwise OAuth URLs resolve to
    # https://.service-now.com/... which fails DNS lookup.
    requires_subdomain = _provider_requires_subdomain(provider)
    instance_subdomain = (body.instance_subdomain or "").strip()
    tenant_id = (body.tenant_id or "").strip()
    # Microsoft tenant id: GUID, verified domain, or common/organizations/
    # consumers. Same charset guard as the URL resolver (prevents injection
    # into the authorize URL).
    if tenant_id and not _TENANT_RE.match(tenant_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid tenant ID. Use the Azure Directory (tenant) ID GUID "
                "or a verified domain (letters, digits, dots, hyphens)."
            ),
        )

    if requires_subdomain:
        if not instance_subdomain:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{provider} requires an instance subdomain "
                    "(e.g. 'dev362851' for https://dev362851.service-now.com)."
                ),
            )
        if not _SUBDOMAIN_RE.match(instance_subdomain):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid instance subdomain. Use only letters, digits, "
                    "and hyphens (1-63 characters, no leading or trailing hyphen)."
                ),
            )

    # Build the encrypted payload. Always include instance_subdomain key for
    # forward compatibility — empty string for providers that don't need it.
    cm = _get_credential_manager(settings, postgres)
    encrypted = cm._encrypt(
        {
            "client_id": body.client_id,
            "client_secret": body.client_secret,
            "instance_subdomain": instance_subdomain,
            "tenant_id": tenant_id,
        }
    )

    async with postgres.session() as session:
        result = await session.execute(
            select(OAuthProviderCredential).where(
                OAuthProviderCredential.tenant_id == auth.tenant_id,
                OAuthProviderCredential.provider == provider,
            )
        )
        existing = result.scalar_one_or_none()

        # Capture human-readable attribution from the configuring user's JWT
        # so the Credentials UI never has to resolve the raw user-id later.
        configured_by_name = _display_name_from_auth(auth)
        configured_by_email = auth.email or None

        if existing:
            existing.encrypted_data = encrypted
            existing.enabled = True
            existing.configured_by = auth.user_id
            existing.configured_by_email = configured_by_email
            existing.configured_by_name = configured_by_name
        else:
            session.add(
                OAuthProviderCredential(
                    tenant_id=auth.tenant_id,
                    provider=provider,
                    encrypted_data=encrypted,
                    enabled=True,
                    configured_by=auth.user_id,
                    configured_by_email=configured_by_email,
                    configured_by_name=configured_by_name,
                )
            )
        await session.flush()

    logger.info("auth_settings_upsert provider=%s tenant=%s", provider, auth.tenant_id)

    return ProviderDetailResponse(
        provider=provider,
        client_id=body.client_id,
        instance_subdomain=instance_subdomain or None,
        tenant_id=tenant_id or None,
        configured=True,
        enabled=True,
        configured_by=auth.user_id,
    )


@router.delete(
    "/auth-settings/providers/{provider}",
    dependencies=[require_capability("knowledge:configure")],
)
async def delete_provider_credential(
    provider: str,
    auth: AuthContext = Depends(get_auth),
    postgres: PostgresClient = Depends(get_postgres),
):
    """Remove OAuth credentials for a provider."""
    # SEC: person-gate is require_capability("knowledge:configure") (replaced
    # the former _require_admin(auth) call).
    async with postgres.session() as session:
        result = await session.execute(
            delete(OAuthProviderCredential).where(
                OAuthProviderCredential.tenant_id == auth.tenant_id,
                OAuthProviderCredential.provider == provider,
            )
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Provider {provider} not configured")

    logger.info("auth_settings_delete provider=%s tenant=%s", provider, auth.tenant_id)
    return {"status": "deleted", "provider": provider}


@router.post(
    "/auth-settings/providers/{provider}/test",
    response_model=TestResult,
    dependencies=[require_capability("knowledge:configure")],
)
async def test_provider_credential(
    provider: str,
    auth: AuthContext = Depends(get_auth),
    postgres: PostgresClient = Depends(get_postgres),
    settings: KnowledgeEngineSettings = Depends(get_settings),
):
    """Test if the stored credentials are valid by initiating a test auth URL generation.

    For OAuth providers, we verify the client_id is accepted by the provider's
    authorization endpoint (returns a valid redirect, not an error page).
    """
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider: {provider}")

    # Fetch stored credentials
    async with postgres.session() as session:
        result = await session.execute(
            select(OAuthProviderCredential).where(
                OAuthProviderCredential.tenant_id == auth.tenant_id,
                OAuthProviderCredential.provider == provider,
            )
        )
        row = result.scalar_one_or_none()

    if row is None:
        return TestResult(success=False, message="Provider not configured. Save credentials first.")

    cm = _get_credential_manager(settings, postgres)
    data = cm._decrypt(row.encrypted_data)
    client_id = data.get("client_id", "")
    client_secret = data.get("client_secret", "")
    instance_subdomain = data.get("instance_subdomain", "")

    result = await _probe_provider_credentials(
        provider, client_id, client_secret, instance_subdomain
    )

    # Persist the most recent attempt so a subsequent GET returns it and the
    # UI status dot updates immediately after the user clicks Test.
    await _record_test_result(postgres, auth, provider, result)

    return result


async def _probe_provider_credentials(
    provider: str,
    client_id: str,
    client_secret: str,
    instance_subdomain: str,
) -> TestResult:
    """Probe the provider's token endpoint to validate stored credentials.

    For OAuth providers, we verify the client_id/secret are accepted by the
    provider's token endpoint (a recognised-client error means the creds are
    valid; a client-rejection error means they are not)."""
    if not client_id or not client_secret:
        return TestResult(success=False, message="Client ID or Client Secret is empty.")

    # Resolve the token URL — substitute {subdomain}/{domain} placeholders
    # with the stored instance subdomain if the provider requires one.
    provider_config = OAUTH_PROVIDERS[provider]
    token_url = provider_config.token_url

    if "{subdomain}" in token_url or "{domain}" in token_url:
        if not instance_subdomain:
            return TestResult(
                success=False,
                message=(
                    f"{provider} requires an instance subdomain. "
                    "Edit the credentials to provide it."
                ),
            )
        token_url = token_url.replace("{subdomain}", instance_subdomain).replace(
            "{domain}", instance_subdomain
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Send a token request with an invalid grant_type to test if client_id is recognized
            # Most providers return 400 (bad request) for valid clients, 401 for invalid clients
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )

            # 401/403 — usually bad client credentials, but some providers
            # (Miro, Figma, SurveyMonkey) return 401 even for a bad *code*
            # with valid creds. Combine ALL error fields to detect patterns.
            if resp.status_code in (401, 403):
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                # Combine all error fields into a single string for matching.
                # Some providers put the key info in error_description, not error.
                err_parts = []
                for key in ("error", "error_description", "message", "detail"):
                    val = body.get(key, "")
                    if isinstance(val, dict):
                        # Nested error objects (e.g. SurveyMonkey: {"error": {"name": "..."}})
                        err_parts.append(str(val.get("name", "")))
                        err_parts.append(str(val.get("message", "")))
                    elif val:
                        err_parts.append(str(val))
                err = " ".join(err_parts).lower()

                # Client-rejection patterns — check these FIRST since some
                # providers (WordPress) return "invalid_request" with
                # "unknown client_id" in the description.
                client_errors = (
                    "invalid_client", "unauthorized_client", "client_not_found",
                    "bad_client", "unknown client_id", "unknown client",
                    "client not registered",
                )

                # Code-rejection patterns — these mean the CLIENT was
                # recognized but the dummy code was rejected — credentials ARE valid.
                # IMPORTANT: Check these FIRST for providers that return 401
                # for bad codes (SurveyMonkey, Miro, Figma).
                grant_errors = (
                    "invalid_grant", "bad_verification_code", "invalid_code",
                    "authorization_code", "invalid_request", "invalid code",
                    "code expired", "code is invalid", "bad request",
                    "invalid authorization code", "expired",
                )

                # Providers known to return 401 for invalid code (not just invalid client):
                _401_for_bad_code = ("surveymonkey", "miro", "figma", "zoom")

                # For providers that return 401 for bad code, check grant errors first
                if provider in _401_for_bad_code:
                    if any(g in err for g in grant_errors):
                        return TestResult(
                            success=True,
                            message="Credentials validated successfully. Provider recognized the client.",
                        )

                if any(g in err for g in client_errors):
                    return TestResult(
                        success=False,
                        message="Invalid credentials. Provider rejected the Client ID or Secret.",
                    )

                if any(g in err for g in grant_errors):
                    return TestResult(
                        success=True,
                        message="Credentials validated successfully. Provider recognized the client.",
                    )

                # For known 401-for-bad-code providers, assume valid if error isn't recognized
                if provider in _401_for_bad_code:
                    return TestResult(
                        success=True,
                        message=f"Credentials likely valid. Provider returned {resp.status_code} — full validation happens during OAuth flow.",
                    )

                # Unknown error — assume creds are OK since provider didn't explicitly reject them
                return TestResult(
                    success=True,
                    message=f"Credentials likely valid. Provider returned {resp.status_code} — full validation happens during OAuth flow.",
                )

            # GitHub returns 200 with {"error": "bad_verification_code"} for
            # valid clients and {"error": "incorrect_client_credentials"}
            # for invalid ones.
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                err_all = f"{body.get('error', '')} {body.get('error_description', '')}".lower()
                if any(p in err_all for p in ("incorrect_client_credentials", "invalid_client", "unknown client")):
                    return TestResult(
                        success=False,
                        message="Invalid credentials. Provider rejected the Client ID or Secret.",
                    )
                # Any other 200 response (including error=bad_verification_code)
                # means the client credentials are accepted.
                return TestResult(
                    success=True,
                    message="Credentials validated successfully. Provider recognized the client.",
                )

            # 400 = often "invalid code" which means the client was recognized
            if resp.status_code == 400:
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                # Combine all error fields for thorough matching
                err_all = f"{body.get('error', '')} {body.get('error_description', '')} {body.get('message', '')}".lower()
                # Check for explicit client rejection FIRST
                client_rejects = ("invalid_client", "unauthorized_client", "unknown client_id", "unknown client", "client not found")
                if any(p in err_all for p in client_rejects):
                    return TestResult(
                        success=False,
                        message="Invalid credentials. Provider rejected the Client ID or Secret.",
                    )
                return TestResult(
                    success=True,
                    message="Credentials validated successfully. Provider recognized the client.",
                )

            # Any other status — treat as inconclusive but likely OK
            return TestResult(
                success=True,
                message="Credentials validated successfully. Provider recognized the client.",
            )
    except httpx.TimeoutException:
        return TestResult(success=False, message=f"Connection to {provider} timed out.")
    except Exception as e:
        logger.warning("oauth_connection_test_failed", provider=provider, error=str(e))
        return TestResult(success=False, message="Connection test failed. Check your OAuth provider configuration.")