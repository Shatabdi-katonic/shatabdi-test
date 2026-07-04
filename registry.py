"""Connector registry.

Maps ConnectorType enum values to their implementation classes.
New connectors are registered by adding an entry to _NATIVE_CONNECTORS.
"""

from __future__ import annotations

from enum import Enum

from platform_knowledge_engine.connectors.base import ConnectorBase


class ConnectorType(str, Enum):
    """Supported knowledge source connector types."""

    # ── Core ──
    FILE_UPLOAD = "file_upload"
    DATABASE = "database"
    SNOWFLAKE = "snowflake"
    SAP = "sap"

    # ── Knowledge Base & Wikis ──
    CONFLUENCE = "confluence"
    SHAREPOINT = "sharepoint"
    NOTION = "notion"
    BOOKSTACK = "bookstack"
    DOCUMENT360 = "document360"
    DISCOURSE = "discourse"
    GITBOOK = "gitbook"
    SLAB = "slab"
    OUTLINE = "outline"
    GOOGLE_SITES = "google_sites"
    GURU = "guru"

    # ── Cloud Storage ──
    GOOGLE_DRIVE = "google_drive"
    DROPBOX = "dropbox"
    S3 = "s3"
    GOOGLE_CLOUD_STORAGE = "google_cloud_storage"
    EGNYTE = "egnyte"
    ORACLE_STORAGE = "oracle_storage"
    CLOUDFLARE_R2 = "r2"

    # ── Ticketing & Task Management ──
    JIRA = "jira"
    ZENDESK = "zendesk"
    AIRTABLE = "airtable"
    LINEAR = "linear"
    FRESHDESK = "freshdesk"
    ASANA = "asana"
    CLICKUP = "clickup"
    PRODUCTBOARD = "productboard"

    # ── Messaging ──
    SLACK = "slack"
    TEAMS = "teams"
    GMAIL = "gmail"
    DISCORD = "discord"
    XENFORO = "xenforo"
    ZULIP = "zulip"

    # ── Sales ──
    SALESFORCE = "salesforce"
    HUBSPOT = "hubspot"
    GONG = "gong"
    FIREFLIES = "fireflies"
    HIGHSPOT = "highspot"

    # ── Code Repository ──
    GITHUB = "github"
    GITLAB = "gitlab"
    BITBUCKET = "bitbucket"

    # ── Project Management ──
    MONDAY = "monday"
    TRELLO = "trello"
    BASECAMP = "basecamp"
    WRIKE = "wrike"
    SMARTSHEET = "smartsheet"

    # ── Customer Support ──
    INTERCOM = "intercom"
    HELPSCOUT = "helpscout"
    FRONT = "front"

    # ── Documents ──
    CODA = "coda"

    # ── CRM ──
    PIPEDRIVE = "pipedrive"
    ZOHOCRM = "zohocrm"

    # ── IT / DevOps ──
    SERVICENOW = "servicenow"
    PAGERDUTY = "pagerduty"

    # ── HR / Recruiting ──
    BAMBOOHR = "bamboohr"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"

    # ── Design ──
    FIGMA = "figma"
    MIRO = "miro"

    # ── CMS ──
    WORDPRESS = "wordpress"
    WEBFLOW = "webflow"

    # ── Cloud Storage ──
    BOX = "box"
    ONEDRIVE = "onedrive"

    # ── Calendar ──
    GOOGLE_CALENDAR = "google_calendar"

    # ── Forms ──
    TYPEFORM = "typeform"
    SURVEYMONKEY = "surveymonkey"

    # ── Other ──
    WEB = "web"
    CUSTOM = "custom"


