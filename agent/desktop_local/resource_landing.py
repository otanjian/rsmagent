# encoding:utf-8
"""Explicit, verified landing for server resources (task 8.6).

The contract sentence this implements (``execution-contract.md`` §6):

    服务器附件用于本机任务时，资源准备返回 ``(resource_id, version, digest)``；
    客户端先下载到本轮临时输入目录并校验，再向工具提供执行端位置。

and its companion rule for MCP native references: "同理，不把 ``server:/...`` 当本机
路径".

So a server attachment reaches a local tool through a **landing**, and the landing
is the only place a server-side byte becomes a local file. That makes this module
the single choke point for the property the requirement is really about: a local
tool must never read *something* while nobody can say whether it is the resource
the server authorized.

Why verification is not optional
--------------------------------

A landing that skips verification is worse than a refusal. The refusal is visible
-- the call fails and says why. An unverified landing produces a file, the tool
reads it, and the model reports success; the bytes might be an older version, a
truncated transfer, or a different resource entirely, and nothing in the run can
tell. So three things are checked before anything becomes visible:

1. **version** -- a resource that comes back at a version other than the pinned one
   is a drift, not an equivalent (``resource_unavailable``);
2. **the fetcher's own digest** -- the claim must match the bytes handed over, which
   is what catches a corrupt or substituted transfer *independently* of whether the
   caller knew a digest;
3. **the caller's pinned digest** -- when the reference carried one, the landed
   bytes must match it (``resource_digest_mismatch``).

Publication is atomic (``.partial-*`` then ``os.replace``), so a failed check
leaves *nothing*: a leftover partial file would be indistinguishable from a good
one to the next caller, which is precisely the failure this ordering prevents.

Landing is idempotent per ``(resource_id, version, digest)``. A run that stages the
same attachment for five tool calls downloads and verifies once, and the second call
must not re-publish over a file another call may already be reading.

The root is granted, never derived
----------------------------------

``root`` is supplied by the caller -- the run's temporary input directory -- and is
never derived from a request. A resource id, by contrast, *is* server input, so it is
normalized through the same relative-path grammar as every other logical reference
and cannot contain ``..``, a backslash or a leading ``/``. It becomes a filename on
this machine; it is exactly the value that must not be trusted as a path.

Boundary
--------

This module does not fetch over the network itself: it is handed a ``fetch``
callable. Deciding *whether* an attachment may be read is authorization's job (the
existing granted-tool path), and this module must not become a second, weaker copy
of that decision.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from common.log import logger

__all__ = [
    "DIGEST_MISMATCH",
    "UNAVAILABLE",
    "LIMIT_EXCEEDED",
    "INVALID_REFERENCE",
    "DEFAULT_MAX_BYTES",
    "FetchedResource",
    "LandingRequest",
    "Landing",
    "ResourceLanding",
    "normalize_digest",
    "digest_of",
]

#: Stable codes, matching the contract's standard error list (``execution-contract``
#: §7). ``resource_digest_mismatch`` is the one this task adds: it is deliberately
#: distinct from ``resource_unavailable`` so an operator can tell "the server would
#: not give it to us" apart from "what we got is not what was promised".
DIGEST_MISMATCH = "resource_digest_mismatch"
UNAVAILABLE = "resource_unavailable"
LIMIT_EXCEEDED = "limit_exceeded"
INVALID_REFERENCE = "invalid_reference"

#: A generous ceiling; the caller's limit config may lower it. Finite on purpose:
#: an unbounded landing is a way to fill the run's input directory.
DEFAULT_MAX_BYTES = 512 * 1024 * 1024

_HEX = re.compile(r"^[0-9a-f]{64}$")
_PARTIAL_PREFIX = ".partial-"


def digest_of(data: bytes) -> str:
    """The one digest this module speaks: sha256, lowercase hex."""
    return hashlib.sha256(data).hexdigest()


def normalize_digest(raw: Any) -> str:
    """``sha256:<hex>`` or bare ``<hex>`` -> lowercase hex, else ``""``.

    An unparsable value returns ``""`` rather than the input, so a caller cannot
    accidentally compare against something that was never a digest. Compare with
    :func:`digest_of` only through :func:`_digests_agree`, which treats ``""`` as
    "no claim" instead of "matches nothing".
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if ":" in text:
        algo, _, rest = text.partition(":")
        if algo != "sha256":
            return ""
        text = rest.strip()
    return text if _HEX.match(text) else ""


def _digests_agree(declared: str, actual: str) -> bool:
    """Whether a declared digest *permits* ``actual``.

    An absent declaration permits anything (it is not a claim); a present one must
    match exactly. Empty means absent here, and only here -- everywhere else an
    empty digest is treated as "not provided".
    """
    return not declared or declared == actual


