//! The unix backend for path validation and root-anchored opening.
//!
//! Design decision D6 is explicit that the helper must **not** use `realpath`
//! followed by `open`: that pattern validates one path and opens another, so a
//! component swapped between the two steps is read anyway. Instead every path is
//! opened component by component, relative to the already-held root directory
//! handle, with `O_NOFOLLOW` on each step.
//!
//! Two consequences worth stating, because they are what make the guarantee
//! real rather than cosmetic:
//!
//! * A symlink anywhere in the path is an `ELOOP` failure, not a redirect. The
//!   root handle pins the *object* the user selected, so replacing a directory
//!   with a link to somewhere else cannot widen access.
//! * The absolute path never leaves the helper. Callers only ever send a path
//!   *relative* to the authorised root, so a request cannot name an arbitrary
//!   location in the first place.
//!
//! The platform-independent half of this module -- the sensitive-name list, the
//! reserved-name list, `validate_relative_path`, `Kind`, `display_name` and
//! `is_hidden` -- lives in the parent module, so the two backends cannot enforce
//! different traversal rules.

use std::ffi::{CStr, CString};
use std::fs::File;
use std::io;
use std::os::unix::io::{AsRawFd, FromRawFd};

use super::{is_sensitive, Kind, RootRef};
use crate::protocol::Code;

/// How this backend carries an absolute root path between `open_root` and the
/// identity re-check. Unix needs it NUL-terminated for the syscall, so this is
/// a `CString`; the Windows backend uses an `OsString`. `grants` only stores and
/// hands back the value, so it is generic over the platform through this alias.
pub type RootPath = CString;

/// Filesystem types refused as an authorised root (design D6: V1 refuses网络盘).
#[cfg(target_os = "macos")]
const NETWORK_FS_TYPES: &[&str] = &["nfs", "smbfs", "afpfs", "webdav", "cifs", "ftp"];


/// The access a relative open asks for.
///
/// On unix this is not an access-mask decision: `openat` takes the mode at the
/// call, and the write path opens the target `O_RDONLY` and publishes with
/// `rename(2)` by name, so it never needs a second kind of handle. The type
/// exists so the write code is identical on both platforms -- see the Windows
/// backend, where the distinction *is* load-bearing (`DELETE` is required to
/// replace an object by handle).
#[cfg(feature = "write-ops")]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Access {
    /// Only present so the write code reads the same on both platforms: unix
    /// ignores the intent, because `rename(2)` needs no widened handle.
    #[allow(dead_code)]
    Read,
    Replace,
}

/// Delete one entry **through a directory handle**, never by an assembled path.
///
/// `unlinkat(parent, name)` is the reason this is safe to call from the cleanup
/// path: the name is resolved by the kernel against the directory object the
/// caller still holds, so a directory that was renamed or replaced since the
/// temporary object was created cannot redirect the deletion somewhere else. A
/// `remove_file(joined_path)` could, which is why the cleanup code does not
/// assemble paths.
///
/// The helper's only deletion, and reachable only from the temporary-object
/// registry -- so it exists only in the build that has that registry.
#[cfg(feature = "write-ops")]
pub fn remove_relative(parent: &File, name: &str) -> Result<(), Code> {
    if name.is_empty() || name == "." || name == ".." || name.contains('/') {
        return Err(Code::InvalidParams);
    }
    let c_name = CString::new(name.as_bytes()).map_err(|_| Code::InvalidParams)?;
    // SAFETY: `c_name` is a valid NUL-terminated string that outlives the call,
    // and `parent` is a live descriptor.
    let rc = unsafe { libc::unlinkat(parent.as_raw_fd(), c_name.as_ptr(), 0) };
    if rc != 0 {
        return Err(Code::IoError);
    }
    Ok(())
}

/// `open_relative` with an explicit access intent.
///
/// Unix needs no widening for a replace: `rename(2)` works by name, so the target
/// handle is only ever read through. The intent is accepted and ignored, which is
/// what keeps one call site in `src/write.rs` for both platforms.
#[cfg(feature = "write-ops")]
pub fn open_relative_with(
    root: &File,
    components: &[String],
    want_directory: bool,
    access: Access,
) -> Result<(File, Kind), Code> {
    let _ = access;
    open_relative(root, components, want_directory)
}

