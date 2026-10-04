import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import io
import stat


def _positive_env(name, default):
    try:
        value = int(os.environ.get(name, default))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


class _RuntimeLogHandler(RotatingFileHandler):
    """Bound logs; preserve existing access modes, defaulting new logs to 0600."""

    _error_reported = False

    def _open(self):
        if not hasattr(self, "_log_file_mode"):
            try:
                self._log_file_mode = stat.S_IMODE(os.stat(self.baseFilename).st_mode)
            except FileNotFoundError:
                self._log_file_mode = 0o600
        return open(self.baseFilename, self.mode, encoding=self.encoding,
                    errors=getattr(self, "errors", None),
                    opener=lambda path, flags: os.open(path, flags, self._log_file_mode))

    def handleError(self, record):
        # Never log via this handler again, or print the failed record/secrets.
        if not self._error_reported:
            self._error_reported = True
            try:
                sys.stderr.write("[log] file logging failed; check disk space and log directory permissions\n")
            except (OSError, ValueError):
                pass


def log_path(filename: str) -> str:
    """Path of a log file beside this process's main log.

    Mirrors config.get_data_root() without importing config (avoids a circular
    import, since config imports this module). The desktop build sets
    COW_DATA_DIR (e.g. ~/.cow); source deployments fall back to CWD.

    Every observability file (run.log, the process-lifecycle record, ...) must
    resolve through here: a second copy of this rule would let them drift, and
    an operator looking in one place would silently miss the other.
    """
    data_dir = os.environ.get("COW_DATA_DIR")
    if data_dir:
        data_dir = os.path.expanduser(data_dir)
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, filename)
    return filename


def _log_path():
    return log_path("run.log")


def _reset_logger(log):
    for handler in list(log.handlers):
        handler.close()
        log.removeHandler(handler)
        del handler
    log.handlers.clear()
    log.propagate = False
    stdout = sys.stdout
    if hasattr(stdout, "buffer"):
        stdout = io.TextIOWrapper(stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    console_handle = logging.StreamHandler(stdout)
    console_handle.setFormatter(
        logging.Formatter(
            "[%(levelname)s][%(asctime)s][%(filename)s:%(lineno)d] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    log.addHandler(console_handle)
    # File logging is best-effort: if the log path isn't writable (e.g. a
    # packaged app installed under Program Files run by a non-admin user, with
    # an unwritable CWD), fall back to console-only instead of crashing the
    # whole process at import time.
    try:
        file_handle = _RuntimeLogHandler(
            _log_path(), encoding="utf-8",
            maxBytes=_positive_env("COW_LOG_MAX_BYTES", 20 * 1024 * 1024),
            backupCount=_positive_env("COW_LOG_BACKUP_COUNT", 5),
        )
        file_handle.setFormatter(
            logging.Formatter(
                "[%(levelname)s][%(asctime)s][%(filename)s:%(lineno)d] - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        log.addHandler(file_handle)
    except OSError:
        console_handle.handle(
            logging.LogRecord(
                "log", logging.WARNING, __file__, 0,
                "[log] file logging unavailable; check data directory permissions and disk space",
                (), None,
            )
        )


def _get_logger():
    log = logging.getLogger("log")
    _reset_logger(log)
    log.setLevel(logging.INFO)
    return log


# 日志句柄
logger = _get_logger()