@dataclass(frozen=True)
class FetchedResource:
    """What a fetcher hands back: the bytes plus the server's own claims.

    ``digest`` is the server's claim about ``data``. It is compared against the
    bytes actually received, so a fetcher cannot vouch for a payload it did not
    deliver correctly.
    """

    version: str = ""
    digest: str = ""
    data: bytes = b""
    name: str = ""


@dataclass(frozen=True)
class LandingRequest:
    """One resource to land, as pinned by the caller (``resource:`` reference).

    ``size``, when declared, is checked *before* fetching: it costs nothing and
    avoids downloading something already known to be over the cap.
    """

    resource_id: str
    version: str = ""
    digest: str = ""
    size: Optional[int] = None
    name: str = ""


@dataclass(frozen=True)
class Landing:
    """A landed resource, or the reason there is none."""

    ok: bool = False
    relative: str = ""
    absolute: str = ""
    digest: str = ""
    version: str = ""
    size: int = 0
    #: True when an already-verified copy was reused instead of fetched again.
    reused: bool = False
    code: str = ""
    message: str = ""

    @staticmethod
    def refusal(code: str, message: str) -> "Landing":
        return Landing(ok=False, code=code, message=message)


def _refusal(code: str, message: str) -> Landing:
    return Landing.refusal(code, message)


