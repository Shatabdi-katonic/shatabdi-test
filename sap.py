"""SAP connector.

API: SAP OData v4 (S/4HANA, BTP)
Auth: OAuth 2.0 (SAP BTP) or Basic Auth (on-prem S/4HANA)
Sync: Scheduled (full refresh). Incremental via $filter on LastChangeDateTime.
Permissions: SAP authorization objects (mapped to viewer/editor)

Content types indexed:
  - Master data (materials, customers, suppliers, products)
  - Transactional summaries (purchase orders, sales orders)
  - Custom CDS views (configurable entity sets)

The connector indexes SAP business object metadata and descriptions
to make enterprise data discoverable by agents. It does NOT export
raw transactional data in bulk.

Role mapping:
  Display (03)  -> viewer
  Change  (02)  -> editor
  Full    (01)  -> owner
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from platform_knowledge_engine.connectors._utils.http_client import (
    RetryClient,
    bearer_headers,
)
from platform_knowledge_engine.connectors.base import (
    ConnectorBase,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = logging.getLogger(__name__)

# Default entity sets to index from S/4HANA
DEFAULT_ENTITY_SETS = [
    {
        "name": "A_Product",
        "label": "Products",
        "key": "Product",
        "title_field": "ProductDescription",
    },
    {"name": "A_Customer", "label": "Customers", "key": "Customer", "title_field": "CustomerName"},
    {"name": "A_Supplier", "label": "Suppliers", "key": "Supplier", "title_field": "SupplierName"},
    {
        "name": "A_PurchaseOrder",
        "label": "Purchase Orders",
        "key": "PurchaseOrder",
        "title_field": "PurchaseOrder",
    },
    {
        "name": "A_SalesOrder",
        "label": "Sales Orders",
        "key": "SalesOrder",
        "title_field": "SalesOrder",
    },
]


class SAPConnector(ConnectorBase):
    """SAP S/4HANA and BTP connector via OData v4."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self._base_url: str = config.get("base_url", "").rstrip("/")
        self._service_path: str = config.get(
            "service_path", "/sap/opu/odata4/sap/api_product/srvd_a2x/sap/product/0001"
        )
        self._entity_sets: list[dict] = config.get("entity_sets", DEFAULT_ENTITY_SETS)
        self._max_records_per_entity: int = config.get("max_records_per_entity", 5000)
        self._sap_client: str = config.get("sap_client", "100")
        self._client: RetryClient | None = None

    async def authenticate(self, credentials: dict) -> None:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "sap-client": self._sap_client,
        }

        if credentials.get("access_token"):
            headers.update(bearer_headers(credentials["access_token"]))
        elif credentials.get("username") and credentials.get("password"):
            import base64

            cred = base64.b64encode(
                f"{credentials['username']}:{credentials['password']}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {cred}"
        else:
            raise ValueError("SAP connector requires access_token (OAuth) or username/password")

        base = self._base_url or credentials.get("base_url", "")
        if not base:
            raise ValueError("SAP connector requires base_url")
        self._base_url = base.rstrip("/")

        self._client = RetryClient(
            base_url=self._base_url,
            headers=headers,
            timeout=60.0,
            rate_limiter=self.rate_limiter,
        )

        # Verify with a metadata request
        try:
            await self._client.get(f"{self._service_path}/$metadata")
            logger.info("SAP authenticated at %s", self._base_url)
        except Exception as e:
            # Try without the specific service path (might be configured differently)
            logger.warning("SAP metadata check failed (%s), proceeding anyway", e)

    async def list_documents(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None

        for entity_config in self._entity_sets:
            entity_name = entity_config["name"]
            label = entity_config.get("label", entity_name)
            key_field = entity_config["key"]
            title_field = entity_config.get("title_field", key_field)

            try:
                async for doc in self._list_entity(
                    entity_name, key_field, title_field, label, since
                ):
                    yield doc
            except Exception as e:
                logger.error("Failed to list SAP entity %s: %s", entity_name, e)

    async def _list_entity(
        self,
        entity_name: str,
        key_field: str,
        title_field: str,
        label: str,
        since: datetime | None,
    ) -> AsyncIterator[DocumentMetadata]:
        assert self._client is not None

        params: dict[str, str] = {
            "$top": str(self._max_records_per_entity),
            "$format": "json",
        }

        if since:
            params["$filter"] = (
                f"LastChangeDateTime gt datetime'{since.strftime('%Y-%m-%dT%H:%M:%S')}'"
            )

        url = f"{self._service_path}/{entity_name}"
        skip = 0

        while True:
            params["$skip"] = str(skip)
            try:
                data = await self._client.get_json(url, params=params)
            except Exception as e:
                logger.warning("OData request failed for %s (skip=%d): %s", entity_name, skip, e)
                break

            # OData v4 response format
            results = data.get("value", data.get("d", {}).get("results", []))
            if not results:
                break

            for record in results:
                key_val = str(record.get(key_field, ""))
                title_val = record.get(title_field, key_val)
                modified = _parse_odata_dt(
                    record.get("LastChangeDateTime", record.get("CreationDate", ""))
                )

                yield DocumentMetadata(
                    external_id=f"{entity_name}:{key_val}",
                    title=f"[{label}] {title_val}",
                    content_type="application/json",
                    modified_at=modified,
                    metadata={
                        "type": "sap_entity",
                        "entity_set": entity_name,
                        "label": label,
                        "key": key_val,
                    },
                )

            # Check for next page
            if len(results) < int(params.get("$top", "5000")):
                break
            skip += len(results)
            if skip >= self._max_records_per_entity:
                break

    async def fetch_document(self, doc_id: str) -> RawDocument:
        assert self._client is not None
        entity_name, key_val = doc_id.split(":", 1)

        # Find entity config
        entity_config = next(
            (e for e in self._entity_sets if e["name"] == entity_name),
            {"name": entity_name, "key": "Id", "label": entity_name},
        )
        key_field = entity_config["key"]
        label = entity_config.get("label", entity_name)

        # Fetch the full record
        url = f"{self._service_path}/{entity_name}('{key_val}')"
        try:
            data = await self._client.get_json(url, params={"$format": "json"})
        except Exception:
            # Some OData services use different key formats
            url = f"{self._service_path}/{entity_name}({key_field}='{key_val}')"
            data = await self._client.get_json(url, params={"$format": "json"})

        record = data.get("d", data)

        # Convert to readable markdown
        lines = [f"# {label}: {key_val}", ""]

        for field_name, value in record.items():
            # Skip OData metadata fields
            if field_name.startswith("__") or field_name.startswith("@odata"):
                continue
            if value is None or value == "":
                continue
            # Format the field name nicely
            display_name = field_name.replace("_", " ")
            lines.append(f"- **{display_name}:** {value}")

        content = "\n".join(lines).encode("utf-8")
        return RawDocument(
            external_id=doc_id,
            content=content,
            content_type="text/markdown",
            metadata={"filename": f"{entity_name}-{key_val}.md"},
        )

    async def get_permissions(self, doc_id: str) -> list[PermissionEntry]:
        """SAP permissions are managed via authorization objects.

        Full integration requires calling SAP's authority check APIs
        (BAPI_USER_GET_DETAIL or /sap/bc/adt/vit/wb/object_type).
        For MVP, we return a workspace-level viewer grant so all
        authenticated users in the tenant can search SAP content.
        Fine-grained SAP auth object mapping is a future enhancement.
        """
        return [
            PermissionEntry(
                subject_type="group",
                subject_id="sap_authenticated_users",
                relation="viewer",
            )
        ]

    async def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.get(f"{self._service_path}/$metadata")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()


def _parse_odata_dt(val: str | int) -> datetime:
    """Parse OData datetime formats."""
    if not val:
        return datetime.now(UTC)
    if isinstance(val, (int, float)):
        # Epoch milliseconds (SAP often returns /Date(1234567890000)/)
        return datetime.fromtimestamp(val / 1000, tz=UTC)
    s = str(val)
    # Handle /Date(...)/ format
    if "/Date(" in s:
        import re

        m = re.search(r"/Date\((\d+)", s)
        if m:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
