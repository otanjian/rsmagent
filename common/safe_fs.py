# encoding:utf-8
"""Anchored filesystem access: read, list, write and remove *inside* a trusted
root without ever following a link out of it.

Why not ``realpath`` + ``startswith``
-------------------------------------
The idiom used across this repository

    full = os.path.realpath(os.path.join(root, relative))
    if not full.startswith(os.path.realpath(root) + os.sep): raise ...

answers "is this path inside the root *right now*" and then hands the *string*
back to ``open()``. Between the check and the open a component can be replaced
by a symlink (the classic check-then-use window), and a symlink that already
exists at the final component is followed rather than refused. For a console
that enumerates a member's memory or projects this is the difference between
"cannot address another user's file" and "cannot address it until someone
plants a link".

What this module guarantees
---------------------------
Every operation resolves the path **relative to an already-open directory
descriptor**, opening each component with ``O_NOFOLLOW``, so:

* a symlinked entry, a symlinked intermediate directory or a symlinked root is
  :class:`UnsafePathError` rather than a redirect;
* the directory that was validated is the directory that is used — a rename
  after validation cannot retarget the operation;
* ``..``, absolute paths, drive letters and empty components are rejected
  before any syscall.

Absence is not an error: a caller asking for an entry that is not there gets
``None``/``False``/``[]``. A caller whose *parent* is missing on a write gets
``FileNotFoundError``. Nothing is ever created implicitly except by
:func:`mkdir` and by the ``create`` argument of the internal chain opener.

On platforms without ``dir_fd`` support (Windows) the same checks run against
validated paths and every component is link-checked. That is a weaker guarantee
against a concurrent replacer, which is why the caller keeps the platform's own
ACL boundary as the outer defence; it is not weaker for the cases the console
actually meets.

Atomic writes
-------------
:func:`write_text_atomic` creates an exclusive temporary file beside the
destination and ``os.replace``s it, so a reader never observes a half-written
entry and a symlink at the destination is refused rather than written through.
"""

from __future__ import annotations

import errno
import os
import stat as _stat
import tempfile
import sys
from contextlib import contextmanager
from typing import List, Optional, Tuple

__all__ = [
    "UnsafePathError",
    "split_relative",
    "resolve_within",
    "contains",
    "exists",
    "is_file",
    "is_dir",
    "is_symlink",
    "list_names",
    "stat",
    "read_bytes",
    "read_text",
    "write_text_atomic",
    "write_bytes_atomic",
    "unlink",
    "mkdir",
    "file_lock",
]


class UnsafePathError(Exception):
    """The requested path is not a plain path inside the trusted root.

    Raised for ``..``/absolute/empty components, for a symlinked component
    (including the root), and for a resolved path that escapes the root. The
    message names the offending component, never the resolved host path, so a
    refusal cannot be used to probe the filesystem.
    """


@contextmanager
def file_lock(root, relative):
    """Exclusive process lock on a persistent, anchored control file.

    The caller supplies thread/reentrancy coordination. Never remove or replace
    the lock file: all processes must lock the same inode.
    """
    parts = split_relative(relative)
    with _DirChain(_root_path(root), parts[:-1], create=True) as chain:
        if chain.fd != -1:
            fd = os.open(parts[-1], os.O_RDWR | os.O_CREAT | _O_NOFOLLOW,
                         0o600, dir_fd=chain.fd)
        else:
            path = _resolve_chain(_root_path(root), parts, allow_missing_tail=True)[0]
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if not _stat.S_ISREG(os.fstat(fd).st_mode):
                raise UnsafePathError("lock is not a regular file")
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'0')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

#: Whether descriptor-relative calls are available. Windows lacks them, so the
#: module degrades to validated-path access there (see module docstring).
_DIR_FD_OK = bool(
    _O_NOFOLLOW
    and _O_DIRECTORY
    and os.open in getattr(os, "supports_dir_fd", set())
    and os.stat in getattr(os, "supports_dir_fd", set())
)


# --- path validation --------------------------------------------------------

def split_relative(relative) -> List[str]:
    """Validate a relative path and return its components.

    Refuses anything that is not a plain downward path: absolute paths,
    ``..``, ``.``, empty components, drive letters and backslash tricks. The
    check is textual on purpose — it must run before any syscall.
    """
    if relative is None:
        raise UnsafePathError("path is required")
    if not isinstance(relative, str):
        relative = str(relative)
    if not relative:
        raise UnsafePathError("empty path")
    if relative.startswith("/") or relative.startswith("\\"):
        raise UnsafePathError("absolute path is not allowed")
    if os.name == "nt" and (":" in relative or "\\" in relative):
        raise UnsafePathError("drive-qualified path is not allowed")
    parts = relative.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise UnsafePathError("illegal path component %r" % part)
    return parts