# Lazy import paths: module_path.ClassName
_NATIVE_CONNECTORS: dict[ConnectorType, str] = {
    # Core
    ConnectorType.FILE_UPLOAD: "platform_knowledge_engine.connectors.file_upload.FileUploadConnector",
    ConnectorType.DATABASE: "platform_knowledge_engine.connectors.database.DatabaseConnector",
    ConnectorType.SNOWFLAKE: "platform_knowledge_engine.connectors.snowflake.SnowflakeConnector",
    ConnectorType.SAP: "platform_knowledge_engine.connectors.sap.SAPConnector",
    # Knowledge Base & Wikis
    ConnectorType.CONFLUENCE: "platform_knowledge_engine.connectors.confluence.ConfluenceConnector",
    ConnectorType.SHAREPOINT: "platform_knowledge_engine.connectors.sharepoint.SharePointConnector",
    ConnectorType.NOTION: "platform_knowledge_engine.connectors.notion.NotionConnector",
    ConnectorType.BOOKSTACK: "platform_knowledge_engine.connectors.bookstack.BookStackConnector",
    ConnectorType.DOCUMENT360: "platform_knowledge_engine.connectors.document360.Document360Connector",
    ConnectorType.DISCOURSE: "platform_knowledge_engine.connectors.discourse.DiscourseConnector",
    ConnectorType.GITBOOK: "platform_knowledge_engine.connectors.gitbook.GitBookConnector",
    ConnectorType.SLAB: "platform_knowledge_engine.connectors.slab.SlabConnector",
    ConnectorType.OUTLINE: "platform_knowledge_engine.connectors.outline.OutlineConnector",
    ConnectorType.GOOGLE_SITES: "platform_knowledge_engine.connectors.google_sites.GoogleSitesConnector",
    ConnectorType.GURU: "platform_knowledge_engine.connectors.guru.GuruConnector",
    # Cloud Storage
    ConnectorType.GOOGLE_DRIVE: "platform_knowledge_engine.connectors.google_drive.GoogleDriveConnector",
    ConnectorType.DROPBOX: "platform_knowledge_engine.connectors.dropbox.DropboxConnector",
    ConnectorType.S3: "platform_knowledge_engine.connectors.s3.S3Connector",
    ConnectorType.GOOGLE_CLOUD_STORAGE: "platform_knowledge_engine.connectors.google_cloud_storage.GoogleCloudStorageConnector",
    ConnectorType.EGNYTE: "platform_knowledge_engine.connectors.egnyte.EgnyteConnector",
    ConnectorType.ORACLE_STORAGE: "platform_knowledge_engine.connectors.oracle_storage.OracleStorageConnector",
    ConnectorType.CLOUDFLARE_R2: "platform_knowledge_engine.connectors.cloudflare_r2.CloudflareR2Connector",
    # Ticketing & Task Management
    ConnectorType.JIRA: "platform_knowledge_engine.connectors.jira.JiraConnector",
    ConnectorType.ZENDESK: "platform_knowledge_engine.connectors.zendesk.ZendeskConnector",
    ConnectorType.AIRTABLE: "platform_knowledge_engine.connectors.airtable.AirtableConnector",
    ConnectorType.LINEAR: "platform_knowledge_engine.connectors.linear.LinearConnector",
    ConnectorType.FRESHDESK: "platform_knowledge_engine.connectors.freshdesk.FreshdeskConnector",
    ConnectorType.ASANA: "platform_knowledge_engine.connectors.asana.AsanaConnector",
    ConnectorType.CLICKUP: "platform_knowledge_engine.connectors.clickup.ClickUpConnector",
    ConnectorType.PRODUCTBOARD: "platform_knowledge_engine.connectors.productboard.ProductBoardConnector",
    # Messaging
    ConnectorType.SLACK: "platform_knowledge_engine.connectors.slack.SlackConnector",
    ConnectorType.TEAMS: "platform_knowledge_engine.connectors.teams.TeamsConnector",
    ConnectorType.GMAIL: "platform_knowledge_engine.connectors.gmail.GmailConnector",
    ConnectorType.DISCORD: "platform_knowledge_engine.connectors.discord.DiscordConnector",
    ConnectorType.XENFORO: "platform_knowledge_engine.connectors.xenforo.XenForoConnector",
    ConnectorType.ZULIP: "platform_knowledge_engine.connectors.zulip.ZulipConnector",
    # Sales
    ConnectorType.SALESFORCE: "platform_knowledge_engine.connectors.salesforce.SalesforceConnector",
    ConnectorType.HUBSPOT: "platform_knowledge_engine.connectors.hubspot.HubSpotConnector",
    ConnectorType.GONG: "platform_knowledge_engine.connectors.gong.GongConnector",
    ConnectorType.FIREFLIES: "platform_knowledge_engine.connectors.fireflies.FirefliesConnector",
    ConnectorType.HIGHSPOT: "platform_knowledge_engine.connectors.highspot.HighspotConnector",
    # Code Repository
    ConnectorType.GITHUB: "platform_knowledge_engine.connectors.github.GitHubConnector",
    ConnectorType.GITLAB: "platform_knowledge_engine.connectors.gitlab.GitLabConnector",
    ConnectorType.BITBUCKET: "platform_knowledge_engine.connectors.bitbucket.BitbucketConnector",
    # Project Management
    ConnectorType.MONDAY: "platform_knowledge_engine.connectors.monday.MondayConnector",
    ConnectorType.TRELLO: "platform_knowledge_engine.connectors.trello.TrelloConnector",
    ConnectorType.BASECAMP: "platform_knowledge_engine.connectors.basecamp.BasecampConnector",
    ConnectorType.WRIKE: "platform_knowledge_engine.connectors.wrike.WrikeConnector",
    ConnectorType.SMARTSHEET: "platform_knowledge_engine.connectors.smartsheet.SmartsheetConnector",
    # Customer Support
    ConnectorType.INTERCOM: "platform_knowledge_engine.connectors.intercom.IntercomConnector",
    ConnectorType.HELPSCOUT: "platform_knowledge_engine.connectors.helpscout.HelpScoutConnector",
    ConnectorType.FRONT: "platform_knowledge_engine.connectors.front.FrontConnector",
    # Documents
    ConnectorType.CODA: "platform_knowledge_engine.connectors.coda.CodaConnector",
    # CRM
    ConnectorType.PIPEDRIVE: "platform_knowledge_engine.connectors.pipedrive.PipedriveConnector",
    ConnectorType.ZOHOCRM: "platform_knowledge_engine.connectors.zohocrm.ZohoCRMConnector",
    # IT / DevOps
    ConnectorType.SERVICENOW: "platform_knowledge_engine.connectors.servicenow.ServiceNowConnector",
    ConnectorType.PAGERDUTY: "platform_knowledge_engine.connectors.pagerduty.PagerDutyConnector",
    # HR / Recruiting
    ConnectorType.BAMBOOHR: "platform_knowledge_engine.connectors.bamboohr.BambooHRConnector",
    ConnectorType.GREENHOUSE: "platform_knowledge_engine.connectors.greenhouse.GreenhouseConnector",
    ConnectorType.LEVER: "platform_knowledge_engine.connectors.lever.LeverConnector",
    # Design
    ConnectorType.FIGMA: "platform_knowledge_engine.connectors.figma.FigmaConnector",
    ConnectorType.MIRO: "platform_knowledge_engine.connectors.miro.MiroConnector",
    # CMS
    ConnectorType.WORDPRESS: "platform_knowledge_engine.connectors.wordpress.WordPressConnector",
    ConnectorType.WEBFLOW: "platform_knowledge_engine.connectors.webflow.WebflowConnector",
    # Cloud Storage
    ConnectorType.BOX: "platform_knowledge_engine.connectors.box.BoxConnector",
    ConnectorType.ONEDRIVE: "platform_knowledge_engine.connectors.onedrive.OneDriveConnector",
    # Calendar
    ConnectorType.GOOGLE_CALENDAR: "platform_knowledge_engine.connectors.google_calendar.GoogleCalendarConnector",
    # Forms
    ConnectorType.TYPEFORM: "platform_knowledge_engine.connectors.typeform.TypeformConnector",
    ConnectorType.SURVEYMONKEY: "platform_knowledge_engine.connectors.surveymonkey.SurveyMonkeyConnector",
    # Web
    ConnectorType.WEB: "platform_knowledge_engine.connectors.web.WebConnector",
}


