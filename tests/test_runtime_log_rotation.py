"""Runtime log storage remains bounded without changing identity audit data."""

import logging
import os

from common.log import _RuntimeLogHandler, _positive_env
from common.utils import tail_lines


def test_rotation_preserves_recent_output_and_private_permissions(tmp_path):
    path = tmp_path / "run.log"
    audit = tmp_path / "identity.db"
    audit.write_bytes(b"synthetic audit data")
    handler = _RuntimeLogHandler(str(path), maxBytes=128, backupCount=2, encoding="utf-8")
    try:
        for i in range(40):
            handler.handle(logging.LogRecord("test", logging.INFO, __file__, 1,
                                            "entry %02d %s", (i, "x" * 30), None))
        assert "entry 39" in "".join(tail_lines(str(path), 10))
        files = list(tmp_path.glob("run.log*"))
        assert len(files) == 3
        assert all(p.stat().st_size <= 128 for p in files)
        if os.name != "nt":
            assert all(p.stat().st_mode & 0o777 == 0o600 for p in files)
        assert audit.read_bytes() == b"synthetic audit data"
    finally:
        handler.close()


def test_rollover_failure_is_reported_once_without_record_contents(tmp_path, monkeypatch, capsys):
    handler = _RuntimeLogHandler(str(tmp_path / "run.log"), maxBytes=1, backupCount=2)
    def fail():
        raise OSError("simulated disk full")
    monkeypatch.setattr(handler, "doRollover", fail)
    try:
        record = logging.LogRecord("test", logging.INFO, __file__, 1,
                                   "SYNTHETIC_PRIVATE_VALUE", (), None)
        handler.handle(record)
        handler.handle(record)
        error = capsys.readouterr().err
        assert error.count("file logging failed") == 1
        assert "SYNTHETIC_PRIVATE_VALUE" not in error
    finally:
        handler.close()


def test_rotation_preserves_existing_group_read_access(tmp_path):
    if os.name == "nt":
        return
    path = tmp_path / "run.log"
    path.write_text("previous output\n")
    path.chmod(0o640)
    handler = _RuntimeLogHandler(str(path), maxBytes=16, backupCount=2)
    try:
        handler.handle(logging.LogRecord("test", logging.INFO, __file__, 1, "new output", (), None))
        assert path.stat().st_mode & 0o777 == 0o640
        assert (tmp_path / "run.log.1").stat().st_mode & 0o777 == 0o640
    finally:
        handler.close()


def test_invalid_rotation_settings_keep_bounded_defaults(monkeypatch):
    for value in ("0", "-1", "invalid"):
        monkeypatch.setenv("COW_LOG_MAX_BYTES", value)
        assert _positive_env("COW_LOG_MAX_BYTES", 20 * 1024 * 1024) == 20 * 1024 * 1024
    monkeypatch.setenv("COW_LOG_MAX_BYTES", "1024")
    assert _positive_env("COW_LOG_MAX_BYTES", 1) == 1024
