//! Root grants: the helper's memory of what the user authorised.
//!
//! A grant is the *only* thing that makes a path reachable. It holds the open
//! directory handle for the user's chosen root plus the identity (device +
//! object id, spelled `st_dev`/`st_ino` on unix and volume serial/file index on
//! Windows) captured when it was opened. Two rules follow from D6:
//!
//! * **The root is re-verified on every request.** If the identity no longer
//!   matches, the grant fails and the user is asked to choose again, rather than
//!   silently reading whatever now sits at that path.
//! * **Grants live in memory only.** Nothing here is persisted, so quitting the
//!   helper drops every authorisation. Persisting a candidate path is the main
//!   process's job and is explicitly only a *candidate*, not an active grant.
//!
//! Version numbers increase per root and are echoed to the caller so a result
//! produced against a stale grant can be discarded (the `grant_version`-bound
//! cursor).
//!
//! Everything here is platform-independent except where the entropy for a grant
//! id comes from, which is why that one item is a per-platform function.

use std::collections::HashMap;
use std::fs::File;

use crate::paths::{self, RootIdentity, RootPath};
use crate::protocol::Code;

/// One authorised root.
pub struct Grant {
    /// The capability name the caller addresses this grant by.
    ///
    /// Held on the grant as well as in the registry map because it is part of the
    /// material a file version is bound to: a value minted under one grant must
    /// not match another, and the map key is not reachable from the grant itself.
    ///
    /// Read by `src/write.rs`, which only exists in the write build.
    #[allow(dead_code)]
    pub id: String,
    pub version: u32,
    root: File,
    identity: RootIdentity,
    /// The absolute path the user selected, kept **privately** for the identity
    /// re-check below. It is never serialised into a response, a log line or an
    /// error message: callers address the grant by id, and the display label is
    /// returned from `open_root` instead of being echoed from here.
    path: RootPath,
}

impl Grant {
    /// The held root handle.
    pub fn root(&self) -> &File {
        &self.root
    }

    /// The absolute path the user selected, for a backend that must re-open the
    /// object by name rather than by handle. Never serialised or logged.
    ///
    /// Reached through [`Grant::root_ref`] by every caller; kept as its own
    /// accessor so a future direct user has an obvious way in that does not
    /// tempt anyone into making the field public.
    #[allow(dead_code)]
    pub fn root_path(&self) -> &RootPath {
        &self.path
    }

    /// The identity captured when the grant was created, so a re-open by path can
    /// be checked against it instead of being trusted.
    #[allow(dead_code)]
    pub fn root_identity(&self) -> RootIdentity {
        self.identity
    }

    /// Everything a backend may need to reach this root again.
    pub fn root_ref(&self) -> paths::RootRef<'_> {
        paths::RootRef {
            handle: &self.root,
            path: &self.path,
            identity: self.identity,
        }
    }

    /// Fail unless the path still names the object the user selected.
    ///
    /// If the selected directory was renamed away, deleted, or replaced by a
    /// link to somewhere else, this fails and the caller must ask the user to
    /// choose again. Note that the *held handle* keeps reading the original
    /// object regardless -- that is the safe half of the requirement ("只访问原已
    /// 打开的合法对象或失败"); this check is what stops a stale grant from being
    /// treated as still authorising whatever now sits at that path.
    pub fn verify_identity(&self) -> Result<(), Code> {
        let current = paths::identity_of_path(&self.path)?;
        if current != self.identity {
            return Err(Code::Unsupported);
        }
        Ok(())
    }
}

pub struct GrantRegistry {
    grants: HashMap<String, Grant>,
    next_version: u32,
}

impl GrantRegistry {
    pub fn new() -> Self {
        GrantRegistry {
            grants: HashMap::new(),
            next_version: 1,
        }
    }

    /// Authorise a root and return `(grant_id, version, label)`.
    ///
    /// Refuses a network filesystem, a non-directory, and a reparse point or
    /// symlink, so a grant always names a real local directory object.
    pub fn open_root(&mut self, absolute_path: &str) -> Result<(String, u32, String), Code> {
        let path = paths::validate_root_path(absolute_path)?;
        let file = paths::open_root(&path)?;
        paths::reject_network_root(&file)?;
        if paths::identity_of(&file).is_err() {
            return Err(Code::IoError);
        }

        let label = label_for(absolute_path);
        let id = new_grant_id();
        let version = self.next_version;
        self.next_version += 1;

        let identity = paths::identity_of(&file)?;
        self.grants.insert(
            id.clone(),
            Grant {
                // The grant keeps its own copy of the id: it is part of the
                // material a file version is bound to (see `write::version_for`),
                // and the registry's map key is not reachable from the value.
                id: id.clone(),
                version,
                root: file,
                identity,
                path,
            },
        );
        Ok((id, version, label))
    }