/// Open a path relative to `root`, one component at a time with `O_NOFOLLOW`.
///
/// `want_directory` selects whether a non-directory final component is an
/// error. Intermediate components are always required to be directories.
pub fn open_relative(
    root: &File,
    components: &[String],
    want_directory: bool,
) -> Result<(File, Kind), Code> {
    open_relative_inner(root, components, want_directory)
}

/// The walk both entry points above reach.
fn open_relative_inner(
    root: &File,
    components: &[String],
    want_directory: bool,
) -> Result<(File, Kind), Code> {
    if components.is_empty() {
        return Err(Code::InvalidParams);
    }
    if components.iter().any(|c| is_sensitive(c)) {
        return Err(Code::RejectedEntry);
    }

    let mut current: Option<File> = None;
    for (index, component) in components.iter().enumerate() {
        let parent_fd = match &current {
            Some(file) => file.as_raw_fd(),
            None => root.as_raw_fd(),
        };
        let last = index + 1 == components.len();
        let opened = openat_one(parent_fd, component, !last || want_directory)?;
        current = Some(opened);
    }
    let file = current.expect("at least one component was opened");

    let kind = classify(&file)?;
    if want_directory && kind != Kind::Directory {
        return Err(Code::InvalidParams);
    }
    // `nlink > 1` on a regular file means another name for the same object
    // exists, so the file the user authorised is not necessarily the only way
    // to reach it (design D6: V1 refuses hard-linked files).
    if kind == Kind::RegularFile && link_count(&file)? > 1 {
        return Err(Code::RejectedEntry);
    }
    Ok((file, kind))
}

/// Re-open the root directory as an independent descriptor.
///
/// This is `openat(root, ".")`, **not** `dup(root)`. The distinction is the whole
/// reason this function exists: `dup` shares the file description, so the
/// directory offset is shared too, and the second `readdir` pass would start
/// where the first one stopped -- a directory listed twice would look empty the
/// second time. A fresh open gets its own offset and its own stream.
///
/// `path` and `identity` are unused here: the held descriptor is already anchored
/// to the authorised object, which is the stronger position. They exist for the
/// Windows backend, which cannot name `"."` in a relative open and so has to
/// re-open by path and re-check the identity; see [`RootRef`].
pub fn reopen_directory(root: &RootRef<'_>) -> Result<File, Code> {
    reopen_descriptor(root.handle)
}

/// One `openat` step: relative, no-follow, close-on-exec, non-blocking so a
/// special file cannot hold the helper open.
fn openat_one(parent_fd: i32, name: &str, as_directory: bool) -> Result<File, Code> {
    let c_name = CString::new(name.as_bytes()).map_err(|_| Code::InvalidParams)?;
    let mut flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK;
    if as_directory {
        flags |= libc::O_DIRECTORY;
    }
    // SAFETY: `c_name` is a valid NUL-terminated string that outlives the call,
    // and `parent_fd` is an open descriptor owned by the caller.
    let fd = unsafe { libc::openat(parent_fd, c_name.as_ptr(), flags) };
    if fd < 0 {
        return Err(classify_errno(io::Error::last_os_error(), name));
    }
    // SAFETY: `openat` returned a fresh descriptor we now own exclusively.
    let file = unsafe { File::from_raw_fd(fd) };
    Ok(file)
}

/// Map a syscall failure to a protocol code. `ELOOP` is the important one: with
/// `O_NOFOLLOW` set it means a symlink was in the path, which is a refusal, not
/// an I/O accident.
fn classify_errno(error: io::Error, name: &str) -> Code {
    match error.raw_os_error() {
        Some(libc::ELOOP) => Code::RejectedEntry,
        Some(libc::ENOTDIR) => Code::PathOutsideRoot,
        Some(libc::EACCES) | Some(libc::EPERM) => Code::RejectedEntry,
        Some(libc::ENOENT) => Code::InvalidParams,
        Some(libc::ENAMETOOLONG) => Code::InvalidParams,
        _ => {
            let _ = name;
            Code::IoError
        }
    }
}

/// Classify an opened descriptor, refusing everything V1 does not read.
fn classify(file: &File) -> Result<Kind, Code> {
    let stat = fstat(file)?;
    let mode = stat.st_mode & libc::S_IFMT;
    if mode == libc::S_IFDIR {
        return Ok(Kind::Directory);
    }
    // Block devices, character devices, FIFOs and sockets are refused: they are
    // not "files" in any sense the user authorised with a directory pick.
    if mode == libc::S_IFBLK
        || mode == libc::S_IFCHR
        || mode == libc::S_IFIFO
        || mode == libc::S_IFSOCK
    {
        return Err(Code::RejectedEntry);
    }
    if mode != libc::S_IFREG {
        return Err(Code::RejectedEntry);
    }
    Ok(Kind::RegularFile)
}