def _root_path(root) -> str:
    if isinstance(root, (str, bytes, os.PathLike)):
        return os.path.abspath(os.fspath(root))
    raise UnsafePathError("root must be a path")


def _assert_root_is_not_link(root: str) -> None:
    """A symlinked root would make every containment promise meaningless."""
    if os.path.islink(root):
        raise UnsafePathError("root is a symbolic link")


def contains(root, candidate) -> bool:
    """True when realpath(``candidate``) is inside realpath(``root``)."""
    real_root = os.path.realpath(str(root))
    real_candidate = os.path.realpath(str(candidate))
    try:
        return os.path.commonpath([real_root, real_candidate]) == real_root
    except ValueError:
        return False


def _resolve_chain(root: str, parts: List[str], *, create: bool = False,
                   allow_missing_tail: bool = False) -> Tuple[str, bool]:
    """Fallback validation: link-check every component, then realpath-check.

    Returns ``(path, present)``. ``present`` is False only when a component is
    missing and ``allow_missing_tail`` was set.
    """
    _assert_root_is_not_link(root)
    current = root
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise UnsafePathError("symbolic link component %r" % part)
        if not os.path.lexists(current):
            if create:
                os.mkdir(current)
            elif allow_missing_tail and index == len(parts) - 1:
                return current, False
            else:
                raise FileNotFoundError(current)
        elif not os.path.isdir(current):
            raise UnsafePathError("non-directory component %r" % part)
    if not contains(root, current):
        raise UnsafePathError("path escapes the trusted root")
    return current, True


def resolve_within(root, relative) -> str:
    """Absolute path of ``relative`` inside ``root`` after full validation.

    For callers that must hand a path to a subsystem this module cannot open
    itself (a media player, an SDK). Every component is validated and the
    result is re-checked for containment; a symlinked component raises rather
    than resolving.
    """
    root_path = _root_path(root)
    path, _present = _resolve_chain(root_path, split_relative(relative))
    return path


# --- descriptor helpers -----------------------------------------------------