    /// Resolve a grant id, re-verifying the root identity.
    pub fn get(&self, grant_id: &str) -> Result<&Grant, Code> {
        let grant = self.grants.get(grant_id).ok_or(Code::NoSuchGrant)?;
        grant.verify_identity()?;
        Ok(grant)
    }

    /// Drop a grant and close its handle.
    pub fn close(&mut self, grant_id: &str) -> Result<(), Code> {
        self.grants
            .remove(grant_id)
            .map(|_| ())
            .ok_or(Code::NoSuchGrant)
    }

    /// Drop every grant (process shutdown, or the user logging out).
    pub fn close_all(&mut self) {
        self.grants.clear();
    }

    /// Used by the grant-registry tests to assert that closing is observable.
    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.grants.len()
    }
}

impl Default for GrantRegistry {
    fn default() -> Self {
        Self::new()
    }
}

/// A last path component, for display. Never the full absolute path: the helper
/// does not echo host paths into diagnostics.
///
/// Both separators are treated as separators, because the label must not leak a
/// whole path on either platform.
fn label_for(absolute_path: &str) -> String {
    absolute_path
        .trim_end_matches(['/', '\\'])
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or("")
        .to_string()
}

/// A non-cryptographic fallback, used only when the OS entropy source cannot be
/// read. It mixes the clock, the pid and a process-local counter so two grants
/// in one session cannot collide; it is not claimed to be unguessable.
fn fallback_grant_id() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    let count = COUNTER.fetch_add(1, Ordering::Relaxed);
    let pid = std::process::id() as u64;
    let mixed = nanos ^ (pid << 32) ^ count.wrapping_mul(0x9E37_79B9_7F4A_7C15);
    format!("fallback{mixed:016x}")
}

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(DIGITS[(byte >> 4) as usize] as char);
        out.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    out
}

#[cfg(unix)]
mod entropy {
    use super::{fallback_grant_id, hex};

    /// 128 bits of entropy from the OS, hex-encoded.
    ///
    /// A grant id is process-local, but it is still a capability name: guessing
    /// one must not be cheaper than reading the frame stream. `/dev/urandom` is
    /// the only source used; if it is unavailable the helper falls back to a
    /// non-cryptographic value rather than failing every `open_root`.
    pub fn new_grant_id() -> String {
        use std::io::Read;
        let mut bytes = [0u8; 16];
        let mut source = match std::fs::File::open("/dev/urandom") {
            Ok(file) => file,
            Err(_) => return fallback_grant_id(),
        };
        match source.read_exact(&mut bytes) {
            Ok(()) => {}
            Err(_) => return fallback_grant_id(),
        }
        hex(&bytes)
    }
}

#[cfg(windows)]
mod entropy {
    use super::{fallback_grant_id, hex};
    use windows_sys::Win32::Security::Cryptography::{
        BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG,
    };

    /// 128 bits of entropy from the OS, hex-encoded.
    ///
    /// `BCryptGenRandom` with `BCRYPT_USE_SYSTEM_PREFERRED_RNG` is the documented
    /// system RNG and needs no algorithm handle. It is the Windows counterpart
    /// of reading `/dev/urandom`: a grant id is a capability name, so it must not
    /// be cheaper to guess than reading the frame stream. If the call fails the
    /// helper falls back to a non-cryptographic value rather than failing every
    /// `open_root`.
    pub fn new_grant_id() -> String {
        let mut bytes = [0u8; 16];
        // SAFETY: the buffer is owned and its exact length is passed.
        let status = unsafe {
            BCryptGenRandom(
                std::ptr::null_mut(),
                bytes.as_mut_ptr(),
                bytes.len() as u32,
                BCRYPT_USE_SYSTEM_PREFERRED_RNG,
            )
        };
        if status != 0 {
            return fallback_grant_id();
        }
        hex(&bytes)
    }
}

