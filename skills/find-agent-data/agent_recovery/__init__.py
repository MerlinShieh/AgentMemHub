"""Read-only local AI conversation recovery protocol.

This package is the single implementation of the evidence-backed recovery
flow used by both the Hub adapters and the find-agent-data skill CLIs.
It never writes to vendor stores and never decrypts vendor fields.
"""

from __future__ import annotations

SCHEMA = "agent-recovery/v1"
QODER_MAP_SCHEMA = "find-agent-data/qoder-map-v1"

from .qoder import (  # noqa: E402
    ProductLayout,
    RecoveredSession,
    layout,
    recover_all,
    recover_query,
)

__all__ = [
    "SCHEMA",
    "QODER_MAP_SCHEMA",
    "ProductLayout",
    "RecoveredSession",
    "layout",
    "recover_all",
    "recover_query",
]