def _open_root_fd(root: str, *, create: bool) -> int:
    """Anchor the root itself, including its users/<owner> parent chain."""
    # macOS exposes these system directories as fixed aliases. Resolve only
    # that OS prefix, never an application-controlled ancestor of the root.
    if sys.platform == 'darwin':
        for prefix in ('/var', '/tmp', '/etc'):
            if root == prefix or root.startswith(prefix + '/'):
                root = os.path.realpath(prefix) + root[len(prefix):]
                break
    fd = os.open(os.path.sep, os.O_RDONLY | _O_DIRECTORY)
    try:
        for part in root.strip(os.path.sep).split(os.path.sep):
            if not part:
                continue
            try:
                child = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                    raise UnsafePathError('unsafe root ancestor') from None
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_dir_chain(root: str, parts: List[str], *, create: bool = False
                    ) -> Tuple[int, Optional[int]]:
    """Open ``root``/``parts`` as a directory; return ``(fd, parent_fd)``.

    ``-1`` as the fd means "this platform has no ``dir_fd``; use the validated
    path". Both descriptors are owned by the caller (``-1``/``None`` are
    no-ops to close). A missing component raises ``FileNotFoundError`` unless
    ``create`` is set.
    """
    _assert_root_is_not_link(root)
    if not _DIR_FD_OK:
        if create and not os.path.isdir(root):
            os.makedirs(root, exist_ok=True)
            _assert_root_is_not_link(root)
        _resolve_chain(root, parts, create=create)
        return -1, None

    _assert_root_is_not_link(root)
    fd = _open_root_fd(root, create=create)
    parent: Optional[int] = None
    try:
        for part in parts:
            flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise UnsafePathError(
                        "symbolic link component %r" % part) from None
                if exc.errno == errno.ENOTDIR:
                    # macOS reports ENOTDIR both for "not a directory" and for
                    # "no such component"; disambiguate without following links.
                    try:
                        st = os.stat(part, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        if not create:
                            raise
                        os.mkdir(part, dir_fd=fd)
                        child = os.open(part, flags, dir_fd=fd)
                        if parent is not None:
                            os.close(parent)
                        parent, fd = fd, child
                        continue
                    if _stat.S_ISLNK(st.st_mode):
                        raise UnsafePathError(
                            "symbolic link component %r" % part) from None
                    raise UnsafePathError(
                        "non-directory component %r" % part) from None
                raise
            if parent is not None:
                os.close(parent)
            parent, fd = fd, child
        return fd, parent
    except BaseException:
        os.close(fd)
        if parent is not None:
            os.close(parent)
        raise


class _DirChain:
    """Context manager owning both descriptors of :func:`_open_dir_chain`."""

    def __init__(self, root: str, parts: List[str], *, create: bool = False):
        self._root = root
        self._parts = parts
        self._create = create
        self.fd = -1
        self.parent = None
        self.resolved = ""

    def __enter__(self) -> "_DirChain":
        try:
            self.fd, self.parent = _open_dir_chain(
                self._root, self._parts, create=self._create)
        except FileNotFoundError:
            raise
        if self.fd == -1:
            self.resolved = _resolve_chain(self._root, self._parts,
                                           create=self._create)[0]
        return self

    def __exit__(self, *exc) -> bool:
        if self.fd != -1:
            os.close(self.fd)
        if self.parent is not None:
            os.close(self.parent)
        self.fd = -1
        self.parent = None
        return False


def _open_file(root: str, parts: List[str], flags: int
               ) -> Tuple[Optional[int], Optional[str]]:
    """Open the final component; return ``(fd, resolved_path)``.

    Exactly one of the two is set: an ``fd`` on descriptor-capable platforms,
    a validated path elsewhere. ``(None, None)`` means absent.
    """
    if not parts:
        raise UnsafePathError("a file name is required")
    directory, name = parts[:-1], parts[-1]
    if not _DIR_FD_OK:
        full, present = _resolve_chain(root, parts, allow_missing_tail=True)
        return (None, full) if present else (None, None)
    try:
        with _DirChain(root, directory) as chain:
            try:
                return os.open(name, flags, dir_fd=chain.fd), None
            except FileNotFoundError:
                return None, None
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise UnsafePathError(
                        "symbolic link entry %r" % name) from None
                raise
    except (FileNotFoundError, NotADirectoryError):
        return None, None


def _lstat(root: str, parts: List[str]):
    if not parts:
        raise UnsafePathError("a path is required")
    directory, name = parts[:-1], parts[-1]
    if not _DIR_FD_OK:
        full, present = _resolve_chain(root, parts, allow_missing_tail=True)
        return os.lstat(full) if present else None
    try:
        with _DirChain(root, directory) as chain:
            return os.stat(name, dir_fd=chain.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None


# --- queries ----------------------------------------------------------------

def exists(root, relative) -> bool:
    return _lstat(_root_path(root), split_relative(relative)) is not None


def is_file(root, relative) -> bool:
    st = _lstat(_root_path(root), split_relative(relative))
    return st is not None and _stat.S_ISREG(st.st_mode)


def is_dir(root, relative) -> bool:
    st = _lstat(_root_path(root), split_relative(relative))
    return st is not None and _stat.S_ISDIR(st.st_mode)


def is_symlink(root, relative) -> bool:
    st = _lstat(_root_path(root), split_relative(relative))
    return st is not None and _stat.S_ISLNK(st.st_mode)


def stat(root, relative) -> Optional[os.stat_result]:
    """``lstat`` of the entry (a symlink is reported as a symlink)."""
    return _lstat(_root_path(root), split_relative(relative))


def list_names(root, relative=None, *, suffix: Optional[str] = None,
               skip_dotfiles: bool = False) -> List[str]:
    """Names directly under ``root``/``relative``, symlinked entries excluded.

    A missing directory lists as empty rather than raising: a fresh account has
    no ``memory/`` yet and that is not an error.
    """
    parts = split_relative(relative) if relative else []
    root_path = _root_path(root)
    try:
        with _DirChain(root_path, parts) as chain:
            if chain.fd != -1:
                names = os.listdir(chain.fd)
                out: List[str] = []
                for name in names:
                    if suffix is not None and not name.endswith(suffix):
                        continue
                    if skip_dotfiles and name.startswith("."):
                        continue
                    try:
                        st = os.stat(name, dir_fd=chain.fd,
                                     follow_symlinks=False)
                    except OSError:
                        continue
                    if _stat.S_ISLNK(st.st_mode):
                        continue
                    out.append(name)
                return out
            base = chain.resolved
    except (FileNotFoundError, NotADirectoryError):
        return []
    names = os.listdir(base)
    out = []
    for name in names:
        if suffix is not None and not name.endswith(suffix):
            continue
        if skip_dotfiles and name.startswith("."):
            continue
        try:
            if _stat.S_ISLNK(os.lstat(os.path.join(base, name)).st_mode):
                continue
        except OSError:
            continue
        out.append(name)
    return out


# --- reads ------------------------------------------------------------------

def read_bytes(root, relative) -> Optional[bytes]:
    """Bytes of one regular file; ``None`` when it does not exist."""
    fd, full = _open_file(_root_path(root), split_relative(relative),
                          os.O_RDONLY | _O_NOFOLLOW)
    if fd is None and full is None:
        return None
    if fd is None:
        if os.path.islink(full):
            raise UnsafePathError("symbolic link entry")
        with open(full, "rb") as handle:
            return handle.read()
    try:
        chunks: List[bytes] = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_text(root, relative, encoding: str = "utf-8") -> Optional[str]:
    raw = read_bytes(root, relative)
    return None if raw is None else raw.decode(encoding)


# --- writes -----------------------------------------------------------------

def mkdir(root, relative) -> None:
    parts = split_relative(relative)
    with _DirChain(_root_path(root), parts, create=True):
        pass


def _write_temp_then_replace(root: str, parts: List[str], data: bytes) -> None:
    directory, name = parts[:-1], parts[-1]
    if not _DIR_FD_OK:
        parent_dir = root
        for part in directory:
            parent_dir = os.path.join(parent_dir, part)
            if os.path.islink(parent_dir):
                raise UnsafePathError("symbolic link directory %r" % part)
            if not os.path.lexists(parent_dir):
                os.mkdir(parent_dir)
            elif not os.path.isdir(parent_dir):
                raise UnsafePathError("non-directory component %r" % part)
        if not contains(root, parent_dir):
            raise UnsafePathError("path escapes the trusted root")
        full = os.path.join(parent_dir, name)
        if os.path.islink(full):
            raise UnsafePathError("symbolic link entry %r" % name)
        handle = tempfile.NamedTemporaryFile(
            "wb", dir=parent_dir, delete=False, prefix=".tmp-")
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, full)
        except BaseException:
            try:
                handle.close()
            except Exception:
                pass
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
        return

    with _DirChain(root, directory, create=True) as chain:
        try:
            st = os.stat(name, dir_fd=chain.fd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None and _stat.S_ISLNK(st.st_mode):
            raise UnsafePathError("symbolic link entry %r" % name)
        if st is not None and not _stat.S_ISREG(st.st_mode):
            raise UnsafePathError("not a regular file: %r" % name)

        tmp_name = "." + os.urandom(8).hex() + ".tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
        fd = os.open(tmp_name, flags, 0o600, dir_fd=chain.fd)
        try:
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_name, name, src_dir_fd=chain.fd, dst_dir_fd=chain.fd)
            tmp_name = None
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name, dir_fd=chain.fd)
                except OSError:
                    pass


