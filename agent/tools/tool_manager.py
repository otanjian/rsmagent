import importlib
import importlib.util
import inspect
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Type
from agent.tools.base_tool import BaseTool
from common.log import logger
from config import conf


def _normalize_mcp_configs(raw) -> list:
    """
    Convert MCP server config to internal list format.
    Supports:
      - list format (mcp_servers):  [{"name": "x", "type": "stdio", ...}]
      - dict format (mcpServers):   {"x": {"command": "npx", ...}}
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        result = []
        for name, cfg in raw.items():
            entry = {"name": name, **cfg}
            if "type" not in entry:
                entry["type"] = "sse" if "url" in entry else "stdio"
            result.append(entry)
        return result
    return []


def _requires_injected_dependencies(cls) -> bool:
    """Whether a tool class needs arguments that only an Agent boot can supply.

    ``load_tools`` builds a throwaway ``cls()`` just to read each tool's name,
    so a class declaring a required ``__init__`` parameter cannot be built
    there. Today those are the memory tools (``MemoryAddTool``,
    ``MemorySearchTool``, ``MemoryGetTool`` want a ``MemoryManager`` and a user
    id) and ``McpTool``; the first three are injected per Agent by
    ``bridge.agent_initializer`` and belong to no engine-level catalog.

    Reading the signature instead of naming the classes keeps a newly added
    dependency-injected tool from failing its constructor here, which logged an
    error to *stdout* — the machine-readable channel of
    ``scripts/auth_preflight.py --json`` — and left the tool out of the catalog
    this same registry feeds.
    """
    try:
        parameters = inspect.signature(cls.__init__).parameters.values()
    except (TypeError, ValueError):
        return False  # not introspectable: let the constructor decide
    for parameter in parameters:
        if parameter.name == "self":
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.default is parameter.empty:
            return True
    return False


def _resolve_mcp_store(path: str) -> dict:
    """Ask the migration ledger which ``mcp.json`` entries the control plane owns.

    Kept as a module-level seam so the wiring is testable without an identity
    database: this is the single call that makes ``ToolManager``'s configuration
    read scope-aware, and the task is precisely about that call existing.

    Imported lazily and on purpose. ``integrations.external.migration`` reaches
    for ``conf()``, the connection service and the adapters, and the tool manager
    is imported on every Agent boot — a failure to import must degrade to "read
    the file" (the ``legacy`` default) rather than take the boot down.
    """
    from auth.service import get_identity_service
    from integrations.external import migration

    return migration.resolve_mcp_servers(get_identity_service(), path=path)


class ToolManager:
    """
    Tool manager for managing tools.

    One instance per Agent workspace. Not a plain singleton: the instance
    caches booted MCP subprocesses so that per-session agent init does not
    re-fork them, and each workspace has its own ``mcp.json``. A single
    process-wide instance would let the first Agent to start decide which MCP
    servers exist, hand its tools (and their credentials) to every other
    Agent, and leave their own servers permanently unloaded.
    """
    _instances: Dict[str, "ToolManager"] = {}
    _instances_lock = threading.Lock()

    def __new__(cls):
        from common.state_dir import real_state_root

        key = real_state_root()
        instance = cls._instances.get(key)
        if instance is not None:
            return instance
        with cls._instances_lock:
            instance = cls._instances.get(key)
            if instance is None:
                instance = super(ToolManager, cls).__new__(cls)
                instance.workspace_root = key
                instance.tool_classes = {}  # Store tool classes instead of instances
                instance._initialized = False
                cls._instances[key] = instance
            return instance

    @classmethod
    def instances(cls) -> list:
        """Every ToolManager built so far, for process-wide operations."""
        with cls._instances_lock:
            return list(cls._instances.values())

    @classmethod
    def reset_instances(cls) -> None:
        """Drop every cached instance. For tests."""
        with cls._instances_lock:
            cls._instances.clear()

    def __init__(self):
        # Runs on every ToolManager() call, including the ones that get a
        # cached instance back, so every field is guarded.
        if not hasattr(self, 'tool_classes'):
            self.tool_classes = {}  # Dictionary to store tool classes
        if not hasattr(self, '_mcp_registry'):
            self._mcp_registry = None  # Lazy init: only created when MCP servers are configured
        if not hasattr(self, '_mcp_tool_instances'):
            self._mcp_tool_instances: dict = {}  # tool_name -> McpTool instance
        if not hasattr(self, '_mcp_lock'):
            # Guards _mcp_loaded check-then-set so concurrent callers
            # don't trigger duplicate background loaders.
            self._mcp_lock = threading.Lock()
        if not hasattr(self, '_mcp_loaded'):
            # Idempotency flag. Flipped to True the moment the first loader
            # is dispatched (synchronously, inside _mcp_lock). Subsequent
            # _load_mcp_tools() calls become no-ops, so per-session agent
            # initialization never re-forks MCP subprocesses.
            self._mcp_loaded = False
        if not hasattr(self, '_mcp_status'):
            # server_name -> "pending" / "ready" / "failed"
            # Useful for UI / introspection while async loading is in progress.
            self._mcp_status: dict = {}
        if not hasattr(self, '_mcp_signature'):
            # (mtime, sha256) of mcp.json the last time we loaded.
            # Used by refresh_mcp_if_changed() to skip re-parsing when nothing changed.
            self._mcp_signature: tuple = (None, None)
        if not hasattr(self, '_mcp_active_configs'):
            # server_name -> normalized config dict, for diff-based reload.
            self._mcp_active_configs: dict = {}
        if not hasattr(self, '_mcp_migrated'):
            # server_name -> the control plane owns this entry, so the file no
            # longer supplies it. Kept so a refresh log or a diagnostic can say
            # why a server that is still in mcp.json is not in the tool list.
            self._mcp_migrated: set = set()
        if not hasattr(self, '_mcp_tool_vectors'):
            # mcp_tool_name -> embedding vector, used by on-demand tool
            # retrieval. Populated lazily on first retrieval so users who
            # never enable the feature pay zero embedding cost.
            self._mcp_tool_vectors: dict = {}
        if not hasattr(self, '_mcp_vector_lock'):
            # Guards incremental index builds so concurrent turns don't
            # double-embed the same newly-loaded MCP tools.
            self._mcp_vector_lock = threading.Lock()
        if not hasattr(self, '_embedding_provider_initialized'):
            # The embedding provider is created once, lazily, and reused for
            # both tool-index and per-query embeddings. None means keyword-only
            # mode (no provider configured) — retrieval then falls back to full
            # injection at the caller.
            self._embedding_provider_initialized = False
            self._embedding_provider = None

    def load_tools(self, tools_dir: str = "", config_dict=None):
        """
        Load tools from both directory and configuration.

        :param tools_dir: Directory to scan for tool modules
        """
        if tools_dir:
            self._load_tools_from_directory(tools_dir)
            self._configure_tools_from_config()
        else:
            self._load_tools_from_init()
            self._configure_tools_from_config(config_dict)

        self._load_mcp_tools()

    def _load_tools_from_init(self) -> bool:
        """
        Load tool classes from tools.__init__.__all__

        :return: True if tools were loaded, False otherwise
        """
        try:
            # Try to import the tools package
            tools_package = importlib.import_module("agent.tools")

            # Check if __all__ is defined
            if hasattr(tools_package, "__all__"):
                tool_classes = tools_package.__all__

                # Import each tool class directly from the tools package
                for class_name in tool_classes:
                    try:
                        # Skip base classes
                        if class_name in ["BaseTool", "ToolManager"]:
                            continue

                        # Get the class directly from the tools package
                        if hasattr(tools_package, class_name):
                            cls = getattr(tools_package, class_name)

                            if (
                                    isinstance(cls, type)
                                    and issubclass(cls, BaseTool)
                                    and cls != BaseTool
                            ):
                                try:
                                    # Skip dependency-injected tools (memory
                                    # tools want a MemoryManager) and the rest
                                    # of the classes this registry cannot build.
                                    if _requires_injected_dependencies(cls):
                                        logger.debug(
                                            f"Skipped tool {class_name} (requires injected dependencies)")
                                        continue
                                    # McpTool instances are registered dynamically via _load_mcp_tools()
                                    if class_name == "McpTool":
                                        logger.debug(f"Skipped tool {class_name} (registered dynamically via mcp_servers config)")
                                        continue
                                    
                                    # Create a temporary instance to get the name
                                    temp_instance = cls()
                                    tool_name = temp_instance.name
                                    # Store the class, not the instance
                                    self.tool_classes[tool_name] = cls
                                    logger.debug(f"Loaded tool: {tool_name} from class {class_name}")
                                except ImportError as e:
                                    # Handle missing dependencies with helpful messages
                                    error_msg = str(e)
                                    if "markdownify" in error_msg:
                                        logger.warning(
                                            f"[ToolManager] {cls.__name__} not loaded - missing markdownify.\n"
                                            f"  Install with: pip install markdownify"
                                        )
                                    else:
                                        logger.warning(f"[ToolManager] {cls.__name__} not loaded due to missing dependency: {error_msg}")
                                except Exception as e:
                                    logger.error(f"Error initializing tool class {cls.__name__}: {e}")
                    except Exception as e:
                        logger.error(f"Error importing class {class_name}: {e}")

                return len(self.tool_classes) > 0
            return False
        except ImportError:
            logger.warning("Could not import agent.tools package")
            return False
        except Exception as e:
            logger.error(f"Error loading tools from __init__.__all__: {e}")
            return False

    def _load_tools_from_directory(self, tools_dir: str):
        """Dynamically load tool classes from directory"""
        tools_path = Path(tools_dir)

        # Traverse all .py files
        for py_file in tools_path.rglob("*.py"):
            # Skip initialization files and base tool files
            if py_file.name in ["__init__.py", "base_tool.py", "tool_manager.py"]:
                continue

            # Get module name
            module_name = py_file.stem

            try:
                # Load module directly from file
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    # Find tool classes in the module
                    for attr_name in dir(module):
                        cls = getattr(module, attr_name)
                        if (
                                isinstance(cls, type)
                                and issubclass(cls, BaseTool)
                                and cls != BaseTool
                        ):
                            try:
                                # Dependency-injected tools (memory tools want a
                                # MemoryManager) are not engine-loadable.
                                if _requires_injected_dependencies(cls):
                                    logger.debug(
                                        f"Skipped tool {attr_name} (requires injected dependencies)")
                                    continue
                                
                                # Create a temporary instance to get the name
                                temp_instance = cls()
                                tool_name = temp_instance.name
                                # Store the class, not the instance
                                self.tool_classes[tool_name] = cls
                            except ImportError as e:
                                # Handle missing dependencies with helpful messages
                                error_msg = str(e)
                                if "markdownify" in error_msg:
                                    logger.warning(
                                        f"[ToolManager] {cls.__name__} not loaded - missing markdownify.\n"
                                        f"  Install with: pip install markdownify"
                                    )
                                else:
                                    logger.warning(f"[ToolManager] {cls.__name__} not loaded due to missing dependency: {error_msg}")
                            except Exception as e:
                                logger.error(f"Error initializing tool class {cls.__name__}: {e}")
            except Exception as e:
                print(f"Error importing module {py_file}: {e}")

    def _configure_tools_from_config(self, config_dict=None):
        """Configure tool classes based on configuration file"""
        try:
            # Get tools configuration
            tools_config = config_dict or conf().get("tools", {})

            # Record tools that are configured but not loaded
            missing_tools = []

            # Store configurations for later use when instantiating
            self.tool_configs = tools_config

            # Check which configured tools are missing
            for tool_name in tools_config:
                if tool_name not in self.tool_classes:
                    missing_tools.append(tool_name)

            # If there are missing tools, record warnings
            if missing_tools:
                for tool_name in missing_tools:
                    if tool_name == "google_search":
                        logger.warning(
                            f"[ToolManager] Google Search tool is configured but may need API key.\n"
                            f"  Get API key from: https://serper.dev\n"
                            f"  Configure in config.json: tools.google_search.api_key"
                        )
                    else:
                        logger.warning(f"[ToolManager] Tool '{tool_name}' is configured but could not be loaded.")

        except Exception as e:
            logger.error(f"Error configuring tools from config: {e}")

    def _mcp_json_path(self) -> str:
        # Anchored to the workspace this instance was built for, not to the
        # ambient identity: the MCP loader and refresh run on background
        # threads that carry no identity and would otherwise read the default
        # Agent's mcp.json.
        from common.state_dir import mcp_config_file
        return str(mcp_config_file(base=self.workspace_root))

    def _read_mcp_json_signature(self):
        """
        Return (mtime, sha256_of_bytes) for ~/cow/mcp.json without parsing.
        Returns (None, None) if the file doesn't exist or is unreadable.
        Cheap enough (one stat + one small read) to call on every agent init.
        """
        import os
        import hashlib
        path = self._mcp_json_path()
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return (None, None)
        try:
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            return (mtime, None)
        return (mtime, digest)

    def _load_mcp_configs(self) -> list:
        """
        Load MCP server configs with priority:
          1. ~/cow/mcp.json  (supports both mcpServers and mcp_servers keys)
          2. config.json mcp_servers field (fallback)

        The file's entries are then filtered through the scope-aware connection
        resolver (:func:`_resolve_mcp_store`): a server the migration ledger has
        imported into a scope that no longer reads the legacy store is served by
        the control plane from then on, per tenant, and must not also be
        registered here from the file. Two copies would be two identities for one
        server, and the file copy is the one ``mcp.json`` trusts as configuration.
        """
        import os
        import json as _json

        mcp_json_path = self._mcp_json_path()

        if os.path.exists(mcp_json_path):
            try:
                with open(mcp_json_path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                raw = data.get("mcpServers") or data.get("mcp_servers") or data
                # DEBUG: with N agents this fires N times for the same shared
                # mcp.json; the real boot is logged once at INFO further below.
                logger.debug(f"[ToolManager] Loading MCP config from {mcp_json_path}")
                return self._without_migrated_servers(
                    mcp_json_path, _normalize_mcp_configs(raw))
            except Exception as e:
                logger.warning(f"[ToolManager] Failed to read {mcp_json_path}: {e}, falling back to config.json")

        raw = conf().get("mcp_servers", [])
        return _normalize_mcp_configs(raw)

    def _without_migrated_servers(self, path: str, entries: list) -> list:
        """Drop the entries the control plane has taken over from the file.

        Filtered by *name* rather than by rebuilding the entries from the
        resolver's answer: the loader needs every key the file carried, and the
        server name is the identity the migration preserved (it maps a legacy
        record to a connection of the same name), so the name is the join key on
        both sides. A resolver that raises leaves the file read intact — the
        default ``store_version`` is ``legacy``, so "no answer" means "the file
        is the answer", not "the server is gone".
        """
        self._mcp_migrated = set()
        try:
            view = _resolve_mcp_store(path)
        except Exception as e:  # noqa: BLE001 - the legacy read is the safe default
            logger.debug(f"[ToolManager] MCP store resolution skipped: {e}")
            return entries
        taken = {str(item.get("name") or "")
                 for item in (view.get("migrated") or ())}
        taken.discard("")
        self._mcp_migrated = taken
        if not taken:
            return entries
        kept = [entry for entry in entries
                if str(entry.get("name") or "") not in taken]
        logger.info(
            "[ToolManager] %d MCP server(s) are served by the control plane and "
            "are no longer read from %s: %s",
            len(entries) - len(kept), path, sorted(taken))
        return kept

    def _mcp_migrated_names(self) -> set:
        """Server names in ``mcp.json`` that the control plane now owns."""
        return set(getattr(self, "_mcp_migrated", None) or ())

    def _mcp_store_marker(self) -> str:
        """The resolver's marker for this file, or ``""`` when it has none."""
        try:
            return str(_resolve_mcp_store(self._mcp_json_path()).get("marker") or "")
        except Exception:  # noqa: BLE001 - no marker, no extra refresh trigger
            return ""

    def _mcp_store_signature(self) -> tuple:
        """The signature that decides whether a refresh is needed.

        The file's own ``(mtime, sha256)`` cannot see the two changes that matter
        most to this list: a store switch (which takes entries away from the file)
        and a console edit of a connection the file once provided. Both move the
        resolver's marker, so it is part of the signature rather than a second
        comparison someone has to remember to add.
        """
        mtime, digest = self._read_mcp_json_signature()
        return (mtime, digest, self._mcp_store_marker())


    def _load_mcp_tools(self):
        """
        Trigger MCP tool loading in a background thread (idempotent).

        Returns immediately. Booting MCP servers (npx, uvx, etc.) takes
        seconds to tens of seconds on first run, which would otherwise
        block agent initialization and the user's first message.
        Built-in tools work fine without MCP, so we let the agent serve
        traffic right away and let MCP servers come online in the
        background. Per-session agents read a snapshot of whatever is
        ready at construction time and gracefully ignore the rest.
        """
        with self._mcp_lock:
            if self._mcp_loaded:
                return
            mcp_servers_config = self._load_mcp_configs()
            # Snapshot the signature now so future refresh_mcp_if_changed()
            # calls can short-circuit when nothing has changed on disk (or in
            # the control plane behind the file).
            self._mcp_signature = self._mcp_store_signature()
            self._mcp_active_configs = {
                cfg.get("name", "<unnamed>"): cfg for cfg in mcp_servers_config
            }
            if not mcp_servers_config:
                # Mark as loaded even when there is nothing to load,
                # so we don't re-read the config file on every call.
                self._mcp_loaded = True
                return

            # Mark pending immediately so list_mcp_status() callers see
            # the in-progress state instead of an empty dict.
            for cfg in mcp_servers_config:
                name = cfg.get("name", "<unnamed>")
                self._mcp_status[name] = "pending"

            self._mcp_loaded = True
            threading.Thread(
                target=self._load_mcp_tools_async,
                args=(mcp_servers_config,),
                daemon=True,
                name="mcp-loader",
            ).start()
            # DEBUG: fires once per agent; with many agents sharing one mcp.json
            # this is just noise. The actual server boot is logged at INFO.
            logger.debug(
                f"[ToolManager] MCP loading started in background "
                f"({len(mcp_servers_config)} server(s) configured)"
            )

    def refresh_mcp_if_changed(self):
        """
        Cheap check whether ~/cow/mcp.json has changed since last load.
        If it has, do a diff-based reload: start newly added servers,
        shut down removed ones, and restart any whose config was edited.
        Untouched servers are left running.

        Designed to be called on every agent creation. The fast path is
        a single os.stat() — completely free when nothing has changed.
        """
        with self._mcp_lock:
            new_sig = self._mcp_store_signature()
            if new_sig == self._mcp_signature:
                return  # no-op fast path

            try:
                new_configs = self._load_mcp_configs()
            except Exception as e:
                logger.warning(f"[ToolManager] MCP reload — failed to parse config: {e}")
                return

            new_by_name = {
                cfg.get("name", "<unnamed>"): cfg for cfg in new_configs
            }
            old_by_name = self._mcp_active_configs

            added = [n for n in new_by_name if n not in old_by_name]
            removed = [n for n in old_by_name if n not in new_by_name]
            changed = [
                n for n in new_by_name
                if n in old_by_name and new_by_name[n] != old_by_name[n]
            ]

            if not (added or removed or changed):
                # Signature drifted but content is logically identical
                # (e.g. user re-saved the file without edits). Just sync.
                self._mcp_signature = new_sig
                return

            logger.info(
                f"[ToolManager] mcp.json changed — "
                f"adding={added}, removing={removed}, restarting={changed}"
            )

            # Tear down removed + changed servers (changed ones get restarted below)
            for name in removed + changed:
                self._teardown_mcp_server(name)

            # Spin up newly added + changed servers in the background
            to_start = [new_by_name[n] for n in added + changed]
            if to_start:
                for cfg in to_start:
                    self._mcp_status[cfg.get("name", "<unnamed>")] = "pending"
                threading.Thread(
                    target=self._load_mcp_tools_async,
                    args=(to_start,),
                    daemon=True,
                    name="mcp-loader-reload",
                ).start()

            self._mcp_active_configs = new_by_name
            self._mcp_signature = new_sig

    def _teardown_mcp_server(self, server_name: str):
        """Shut down one MCP server and drop its tools from the registry."""
        if self._mcp_registry is None:
            return
        client = None
        with self._mcp_registry._registry_lock:
            client = self._mcp_registry._clients.pop(server_name, None)
        if client is not None:
            # This client may be pooled and shared with other Agents on the same
            # mcp.json. Drop the matching pool entry so a later reload re-boots a
            # fresh subprocess rather than handing out the one we're stopping.
            try:
                pool = self._mcp_registry._shared_pool
                with self._mcp_registry._shared_pool_lock:
                    for k in [k for k, v in pool.items() if v is client]:
                        pool.pop(k, None)
            except Exception:
                pass
            try:
                client.shutdown()
            except Exception as e:
                logger.warning(f"[MCP] Error shutting down '{server_name}': {e}")
        # Drop tools that belonged to this server.
        for tool_name in list(self._mcp_tool_instances.keys()):
            tool = self._mcp_tool_instances.get(tool_name)
            if tool is not None and getattr(tool, "server_name", None) == server_name:
                self._mcp_tool_instances.pop(tool_name, None)
        self._mcp_status.pop(server_name, None)

    def _load_mcp_tools_async(self, mcp_servers_config):
        """
        Background worker: bring up each MCP server one-by-one and
        publish ready tools to _mcp_tool_instances as they come online.

        Server failures are isolated — one bad server cannot block
        the others, and never raises out of the worker thread.
        """
        try:
            from agent.tools.mcp.mcp_client import McpClient, McpClientRegistry, set_reload_callback
            from agent.tools.mcp.mcp_tool import McpTool

            registry = McpClientRegistry()
            self._mcp_registry = registry
            # Let the OAuth web callback bring a server online once authorized.
            set_reload_callback(self.reload_mcp_server)

            mcp_json_path = self._mcp_json_path()

            booted_any = False
            for cfg in mcp_servers_config:
                server_name = cfg.get("name", "<unnamed>")
                try:
                    # Reuse a subprocess already booted from the *same* mcp.json
                    # with the *same* config, so several Agents sharing one
                    # mcp.json don't each fork their own copy of every server.
                    # Booting is serialized per key, so concurrent loader threads
                    # racing on the same server end up sharing one subprocess.
                    share_key = registry.shared_key(mcp_json_path, server_name, cfg)
                    boot_failure = {}

                    def _boot():
                        c = McpClient(cfg)
                        if c.initialize():
                            return c
                        boot_failure["needs_auth"] = getattr(c, "needs_auth", False)
                        return None

                    client, reused = registry.get_or_boot_shared(share_key, _boot)
                    if client is None:
                        if boot_failure.get("needs_auth"):
                            self._mcp_status[server_name] = "needs_auth"
                            logger.info(
                                f"[MCP] Server '{server_name}' needs authorization — "
                                f"waiting for the user to complete the OAuth flow"
                            )
                        else:
                            self._mcp_status[server_name] = "failed"
                            logger.warning(
                                f"[MCP] Server '{server_name}' failed to initialize — skipping"
                            )
                        continue

                    tool_schemas = client.list_tools()
                    added = []
                    for schema in tool_schemas:
                        tool_name = schema.get("name", "")
                        if not tool_name:
                            continue
                        mcp_tool = McpTool(
                            client, schema, server_name,
                            name_prefix=cfg.get("tool_name_prefix", ""),
                        )
                        # Atomic dict assignment is GIL-safe; readers iterate
                        # over a list() snapshot to avoid concurrent mutation.
                        self._mcp_tool_instances[mcp_tool.name] = mcp_tool
                        added.append(mcp_tool.name)

                    # Register client into the shared registry only after its
                    # tools are visible, so callers never see a half-loaded server.
                    with registry._registry_lock:
                        registry._clients[server_name] = client
                    self._mcp_status[server_name] = "ready"
                    if reused:
                        # A shared subprocess this Agent attached to; log quietly
                        # so N Agents sharing one mcp.json don't repeat the line.
                        logger.debug(
                            f"[MCP] Server '{server_name}' reused — "
                            f"{len(added)} tool(s) attached"
                        )
                    else:
                        booted_any = True
                        logger.info(
                            f"[MCP] Server '{server_name}' ready — "
                            f"{len(added)} tool(s): {added}"
                        )
                except Exception as e:
                    self._mcp_status[server_name] = "failed"
                    logger.warning(f"[MCP] Server '{server_name}' load failed: {e}")

            ready = sum(1 for s in self._mcp_status.values() if s == "ready")
            total = len(self._mcp_status)
            # Only surface the summary at INFO when this loader actually booted a
            # server. When every server was reused from the shared pool (the
            # common case for the 2nd..Nth agent sharing one mcp.json) keep it at
            # DEBUG to avoid N identical "loading complete" lines.
            _complete_log = logger.info if booted_any else logger.debug
            _complete_log(
                f"[ToolManager] MCP loading complete: "
                f"{ready}/{total} server(s) ready, "
                f"{len(self._mcp_tool_instances)} tool(s) available"
            )
        except Exception as e:
            logger.warning(f"[ToolManager] MCP background loader crashed: {e}")

    def reload_mcp_server(self, server_name: str) -> None:
        """Re-initialize a single MCP server (e.g. after OAuth authorization).

        Tears down any existing client for the server and starts it again in
        the background, so a freshly-stored access token is picked up and the
        server's tools become available on the next message.
        """
        with self._mcp_lock:
            cfg = self._mcp_active_configs.get(server_name)
        if not cfg:
            logger.warning(f"[MCP] reload requested for unknown server '{server_name}'")
            return
        logger.info(f"[MCP] Reloading server '{server_name}' after authorization")
        self._teardown_mcp_server(server_name)
        self._mcp_status[server_name] = "pending"
        threading.Thread(
            target=self._load_mcp_tools_async,
            args=([cfg],),
            daemon=True,
            name=f"mcp-reload-{server_name}",
        ).start()

    def list_mcp_status(self) -> dict:
        """Return {server_name: status} snapshot for UI / debugging."""
        return dict(self._mcp_status)

    def sync_mcp_into_agent(self, agent) -> tuple:
        """
        Reconcile a live agent's tool collection with the current MCP tool registry.

        Adds tools that finished loading after the agent was created,
        and removes tools whose MCP server was torn down. Built-in tools
        on the agent are left untouched.

        Handles both representations RongAI uses:
          - Agent.tools: list[BaseTool]               (default Agent class)
          - AgentStream.tools: dict[str, BaseTool]    (streaming agent)

        Returns (added_names, removed_names) for logging.
        """
        if agent is None or not hasattr(agent, "tools"):
            return ([], [])

        # Never re-inject MCP tools into a restricted Self-Evolution review agent.
        # The review agent is created with a deliberately reduced, workspace-guarded
        # toolset; silently re-adding configured MCP tools here would bypass that
        # policy boundary (see agent/evolution/executor.py). The flag may live on
        # the agent itself (Agent) or on the wrapping stream executor's .agent.
        if getattr(agent, "_evolution_restricted", False) or getattr(
            getattr(agent, "agent", None), "_evolution_restricted", False
        ):
            return ([], [])

        from agent.tools.mcp.mcp_tool import McpTool
        # A restriction may apply to this agent (digital-employee allow/deny).
        # Re-injecting MCP tools that finished loading after the agent was built
        # must respect it, otherwise the policy is bypassed.
        allowed_names = self._agent_allowed_mcp_names(agent)
        current = self._mcp_tool_instances
        registry_names = set(current.keys())
        if allowed_names is not None:
            registry_names = {n for n in registry_names if n in allowed_names}

        agent_tools = agent.tools

        if isinstance(agent_tools, dict):
            agent_mcp_names = {
                name for name, tool in agent_tools.items()
                if isinstance(tool, McpTool)
            }
            added = registry_names - agent_mcp_names
            removed = agent_mcp_names - registry_names
            if not (added or removed):
                return ([], [])
            for name in added:
                agent_tools[name] = current[name]
            for name in removed:
                agent_tools.pop(name, None)

        elif isinstance(agent_tools, list):
            agent_mcp_names = {
                t.name for t in agent_tools if isinstance(t, McpTool)
            }
            added = registry_names - agent_mcp_names
            removed = agent_mcp_names - registry_names
            if not (added or removed):
                return ([], [])
            if removed:
                agent.tools = [
                    t for t in agent_tools
                    if not (isinstance(t, McpTool) and t.name in removed)
                ]
            for name in added:
                agent.tools.append(current[name])

        else:
            return ([], [])

        return (sorted(added), sorted(removed))

    def sync_external_into_agent(self, agent) -> tuple:
        """Reconcile a live agent's tools with the *caller's* external tools.

        The mirror of :meth:`sync_mcp_into_agent`, and deliberately not the same
        thing. MCP tools come from configuration and are the same for everyone;
        external tools come from the actor's connections, permissions and object
        scope, and change when any of those change. So this is called per turn
        (the identity is a per-turn fact) and it stores the result on the agent,
        never in a manager-level registry: a manager singleton outlives the
        actor, and a tool left there would appear in the next actor's list.

        A turn for an actor with no external capability therefore *removes* the
        previous set instead of leaving it: revocation has to take effect at the
        next turn, not at the next restart (spec: 工具发现不等于调用授权 — and a
        revoked tool must not even be offered).

        Returns ``(added_names, removed_names)`` for logging.
        """
        if agent is None or not hasattr(agent, "tools"):
            return ([], [])

        # The same boundary as MCP: the Self-Evolution review agent runs with a
        # deliberately reduced toolset, and an external action reaches someone
        # else's system, so it must not be silently re-added there.
        if getattr(agent, "_evolution_restricted", False) or getattr(
            getattr(agent, "agent", None), "_evolution_restricted", False
        ):
            return ([], [])

        from agent.tools.external.external_tool import (
            ExternalConnectionTool, external_tools_for,
            reconcile_external_tools)
        from common.runtime_identity import current_identity

        agent_tools = agent.tools
        if not isinstance(agent_tools, (dict, list)):
            return ([], [])

        existing = self._external_names_in(agent_tools)
        ident = current_identity()
        tenant_id = str(getattr(ident, "tenant_id", "") or "")
        user_id = str(getattr(ident, "user_id", "") or "")
        # The runtime Agent of this turn. Carried into the listing so the
        # tenant-admin exemption can be evaluated there too — an exemption that
        # only worked at dispatch would offer the admin nothing to call.
        agent_id = str(getattr(ident, "agent_id", "") or "")
        if not tenant_id or not user_id:
            # No trusted identity: nothing to offer, and anything left over is
            # removed rather than kept. A tool list is not a place to guess an
            # actor.
            wanted: dict = {}
            to_add, to_remove = [], sorted(existing)
        else:
            # Connection-derived MCP tools are discovered in the background,
            # and the pass that drops a vanished connection's names happens
            # here — at the same seam, and under the same identity, as the
            # listing itself. Mirrors refresh_mcp_if_changed() for mcp.json.
            self.refresh_external_mcp_tools(tenant_id=tenant_id,
                                            actor_user_id=user_id)
            candidates = external_tools_for(tenant_id=tenant_id,
                                            actor_user_id=user_id,
                                            agent_id=agent_id)
            # MCP capabilities discovered from a connection carry the remote
            # tool's own name and schema, so they are re-wrapped before the
            # reconcile: the wrapper refines what the model reads and inherits
            # dispatch, authorization and refusal handling unchanged.
            try:
                from agent.tools.mcp import external as mcp_external
                candidates = mcp_external.upgrade_external_mcp_tools(candidates)
            except Exception as exc:  # noqa: BLE001 - the base tools still work
                logger.debug(f"[ToolManager] MCP tool upgrade skipped: {exc}")
            # The allow/deny filter is applied inside the reconcile so the
            # listing and the removal cannot disagree about what is allowed.
            wanted, to_add, to_remove = reconcile_external_tools(
                existing, candidates,
                allowed=self._agent_allowed_names(agent, set(candidates)))

        allowed = self._agent_allowed_names(agent, set(wanted))
        if allowed is not None:
            wanted = {n: t for n, t in wanted.items() if n in allowed}

        # Reconcile as a set difference so the reported add/remove lists describe
        # what actually changed: a denied tool that was never present is not a
        # removal, and reporting it as one would make the log lie.
        existing = self._external_names_in(agent_tools)
        to_add = sorted(set(wanted) - existing)
        to_remove = sorted(existing - set(wanted))

        if isinstance(agent_tools, dict):
            for name in to_remove:
                agent_tools.pop(name, None)
            for name in to_add:
                agent_tools[name] = wanted[name]
        else:
            if to_remove:
                agent.tools = [
                    t for t in agent_tools
                    if not (isinstance(t, ExternalConnectionTool)
                            and t.name in to_remove)
                ]
            agent.tools.extend(wanted[name] for name in to_add)

        return (to_add, to_remove)

    @staticmethod
    def _external_names_in(collection) -> set:
        """The external tool names present in an agent's tool collection."""
        from agent.tools.external.external_tool import ExternalConnectionTool

        if isinstance(collection, dict):
            return {
                name for name, tool in collection.items()
                if isinstance(tool, ExternalConnectionTool)
            }
        return {
            tool.name for tool in collection or ()
            if isinstance(tool, ExternalConnectionTool)
        }

    def refresh_external_mcp_tools(self, *, tenant_id: str,
                                   actor_user_id: str = "") -> int:
        """Reconcile connection-derived MCP discovery with the live connections.

        The MCP counterpart of :meth:`refresh_mcp_if_changed`. Discovery is I/O,
        so it never runs on this pass: the pass compares each connection's
        version and credential markers against the memo, drops the memo of
        connections the tenant no longer has enabled, and starts a bounded
        background discovery only for a new, edited or re-credentialed one.

        Registration lifetimes are not duplicated here. A discovered tool is an
        :class:`~agent.tools.external.external_tool.ExternalConnectionTool`, so
        the per-turn external reconcile in :meth:`sync_external_into_agent`
        already removes it from every live agent the moment the connection stops
        being offered, and the call itself is re-authorized inside
        ``ConnectionRuntime`` regardless of what the tool list says.

        Returns the number of MCP connections considered.
        """
        try:
            from agent.tools.mcp import external as mcp_external
        except Exception as exc:  # noqa: BLE001 - no module, no discovery
            logger.debug(f"[ToolManager] external MCP discovery unavailable: {exc}")
            return 0
        try:
            return mcp_external.refresh_tenant_tools(
                tenant_id=tenant_id, actor_user_id=actor_user_id)
        except Exception as exc:  # noqa: BLE001 - listing must not fail
            logger.warning(f"[ToolManager] external MCP discovery failed: {exc}")
            return 0

    def invalidate_external_mcp_tools(self, *, tenant_id: str = "",
                                      connection_id: str = "") -> None:
        """Forget discovered MCP tool identities right now.

        Belongs to the same invalidation family as ``_teardown_mcp_server``:
        called when a connection is deleted, disabled or re-credentialed
        outside the turn loop (a console action, a migration), it makes the
        next listing re-discover instead of waiting for the row comparison to
        notice. With neither argument, everything is dropped.
        """
        try:
            from agent.tools.mcp import external as mcp_external
        except Exception:  # noqa: BLE001
            return
        if connection_id and tenant_id:
            mcp_external.forget(tenant_id=tenant_id, connection_id=connection_id)
        elif tenant_id:
            mcp_external.forget_tenant(tenant_id)
        else:
            mcp_external._reset_for_tests()

    def _agent_allowed_names(self, agent, names: set) -> Optional[set]:
        """Filter ``names`` through this agent's own tool allow/deny policy.

        ``None`` means "no allowlist" (nothing to filter). Shared by the MCP
        and the external sync on purpose: an external tool is a tool, so a
        scene or employee profile that denies a tool must deny it here too —
        otherwise the external entrance is the way around the policy.
        """
        profile = getattr(agent, "agent_profile", None)
        if profile is None:
            return None
        from agent.effective_capabilities import (
            resolve_effective_capabilities, is_tool_allowed)
        scene = None
        if getattr(profile, "scene_id", None):
            try:
                from scenes.service import find_scene
                scene, _ = find_scene(profile.scene_id)
            except Exception:  # pragma: no cover - defensive
                scene = None
        effective = resolve_effective_capabilities(profile, scene=scene)
        if effective.tools_allowlist is None and not effective.tools_denylist:
            return None
        return {name for name in names if is_tool_allowed(name, effective)}

    def _agent_allowed_mcp_names(self, agent) -> Optional[set]:
        """Return the set of MCP tool names this agent may use, or ``None``.

        ``None`` means "no allowlist" (all MCP tools allowed); an empty set
        means the agent allows none. A denylist is honoured too.
        """
        return self._agent_allowed_names(agent, set(self._mcp_tool_instances))

    # ------------------------------------------------------------------
    # On-demand MCP tool retrieval support
    #
    # The vector index and the embedding provider are owned here (singleton,
    # process-wide, aligned with the MCP tool lifecycle). The context-aware
    # selection itself lives in agent.tools.mcp.tool_retrieval, driven by the
    # executor which is the only place that knows the conversation context.
    # ------------------------------------------------------------------

    def count_mcp_tools(self) -> int:
        """Return the number of currently loaded MCP tools."""
        return len(self._mcp_tool_instances)

    def get_mcp_tool_vectors(self) -> dict:
        """Return ``{mcp_tool_name: vector}`` for currently loaded MCP tools.

        Lazily embeds any MCP tools not yet in the cache (MCP servers load
        asynchronously, so tools may appear over time). Returns an empty dict
        when no embedding provider is available or embedding fails — the caller
        then falls back to full injection. Never raises.
        """
        try:
            self._ensure_mcp_tool_vectors()
        except Exception as e:
            logger.debug(f"[ToolManager] MCP tool vector build skipped: {e}")
        return dict(self._mcp_tool_vectors)

    def embed_query(self, text: str):
        """Embed a retrieval query with the shared provider.

        Returns the embedding vector, or None if no provider is available or
        the call fails (caller falls back to full injection). Never raises.
        """
        if not text:
            return None
        provider = self._get_embedding_provider()
        if provider is None:
            return None
        try:
            return provider.embed_query(text)
        except Exception as e:
            logger.debug(f"[ToolManager] query embedding failed: {e}")
            return None

    def _ensure_mcp_tool_vectors(self) -> None:
        """Incrementally embed MCP tools that are not yet cached."""
        # Snapshot to avoid concurrent-mutation while the async loader runs.
        current = dict(self._mcp_tool_instances)
        missing = [name for name in current if name not in self._mcp_tool_vectors]
        if not missing:
            return

        provider = self._get_embedding_provider()
        if provider is None:
            return

        with self._mcp_vector_lock:
            # Re-check under lock: another thread may have filled these in.
            missing = [name for name in current if name not in self._mcp_tool_vectors]
            if not missing:
                return
            texts = [self._mcp_tool_embed_text(current[name]) for name in missing]
            vectors = provider.embed_batch(texts)
            for name, vec in zip(missing, vectors):
                self._mcp_tool_vectors[name] = vec

    @staticmethod
    def _mcp_tool_embed_text(tool) -> str:
        """Build the text that represents an MCP tool for embedding."""
        name = getattr(tool, "name", "") or ""
        description = getattr(tool, "description", "") or ""
        return f"{name}: {description}".strip()

    def _get_embedding_provider(self):
        """Lazily create and cache the shared embedding provider (or None)."""
        if not self._embedding_provider_initialized:
            try:
                from agent.memory.embedding import create_default_embedding_provider
                self._embedding_provider = create_default_embedding_provider()
            except Exception as e:
                logger.warning(f"[ToolManager] embedding provider init failed: {e}")
                self._embedding_provider = None
            self._embedding_provider_initialized = True
        return self._embedding_provider

    def create_tool(self, name: str) -> BaseTool:
        """
        Get a new instance of a tool by name.

        :param name: The name of the tool to get.
        :return: A new instance of the tool or None if not found.
        """
        tool_class = self.tool_classes.get(name)
        if tool_class:
            # Create a new instance
            tool_instance = tool_class()

            # Apply configuration if available
            if hasattr(self, 'tool_configs') and name in self.tool_configs:
                tool_instance.config = self.tool_configs[name]

            return tool_instance

        # Fall back to MCP tool instances
        mcp_tool = self._mcp_tool_instances.get(name)
        if mcp_tool:
            return mcp_tool

        return None

    def list_tools(self) -> dict:
        """
        Get information about all loaded tools.

        :return: A dictionary with tool information.
        """
        result = {}
        for name, tool_class in self.tool_classes.items():
            # Create a temporary instance to get schema
            temp_instance = tool_class()
            result[name] = {
                "description": temp_instance.description,
                "parameters": temp_instance.get_json_schema()
            }

        # Include MCP tool instances
        for name, mcp_tool in self._mcp_tool_instances.items():
            result[name] = {
                "description": mcp_tool.description,
                "parameters": mcp_tool.params,
            }

        return result

    def shutdown_mcp(self):
        """Shut down all MCP server clients."""
        if self._mcp_registry:
            self._mcp_registry.shutdown_all()
