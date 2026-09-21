# encoding:utf-8
"""The one shared OpenCode service: how it is configured, and what the console sees.

The requirement is that there is exactly **one** administrator-configured service
and that it is closed by default — a deployment that has never heard of coding
Agents must keep running its normal ones untouched. Two things follow that are
easy to get wrong and cheap to pin here:

* A missing, empty or unparsable ``opencode`` block is a supported input, not an
  error. An operator's typo in a block their normal Agents never read must not
  take the instance down.
* The password is read live from the named environment variable and never
  travels back out — not in the console projection, not in a URL, not in a
  cache. ``settings_for_console`` is the projection the Agent form renders, so
  the strongest form of that claim is a test over its keys.
"""

import pytest

from agent.coding import (
    CODING_DISABLED,
    CODING_SERVICE_CHANGED,
    CODING_WEB_ONLY,
    CodingError,
    coding_disabled,
    coding_service_changed,
    coding_web_only,
    resolve_settings,
    settings_for_console,
)

PASSWORD = "sup3r-s3cret-upstream"


@pytest.fixture
def password_env(monkeypatch):
    monkeypatch.setenv("RSM_OPENCODE_PASSWORD", PASSWORD)
    return "RSM_OPENCODE_PASSWORD"


# -- the closed default ----------------------------------------------------


@pytest.mark.parametrize("raw", [None, {}, {"enabled": None}])
def test_an_absent_or_empty_block_resolves_to_the_closed_default(raw):
    settings = resolve_settings(raw)
    assert settings.enabled is False
    assert settings.configured is False


@pytest.mark.parametrize("raw", [
    "not-a-mapping",
    {"enabled": "definitely", "api_url": 17},
    {"service_id": None, "password_env": None},
])
def test_an_unparsable_block_degrades_instead_of_raising(raw):
    """Older config files and operator typos are the same input here."""
    settings = resolve_settings(raw)
    assert settings.enabled is False
    assert settings.api_url  # still a usable address for the rest of the code


def test_the_template_defaults_the_capability_off():
    """The shipped template is the deployment default, and it must be closed."""
    import json
    from pathlib import Path

    template = json.loads(
        (Path(__file__).resolve().parents[1] / "config-template.json")
        .read_text(encoding="utf-8")
    )
    assert template["opencode"]["enabled"] is False
    assert template["opencode"]["web_url"] == ""


def test_the_capability_is_registered_so_a_deployment_can_edit_it():
    """A block the settings validator drops would silently never apply."""
    from config import available_setting

    block = available_setting["opencode"]
    assert block["enabled"] is False
    assert set(block) == {"enabled", "service_id", "api_url", "web_url",
                          "username", "password_env"}


# -- enabled / configured are different questions --------------------------


def test_enabled_and_configured_are_independent():
    """The console has to be able to say "configured but switched off"."""
    off_but_configured = resolve_settings(
        {"enabled": False, "api_url": "http://oc:4096", "web_url": "http://oc:4096"})
    assert off_but_configured.configured is True
    assert off_but_configured.enabled is False

    on_but_unaddressed = resolve_settings({"enabled": True, "api_url": "", "web_url": ""})
    assert on_but_unaddressed.enabled is True
    assert on_but_unaddressed.configured is False


def test_a_trailing_slash_is_normalized_once():
    settings = resolve_settings(
        {"api_url": "http://oc:4096/", "web_url": "http://oc:3000/"})
    assert settings.api_url == "http://oc:4096"
    assert settings.web_url == "http://oc:3000"


# -- the credential never leaves the server --------------------------------


def test_the_password_is_read_live_from_the_named_variable(password_env):
    settings = resolve_settings({"password_env": password_env})
    assert settings.password == PASSWORD
    assert settings.auth_headers()["Authorization"].startswith("Basic ")


def test_an_unset_variable_is_simply_no_credential(monkeypatch):
    monkeypatch.delenv("RSM_OPENCODE_PASSWORD", raising=False)
    settings = resolve_settings({"password_env": "RSM_OPENCODE_PASSWORD"})
    assert settings.password is None
    assert settings.auth_headers() == {}


def test_the_console_projection_carries_no_credential(password_env):
    """The form displays the service; it never submits it.

    Asserted on the keys rather than by searching for the secret, because a
    future field that *happened* to be empty today would pass a substring check
    and still be a leak waiting for a value.
    """
    projection = settings_for_console(
        {"enabled": True, "web_url": "http://oc:3000", "api_url": "http://oc:4096",
         "password_env": password_env, "username": "opencode"})

    assert set(projection) == {"enabled", "service_id", "web_url", "configured"}
    assert PASSWORD not in str(projection)
    assert password_env not in projection
    # The browser is given the page to embed, never the backend to call.
    assert "api_url" not in projection
    assert "username" not in projection


def test_the_header_uses_the_configured_username(monkeypatch):
    import base64

    monkeypatch.setenv("RSM_OPENCODE_PASSWORD", PASSWORD)
    settings = resolve_settings({"username": "operator"})
    header = settings.auth_headers()["Authorization"]
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    assert decoded == f"operator:{PASSWORD}"


# -- the stable refusals ---------------------------------------------------


def test_each_failure_has_one_code_and_one_status():
    """Callers branch on the code, so the mapping itself is the contract."""
    assert (coding_web_only("erp-coder").code, coding_web_only("erp-coder").status) \
        == (CODING_WEB_ONLY, 400)
    assert (coding_disabled().code, coding_disabled().status) == (CODING_DISABLED, 503)
    assert (coding_service_changed().code, coding_service_changed().status) \
        == (CODING_SERVICE_CHANGED, 409)


def test_a_refusal_never_reuses_the_callers_own_session_status():
    """A broken OpenCode service must not read as "you are logged out"."""
    statuses = {coding_web_only().status, coding_disabled().status,
                coding_service_changed().status}
    assert statuses.isdisjoint({401, 403})


def test_a_refusal_is_an_exception_carrying_its_own_reason():
    with pytest.raises(CodingError) as raised:
        raise coding_web_only("erp-coder")
    assert raised.value.code == CODING_WEB_ONLY
    assert "erp-coder" in raised.value.message
