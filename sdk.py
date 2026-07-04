"""Connector SDK for building custom knowledge source connectors.

This module provides the public API for customers to build their own
connectors. It re-exports the core interfaces and adds:
  - ConnectorPlugin: decorator-based registration
  - validate_connector(): pre-flight checks
  - load_custom_connector(): dynamic import from a Python module path

Usage (customer-side):

    from platform_knowledge_engine.connectors.sdk import (
        ConnectorBase,
        ConnectorPlugin,
        DocumentMetadata,
        PermissionEntry,
        RawDocument,
    )

    @ConnectorPlugin(
        name="my_internal_wiki",
        display_name="Internal Wiki",
        auth_type="api_key",
        description="Indexes pages from our internal wiki.",
    )
    class InternalWikiConnector(ConnectorBase):
        def __init__(self, config: dict | None = None) -> None:
            ...
        async def authenticate(self, credentials: dict) -> None:
            ...
        async def list_documents(self, since=None):
            ...
        async def fetch_document(self, doc_id: str):
            ...
        async def get_permissions(self, doc_id: str):
            ...

Registration (platform-side):

    from platform_knowledge_engine.connectors.sdk import load_custom_connector
    connector_cls = load_custom_connector("mypackage.wiki_connector.InternalWikiConnector")
"""

from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass, field

from platform_knowledge_engine.connectors.base import (
    ConnectorBase,
    DocumentMetadata,
    PermissionEntry,
    RawDocument,
)

logger = logging.getLogger(__name__)

# Re-export core types for SDK consumers
__all__ = [
    "ConnectorBase",
    "ConnectorPlugin",
    "DocumentMetadata",
    "PermissionEntry",
    "RawDocument",
    "ConnectorManifest",
    "load_custom_connector",
    "validate_connector",
    "list_registered_plugins",
]


# ---------------------------------------------------------------------------
# Plugin registry
# ---------------------------------------------------------------------------


@dataclass
class ConnectorManifest:
    """Metadata about a connector plugin."""

    name: str  # unique identifier (snake_case)
    display_name: str  # human-readable name
    auth_type: str  # "oauth", "api_key", "basic", "service_account", "none"
    description: str = ""
    version: str = "1.0.0"
    author: str = ""
    config_schema: dict = field(default_factory=dict)  # JSON Schema for config
    credential_fields: list[str] = field(default_factory=list)
    supported_content_types: list[str] = field(default_factory=list)
    supports_incremental_sync: bool = True
    supports_permissions: bool = True
    connector_class: type[ConnectorBase] | None = None


_PLUGIN_REGISTRY: dict[str, ConnectorManifest] = {}


