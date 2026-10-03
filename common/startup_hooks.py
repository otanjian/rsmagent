# encoding:utf-8
"""Fork-owned startup hooks (tasks 6.11 and 8.9).

``app.py`` is an upstream file and its boot sequence is upstream's to change.
This fork used to answer that with inline branches in the sequence --
``_guard_identity_mode_consistency()`` aborting the boot, a tenancy backfill
added straight into the startup path -- and every upstream edit to ``run()``
then had to be merged against them.

The seam instead is a tiny registry. The fork registers a named hook here, and
``app.py`` keeps a named entry point that runs whatever is registered:

    app.py:      def _guard_identity_mode_consistency() -> bool:
                     return run_startup_hook(HOOK_IDENTITY_MODE_CONSISTENCY)

    this module: register(HOOK_IDENTITY_MODE_CONSISTENCY, _identity_mode_consistency)

Adding a guard, or removing the fork entirely, is then a change *here*, not in
``app.py``: with nothing registered ``run_startup_hook`` returns False and the
upstream boot is exactly upstream's (task 8.10). The hooks are registered when
this module is imported, and ``run_startup_hook`` lives here, so importing the
seam is enough to arm them -- ``app.py`` cannot end up calling a seam whose
implementation was never registered.

Hooks run in the calling thread, in the boot's single-threaded phase, and are
*not* individually swallowed: a hook that raises propagates, because the
identity-mode guard exists precisely to abort a boot that would read the wrong
identity. A hook that is merely best-effort catches its own errors.
"""

from __future__ import annotations

import os
import secrets
from typing import Callable, Dict, List, Optional, Tuple

HOOK_IDENTITY_MODE_CONSISTENCY = "identity_mode_consistency"
HOOK_DATABASE_BOOTSTRAP = "database_bootstrap"
HOOK_TENANT_CONVERSATION_BACKFILL = "tenant_conversation_backfill"
HOOK_SCHEDULER_TASK_MIGRATION = "scheduler_task_migration"
HOOK_EXTERNAL_STORE_VERSION = "external_store_version"
HOOK_EXECUTION_SANDBOX = "execution_sandbox"

#: One-shot initial admin password under the data root (mode 0600).
BOOTSTRAP_PASSWORD_FILENAME = ".bootstrap_admin_password"

_HOOKS: Dict[str, Tuple[int, Callable[[], None]]] = {}


def bootstrap_password_path(data_root: Optional[str] = None) -> str:
    from config import get_data_root
    root = data_root if data_root is not None else get_data_root()
    return os.path.join(root, BOOTSTRAP_PASSWORD_FILENAME)


