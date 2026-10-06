# encoding:utf-8
"""Keep the suite out of the developer's real workspace.

Whatever a test loads config for, ``agent_workspace`` ends up pointing at the
directory the person running the suite actually uses, and anything that resolves
a path without pinning one down writes there: the memory files, the scheduler
store and the tmp directory are all reachable that way.

Only that one key is redirected, and only for the session. Loading the config
outright would be worse than the problem: tests would inherit the developer's
model, language and channel settings, and start passing or failing on them.
"""

import atexit
import os
import re
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Redirecting agent_workspace is not enough: ~/.cow/.env, a restore target
# without agent_workspace and every other "~" path resolve through the home
# directory itself. Set before any test module is imported.
_FAKE_HOME = tempfile.mkdtemp(prefix="cow-tests-home-")
for _name in ("HOME", "USERPROFILE"):
    os.environ[_name] = _FAKE_HOME
atexit.register(shutil.rmtree, _FAKE_HOME, ignore_errors=True)

_WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "channel", "web")


def console_js():
    """Every script the console loads, concatenated in load order.

    console.js was split into a core/ and views/ tree, so a test that wants to
    assert on "the console's code" has to look at all of it. The list comes
    from the page's own script tags rather than a copy here, so it cannot fall
    behind a file being added or reordered.
    """
    from channel.web.core import template

    page = template.render("chat.html")
    parts = []
    # Asset URLs are absolute so they resolve the same from every routed path,
    # and each carries a ?v= stamp, so the name sits between /assets/ and the
    # query rather than between /assets/ and the closing quote.
    for src in re.findall(r'<script defer src="/?assets/(js/[^"?]+)(?:\?[^"]*)?"', page):
        with open(os.path.join(_WEB_DIR, "static", src), encoding="utf-8") as f:
            parts.append(f.read())
    return "\n".join(parts)


def web_backend_py():
    """Every Python file behind the web console, concatenated.

    The counterpart of ``console_js()`` for the backend: a test that wants to
    assert on "the console's server code" should not have to know which file a
    handler or helper currently sits in, or the split of web_channel.py would
    break tests that have nothing to do with it. The list is read off the
    directory, so it cannot fall behind a file being added.

    Use this for "is this still wired up" assertions. A test that parses a
    specific structure -- the URL table, a class body -- should keep reading
    the one file it means, so that it fails loudly when that structure moves.
    """
    parts = []
    for dirpath, dirnames, filenames in os.walk(_WEB_DIR):
        dirnames[:] = [d for d in dirnames if d not in ("static", "templates", "tools", "__pycache__")]
        for name in sorted(filenames):
            if name.endswith(".py"):
                with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                    parts.append(f.read())
    return "\n".join(parts)


@pytest.fixture(autouse=True)
def sap_workbench_never_warms_a_real_engine(monkeypatch):
    """Keep the scene's engine warm-up out of the suite.

    Opening the workbench starts a background Bun warm-up once the visual scene
    is configured (``Scene.sap_workbench.backend.prewarm``). That is right for a
    deployment and wrong here: the suite would spawn and kill the real engine,
    and load a multi-gigabyte source tree, for every test that reads the scene
    configuration.
    """
    monkeypatch.setenv("SAP_WORKBENCH_PREWARM", "0")


@pytest.fixture(autouse=True)
def global_config_object_is_restored():
    """Put the process-global config back after a test that replaced it.

    ``config.conf()`` reads one module-level object. Several suites install a
    small stand-in (``config_module.config = Config({...})``) or call
    ``load_config()`` to swap in a fresh one, and nothing puts the original
    back, so every later test in the run reads the stand-in -- measured on the
    persona and sub-Agent suites, which build their prompts out of config
    values, and on anything that resolves a workspace through it. Restoring the
    *object* (not a fresh load) keeps the workspace redirect the session
    fixture applied to it.
    """
    import config as config_module

    previous = getattr(config_module, "config", None)
    yield
    if previous is not None and getattr(config_module, "config", None) is not previous:
        config_module.config = previous


@pytest.fixture(autouse=True)
def web_stub_carries_a_request_context():
    """Give the fake ``web`` module a ``ctx``, as the real one has.

    Nine test modules install a stub under ``sys.modules["web"]`` when web.py
    has not been imported yet, and which of them gets there first depends on
    the order the run collected. Handlers driven straight from a test read
    ``web.ctx`` for the query string and the request headers -- absent from the
    stubs, so whether a test sees an empty context or an AttributeError came
    down to that order. An empty context is the case the handlers are written
    for; this makes it the case they get.
    """
    web = sys.modules.get("web")
    if web is not None and not hasattr(web, "ctx"):
        web.ctx = {}
        try:
            yield
        finally:
            del web.ctx
    else:
        yield


@pytest.fixture(autouse=True)
def console_template_cache_not_poisoned():
    """Keep one test's mocked ``open`` out of the console's fragment cache.

    ``template`` caches each fragment under its mtime, which nothing in a test
    run disturbs, so a read that happened while ``builtins.open`` was patched
    is held for the rest of the session -- and every later ``render()`` returns
    that test's stand-in markup instead of the page. The cache is an
    optimisation, so dropping it around each test costs a few file reads and
    makes the suite independent of the order it ran in.
    """
    from channel.web.core import template

    template._cache.clear()
    yield
    template._cache.clear()


@pytest.fixture(autouse=True, scope="session")
def workspace_out_of_the_way():
    import config as config_module

    with tempfile.TemporaryDirectory(prefix="cow-tests-") as tmp:
        workspace = os.path.join(tmp, "cow")
        real_load = config_module.load_config

        def load_then_redirect():
            real_load()
            config_module.conf()["agent_workspace"] = workspace

        # Applied now for tests that never load, and re-applied after any that
        # do, since loading replaces the value with the real one.
        config_module.conf()["agent_workspace"] = workspace
        config_module.load_config = load_then_redirect

        # Collection imports every test module before this runs, and a module
        # that loads config on import can leave a registry or a store already
        # resolved against the real path. Drop those.
        _forget_resolved_paths()
        try:
            yield workspace
        finally:
            config_module.load_config = real_load


def _forget_resolved_paths():
    from agent.memory import clear_conversation_store_cache
    from agent.memory.config import reset_memory_configs
    from agent.registry import set_agent_registry

    set_agent_registry(None)
    reset_memory_configs()
    clear_conversation_store_cache()


@pytest.fixture
def web_app(tmp_path):
    """Build real WSGI apps over private identity databases.

    A factory rather than a single object because some tests need two tenants in
    two databases to prove an isolation rule. Every app built here is torn down
    (and its ``conf`` patch removed) at the end of the test.
    """
    from tests._helpers import WebAppHarness

    built = []

    def build(name="app", **kwargs):
        harness = WebAppHarness(tmp_path / name, **kwargs)
        built.append(harness)
        return harness

    yield build
    for harness in reversed(built):
        harness.close()