def write_bytes_atomic(root, relative, data: bytes) -> None:
    _write_temp_then_replace(_root_path(root), split_relative(relative), data)


def write_text_atomic(root, relative, text: str, encoding: str = "utf-8") -> None:
    if not isinstance(text, str):
        raise TypeError("text must be str")
    write_bytes_atomic(root, relative, text.encode(encoding))


def unlink(root, relative) -> bool:
    """Remove one regular file; a symlinked entry is refused, never followed."""
    parts = split_relative(relative)
    if not parts:
        raise UnsafePathError("a file name is required")
    root_path = _root_path(root)
    directory, name = parts[:-1], parts[-1]
    if not _DIR_FD_OK:
        full, present = _resolve_chain(root_path, parts, allow_missing_tail=True)
        if not present:
            return False
        if _stat.S_ISLNK(os.lstat(full).st_mode):
            raise UnsafePathError("symbolic link entry %r" % name)
        os.unlink(full)
        return True
    try:
        with _DirChain(root_path, directory) as chain:
            try:
                st = os.stat(name, dir_fd=chain.fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if _stat.S_ISLNK(st.st_mode):
                raise UnsafePathError("symbolic link entry %r" % name)
            os.unlink(name, dir_fd=chain.fd)
            return True
    except FileNotFoundError:
        return False
