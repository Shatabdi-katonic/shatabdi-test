"""Base connector interface.

All knowledge source connectors implement ConnectorBase.
Each connector lives in its own module under connectors/ and is
registered in registry.py for lazy loading.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal


class ConnectorError(Exception):
    """Base exception for all connector errors."""

    def __init__(self, message: str, connector_type: str = "", retryable: bool = False) -> None:
        self.connector_type = connector_type
        self.retryable = retryable
        super().__init__(message)


class ConnectorAuthError(ConnectorError):
    """Authentication or authorization failure (e.g. expired token, revoked access)."""

    def __init__(self, message: str, connector_type: str = "") -> None:
        super().__init__(message, connector_type=connector_type, retryable=False)


class ConnectorRateLimitError(ConnectorError):
    """Source API rate limit hit. Retry after backoff."""

    def __init__(self, message: str, connector_type: str = "", retry_after: float = 0) -> None:
        self.retry_after = retry_after
        super().__init__(message, connector_type=connector_type, retryable=True)


class ConnectorNotFoundError(ConnectorError):
    """Requested resource (document, folder) not found in source system."""

    def __init__(self, message: str, connector_type: str = "") -> None:
        super().__init__(message, connector_type=connector_type, retryable=False)


class ConnectorTransientError(ConnectorError):
    """Transient failure (network timeout, 5xx). Safe to retry."""

    def __init__(self, message: str, connector_type: str = "") -> None:
        super().__init__(message, connector_type=connector_type, retryable=True)


@dataclass
class DocumentMetadata:
    """Metadata for a document discovered by a connector."""

    external_id: str
    title: str
    url: str | None = None
    content_type: str = "application/octet-stream"
    size_bytes: int | None = None
    author: str | None = None
    modified_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    folder_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class RawDocument:
    """Raw document bytes fetched from a source system."""

    external_id: str
    content: bytes
    content_type: str
    metadata: dict = field(default_factory=dict)


@dataclass
class PermissionEntry:
    """A single permission grant on a document or folder."""

    subject_type: str  # 'user', 'group', 'domain'
    subject_id: str  # email, group ID, domain name
    relation: str  # 'viewer', 'editor', 'owner'
    inherited: bool = False  # true if inherited from parent folder


@dataclass(frozen=True)
class ConfigField:
    """Declarative optional config field exposed to the wizard UI.

    Each connector declares its non-auth, non-schedule settings in a
    CONFIG_SCHEMA class attribute. The KE exposes them via
    GET /v1/sources/config-schemas so the frontend renders the right
    inputs per connector. Values submitted by the user are stored in
    KnowledgeSource.config and read by the connector at sync time.
    """

    key: str                                   # stored as-is in source.config
    label: str                                 # human-readable
    type: Literal["text", "number", "boolean", "select", "textarea"]
    required: bool = False
    default: Any = None
    placeholder: str | None = None
    help_text: str | None = None
    options: list[dict] | None = None          # for type="select": [{"value","label"}]


class ConnectorBase(ABC):
    """Abstract base class for all knowledge source connectors."""

    # Optional distributed rate limiter (set by pipeline after construction).
    # Connectors should call `await self.rate_limiter.acquire()` before each
    # API call if present.
    rate_limiter: object | None = None

    # Declarative optional config fields shown in the Add Knowledge Source
    # wizard. Auth credentials and sync scheduling are handled elsewhere —
    # this schema is strictly for connector behaviour knobs (team/workspace
    # scoping, filters, flags). Default is an empty list; connectors that
    # accept overrides override this attribute.
    CONFIG_SCHEMA: ClassVar[list[ConfigField]] = []

    @abstractmethod
    async def authenticate(self, credentials: dict) -> None:
        """Authenticate with the source system.

        credentials dict contents vary by auth_type:
        - oauth: {access_token: str}  (auto-refreshed by pipeline)
        - api_key: {api_key: str}
        - service_account: {service_account_json: dict}
        """
        ...

    @abstractmethod
    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        """List all documents, or documents modified since `since`.

        Yields DocumentMetadata for each discovered document.
        Used by sync workflows for both full and incremental syncs.
        """
        ...

    @abstractmethod
    async def fetch_document(self, doc_id: str) -> RawDocument:
        """Fetch raw document bytes by external ID.

        Returns the complete document content for parsing.
        """
        ...

    @abstractmethod
    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """Get permission entries for a document.

        Returns the full ACL as a list of PermissionEntry objects.
        Used by permission_syncer to write to SpiceDB.
        """
        ...

    async def get_folder_permissions(self, folder_id: str) -> list[PermissionEntry]:
        """Get permission entries for a folder.

        Default: empty list (connectors without folder-level permissions).
        Override for sources with hierarchical permissions (Google Drive, SharePoint).
        """
        return []

    async def health_check(self) -> bool:
        """Test connectivity to the source system.

        Default: returns True. Override for actual connectivity test.
        """
        return True

    async def close(self) -> None:
        """Clean up resources. Default: no-op."""
        pass