class ConnectorPlugin:
    """Decorator to register a custom connector with the platform.

    Example:
        @ConnectorPlugin(name="my_wiki", display_name="My Wiki", auth_type="api_key")
        class MyWikiConnector(ConnectorBase):
            ...
    """

    def __init__(
        self,
        name: str,
        display_name: str,
        auth_type: str = "api_key",
        description: str = "",
        version: str = "1.0.0",
        author: str = "",
        config_schema: dict | None = None,
        credential_fields: list[str] | None = None,
        supported_content_types: list[str] | None = None,
        supports_incremental_sync: bool = True,
        supports_permissions: bool = True,
    ) -> None:
        self._manifest = ConnectorManifest(
            name=name,
            display_name=display_name,
            auth_type=auth_type,
            description=description,
            version=version,
            author=author,
            config_schema=config_schema or {},
            credential_fields=credential_fields or [],
            supported_content_types=supported_content_types or [],
            supports_incremental_sync=supports_incremental_sync,
            supports_permissions=supports_permissions,
        )

    def __call__(self, cls: type[ConnectorBase]) -> type[ConnectorBase]:
        """Register the decorated class as a connector plugin."""
        errors = validate_connector(cls)
        if errors:
            raise TypeError(
                f"Connector '{self._manifest.name}' failed validation:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

        self._manifest.connector_class = cls
        _PLUGIN_REGISTRY[self._manifest.name] = self._manifest

        # Attach manifest to the class for introspection
        cls._connector_manifest = self._manifest  # type: ignore[attr-defined]

        logger.info(
            "Registered connector plugin: %s (%s) v%s",
            self._manifest.name,
            self._manifest.display_name,
            self._manifest.version,
        )
        return cls


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_REQUIRED_METHODS = [
    ("authenticate", ["self", "credentials"]),
    ("list_documents", ["self", "since"]),
    ("fetch_document", ["self", "doc_id"]),
    ("get_permissions", ["self", "doc_id"]),
]


def validate_connector(cls: type) -> list[str]:
    """Validate that a class correctly implements ConnectorBase.

    Returns a list of error messages. Empty list means valid.
    """
    errors: list[str] = []

    # Must be a subclass of ConnectorBase
    if not issubclass(cls, ConnectorBase):
        errors.append(f"{cls.__name__} must inherit from ConnectorBase")
        return errors  # Can't check further

    # Must accept config in __init__
    init_sig = inspect.signature(cls.__init__)
    init_params = list(init_sig.parameters.keys())
    if "config" not in init_params and len(init_params) < 2:
        errors.append(f"{cls.__name__}.__init__ should accept 'config: dict | None = None'")

    # Must implement all required abstract methods
    for method_name, expected_params in _REQUIRED_METHODS:
        method = getattr(cls, method_name, None)
        if method is None:
            errors.append(f"Missing required method: {method_name}")
            continue

        # Check it's actually implemented (not just inherited abstract)
        if getattr(method, "__isabstractmethod__", False):
            errors.append(f"Method {method_name} is still abstract (not implemented)")
            continue

        # Check it's async
        if not (inspect.iscoroutinefunction(method) or inspect.isasyncgenfunction(method)):
            errors.append(f"Method {method_name} must be async (async def)")

    # Check __init__ doesn't require positional args beyond self and config
    try:
        # Try to instantiate with just config={}
        # We don't actually call it, just verify the signature works
        init_sig.bind(None, config={})
    except TypeError:
        # Try without config (some connectors have no config)
        try:
            init_sig.bind(None)
        except TypeError:
            errors.append(f"{cls.__name__}.__init__ must be callable with (config=dict) or no args")

    return errors


# ---------------------------------------------------------------------------
# Dynamic loading
# ---------------------------------------------------------------------------


def load_custom_connector(module_path: str) -> type[ConnectorBase]:
    """Dynamically import and return a connector class from a module path.

    Args:
        module_path: Fully qualified Python path, e.g.
                     "mypackage.connectors.wiki.WikiConnector"

    Returns:
        The connector class (not an instance).

    Raises:
        ImportError: If the module can't be imported.
        AttributeError: If the class doesn't exist in the module.
        TypeError: If the class doesn't pass validation.
    """
    parts = module_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ImportError(
            f"Invalid module path '{module_path}'. Expected format: 'package.module.ClassName'"
        )

    module_name, class_name = parts

    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ImportError(f"Failed to import module '{module_name}': {e}") from e

    cls = getattr(module, class_name, None)
    if cls is None:
        raise AttributeError(f"Module '{module_name}' has no class '{class_name}'")

    if not inspect.isclass(cls) or not issubclass(cls, ConnectorBase):
        raise TypeError(f"'{module_path}' is not a ConnectorBase subclass")

    errors = validate_connector(cls)
    if errors:
        raise TypeError(
            f"Connector '{class_name}' failed validation:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    logger.info("Loaded custom connector: %s", module_path)
    return cls


def list_registered_plugins() -> list[ConnectorManifest]:
    """Return all registered connector plugins."""
    return list(_PLUGIN_REGISTRY.values())


def get_plugin(name: str) -> ConnectorManifest | None:
    """Get a registered plugin by name."""
    return _PLUGIN_REGISTRY.get(name)