def clear_bootstrap_password_file() -> None:
    """Remove the one-shot bootstrap password file after forced change."""
    path = bootstrap_password_path()
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _write_bootstrap_password(path: str, password: str) -> None:
    """Write ``password`` to ``path`` with mode 0600 (best-effort on Windows)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(password)
            handle.write("\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def register_startup_hook(name: str, fn: Callable[[], None],
                          order: int = 100) -> None:
    """Register ``fn`` under ``name``; re-registering replaces it."""
    _HOOKS[name] = (order, fn)


def is_registered(name: str) -> bool:
    return name in _HOOKS


#: The authorization seams a boot MUST find armed before it serves anything
#: (task R5). Declared here, *with the registry* rather than inside the
#: extensions being checked: an extension that deletes its own registration, or
#: a merge that drops one line of ``register_fork_startup_hooks``, would
#: otherwise degrade that seam to a silent no-op -- the identity guard, the
#: first-run bootstrap, the tenant backfill and the scheduled-task ownership
#: migration would simply stop happening, with the console still answering.
#:
#: The manifest is checked by :func:`verify_required_seams`, which the boot
#: assembly calls *directly* (not through :func:`run_startup_hook`): a check
#: that is itself a hook could be skipped by exactly the failure it exists to
#: catch.
REQUIRED_HOOKS: Tuple[str, ...] = (
    HOOK_IDENTITY_MODE_CONSISTENCY,
    HOOK_EXECUTION_SANDBOX,
    HOOK_DATABASE_BOOTSTRAP,
    HOOK_TENANT_CONVERSATION_BACKFILL,
    HOOK_SCHEDULER_TASK_MIGRATION,
    # Not an authorization boundary but the same failure mode: the guard
    # refuses a boot that would read the new external-connection store while
    # legacy records are still unimported. A dropped registration would make
    # that read happen silently, which is the outcome the guard exists to stop.
    HOOK_EXTERNAL_STORE_VERSION,
)


def missing_required_hooks(
        required: Optional[Tuple[str, ...]] = None) -> List[str]:
    """Names in the required-seam manifest that are not registered."""
    manifest = REQUIRED_HOOKS if required is None else required
    return [name for name in manifest if not is_registered(name)]


def verify_required_seams(required: Optional[Tuple[str, ...]] = None) -> None:
    """Refuse the boot when a required authorization seam is not armed (R5).

    rdai is *not* upstream: the seams in the manifest carry identity, tenant,
    owner and task-execution authorization, so a missing one is a missing
    authorization boundary, not a missing convenience. The two permitted
    answers are "refuse to start" (here) or "close the affected capability";
    silently booting without the seam is the one answer that is NOT permitted,
    and it is the answer a dropped registration would otherwise give.
    """
    missing = missing_required_hooks(required)
    if not missing:
        return
    from common.log import logger
    logger.error(
        "[App] Refusing to start: required authorization seam(s) not "
        "registered: %s. rdai cannot serve identity, tenant or task execution "
        "without them; restore the fork's startup assembly "
        "(common/startup_hooks.py) before starting." % ", ".join(missing)
    )
    raise RuntimeError(
        "required startup seam(s) missing: %s" % ", ".join(missing)
    )


def registered_hooks() -> List[str]:
    return [name for name, _ in sorted(_HOOKS.items(),
                                       key=lambda item: (item[1][0], item[0]))]


def run_startup_hook(name: str) -> bool:
    """Run the hook registered under ``name``; False when there is none."""
    entry = _HOOKS.get(name)
    if entry is None:
        return False
    _, fn = entry
    fn()
    return True


# ---------------------------------------------------------------------------
# This fork's hooks
# ---------------------------------------------------------------------------

def _identity_mode_consistency() -> None:
    """Refuse explicit legacy identity mode; database is the only mode.

    After retire-legacy-identity-mode, ``identity_mode=legacy`` MUST abort boot
    with an actionable message. Missing/other values run as database.
    """
    from common.log import logger
    try:
        from config import conf
        raw = conf().get("identity_mode", None)
        if raw is None or str(raw).strip() == "":
            return
        mode = str(raw).strip().lower()
        if mode == "legacy":
            logger.error(
                "[App] Refusing to start: identity_mode=legacy is no longer "
                "supported. Remove the key or set identity_mode=database, then "
                "restart. Shared-password / cow_auth_token auth has been retired."
            )
            raise RuntimeError(
                "identity_mode=legacy is no longer supported; use database identity"
            )
        if mode != "database":
            logger.error(
                f"[App] Refusing to start: unknown identity_mode={mode!r}. "
                f"Only database is supported."
            )
            raise RuntimeError(f"unsupported identity_mode={mode}")
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        logger.warning(f"[App] Identity-mode consistency check skipped: {e}")


def _database_bootstrap_auto_init() -> None:
    """Create default tenant + platform admin when identity.db has none.

    Failure aborts boot — never falls back to anonymous / shared-password
    access. The one-shot password is printed to the console and written to
    ``.bootstrap_admin_password`` (0600); it MUST NOT be logged.
    """
    from config import conf, get_data_root

    raw = conf().get("identity_mode", None)
    if raw is not None and str(raw).strip().lower() == "legacy":
        # Consistency guard raises first; skip as a safety net.
        return

    from auth.service import IdentityService, identity_db_path

    data_root = get_data_root()
    db_path = identity_db_path()
    try:
        svc = IdentityService(db_path)
        if svc.has_any_platform_admin():
            return

        password = secrets.token_urlsafe(24)
        shared_root = os.path.join(data_root, "tenants", "default")
        os.makedirs(shared_root, exist_ok=True)
        svc.bootstrap(
            tenant_code="default",
            tenant_name="Default",
            admin_username="admin",
            admin_display="Administrator",
            admin_password=password,
            shared_root=shared_root,
            allow_weak=False,
        )
        try:
            from agent.todo.service import reassign_empty_owner_todos
            admin = svc._find_user_by_username("admin")
            tenant = svc._find_tenant_by_code("default")
            if admin and tenant:
                reassign_empty_owner_todos(
                    scope_id=tenant["id"], owner_id=admin["id"],
                    app_data_root=shared_root,
                )
        except Exception:
            pass
        pw_path = bootstrap_password_path(data_root)
        _write_bootstrap_password(pw_path, password)
        # Console only — never logger (password must not enter log files).
        print(
            "[Bootstrap] Initial platform admin created.\n"
            f"  username: admin\n"
            f"  password: {password}\n"
            f"  password file: {pw_path} (mode 0600; deleted after first password change)\n"
            "  You MUST change this password on first login."
        )
    except Exception as e:
        raise RuntimeError(
            "database identity bootstrap failed; refusing to enter business "
            f"without a platform admin: {e}"
        ) from e


def _tenant_conversation_backfill() -> None:
    """Attribute pre-isolation conversations to their tenant (task 6.11).

    It runs here rather than in ``conversation_store`` because it is the one
    moment the boot is single-threaded, before any channel has opened a store.
    The assumption it shares with upstream's own conversation migration is
    design D10: conversations live in one store keyed by ``agent_id``, and this
    fork's tenancy is a *filter* column on that store (``owner``/``tenant_id``),
    never a per-Agent file. The backfill therefore only fills columns; it does
    not move or split files, so it stays compatible with
    ``migrate_conversations_to_global`` running alongside it.
    """
    from common.log import logger
    try:
        from auth.service import get_identity_service
        from agent.memory.conversation_store import (
            conversation_store_path,
            get_conversation_store,
        )

        svc = get_identity_service()
        tenants = svc.tenant_shared_roots()

        def tenants_for_owner(user_id: str):
            """The single tenant this user belongs to, or None when ambiguous.

            Multiple memberships are deliberately *not* resolved to one: a
            conversation whose owner belongs to two tenants has no derivable
            tenant, and guessing would leak it into whichever was picked.
            """
            matches = []
            for tenant in tenants:
                membership = svc.get_membership(user_id, tenant["id"])
                if membership and membership.get("active") \
                        and membership.get("user_active"):
                    matches.append(tenant["id"])
            return matches[0] if len(matches) == 1 else None

        roots = [None] + [
            t["shared_root"] for t in tenants if t.get("shared_root")
        ]
        for root in roots:
            path = conversation_store_path(root)
            if not path.exists():
                # Never manufacture an empty store for a workspace that has
                # none: opening one creates the file.
                continue
            counts = get_conversation_store(root).backfill_tenant(
                tenants_for_owner)
            if counts["sessions"] or counts["messages"] or counts["unresolved"]:
                logger.info(
                    "[App] Conversation tenancy backfill on %s: "
                    "%(sessions)s sessions, %(messages)s messages attributed, "
                    "%(unresolved)s owners left unattributed" % counts
                )
    except Exception as e:
        # A failed backfill leaves rows unattributed, which is the safe state
        # (unreadable to a tenant scope) -- it must not stop the boot.
        logger.warning(f"[App] Conversation tenancy backfill skipped: {e}")


def _scheduler_task_migration() -> None:
    """Stamp owner/scope onto historical tasks, quarantining the unattributable.

    A task written before owner tracking has no member owner, and the runtime
    refuses to execute an ownerless task on database identity (task 4.2): it
    would fire as the Agent itself, under nobody's grants. So the boot classifies
    every stored task once —

    * a task that already carries an owner gets ``scope=personal`` stamped (its
      schedule, action and id are untouched, so nothing starts running twice);
    * a task with neither an owner nor an explicit ``public`` scope is
      *quarantined*: disabled, with a redacted reason and a recovery hint. It is
      never handed to the administrator running the boot, which is the rule this
      migration exists to keep;
    * a task already declaring ``scope`` is left exactly as it is.

    Idempotent by construction: the second boot finds everything classified and
    reports ``unchanged``. A migration that is interrupted mid-way is therefore
    safe to re-run — the tasks it did not reach are still unclassified, and the
    ones it did reach keep their stamps.

    The migration runs in the boot's single-threaded phase, before any scheduler
    loop starts, so it never races a fire. Its failures are the one exception to
    "hooks propagate": a store that cannot be read leaves the tasks in their
    current (unclassified but *not executing*) state, which is safe, while
    aborting the boot would take the whole console down over one bad file.
    """
    from common.log import logger
    try:
        from agent.registry import get_agent_registry
        from agent.tools.scheduler.authorization import (
            TaskAccessService, TaskActor,
        )
        from agent.tools.scheduler.task_store import TaskStore
        from common import state_dir
        from common.runtime_identity import RuntimeIdentity

        agent_ids = [p.id for p in get_agent_registry().list(include_disabled=False)]
        if not agent_ids:
            return

        def store_for(_actor, agent_id):
            identity = RuntimeIdentity(agent_id=agent_id)
            return TaskStore(str(state_dir.scheduler_file(identity)))

        service = TaskAccessService(
            store_resolver=store_for,
            agent_ids=lambda _actor: agent_ids,
            coordinator="migration",
        )
        # Plan first, back up what the plan would touch, then apply (task 4.3's
        # "consistent backup" requirement). The dry run is the same query the
        # apply uses, so the backup covers exactly the files that change; a boot
        # that finds everything classified writes no backup at all.
        plan = service.migrate_tasks(
            TaskActor(source="migration"), agent_ids=agent_ids, apply=False)
        backups = []
        if plan["stamped"] or plan["quarantined"]:
            backups = _backup_task_stores(
                state_dir, agent_ids, plan, source="pre-migration")
        report = service.migrate_tasks(
            TaskActor(source="migration"), agent_ids=agent_ids, apply=True)
        if report["stamped"] or report["quarantined"]:
            logger.info(
                "[App] Scheduled-task ownership migration: %(stamped)s stamped, "
                "%(quarantined)s quarantined, %(unchanged)s unchanged "
                "(quarantined tasks are disabled; their owners recreate them in chat)"
                % report
            )
        if backups:
            logger.info(
                "[App] Scheduled-task migration backup written: %s "
                "(remove after the drill is recorded)" % ", ".join(backups))
    except Exception as error:
        logger.warning(f"[App] Scheduled-task ownership migration skipped: {error}")


#: How many scheduled-task backups to keep per Agent. Enough for the drill's
#: "restore to before this migration" step without growing the data directory
#: boot after boot.
TASK_STORE_BACKUP_KEEP = 5


def _external_store_version_guard() -> None:
    """Refuse a boot that would read the new store before the import (task 12.3).

    ``store_version`` selects which store the external-connection runtime reads:
    ``legacy`` (the shipped default), ``dual`` (read legacy, write both), or
    ``new``. ``new`` is only safe once every importable legacy record has been
    imported, and this is where that is enforced — the check existed as
    ``assert_store_version_safe`` but had no caller, so a deployment could set
    ``new`` and have the runtime read an empty store while the legacy ERP file
    and per-Agent ``mcp.json`` still held the real connections. The symptom
    would be "my connections disappeared", with the data still on disk.

    Three deliberate choices:

    * **The default deployment sees nothing.** ``store_version`` defaults to
      ``legacy``, for which the check is trivially safe, so a normal boot pays
      only for the configuration read.
    * **Only ``new`` can refuse the boot.** A scan that fails while the
      deployment is on ``legacy`` is downgraded to a warning: refusing to boot
      over an unused store would be an availability bug, while refusing over the
      store actually being read is the point.
    * **The refusal names the work.** ``assert_store_version_safe`` raises with
      the pending and blocking counts, so the operator is told to import first
      rather than being left with an unexplained exit.
    """
    from common.log import logger
    try:
        from integrations.external.migration import (
            STORE_NEW,
            active_store_version,
            assert_store_version_safe,
        )
        from auth.service import get_identity_service

        if active_store_version() != STORE_NEW:
            return

        report = assert_store_version_safe(get_identity_service())
        logger.info(
            "[App] External-connection store_version=new accepted: %d legacy"
            " record(s) imported, none pending"
            % report.get("imported", 0)
        )
    except Exception as error:
        if _external_store_version_is_new():
            logger.error(
                "[App] Refusing to start: external-connection store_version=new "
                "is unsafe (%s). Import the legacy records first "
                "(python -m cli.cli external-connections import) or set "
                "store_version back to 'legacy'." % error
            )
            raise
        logger.warning(
            "[App] External-connection store-version check skipped: %s" % error)


def _external_store_version_is_new() -> bool:
    """Whether the deployment is configured to read the new store.

    Re-read here rather than passed down so the ``except`` path of the guard can
    distinguish "the deployment asked for ``new`` and it is unsafe" (refuse the
    boot) from "the scan broke while the deployment reads ``legacy``" (warn).
    Returns False on any read failure: not knowing the mode means not being
    entitled to refuse the boot.
    """
    try:
        from integrations.external.migration import STORE_NEW, active_store_version
        return active_store_version() == STORE_NEW
    except Exception:  # noqa: BLE001 - cannot read the mode => do not refuse
        return False


def _backup_task_stores(state_dir, agent_ids, plan, *, source: str) -> List[str]:
    """Copy every task store the migration is about to rewrite (task 4.3).

    The migration mutates one ``tasks.json`` per Agent. Copying the file before
    the first write is the whole point of the drill's backup evidence: the copy
    is taken while nothing is firing (the hook runs in the boot's
    single-threaded phase, before any timer starts), so it is a consistent
    snapshot of the pre-migration state.

    Best effort on purpose: a backup that cannot be written must not stop the
    migration (the quarantine is the safety property; the copy is the recovery
    convenience), but it is logged loudly.
    """
    from datetime import datetime

    from common import log as _log
    from common.runtime_identity import RuntimeIdentity

    touched = {entry["agent_id"] for entry in plan.get("agents", [])
               if entry.get("stamped") or entry.get("quarantined")}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    written: List[str] = []
    for agent_id in sorted(touched or set(agent_ids)):
        if touched and agent_id not in touched:
            continue
        path = str(state_dir.scheduler_file(RuntimeIdentity(agent_id=agent_id)))
        if not os.path.isfile(path):
            continue
        target = "%s.bak-%s-%s" % (path, source, stamp)
        try:
            with open(path, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
            os.chmod(target, 0o600)
            written.append(target)
            _prune_task_store_backups(path, source)
        except OSError as error:
            _log.logger.warning(
                "[App] Scheduled-task backup failed for %s: %s" % (path, error))
    return written


def _prune_task_store_backups(path: str, source: str) -> None:
    """Keep the newest ``TASK_STORE_BACKUP_KEEP`` backups of one store."""
    prefix = os.path.basename(path) + ".bak-" + source + "-"
    directory = os.path.dirname(path)
    try:
        found = sorted(name for name in os.listdir(directory)
                       if name.startswith(prefix))
    except OSError:
        return
    for stale in found[:-TASK_STORE_BACKUP_KEEP]:
        try:
            os.remove(os.path.join(directory, stale))
        except OSError:
            pass


def _execution_sandbox() -> None:
    from agent.execution.sandbox import install
    from common.readiness import probe_execution
    install()
    import atexit
    from agent.tools.bash import background
    atexit.register(background.reset)
    probe_execution()


def register_fork_startup_hooks() -> None:
    """Idempotently arm this fork's hooks."""
    register_startup_hook(HOOK_EXECUTION_SANDBOX, _execution_sandbox, order=18)
    register_startup_hook(HOOK_IDENTITY_MODE_CONSISTENCY,
                          _identity_mode_consistency, order=10)
    register_startup_hook(HOOK_DATABASE_BOOTSTRAP,
                          _database_bootstrap_auto_init, order=15)
    register_startup_hook(HOOK_TENANT_CONVERSATION_BACKFILL,
                          _tenant_conversation_backfill, order=20)
    register_startup_hook(HOOK_SCHEDULER_TASK_MIGRATION,
                          _scheduler_task_migration, order=25)
    register_startup_hook(HOOK_EXTERNAL_STORE_VERSION,
                          _external_store_version_guard, order=30)


register_fork_startup_hooks()
