# encoding:utf-8
"""Guard the proxy-side gzip scope added for console login startup
(change optimize-console-login-startup, tasks 1.2 / 1.3).

The production edge is a host Nginx that proxies the app (see
``docker/nginx.conf.example``). Compression is only safe here if it stays
limited to the non-sensitive public shell and its first-party JS/CSS: the API
and user data are JSON, the live channels use SSE, and both must keep their
existing delivery behavior. These assertions pin that boundary in the shipped
example, which is the patch the target site's config is derived from.
"""

import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
EXAMPLE = os.path.join(ROOT, "docker", "nginx.conf.example")


def _config():
    with open(EXAMPLE, encoding="utf-8") as f:
        return f.read()


def _directive(config, name):
    match = re.search(rf"^\s*{re.escape(name)}\s+([^;]+);", config, re.M)
    assert match, f"missing {name} directive"
    return match.group(1).strip()


def test_compression_is_enabled_with_negotiation_and_proxied_responses():
    config = _config()
    assert _directive(config, "gzip") == "on"
    assert _directive(config, "gzip_vary") == "on"
    # Responses come from the upstream app, so proxied responses must be
    # eligible for compression at all.
    assert _directive(config, "gzip_proxied") == "any"


def test_compression_covers_the_public_shell_and_first_party_assets_only():
    types = set(_directive(_config(), "gzip_types").split())
    assert {"text/html", "text/css"} <= types, types
    # The console serves its first-party scripts as text/javascript.
    assert {"text/javascript", "application/javascript"} <= types, types
    # Personal/API and streaming payloads must stay uncompressed. Excluding
    # them here is what leaves their caching and immediate delivery untouched.
    assert "application/json" not in types, types
    assert "text/event-stream" not in types, types


def test_compression_does_not_rebuffer_streaming_or_overwrite_forwarding():
    config = _config()
    # The streaming contract (SSE) relies on buffering off; the gzip change
    # must not have replaced it.
    assert re.search(r"^\s*proxy_buffering\s+off;", config, re.M)
    # Forwarding headers are still overwritten, not appended.
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in config
    assert "proxy_set_header X-Forwarded-Proto https;" in config