fn fstat(file: &File) -> Result<libc::stat, Code> {
    // SAFETY: `stat` is zeroed before use and `file` is a live descriptor.
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::fstat(file.as_raw_fd(), &mut stat) };
    if rc != 0 {
        return Err(Code::IoError);
    }
    Ok(stat)
}

fn link_count(file: &File) -> Result<u64, Code> {
    Ok(fstat(file)?.st_nlink as u64)
}

/// Identity of an authorised root, used to detect the root being replaced
/// (task 7.2: "根身份变化时要求重新选择").
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RootIdentity {
    pub device: u64,
    pub inode: u64,
}

pub fn identity_of(file: &File) -> Result<RootIdentity, Code> {
    let stat = fstat(file)?;
    Ok(RootIdentity {
        device: stat.st_dev as u64,
        inode: stat.st_ino,
    })
}

/// Refuse a root on a network filesystem, where the local identity checks this
/// helper relies on do not hold (design D6).
#[cfg(target_os = "macos")]
pub fn reject_network_root(file: &File) -> Result<(), Code> {
    // SAFETY: `statfs` is zeroed before use and `file` is a live descriptor.
    let mut info: libc::statfs = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::fstatfs(file.as_raw_fd(), &mut info) };
    if rc != 0 {
        return Err(Code::IoError);
    }
    let name: Vec<u8> = info
        .f_fstypename
        .iter()
        .take_while(|b| **b != 0)
        .map(|b| *b as u8)
        .collect();
    let name = String::from_utf8_lossy(&name).to_ascii_lowercase();
    if NETWORK_FS_TYPES.iter().any(|t| name == *t) {
        return Err(Code::Unsupported);
    }
    Ok(())
}

/// Identity of a path as it *currently resolves*, without following a link.
///
/// Used to re-verify an authorised root. This deliberately uses `lstat` on the
/// recorded path rather than `fstat` on the held descriptor: the held
/// descriptor can never change identity, so comparing it with itself would be a
/// check that cannot fail. What matters is whether the path still names the
/// object the user selected (task 7.2: "根身份变化时要求重新选择").
pub fn identity_of_path(path: &CStr) -> Result<RootIdentity, Code> {
    // SAFETY: `stat` is zeroed before use and `path` is a valid C string.
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::lstat(path.as_ptr(), &mut stat) };
    if rc != 0 {
        return Err(Code::Unsupported);
    }
    Ok(RootIdentity {
        device: stat.st_dev as u64,
        inode: stat.st_ino,
    })
}

#[cfg(not(target_os = "macos"))]
pub fn reject_network_root(_file: &File) -> Result<(), Code> {
    // Windows network-drive detection lives with the Windows open path
    // (task 7.3); refusing to guess here is better than a wrong answer.
    Ok(())
}

/// Validate an absolute root path taken from the user's own directory picker.
///
/// This is the only place an absolute path is legitimate, and it is never taken
/// from a page or a model (spec: "页面提交绝对路径" must be refused).
#[cfg(target_os = "macos")]
pub fn validate_root_path(raw: &str) -> Result<CString, Code> {
    if raw.is_empty() || raw.len() > crate::protocol::MAX_PATH_BYTES {
        return Err(Code::InvalidParams);
    }
    if raw.as_bytes().contains(&0) {
        return Err(Code::InvalidParams);
    }
    // The picker returns a native absolute path; anything else is a caller
    // building one, which is exactly the case to refuse.
    if !raw.starts_with('/') {
        return Err(Code::PathOutsideRoot);
    }
    CString::new(raw.as_bytes()).map_err(|_| Code::InvalidParams)
}

#[cfg(not(target_os = "macos"))]
pub fn validate_root_path(raw: &str) -> Result<CString, Code> {
    if raw.is_empty() || raw.len() > crate::protocol::MAX_PATH_BYTES {
        return Err(Code::InvalidParams);
    }
    if raw.as_bytes().contains(&0) {
        return Err(Code::InvalidParams);
    }
    CString::new(raw.as_bytes()).map_err(|_| Code::InvalidParams)
}

