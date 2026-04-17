from __future__ import annotations

import ssl

import truststore

from .settings import AssistantSettings


def build_ssl_context(settings: AssistantSettings | None = None) -> ssl.SSLContext:
    if settings is not None and settings.openclaw_tls_insecure_skip_verify:
        context = ssl._create_unverified_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    if settings is not None and settings.openclaw_ca_bundle_path:
        return ssl.create_default_context(cafile=settings.openclaw_ca_bundle_path)

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
