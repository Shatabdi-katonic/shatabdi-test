"""
OpenTelemetry tracing callback.

Creates a span for every LLM call with attributes matching the
``gen_ai`` semantic conventions (https://opentelemetry.io/docs/specs/semconv/gen-ai/).

Initialised only when ``GATEWAY_OTEL_ENABLED=true``.  The exporter targets
the OTLP endpoint configured by ``GATEWAY_OTEL_ENDPOINT``.

Usage (in app lifespan)::

    if settings.otel_enabled:
        otel = OTelCallback(settings)
        otel.setup()
        app.state.otel_callback = otel

Then call ``otel.start_span`` / ``otel.end_span`` around each provider call.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("gateway.otel")

# Lazy imports: these are only needed when OTel is enabled.
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import StatusCode

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False


class OTelCallback:
    """Manages OpenTelemetry tracing for LLM calls."""

    def __init__(self, settings: Any):
        self._endpoint = settings.otel_endpoint
        self._service_name = settings.service_name
        self._environment = getattr(settings, "environment", "production")
        # ``otel_insecure`` is tri-state: explicit True, explicit False,
        # or None (= derive from URL scheme). See config.GatewaySettings.
        self._insecure_override = getattr(settings, "otel_insecure", None)
        self._tracer: Any = None

    @staticmethod
    def _resolve_insecure(
        endpoint: str,
        override: bool | None,
        environment: str,
    ) -> bool:
        """Decide whether to send spans over plaintext gRPC.

        Precedence:
          1. Explicit override (``GATEWAY_OTEL_INSECURE`` env var).
          2. Endpoint scheme — ``grpcs://`` / ``https://`` ⇒ secure.
          3. Otherwise plaintext is permitted only when running in
             ``development``; production refuses to start a plaintext
             exporter (M4: span attributes carry tenant_id / user_id).
        """
        if override is not None:
            return bool(override)
        scheme = endpoint.split("://", 1)[0].lower() if "://" in endpoint else ""
        if scheme in {"grpcs", "https"}:
            return False
        # Plaintext schemes (grpc://, http://) or no scheme — only
        # allowed in development.
        if environment.lower() != "development":
            raise ValueError(
                "OTEL endpoint uses a plaintext scheme "
                f"({scheme or '<none>'}) but environment is "
                f"'{environment}'. Switch to grpcs:// / https:// or set "
                "GATEWAY_OTEL_INSECURE=true to acknowledge plaintext."
            )
        return True

    def setup(self) -> None:
        """Initialise the TracerProvider and OTLP exporter."""
        if not _HAS_OTEL:
            logger.warning("otel_packages_not_installed")
            return

        resource = Resource.create({"service.name": self._service_name})
        provider = TracerProvider(resource=resource)

        if self._endpoint:
            try:
                insecure = self._resolve_insecure(
                    self._endpoint, self._insecure_override, self._environment,
                )
            except ValueError as exc:
                # Fail-closed in production rather than silently sending
                # tenant-tagged spans in cleartext.
                logger.error("otel_insecure_endpoint_in_production: %s", exc)
                trace.set_tracer_provider(provider)
                self._tracer = trace.get_tracer("ai-gateway", "0.1.0")
                return
            exporter = OTLPSpanExporter(endpoint=self._endpoint, insecure=insecure)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info(
                "otel_exporter_configured",
                extra={"endpoint": self._endpoint, "insecure": insecure},
            )
        else:
            logger.info("otel_endpoint_not_set_traces_will_be_dropped")

        trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer("ai-gateway", "0.1.0")

    def start_span(
        self,
        operation: str,
        *,
        tier: str = "",
        model: str = "",
        provider: str = "",
        tenant_id: str = "",
        user_id: str = "",
        stream: bool = False,
    ) -> Any:
        """Start a new span for an LLM operation.

        Returns:
            The span object (or a no-op stub if OTel is not available).
        """
        if not self._tracer:
            return _NoOpSpan()

        span = self._tracer.start_span(
            f"llm.{operation}",
            attributes={
                "gen_ai.system": provider,
                "gen_ai.request.model": model,
                "gen_ai.request.tier": tier,
                "gen_ai.request.streaming": stream,
                "tenant.id": tenant_id,
                "user.id": user_id,
            },
        )
        return span

    @staticmethod
    def end_span(
        span: Any,
        *,
        status: str = "success",
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: int = 0,
        error: str | None = None,
        model: str = "",
        is_fallback: bool = False,
    ) -> None:
        """Close a span with final attributes."""
        if isinstance(span, _NoOpSpan):
            return

        try:
            span.set_attribute("gen_ai.response.model", model)
            span.set_attribute("gen_ai.usage.prompt_tokens", tokens_in)
            span.set_attribute("gen_ai.usage.completion_tokens", tokens_out)
            span.set_attribute("llm.latency_ms", latency_ms)
            span.set_attribute("llm.is_fallback", is_fallback)

            if _HAS_OTEL:
                if status == "error" and error:
                    span.set_status(StatusCode.ERROR, error[:200])
                    span.set_attribute("error.type", error.split(":")[0])
                else:
                    span.set_status(StatusCode.OK)

            span.end()
        except Exception:
            logger.debug("otel_end_span_failed", exc_info=True)

    def shutdown(self) -> None:
        """Flush pending spans and shut down."""
        if _HAS_OTEL:
            try:
                provider = trace.get_tracer_provider()
                if hasattr(provider, "shutdown"):
                    provider.shutdown()
            except Exception:
                logger.debug("otel_shutdown_failed", exc_info=True)


class _NoOpSpan:
    """Stub span when OTel is disabled."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def end(self) -> None:
        pass