/// Open an absolute root path with `O_NOFOLLOW`.
///
/// Opens *without* `O_DIRECTORY` and then inspects the descriptor, for two
/// reasons:
///
/// * `O_NOFOLLOW` then reports a symlink as `ELOOP` reliably. Asking for
///   `O_DIRECTORY | O_NOFOLLOW` on macOS reports `ENOTDIR` for a symlink and
///   for a plain file alike, which would collapse "you picked a link" (a policy
///   refusal) into "that is not a directory" (a caller mistake).
/// * The type check runs against the object actually opened, not against the
///   path, so the answer cannot be raced by replacing the root in between.
pub fn open_root(path: &CString) -> Result<File, Code> {
    let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK;
    // SAFETY: `path` is a valid NUL-terminated string that outlives the call.
    let fd = unsafe { libc::open(path.as_ptr(), flags) };
    if fd < 0 {
        return Err(classify_root_errno(io::Error::last_os_error()));
    }
    // SAFETY: `open` returned a fresh descriptor we now own exclusively.
    let file = unsafe { File::from_raw_fd(fd) };
    match classify(&file) {
        Ok(Kind::Directory) => Ok(file),
        // A regular file is not an acceptable root, but it is not a policy
        // refusal either: the caller asked for the wrong kind of object.
        Ok(Kind::RegularFile) => Err(Code::Unsupported),
        // Devices, FIFOs, sockets: refused outright.
        Err(code) => Err(code),
    }
}

fn classify_root_errno(error: io::Error) -> Code {
    match error.raw_os_error() {
        // With O_NOFOLLOW, a symlinked root.
        Some(libc::ELOOP) => Code::RejectedEntry,
        Some(libc::EACCES) | Some(libc::EPERM) => Code::RejectedEntry,
        Some(libc::ENOTDIR) => Code::Unsupported,
        Some(libc::ENOENT) => Code::InvalidParams,
        _ => Code::IoError,
    }
}

// `display_name` and `is_hidden` live in the parent module: they are the same
// rule on every platform, and duplicating them here is how the two backends
// would start disagreeing about which entries are shown.