use entropy::new_grant_id;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grant_ids_are_long_and_distinct() {
        let a = new_grant_id();
        let b = new_grant_id();
        assert_eq!(a.len(), 32, "16 bytes must hex-encode to 32 characters");
        assert_ne!(a, b);
    }

    #[test]
    fn a_label_never_contains_the_full_path() {
        assert_eq!(label_for("/Users/someone/secret-project"), "secret-project");
        assert_eq!(label_for("/Users/someone/reports/"), "reports");
        assert_eq!(label_for(r"C:\Users\someone\secret-project"), "secret-project");
        assert_eq!(label_for(r"C:\Users\someone\reports\"), "reports");
    }

    #[test]
    fn versions_increase_so_stale_results_can_be_detected() {
        let mut registry = GrantRegistry::new();
        let dir = std::env::temp_dir();
        let (id_a, version_a, _) = registry.open_root(dir.to_str().unwrap()).unwrap();
        let (_id_b, version_b, _) = registry.open_root(dir.to_str().unwrap()).unwrap();
        assert!(version_b > version_a);
        assert_eq!(registry.len(), 2);
        registry.close(&id_a).unwrap();
        assert_eq!(registry.len(), 1);
        assert_eq!(registry.close(&id_a), Err(Code::NoSuchGrant));
    }

    #[test]
    fn an_unknown_grant_is_refused() {
        let registry = GrantRegistry::new();
        assert_eq!(registry.get("nope").err(), Some(Code::NoSuchGrant));
    }

    #[test]
    fn a_file_is_not_an_acceptable_root() {
        let base = std::env::temp_dir().join(format!("fsguard-fileroot-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let file = base.join("not-a-dir.txt");
        std::fs::write(&file, b"x").unwrap();

        let mut registry = GrantRegistry::new();
        assert_eq!(
            registry.open_root(file.to_str().unwrap()),
            Err(Code::Unsupported),
            "a directory pick must resolve to a directory",
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn a_replaced_root_path_invalidates_the_grant() {
        let base = std::env::temp_dir().join(format!("fsguard-swaproot-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let root = base.join("root");
        std::fs::create_dir_all(&root).unwrap();

        let mut registry = GrantRegistry::new();
        let (id, _, _) = registry.open_root(root.to_str().unwrap()).unwrap();
        assert!(registry.get(&id).is_ok(), "a fresh grant must verify");

        // The user's chosen directory is moved away and a different directory
        // takes its place. The grant must stop being usable, so the caller asks
        // the user to choose again rather than reading the new occupant.
        std::fs::rename(&root, base.join("root-moved")).unwrap();
        std::fs::create_dir_all(&root).unwrap();

        assert_eq!(registry.get(&id).err(), Some(Code::Unsupported));

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn a_deleted_root_invalidates_the_grant() {
        let base = std::env::temp_dir().join(format!("fsguard-delroot-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let root = base.join("root");
        std::fs::create_dir_all(&root).unwrap();

        let mut registry = GrantRegistry::new();
        let (id, _, _) = registry.open_root(root.to_str().unwrap()).unwrap();
        std::fs::remove_dir_all(&root).unwrap();

        assert_eq!(registry.get(&id).err(), Some(Code::Unsupported));
        let _ = std::fs::remove_dir_all(&base);
    }

    /// A root that is itself a link to a directory must be refused.
    ///
    /// Symlink creation needs a privilege on Windows that an ordinary user does
    /// not hold, so the case that *is* reachable there -- a directory junction,
    /// which any user can create -- is used instead. Both are reparse points and
    /// the refusal is the same one.
    #[cfg(unix)]
    #[test]
    fn a_symlinked_root_is_refused() {
        let base = std::env::temp_dir().join(format!("fsguard-symlink-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let real = base.join("real");
        std::fs::create_dir_all(&real).unwrap();
        let link = base.join("link");
        std::os::unix::fs::symlink(&real, &link).unwrap();

        let mut registry = GrantRegistry::new();
        assert_eq!(
            registry.open_root(link.to_str().unwrap()),
            Err(Code::RejectedEntry),
            "the selected root itself must not be a link",
        );
        // The real directory is accepted, so the refusal above is about the
        // link and not about the parent path.
        assert!(registry.open_root(real.to_str().unwrap()).is_ok());
        let _ = std::fs::remove_dir_all(&base);
    }
}
