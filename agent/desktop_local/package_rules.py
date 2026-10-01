# encoding:utf-8
"""What may enter a skill package, defined once for both ends.

The server builds a skill package and the device verifies it. If those two ends
each carried their own copy of "is this path safe" / "is this a credential",
they would drift, and the drift would be invisible: the device rejecting a valid
package looks like a transfer bug, and the device *accepting* an invalid one looks
like nothing at all until it is exploited.

So the rules live here, on the device side of the boundary, and the server-side
manifest builder imports *them*. The dependency points this way on purpose:
``agent/skills`` may know what a device accepts; the device worker must never
import the server's skill machinery, because it has to stay runnable inside the
sandbox with nothing but the standard library.

Nothing here touches the filesystem -- these are pure predicates over strings and
bytes, which is what makes them usable from a manifest builder that only has a
path and from a cache that only has a payload.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

__all__ = [
    "PackageRuleError",
    "digest_of",
    "safe_relative",
    "refuse_secrets",
    "is_secret_name",
]


class PackageRuleError(Exception):
    """A refused package entry, with a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


#: Basenames that are credentials by convention. A skill never legitimately
#: ships these, and "the package happened to include one" is how a skill becomes
#: a credential-exfiltration route.
SECRET_BASENAMES = frozenset({
    ".env", ".netrc", ".pgpass", "credentials", "credentials.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "identity.db",
})

#: Extensions that carry keys or stores rather than skill content.
SECRET_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".keystore", ".jks")

#: Path segments that must never appear: a VCS directory holds history and
#: remote credentials, not skill resources.
SECRET_SEGMENTS = frozenset({".git", ".ssh", ".aws", ".gnupg"})


def digest_of(payload: bytes) -> str:
    """``sha256:<hex>`` over a payload. One spelling, both ends."""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def is_secret_name(relative: str) -> bool:
    """Whether a package-relative path names a credential."""
    parts = relative.replace("\\", "/").split("/")
    basename = parts[-1].lower()
    if any(segment in SECRET_SEGMENTS for segment in parts[:-1]):
        return True
    if basename in SECRET_BASENAMES or basename.startswith(".env"):
        return True
    return basename.endswith(SECRET_SUFFIXES)


def refuse_secrets(relative: str) -> None:
    """:func:`is_secret_name`, as a refusal."""
    if is_secret_name(relative):
        raise PackageRuleError(
            "secret_refused", f"{relative!r} is a credential, not skill content")


def safe_relative(raw: Any) -> str:
    """Validate a package-relative path, or refuse it.

    The refusals matter more than the acceptance: this is the only thing between
    a manifest written by a remote server and a write anywhere on the machine.
    Returning the *normalised* form (forward slashes, no ``.`` segments) rather
    than the input keeps the two ends agreeing on how a path is spelled, which is
    what lets a manifest entry and a payload key be compared at all.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PackageRuleError("path_outside_package", "empty resource path")
    text = raw.strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise PackageRuleError(
            "path_outside_package", f"{raw!r} is absolute; resources are relative")
    parts = text.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise PackageRuleError(
                "path_outside_package",
                f"{raw!r} is not a normalised relative path")
    return "/".join(parts)