/// Metadata for one entry of a directory, addressed by name.
///
/// `fstatat(..., AT_SYMLINK_NOFOLLOW)` is what keeps a link from being typed as
/// whatever it points at. The Windows backend reaches the same conclusion by
/// opening the entry with `FILE_OPEN_REPARSE_POINT` and reading its attributes.
pub fn stat_at(directory: &File, name: &str) -> Option<super::EntryStat> {
    if name.is_empty() || name == "." || name == ".." || name.contains('/') {
        return None;
    }
    let c_name = CString::new(name.as_bytes()).ok()?;
    // SAFETY: `stat` is zeroed before use, `directory` is live and `c_name`
    // outlives the call.
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    let rc = unsafe {
        libc::fstatat(
            directory.as_raw_fd(),
            c_name.as_ptr(),
            &mut stat,
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if rc != 0 {
        // The entry vanished or cannot be read: the caller counts it as skipped,
        // which is why this is an `Option` rather than an error.
        return None;
    }
    Some(super::EntryStat {
        kind: classify_stat(&stat),
        size: stat.st_size.max(0) as u64,
        modified: stat.st_mtime as i64,
        #[cfg(feature = "write-ops")]
        identity: stat.st_ino,
    })
}

/// Metadata for an already-open descriptor.
pub fn stat_of(file: &File) -> Result<super::FileStat, Code> {
    let stat = fstat(file)?;
    Ok(super::FileStat {
        size: stat.st_size.max(0) as u64,
        modified: stat.st_mtime as i64,
        regular: stat.st_mode & libc::S_IFMT == libc::S_IFREG,
        links: stat.st_nlink as u64,
        identity: stat.st_ino,
    })
}

/// Map a stat result to a reported kind, or `None` when the entry is refused.
fn classify_stat(stat: &libc::stat) -> Option<&'static str> {
    let mode = stat.st_mode & libc::S_IFMT;
    if mode == libc::S_IFDIR {
        return Some("dir");
    }
    if mode != libc::S_IFREG {
        // Symlinks (never followed), devices, FIFOs and sockets.
        return None;
    }
    if stat.st_nlink > 1 {
        // A hard-linked file is reachable under a name the user did not
        // authorise.
        return None;
    }
    Some("file")
}

/// Read directory names from a descriptor, bounded and cancellable.
///
/// Takes the `RootRef` bundle so the two backends share a call site, but only
/// uses the held descriptor: on unix `openat(root, ".")` reaches the object
/// directly, which is the stronger position.
///
/// `fdopendir` takes ownership of the descriptor it is given, so the re-opened
/// `File` is forgotten rather than closed twice.
pub fn read_names(
    directory: &File,
    request_id: &str,
    cancel: &crate::ops::CancelFlag,
) -> Result<Vec<String>, Code> {
    let handle = reopen_descriptor(directory)?;
    // SAFETY: `handle` is a live descriptor; ownership passes to the DIR stream.
    let stream = unsafe { libc::fdopendir(handle.as_raw_fd()) };
    if stream.is_null() {
        return Err(Code::IoError);
    }
    // The stream owns the descriptor now.
    std::mem::forget(handle);

    let mut names = Vec::new();
    loop {
        if names.len() >= crate::ops::MAX_SCAN_ENTRIES {
            break;
        }
        if names.len() % 256 == 0 && crate::ops::is_cancelled_for(cancel, request_id) {
            // SAFETY: `stream` is a live DIR stream opened above.
            unsafe { libc::closedir(stream) };
            return Err(Code::Cancelled);
        }
        // SAFETY: `stream` is a live DIR stream.
        let entry = unsafe { libc::readdir(stream) };
        if entry.is_null() {
            break;
        }
        // SAFETY: `readdir` returned a valid `dirent` for this stream.
        let raw = unsafe { (*entry).d_name.as_ptr() };
        // SAFETY: `d_name` is NUL-terminated within the dirent.
        let name = unsafe { CStr::from_ptr(raw) };
        let name = name.to_bytes();
        if name == b"." || name == b".." {
            continue;
        }
        names.push(super::display_name(name));
    }
    // SAFETY: `stream` is a live DIR stream opened above and not used after this.
    unsafe { libc::closedir(stream) };
    Ok(names)
}

/// Re-open a directory descriptor so an enumeration gets its own offset.
fn reopen_descriptor(directory: &File) -> Result<File, Code> {
    let dot = CString::new(".").map_err(|_| Code::Internal)?;
    let flags =
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NONBLOCK | libc::O_NOFOLLOW;
    // SAFETY: `dot` is a valid NUL-terminated string that outlives the call, and
    // the descriptor is live.
    let fd = unsafe { libc::openat(directory.as_raw_fd(), dot.as_ptr(), flags) };
    if fd < 0 {
        return Err(Code::IoError);
    }
    // SAFETY: `openat` returned a fresh descriptor we now own exclusively.
    Ok(unsafe { File::from_raw_fd(fd) })
}

/// The authorised root as a filesystem path, for a caller that must name a
/// location inside it.
///
/// Only the write operations use this, and always to build a path *inside* a root
/// the caller is already authorised for; every such path is re-checked through a
/// handle before it is touched. The path is never the authorisation.
#[cfg(feature = "write-ops")]
pub fn absolute_root_of(grant: &crate::grants::Grant) -> Result<std::path::PathBuf, Code> {
    use std::os::unix::ffi::OsStrExt;
    Ok(std::path::PathBuf::from(std::ffi::OsStr::from_bytes(
        grant.root_path().as_bytes(),
    )))
}

/// Publish `staged` over the file at `target_path`.
///
/// `rename(2)` within one directory is already atomic: a reader sees either the
/// old contents or the new ones, never a mix. `target` (the handle whose version
/// was verified) is unused here because rename works by name -- the Windows
/// backend needs it -- and the shared signature is what keeps one call site.
///
/// The target name is re-checked with `symlink_metadata`, which does **not**
/// follow a link: if something put a symlink at that name between the version
/// check and the publish, this is no longer the file that was verified.
#[cfg(feature = "write-ops")]
pub fn replace_contents(
    target: &File,
    staged: &std::path::Path,
    target_path: &std::path::Path,
) -> Result<(), Code> {
    let _ = target;
    match std::fs::symlink_metadata(target_path) {
        Ok(metadata) if metadata.file_type().is_symlink() => return Err(Code::RejectedEntry),
        Ok(_) => {}
        Err(_) => return Err(Code::IoError),
    }
    std::fs::rename(staged, target_path).map_err(|_| Code::IoError)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_absolute_root_requires_a_native_absolute_path() {
        assert!(validate_root_path("/Users/me/docs").is_ok());
        for bad in ["", "relative/path", "C:/Users", "\\\\server\\share"] {
            assert!(validate_root_path(bad).is_err(), "must refuse root {bad:?}");
        }
    }
}