def get_connector_class(connector_type: ConnectorType | str) -> type[ConnectorBase]:
    """Get connector class for a given type.

    Supports both native ConnectorType enum values and custom SDK-registered
    connectors (pass the full module path as a string for custom connectors,
    or use ConnectorType.CUSTOM with a 'connector_class' config field).

    Raises ValueError if the connector type is not registered.
    """
    # Handle string inputs (custom connector module paths)
    if isinstance(connector_type, str) and "." in connector_type:
        from platform_knowledge_engine.connectors.sdk import load_custom_connector

        return load_custom_connector(connector_type)

    # Convert string to enum if needed
    if isinstance(connector_type, str):
        try:
            connector_type = ConnectorType(connector_type)
        except ValueError:
            pass

    # Check SDK plugin registry first
    if isinstance(connector_type, str):
        from platform_knowledge_engine.connectors.sdk import get_plugin

        plugin = get_plugin(connector_type)
        if plugin and plugin.connector_class:
            return plugin.connector_class

    class_path = _NATIVE_CONNECTORS.get(connector_type)
    if not class_path:
        available = ", ".join(t.value for t in _NATIVE_CONNECTORS)
        raise ValueError(
            f"Connector type '{connector_type}' not registered. Available: {available}"
        )

    module_path, class_name = class_path.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def get_available_connectors() -> list[ConnectorType]:
    """Return list of all registered connector types (native + SDK plugins)."""
    return list(_NATIVE_CONNECTORS.keys())


def get_config_schema(connector_type: ConnectorType | str) -> list:
    """Return a connector's declared optional config fields (ConfigField list).

    Used by the wizard UI to render per-connector "Connector settings" inputs
    and by source create/update validation. Returns [] for connectors that
    have not declared a schema — those behave exactly as before this feature.
    Never raises on import errors; a missing module just yields an empty list.
    """
    try:
        cls = get_connector_class(connector_type)
    except ValueError:
        return []
    except Exception:
        return []
    return list(getattr(cls, "CONFIG_SCHEMA", []) or [])


def get_all_config_schemas() -> dict[str, list]:
    """Return {connector_type_value: [ConfigField, ...]} for every native connector.

    Imports each connector module lazily; failures are swallowed per-connector
    so one broken optional dependency doesn't take down the whole endpoint.
    """
    schemas: dict[str, list] = {}
    for ctype in _NATIVE_CONNECTORS:
        schemas[ctype.value] = get_config_schema(ctype)
    return schemas


def get_all_available_names() -> list[str]:
    """Return all available connector names including SDK plugins."""
    names = [t.value for t in _NATIVE_CONNECTORS]
    try:
        from platform_knowledge_engine.connectors.sdk import list_registered_plugins

        for plugin in list_registered_plugins():
            if plugin.name not in names:
                names.append(plugin.name)
    except ImportError:
        pass
    return names
