//! Path validation and root-anchored opening.
//!
//! This is the module the whole helper exists for. Design decision D6 is
//! explicit that the helper must **not** use `realpath` followed by `open`:
//! that pattern validates one path and opens another, so a component swapped
//! between the two steps is read anyway. Instead every path is opened
//! component by component, relative to the already-held root directory handle,
//! with `O_NOFOLLOW` on each step.
//!
//! Two consequences worth stating, because they are what make the guarantee
//! real rather than cosmetic:
//!
//! * A symlink anywhere in the path is an `ELOOP` failure, not a redirect. The
//!   root handle pins the *object* the user selected, so replacing a directory
//!   with a link to somewhere else cannot widen access (task 7.2's
//!   "目录校验后被替换" scenario).
//! * The absolute path never leaves the helper. Callers only ever send a path
//!   *relative* to the authorised root, so a request cannot name an arbitrary
//!   location in the first place (task 7.1's "reject arbitrary path open").

use std::ffi::{CStr, CString};
use std::fs::File;
use std::io;
use std::os::unix::io::{AsRawFd, FromRawFd};

use crate::protocol::Code;

/// Components that are never readable, regardless of what the caller asks for.
///
/// Decision D6: "预设敏感路径排除不能由模型参数关闭". There is deliberately no
/// parameter, grant flag or request field that removes an entry from this list
/// -- the only way to change it is to change this constant, which is a code
/// review, not a runtime decision. Names are compared case-insensitively
/// because the default macOS filesystem is case-insensitive, so `Id_Rsa` must
/// not slip past `id_rsa`.
const SENSITIVE_COMPONENTS: &[&str] = &[
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".netrc",
    ".npmrc",
    ".git-credentials",
    ".gitconfig",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "authorized_keys",
    "known_hosts",
    "keychains",
    "credentials",
];

/// Windows reserved device names. Rejected even on unix so a path that would be
/// dangerous once synced or copied to Windows is never treated as ordinary.
const RESERVED_NAMES: &[&str] = &[
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8",
    "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
];

/// Filesystem types refused as an authorised root (design D6: V1 refuses网络盘).
#[cfg(target_os = "macos")]
const NETWORK_FS_TYPES: &[&str] = &["nfs", "smbfs", "afpfs", "webdav", "cifs", "ftp"];

/// True if this component names something V1 never reads.
pub fn is_sensitive(component: &str) -> bool {
    let lowered = component.to_ascii_lowercase();
    SENSITIVE_COMPONENTS.contains(&lowered.as_str())
}

/// Split and validate a caller-supplied relative path.
///
/// Returns the components on success. Everything a traversal would need is
/// refused here, *before* any syscall: `..`, an absolute path, a Windows
/// separator or drive letter, a UNC prefix, a NUL byte, a control character, an
/// empty component, and a reserved device name.
pub fn validate_relative_path(raw: &str) -> Result<Vec<String>, Code> {
    if raw.is_empty() {
        return Err(Code::InvalidParams);
    }
    if raw.len() > crate::protocol::MAX_PATH_BYTES {
        return Err(Code::InvalidParams);
    }
    if raw.as_bytes().iter().any(|b| *b == 0 || *b < 0x20) {
        return Err(Code::InvalidParams);
    }
    // A backslash is refused rather than normalised: on a platform where it is
    // a separator, treating it as a literal name is how a path escapes a check.
    if raw.contains('\\') {
        return Err(Code::PathOutsideRoot);
    }
    if raw.starts_with('/') {
        return Err(Code::PathOutsideRoot);
    }
    if raw.contains(':') {
        // `C:`, `file:`, and NTFS alternate data streams all live here.
        return Err(Code::PathOutsideRoot);
    }
    let mut out = Vec::new();
    for component in raw.split('/') {
        if component.is_empty() || component == "." || component == ".." {
            return Err(Code::PathOutsideRoot);
        }
        if RESERVED_NAMES.contains(&component.to_ascii_lowercase().as_str()) {
            return Err(Code::InvalidParams);
        }
        out.push(component.to_string());
    }
    Ok(out)
}

/// The kind of an opened object, after V1's refusals are applied.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Directory,
    RegularFile,
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

/// A display-safe name for an entry. Invalid UTF-8 is not an error here: the
/// name is returned as a lossy string for display, and the bytes on disk are
/// never re-derived from it (the `openat` above used the real name).
pub fn display_name(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

/// True if an entry name is hidden by policy (leading dot).
pub fn is_hidden(name: &str) -> bool {
    name.starts_with('.')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_every_traversal_shape() {
        for bad in [
            "..",
            "../etc/passwd",
            "a/../../b",
            "/etc/passwd",
            "/",
            "",
            ".",
            "a//b",
            "a/./b",
            "a\\b",
            "C:/Windows",
            "C:\\Windows",
            "\\\\server\\share",
            "file:stream",
            "a:b",
            "con",
            "NUL",
            "com1",
            "a/\u{0}b",
        ] {
            assert!(validate_relative_path(bad).is_err(), "must refuse {bad:?}",);
        }
    }

    #[test]
    fn accepts_ordinary_relative_paths() {
        assert_eq!(
            validate_relative_path("a/b.txt").unwrap(),
            vec!["a", "b.txt"]
        );
        assert_eq!(
            validate_relative_path("报告/2024.csv").unwrap(),
            vec!["报告", "2024.csv"]
        );
        assert_eq!(
            validate_relative_path("a b/c-d_e.f").unwrap(),
            vec!["a b", "c-d_e.f"]
        );
    }

    #[test]
    fn sensitive_names_are_case_insensitive() {
        for name in [".ssh", "id_rsa", "ID_RSA", "Keychains", ".Netrc", ".AWS"] {
            assert!(is_sensitive(name), "{name} must be sensitive");
        }
        assert!(!is_sensitive("reports"));
        assert!(!is_sensitive(".hidden-but-not-secret"));
    }

    #[test]
    fn an_absolute_root_requires_a_native_absolute_path() {
        assert!(validate_root_path("/Users/me/docs").is_ok());
        for bad in ["", "relative/path", "C:/Users", "\\\\server\\share"] {
            assert!(validate_root_path(bad).is_err(), "must refuse root {bad:?}");
        }
    }
}
