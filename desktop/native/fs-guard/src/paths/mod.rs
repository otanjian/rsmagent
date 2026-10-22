//! Path validation and root-anchored opening.
//!
//! This is the module the whole helper exists for. Design decision D6 is
//! explicit that the helper must **not** use `realpath` followed by `open`:
//! that pattern validates one path and opens another, so a component swapped
//! between the two steps is read anyway. Instead every path is opened
//! component by component, relative to the already-held root directory handle,
//! with reparse points (symlinks) refused on each step.
//!
//! What lives here is everything that is true on every platform; the two
//! backends that actually touch a filesystem are [`unix`] and [`windows`].
//! Keeping the split at this seam is what makes the platform guarantee a single
//! reviewed implementation rather than two that can drift:
//!
//! * A symlink or reparse point anywhere in the path is a refusal, not a
//!   redirect. The root handle pins the *object* the user selected, so replacing
//!   a directory with a link to somewhere else cannot widen access.
//! * The absolute path never leaves the helper. Callers only ever send a path
//!   *relative* to the authorised root, so a request cannot name an arbitrary
//!   location in the first place.

#[cfg(unix)]
mod unix;
#[cfg(unix)]
pub use unix::*;

#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use windows::*;

use crate::protocol::Code;

/// Components that are never readable, regardless of what the caller asks for.
///
/// Decision D6: "预设敏感路径排除不能由模型参数关闭". There is deliberately no
/// parameter, grant flag or request field that removes an entry from this list
/// -- the only way to change it is to change this constant, which is a code
/// review, not a runtime decision. Names are compared case-insensitively
/// because the default filesystem on both target platforms is case-insensitive,
/// so `Id_Rsa` must not slip past `id_rsa`.
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

/// Windows reserved device names. Rejected on every platform so a path that
/// would be dangerous once synced or copied to Windows is never treated as
/// ordinary.
const RESERVED_NAMES: &[&str] = &[
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8",
    "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
];

/// True if this component names something the helper never reads.
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
///
/// This is the same rule on both platforms on purpose. On Windows a backslash
/// *is* a separator, so refusing it is not merely conservative; on unix it is a
/// legal file name character, but a request that uses one is still refused
/// rather than normalised, because treating a separator as a literal name is
/// exactly how a path escapes a check on the other platform.
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

/// The kind of an opened object, after the refusals are applied.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Directory,
    RegularFile,
}

/// Metadata for one entry of a directory, addressed by name.
///
/// Shared shape so the listing logic in `ops` is identical on both platforms:
/// the unix backend fills it from `fstatat(..., AT_SYMLINK_NOFOLLOW)` and the
/// Windows backend from an entry reached with `FILE_OPEN_REPARSE_POINT`.
#[derive(Debug, Clone, Copy)]
pub struct EntryStat {
    /// `Some("dir")` or `Some("file")`, or `None` when the entry is one the
    /// helper refuses (a link, a device, a hard-linked file).
    pub kind: Option<&'static str>,
    pub size: u64,
    /// Unix seconds, so the client does not have to know which platform produced
    /// the number.
    pub modified: i64,
    /// The entry's object identity, for callers that must prove the name still
    /// refers to the object they created. The temporary-object registry is that
    /// caller: deleting "whatever is at this name" is exactly what it must not
    /// do, so it exists only in the build that has the registry.
    #[cfg(feature = "write-ops")]
    pub identity: u64,
}

/// Metadata for an already-open handle.
#[derive(Debug, Clone, Copy)]
pub struct FileStat {
    pub size: u64,
    pub modified: i64,
    /// False for a directory.
    pub regular: bool,
    /// Hard-link count; more than one is a refusal for a regular file.
    pub links: u64,
    /// The object's identity (device+inode / volume serial+file index), so a
    /// write's `expected_version` can bind the object and not only its bytes.
    ///
    /// Read by `src/write.rs`, which only exists in the write build; the
    /// read-only build still fills it because both backends return one shape.
    #[allow(dead_code)]
    pub identity: u64,
}

/// Everything a backend may need to reach an authorised root again.
///
/// The unix backend only needs the held descriptor (`openat(root, ".")`), while
/// the Windows backend has to open the recorded path and re-check the identity,
/// because NT's relative open refuses `"."` as a name. Passing one bundle keeps
/// the call sites identical on both platforms, so `ops` has no `cfg` in it.
///
/// `path` and `identity` are therefore unread on unix -- deliberately, since the
/// held descriptor is the stronger position there -- and the allow below is the
/// note that a warning is expected rather than an oversight.
pub struct RootRef<'a> {
    /// The descriptor held since the grant was created.
    pub handle: &'a std::fs::File,
    /// The absolute path the user selected. Private to the helper.
    #[allow(dead_code)]
    pub path: &'a RootPath,
    /// The identity captured when the grant was created.
    #[allow(dead_code)]
    pub identity: RootIdentity,
}

/// A display-safe name for an entry.
///
/// Invalid encoding is not an error here: the name is returned as a lossy string
/// for display, and the bytes on disk are never re-derived from it (the
/// component open used the real name, not this one).
///
/// The unix backend enumerates names as bytes, so it needs this conversion; the
/// Windows backend already holds them as `String`, so this is unix-only. Keeping
/// one implementation where it is used is better than two that could disagree.
#[cfg(unix)]
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
    fn path_limit_counts_bytes_and_preserves_the_exact_boundary() {
        // Small components keep the protocol limit independent of a native
        // filesystem's per-component limit. Include UTF-8 to catch char counts.
        let at_bound = format!("{}é", "a/".repeat(2047));
        assert_eq!(at_bound.len(), 4096);
        let components = validate_relative_path(&at_bound).unwrap();
        assert_eq!(components.join("/"), at_bound);
        assert!(matches!(
            validate_relative_path(&(at_bound + "a")),
            Err(Code::InvalidParams)
        ));
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
    fn the_sensitive_list_is_not_reachable_from_a_request() {
        // There is no parameter that removes an entry: the list is a constant and
        // this test is the note that says so, so a future "allow list" argument
        // has to change this assertion deliberately.
        assert!(is_sensitive(".ssh"));
        assert!(validate_relative_path(".ssh/id_rsa").is_ok(),
                "the path level accepts it; open_relative is what refuses");
    }
}
