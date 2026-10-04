"""Process-wide state shared between the WSGI handler and the gateway thread.

The scene handler (WSGI thread) mints the token; the gateway (its own asyncio
loop in a daemon thread) verifies it. A module-level store is what lets both
sides agree without a database round-trip per frame.
"""
from __future__ import annotations

import os

from .tokens import ViewTokens

#: Long enough for the pane to open the socket right after the mint round-trip,
#: short enough that a leaked value is worthless. Revoked as soon as the WS
#: closes, too.
VIEW_TOKEN_TTL_SECONDS = float(os.environ.get("SAP_WORKBENCH_VIEW_TOKEN_TTL", "90"))

view_tokens = ViewTokens(ttl=VIEW_TOKEN_TTL_SECONDS)
