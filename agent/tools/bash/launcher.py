"""Small process-creation seam; distribution policy is installed at startup."""
import subprocess

_launcher = None


def install(launcher):
    global _launcher
    _launcher = launcher


def installed():
    return _launcher is not None


def popen(command, **kwargs):
    return (_launcher or subprocess.Popen)(command, **kwargs)


def release(process):
    cleanup = getattr(process, 'execution_cleanup', None)
    if cleanup:
        cleanup()
        process.execution_cleanup = None


def current_owner():
    if _launcher is None:
        return None
    from common.runtime_identity import current_identity
    ident = current_identity()
    return (ident.tenant_id, ident.user_id, ident.agent_id, ident.session_id)


def may_access(job_owner):
    if job_owner != current_owner():
        return False
    check = getattr(_launcher, 'access_owner', None)
    if check:
        try:
            return check() == job_owner
        except Exception:
            return False
    return True


def output_directory():
    callback = getattr(_launcher, 'output_directory', None)
    return callback() if callback else None


def read_filter():
    callback = getattr(_launcher, 'read_filter', None)
    return callback() if callback else None