class ResourceLanding:
    """Lands server resources into one run's input directory, verified.

    ``fetch`` is called as ``fetch(resource_id)`` and returns a
    :class:`FetchedResource` (or ``None`` for "no such resource"). It is injected
    because authorization belongs to the caller: this class must not decide
    whether an attachment is readable.
    """

    def __init__(self, fetch: Callable[[str], Optional[FetchedResource]],
                 root: str, *, max_bytes: Optional[int] = None):
        self._fetch = fetch
        self._root = str(root or "")
        self._max_bytes = DEFAULT_MAX_BYTES if max_bytes is None else int(max_bytes)
        #: (resource_id, version, digest) -> relative path already verified.
        self._landed: dict = {}

    @property
    def root(self) -> str:
        return self._root

    def _root_or_none(self) -> Optional[str]:
        """The landing root as a real path, or ``None`` when it is unusable.

        A missing root is a refusal and *not* a directory to create: the root is
        granted by the caller, so provisioning one from here would put files
        somewhere nobody authorized.
        """
        if not self._root or not os.path.isabs(self._root):
            return None
        real = os.path.realpath(self._root)
        return real if os.path.isdir(real) else None

    def land(self, request: LandingRequest) -> Landing:
        """Fetch, verify and publish one resource; never publish unverified bytes."""
        relative = self._safe_relative(request.resource_id)
        if relative is None:
            return _refusal(
                INVALID_REFERENCE,
                f"resource id {request.resource_id!r} is not a usable relative "
                f"resource name")

        root = self._root_or_none()
        if root is None:
            return _refusal(
                UNAVAILABLE,
                f"the run's input directory is not available ({self._root!r}); "
                f"nothing was downloaded and no directory was created")

        pinned = normalize_digest(request.digest)
        declared_pin = str(request.digest or "").strip()
        if declared_pin and not pinned:
            # A pin that cannot be parsed must not silently become "no pin": that
            # would downgrade a typo into an unverified landing, and the whole
            # point of the pin is that it is checked.
            return _refusal(
                INVALID_REFERENCE,
                f"the pinned digest for {request.resource_id!r} is not a sha256 "
                f"digest ({declared_pin!r}); refusing rather than landing unverified")

        if request.size is not None and request.size > self._max_bytes:
            return _refusal(
                LIMIT_EXCEEDED,
                f"resource {request.resource_id!r} declares {request.size} bytes, "
                f"over the {self._max_bytes}-byte limit; it was not downloaded")

        cached = self._cached(relative, request.version, pinned)
        if cached is not None:
            return cached

        try:
            fetched = self._fetch(request.resource_id)
        except Exception as e:  # noqa: BLE001 - a failed fetch is a refusal
            logger.warning(f"[ResourceLanding] fetching {request.resource_id!r} "
                           f"failed: {e}")
            return _refusal(
                UNAVAILABLE,
                f"resource {request.resource_id!r} could not be retrieved: {e}")

        if fetched is None:
            return _refusal(
                UNAVAILABLE, f"resource {request.resource_id!r} is not available")

        if request.version and str(fetched.version or "") != request.version:
            return _refusal(
                UNAVAILABLE,
                f"resource {request.resource_id!r} came back as version "
                f"{fetched.version!r}, but version {request.version!r} was pinned; "
                f"a drifted version is not used")

        data = bytes(fetched.data or b"")
        if len(data) > self._max_bytes:
            return _refusal(
                LIMIT_EXCEEDED,
                f"resource {request.resource_id!r} is {len(data)} bytes, over the "
                f"{self._max_bytes}-byte limit; it was not landed")

        actual = digest_of(data)
        claimed = normalize_digest(fetched.digest)
        raw_claim = str(fetched.digest or "").strip()
        if raw_claim and not claimed:
            return _refusal(
                DIGEST_MISMATCH,
                f"the source of {request.resource_id!r} reported an unusable digest "
                f"({raw_claim!r}); nothing was landed")
        if not claimed and not pinned:
            # Nothing to verify against. An unverified landing is exactly the
            # outcome 8.6 exists to prevent, so this is a refusal rather than a
            # silent "no digest, proceed".
            return _refusal(
                UNAVAILABLE,
                f"neither the request nor the source of {request.resource_id!r} "
                f"supplied a digest; refusing to land unverified bytes")
        if not _digests_agree(claimed, actual):
            # The fetcher's own claim disagrees with what it delivered. This is
            # the check that does not depend on the caller having pinned a digest.
            return _refusal(
                DIGEST_MISMATCH,
                f"resource {request.resource_id!r} does not match the digest its own "
                f"source reported (expected {claimed}, computed {actual}); nothing "
                f"was landed")
        if not _digests_agree(pinned, actual):
            return _refusal(
                DIGEST_MISMATCH,
                f"resource {request.resource_id!r} does not match the pinned digest "
                f"(expected {pinned}, computed {actual}); nothing was landed")

        published = self._publish(root, relative, data)
        if published is None:
            return _refusal(
                UNAVAILABLE,
                f"resource {request.resource_id!r} could not be written into the "
                f"run's input directory")

        landing = Landing(ok=True, relative=relative, absolute=published,
                          digest=actual, version=str(fetched.version or ""),
                          size=len(data), reused=False)
        self._landed[(relative, request.version, actual)] = landing
        return landing

    def _cached(self, relative: str, version: str, pinned: str) -> Optional[Landing]:
        """The already-verified landing for this exact pin, when there is one.

        Keyed by the *computed* digest, so a cache hit can only ever be a landing
        that passed every check. A caller who pinned a digest that the cached
        landing does not have must not be answered from the cache.
        """
        for (cached_relative, cached_version, cached_digest), landing in self._landed.items():
            if cached_relative != relative or cached_version != version:
                continue
            if pinned and cached_digest != pinned:
                continue
            if not os.path.isfile(landing.absolute):
                # Removed out from under us; land it again rather than hand back
                # a path that no longer exists.
                continue
            return Landing(ok=True, relative=landing.relative,
                           absolute=landing.absolute, digest=landing.digest,
                           version=landing.version, size=landing.size, reused=True)
        return None

    def _safe_relative(self, resource_id: Any) -> Optional[str]:
        """``resource_id`` as a normalized relative path, or ``None`` if unusable."""
        text = str(resource_id or "").strip()
        if not text or "\\" in text or text.startswith(("/", "~")):
            return None
        if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
            return None
        parts = [p for p in text.split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            return None
        return "/".join(parts)

    def _publish(self, root: str, relative: str, data: bytes) -> Optional[str]:
        """Write ``data`` to ``relative`` atomically, returning the real path.

        The bytes go to a ``.partial-`` sibling first and are renamed into place
        only once they are complete, so a reader never observes a short file and
        an interrupted landing leaves no visible result.
        """
        target = os.path.join(root, *relative.split("/"))
        real_root = os.path.realpath(root)
        real_target = os.path.realpath(os.path.dirname(target))
        if real_target != real_root and not real_target.startswith(real_root + os.sep):
            logger.info(f"[ResourceLanding] refusing a landing outside the run's "
                        f"input directory: {relative!r}")
            return None
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            partial = os.path.join(os.path.dirname(target),
                                   f"{_PARTIAL_PREFIX}{uuid.uuid4().hex}")
            try:
                with open(partial, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(partial, target)
            except Exception:
                # Never leave the partial behind: it is the "exists but is not
                # the resource" artefact this module exists to prevent.
                try:
                    os.unlink(partial)
                except OSError:
                    pass
                raise
        except Exception as e:  # noqa: BLE001 - reported, not swallowed
            logger.warning(f"[ResourceLanding] writing {relative!r} failed: {e}")
            return None
        return os.path.realpath(target)
