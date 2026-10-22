//! The limited **write** operations, for the `project-fs-guard` entry point only.
//!
//! This module is compiled only when the `write-ops` feature is on, and that
//! feature is what turns the read-only `fs-guard` binary into the separate
//! `project-fs-guard` binary. Keeping them the same source with one feature
//! rather than two `main.rs` files is deliberate: the frames, the cancel flag,
//! the path resolution and the trusted-computing-base around them are shared, so
//! the two entries cannot drift. What the read-only entry does *not* have is this
//! file at all -- "the read-only helper cannot write" is true because the code is
//! absent, not because a check says no.
//!
//! Three operations, and no more:
//!
//! * `write` -- create a file that must not exist, or overwrite one whose
//!   `expected_version` still matches what was read.
//! * `edit` -- replace a literal string, only when `expected_version` still
//!   matches.
//! * `mkdir` -- an *internal* step for the parent directories a write needs. It
//!   is not exposed to the model as a general operation, and it walks the same
//!   per-component path checks as everything else.
//!
//! What is deliberately absent: `rename`, `unlink`, `chmod`, any way to name a
//! second root, and any way to move something across volumes. A delete is not a
//! capability this helper has.
//!
//! ## Why the publish step is not `fs::rename`
//!
//! Design D3 requires that a reader never sees a half-written file, and that the
//! target's own access permissions are not silently widened by the replacement.
//! A plain rename over a target has two problems on Windows: it fails when the
//! target is open, and the replacement inherits the *staging* file's security
//! descriptor rather than keeping the target's. So the publish is
//! `FILE_RENAME_INFORMATION`-by-handle (`NtSetInformationFile`) on a handle to the
//! still-open target, which replaces the data while the target object's
//! descriptor stays attached. On unix the equivalent is `rename(2)` on the same
//! directory, which is already atomic.

use std::fs::File;
use std::io::Write;
use std::path::Path;

use serde_json::{json, Value};

use crate::grants::Grant;
use crate::paths::{self, Kind};
use crate::protocol::Code;
use crate::temp::TempRegistry;

/// Largest content one `write` may carry, in bytes.
///
/// Bounds the request the same way `MAX_READ_CHUNK` bounds a response: a frame is
/// 64 KiB, so a body that fits the protocol cannot be much larger than this once
/// base64 and the envelope are accounted for. A larger write is refused by name
/// rather than truncated.
pub const MAX_WRITE_BYTES: usize = 48 * 1024;

/// Largest file an `edit` will read in order to apply its replacement.
///
/// An edit has to hold the whole file to do a literal replacement, so it is
/// bounded separately from `write`: a 2 GiB log file is not something to load
/// because the model asked to change one string in it.
pub const MAX_EDIT_BYTES: u64 = 8 * 1024 * 1024;

/// The prefix that marks a value this helper minted as an opaque version.
///
/// The value is deliberately not something a caller can reproduce from the file
/// it is holding: it covers the grant the read happened through. See
/// [`version_for`].
///
/// The recipe itself lives in [`crate::version`] so the read helper and this one
/// mint the same value from one implementation -- the contract forbids a client
/// from computing it, so two recipes would mean the precondition could never be
/// satisfied.
pub use crate::version::version_for;

// ``looks_like_version`` is used below for the shape check; re-exported rather than
// copied for the same reason.
use crate::version::looks_like_version;

/// The staging file name for a target, unique within the same directory.
///
/// Chosen so it cannot be mistaken for a user file, and so two concurrent writes
/// to the same target by the same process do not collide. The leading dot matches
/// the "hidden by policy" convention, which means a listing will not show it.
fn staging_name(attempt: u32) -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!(".fsguard-tmp-{:x}-{attempt}", nanos)
}

/// The outcome of one write, as the caller sees it.
struct Written {
    version: String,
    bytes: usize,
}

/// `write`: create a file that must not exist, or overwrite a matching version.
///
/// `expected` is `None` only when `create` is true; every overwrite path requires
/// a version, and a mismatch is refused **before any staging happens**.
pub fn write(
    grant: &Grant,
    relpath: &str,
    content: &[u8],
    expected: Option<&str>,
    create: bool,
) -> Result<Value, Code> {
    if content.len() > MAX_WRITE_BYTES {
        return Err(Code::InvalidParams);
    }
    let components = paths::validate_relative_path(relpath)?;

    // The parent must already exist and be a real directory: a write is not a
    // mkdir, and silently creating a tree from a typo is how a model writes a
    // file somewhere the user did not expect.
    let (parent_dir, file_name) = split_parent(&components)?;
    let parent = open_parent(grant, parent_dir)?;
    if paths::stat_at(&parent, file_name).is_some() {
        // It exists. Decide create-vs-overwrite from here rather than from a
        // separate check, so the two cannot disagree.
        if create {
            discard_nothing();
            return Err(Code::FileChanged);
        }
    } else if !create {
        // An overwrite of something that is not there: the caller's view is
        // stale, which is exactly what `file_changed` means.
        return Err(Code::FileChanged);
    }

    let parent_path = absolute_of(grant, parent_dir)?;

    // --- create: a fresh file, no replacement needed -----------------------
    if create {
        if paths::stat_at(&parent, file_name).is_some() {
            return Err(Code::FileChanged);
        }
        let outcome = write_new_file(grant, relpath, &parent_path, file_name, content)?;
        return Ok(json!({
            "path": relpath,
            "grant_version": grant.version,
            "created": true,
            "bytes": outcome.bytes,
            "version": outcome.version,
        }));
    }

    // --- overwrite: verify the version, then publish atomically ------------
    let expected = expected.ok_or(Code::FileChanged)?;
    if !looks_like_version(expected) {
        // Not a value this helper mints. Refused as a conflict rather than as a
        // parameter error: the caller's view of the file is what is wrong.
        return Err(Code::FileChanged);
    }
    // `Access::Replace` asks for `DELETE` on the target as well as read: the
    // publish replaces *this* object by handle, so that is the access it needs.
    let current = paths::open_relative_with(
        grant.root(),
        &components,
        false,
        paths::Access::Replace,
    )?;
    let current_stat = paths::stat_of(&current.0)?;
    if current.1 != Kind::RegularFile || !current_stat.regular || current_stat.links > 1 {
        return Err(Code::RejectedEntry);
    }
    if version_for(grant, relpath, &current_stat) != expected {
        // No staging, no target touch: the refusal is the whole effect.
        return Err(Code::FileChanged);
    }

    let outcome =
        publish_over(grant, relpath, &parent_path, &parent, file_name, &current.0, content)?;
    Ok(json!({
        "path": relpath,
        "grant_version": grant.version,
        "created": false,
        "bytes": outcome.bytes,
        "version": outcome.version,
    }))
}

/// `edit`: a literal replacement, only when the version still matches.
pub fn edit(
    grant: &Grant,
    relpath: &str,
    old: &str,
    new: &str,
    replace_all: bool,
    expected: &str,
) -> Result<Value, Code> {
    if old.is_empty() {
        // An empty needle matches everywhere and would turn an edit into a
        // whole-file rewrite the caller did not ask for.
        return Err(Code::InvalidParams);
    }
    if !looks_like_version(expected) {
        return Err(Code::FileChanged);
    }
    let components = paths::validate_relative_path(relpath)?;
    let (opened, kind) = paths::open_relative(grant.root(), &components, false)?;
    let stat = paths::stat_of(&opened)?;
    if kind != Kind::RegularFile || !stat.regular || stat.links > 1 {
        return Err(Code::RejectedEntry);
    }
    if stat.size > MAX_EDIT_BYTES {
        return Err(Code::InvalidParams);
    }
    if version_for(grant, relpath, &stat) != expected {
        return Err(Code::FileChanged);
    }

    let original = std::fs::read_to_string(absolute_of(grant, &components)?)
        .map_err(|_| Code::InvalidParams)?;
    let occurrences = original.matches(old).count();
    if occurrences == 0 {
        // "I could not find the text" is a caller mistake, not a conflict.
        return Err(Code::InvalidParams);
    }
    if occurrences > 1 && !replace_all {
        // Ambiguous: the caller must say whether it meant all of them. Refusing
        // is the only answer that cannot silently change the wrong one.
        return Err(Code::InvalidParams);
    }
    let updated = if replace_all {
        original.replace(old, new)
    } else {
        original.replacen(old, new, 1)
    };
    if updated.len() > MAX_WRITE_BYTES {
        return Err(Code::InvalidParams);
    }

    let (parent_dir, file_name) = split_parent(&components)?;
    let parent_path = absolute_of(grant, parent_dir)?;
    // The handle the staging object is created and deleted through.
    let parent = open_parent(grant, parent_dir)?;
    // Re-open so the handle we publish through is the one whose version we just
    // checked, rather than the handle we read the text from.
    let (target, _) = paths::open_relative_with(
        grant.root(),
        &components,
        false,
        paths::Access::Replace,
    )?;
    let outcome = publish_over(
        grant,
        relpath,
        &parent_path,
        &parent,
        file_name,
        &target,
        updated.as_bytes(),
    )?;
    Ok(json!({
        "path": relpath,
        "grant_version": grant.version,
        "created": false,
        "bytes": outcome.bytes,
        "version": outcome.version,
    }))
}

/// `mkdir`: create one directory level inside the grant.
///
/// Internal, but it goes through the same open path as everything else so it
/// cannot be used to reach outside the root. It refuses an existing entry rather
/// than silently succeeding, because "it was already there" and "I made it" are
/// different answers for a caller building a tree.
pub fn mkdir(grant: &Grant, relpath: &str) -> Result<Value, Code> {
    let components = paths::validate_relative_path(relpath)?;
    let (parent_dir, name) = split_parent(&components)?;
    let parent = open_parent(grant, parent_dir)?;
    if paths::stat_at(&parent, name).is_some() {
        return Err(Code::FileChanged);
    }
    let parent_path = absolute_of(grant, parent_dir)?;
    let target = parent_path.join(name);
    match std::fs::create_dir(&target) {
        Ok(()) => Ok(json!({ "path": relpath, "grant_version": grant.version, "created": true })),
        Err(_) => Err(Code::IoError),
    }
}

/// Split a validated component list into `(parent, name)`.
fn split_parent(components: &[String]) -> Result<(&[String], &str), Code> {
    match components.split_last() {
        Some((name, parent)) => Ok((parent, name.as_str())),
        None => Err(Code::InvalidParams),
    }
}

/// Open the directory that holds the target, re-verifying the root identity.
///
/// An empty parent means the file sits directly in the authorised root, which is
/// the common case: `"a.txt"` has one component and no parent. `open_relative`
/// refuses an empty list -- correctly, since "no components" is not a path -- so
/// the root itself is re-opened through the same identity check the grant uses.
fn open_parent(grant: &Grant, parent_dir: &[String]) -> Result<File, Code> {
    if parent_dir.is_empty() {
        return paths::reopen_directory(&grant.root_ref());
    }
    Ok(paths::open_relative(grant.root(), parent_dir, true)?.0)
}

/// The absolute path of a component list inside this grant.
///
/// Only used to name a location *inside* a root the caller has already been
/// authorised for, and every such use re-checks through a handle afterwards. It
/// is never the authorisation itself.
fn absolute_of(grant: &Grant, components: &[String]) -> Result<std::path::PathBuf, Code> {
    let mut path = paths::absolute_root_of(grant)?;
    for component in components {
        path.push(component);
    }
    Ok(path)
}

/// Create a brand-new file, refusing to replace anything.
///
/// `create_new` is `O_EXCL`/`CREATE_NEW`: the check and the creation are one
/// syscall, so a file that appeared between the caller's stat and this call is
/// refused rather than overwritten.
fn write_new_file(
    grant: &Grant,
    relpath: &str,
    parent: &Path,
    name: &str,
    content: &[u8],
) -> Result<Written, Code> {
    let target = parent.join(name);
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    let mut file = options.open(&target).map_err(|error| match error.kind() {
        std::io::ErrorKind::AlreadyExists => Code::FileChanged,
        _ => Code::IoError,
    })?;
    file.write_all(content).map_err(|_| Code::IoError)?;
    file.sync_all().map_err(|_| Code::IoError)?;
    drop(file);
    let version = version_of_path(grant, relpath, &target)?;
    Ok(Written {
        version,
        bytes: content.len(),
    })
}

/// Replace a file's contents without ever exposing a half-written result.
///
/// Sequence, and why each step is where it is:
///
/// 1. write the new bytes into a **staging** file created with `CREATE_NEW` in
///    the same directory (`create_new` again, so a collision is an error and not
///    a clobber of somebody else's staging file);
/// 2. register that staging object with the temporary-object registry, so it is
///    the only thing a cleanup can ever remove;
/// 3. `sync_all`, so the bytes are durable before anything points at them;
/// 4. publish with `ReplaceFileW`, which keeps the *target's* descriptor and
///    attributes and makes the switch atomic for a reader;
/// 5. re-read the version from the published file, so the value returned to the
///    caller describes what is actually on disk now.
///
/// ## Cleanup, and the one case that does not clean up
///
/// A definite failure means the target was never touched, so the staging object
/// is removed through the registry -- which deletes only what it recorded, and
/// only after re-checking that the name still refers to that very object.
///
/// An **ambiguous** failure is different: the replacement may or may not have
/// happened. Removing the staging file then could destroy the only copy of the
/// new content, so nothing is deleted and the residue is left for the caller to
/// report. "I cleaned up" is not something to claim about an outcome you cannot
/// see.
fn publish_over(
    grant: &Grant,
    relpath: &str,
    parent: &Path,
    directory: &File,
    name: &str,
    target: &File,
    content: &[u8],
) -> Result<Written, Code> {
    let target_path = parent.join(name);
    // The registry holds a handle to the directory the staging file lives in, so
    // a later deletion resolves the name inside *that* directory object rather
    // than through a re-assembled path.
    let mut registry = TempRegistry::new();
    let mut staged: Option<usize> = None;
    let mut result: Result<(), Code> = Err(Code::IoError);

    for attempt in 0..8u32 {
        let candidate_name = staging_name(attempt);
        let candidate = parent.join(&candidate_name);
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        match options.open(&candidate) {
            Ok(mut handle) => {
                let identity = paths::stat_of(&handle).map(|stat| stat.identity).unwrap_or(0);
                if !registry.track(directory, &candidate_name, identity) {
                    // The directory handle could not be duplicated, so this object
                    // could never be cleaned up. Refusing now beats creating a file
                    // nothing will remove; the empty file is removed by name here
                    // because it is provably empty (we have written nothing yet).
                    drop(handle);
                    let _ = std::fs::remove_file(&candidate);
                    return Err(Code::IoError);
                }
                staged = Some(registry.len() - 1);
                let write_result = handle
                    .write_all(content)
                    .map_err(|_| Code::IoError)
                    .and_then(|_| handle.sync_all().map_err(|_| Code::IoError));
                drop(handle);
                result = write_result.and_then(|_| {
                    paths::replace_contents(target, &candidate, &target_path)
                });
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(_) => {
                result = Err(Code::IoError);
                break;
            }
        }
    }

    match result {
        Ok(()) => {
            // The publish consumed the staging name. Discarding anyway turns
            // "should be gone" into a checked statement, and a survivor is
            // reported rather than assumed away.
            let residue = registry.discard_all();
            if !residue.iter().all(|outcome| outcome.is_clean()) {
                return Err(Code::IoError);
            }
        }
        Err(code) => {
            if let Some(index) = staged {
                if !crate::temp::publish_is_ambiguous(code) {
                    // Definite failure: the target is untouched, so our own object
                    // is safe to remove.
                    let _ = registry.discard(index);
                }
                // Ambiguous: deliberately left in place. See the note above.
            }
            return Err(code);
        }
    }

    let version = version_of_path(grant, relpath, &target_path)?;
    Ok(Written {
        version,
        bytes: content.len(),
    })
}

/// The current version of a file, by path, built the same way as
/// [`version_for`] so a caller can compare the two directly.
fn version_of_path(grant: &Grant, relpath: &str, path: &Path) -> Result<String, Code> {
    let file = std::fs::File::open(path).map_err(|_| Code::IoError)?;
    let stat = paths::stat_of(&file)?;
    Ok(version_for(grant, relpath, &stat))
}

/// A no-op that exists so the `create`-over-existing branch reads as a decision.
fn discard_nothing() {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::grants::GrantRegistry;

    struct Fixture {
        root: std::path::PathBuf,
        registry: GrantRegistry,
        grant_id: String,
    }

    impl Fixture {
        fn new(tag: &str) -> Self {
            let root = std::env::temp_dir()
                .join(format!("fsguard-write-{tag}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&root);
            std::fs::create_dir_all(&root).unwrap();
            let mut registry = GrantRegistry::new();
            let (grant_id, _, _) = registry.open_root(root.to_str().unwrap()).unwrap();
            Fixture {
                root,
                registry,
                grant_id,
            }
        }

        fn grant(&self) -> &Grant {
            self.registry.get(&self.grant_id).unwrap()
        }

        fn read(&self, relative: &str) -> String {
            std::fs::read_to_string(self.root.join(relative)).unwrap()
        }

        fn exists(&self, relative: &str) -> bool {
            self.root.join(relative).exists()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn a_version_is_not_reproducible_from_the_bytes_alone() {
        let fixture = Fixture::new("version");
        std::fs::write(fixture.root.join("a.txt"), b"same").unwrap();
        let grant = fixture.grant();

        let (opened, _) = paths::open_relative(grant.root(), &["a.txt".into()], false).unwrap();
        let stat = paths::stat_of(&opened).unwrap();
        let version = version_for(grant, "a.txt", &stat);
        assert!(looks_like_version(&version));
        // The same object under a different relative path is a different version:
        // the digest covers the path, not just the content.
        assert_ne!(version, version_for(grant, "b.txt", &stat));
    }

    #[test]
    fn a_bogus_version_is_refused_rather_than_treated_as_a_mismatch() {
        let fixture = Fixture::new("bogus");
        std::fs::write(fixture.root.join("a.txt"), b"orig").unwrap();
        let grant = fixture.grant();
        assert_eq!(
            write(grant, "a.txt", b"new", Some("not-a-version"), false).err(),
            Some(Code::FileChanged)
        );
        assert_eq!(fixture.read("a.txt"), "orig", "no write may have happened");
    }

    #[test]
    fn create_refuses_an_existing_file_and_writes_nothing() {
        let fixture = Fixture::new("create-exists");
        std::fs::write(fixture.root.join("a.txt"), b"orig").unwrap();
        let grant = fixture.grant();
        assert_eq!(
            write(grant, "a.txt", b"new", None, true).err(),
            Some(Code::FileChanged)
        );
        assert_eq!(fixture.read("a.txt"), "orig");
    }

    #[test]
    fn create_makes_a_new_file_and_reports_its_version() {
        let fixture = Fixture::new("create-new");
        let grant = fixture.grant();
        let result = write(grant, "fresh.txt", b"hello", None, true).unwrap();
        assert_eq!(result["created"], true);
        assert_eq!(result["bytes"], 5);
        assert!(looks_like_version(result["version"].as_str().unwrap()));
        assert_eq!(fixture.read("fresh.txt"), "hello");
    }

    #[test]
    fn an_overwrite_without_a_version_is_refused() {
        let fixture = Fixture::new("no-version");
        std::fs::write(fixture.root.join("a.txt"), b"orig").unwrap();
        let grant = fixture.grant();
        assert_eq!(
            write(grant, "a.txt", b"new", None, false).err(),
            Some(Code::FileChanged)
        );
        assert_eq!(fixture.read("a.txt"), "orig");
    }

    #[test]
    fn a_stale_version_is_refused_and_the_newer_content_survives() {
        let fixture = Fixture::new("stale");
        std::fs::write(fixture.root.join("a.txt"), b"orig").unwrap();
        let grant = fixture.grant();
        let stale = {
            let (opened, _) =
                paths::open_relative(grant.root(), &["a.txt".into()], false).unwrap();
            let stat = paths::stat_of(&opened).unwrap();
            version_for(grant, "a.txt", &stat)
        };
        // Somebody else writes in between.
        std::fs::write(fixture.root.join("a.txt"), b"edited by the user").unwrap();

        assert_eq!(
            write(grant, "a.txt", b"model", Some(&stale), false).err(),
            Some(Code::FileChanged)
        );
        assert_eq!(
            fixture.read("a.txt"),
            "edited by the user",
            "the user's newer content must survive"
        );
    }

    #[test]
    fn a_matching_version_publishes_and_leaves_no_staging_file() {
        let fixture = Fixture::new("publish");
        std::fs::write(fixture.root.join("a.txt"), b"orig").unwrap();
        let grant = fixture.grant();
        let version = {
            let (opened, _) =
                paths::open_relative(grant.root(), &["a.txt".into()], false).unwrap();
            let stat = paths::stat_of(&opened).unwrap();
            version_for(grant, "a.txt", &stat)
        };
        let result = write(grant, "a.txt", b"replaced", Some(&version), false).unwrap();
        assert_eq!(result["created"], false);
        assert_eq!(fixture.read("a.txt"), "replaced");
        assert_ne!(result["version"].as_str().unwrap(), version,
                   "the published content has a new version");

        let leftovers: Vec<String> = std::fs::read_dir(&fixture.root)
            .unwrap()
            .filter_map(|entry| entry.ok())
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .filter(|name| name.starts_with(".fsguard-tmp-"))
            .collect();
        assert!(leftovers.is_empty(), "staging files left behind: {leftovers:?}");
    }

    #[test]
    fn a_new_file_needs_its_parent_to_exist() {
        let fixture = Fixture::new("no-parent");
        let grant = fixture.grant();
        // The parent is not created implicitly: a write is not a mkdir.
        assert!(write(grant, "missing/fresh.txt", b"x", None, true).is_err());
        assert!(!fixture.exists("missing/fresh.txt"));
    }

    #[test]
    fn mkdir_creates_one_level_and_refuses_a_second_time() {
        let fixture = Fixture::new("mkdir");
        let grant = fixture.grant();
        assert!(mkdir(grant, "newdir").is_ok());
        assert!(fixture.exists("newdir"));
        assert_eq!(mkdir(grant, "newdir").err(), Some(Code::FileChanged));
    }

    #[test]
    fn mkdir_cannot_escape_the_root() {
        let fixture = Fixture::new("mkdir-escape");
        let grant = fixture.grant();
        for bad in ["../outside", "a/../../b", "/abs", "a\\b"] {
            assert!(mkdir(grant, bad).is_err(), "must refuse {bad:?}");
        }
    }

    #[test]
    fn edit_requires_a_matching_version() {
        let fixture = Fixture::new("edit-version");
        std::fs::write(fixture.root.join("a.txt"), b"hello world").unwrap();
        let grant = fixture.grant();
        let version = {
            let (opened, _) =
                paths::open_relative(grant.root(), &["a.txt".into()], false).unwrap();
            let stat = paths::stat_of(&opened).unwrap();
            version_for(grant, "a.txt", &stat)
        };
        let result = edit(grant, "a.txt", "world", "there", false, &version).unwrap();
        assert_eq!(result["bytes"], 11);
        assert_eq!(fixture.read("a.txt"), "hello there");

        // The old version is stale now.
        assert_eq!(
            edit(grant, "a.txt", "there", "again", false, &version).err(),
            Some(Code::FileChanged)
        );
        assert_eq!(fixture.read("a.txt"), "hello there");
    }

    #[test]
    fn edit_refuses_an_ambiguous_replacement() {
        let fixture = Fixture::new("edit-ambiguous");
        std::fs::write(fixture.root.join("a.txt"), b"x x x").unwrap();
        let grant = fixture.grant();
        let version = {
            let (opened, _) =
                paths::open_relative(grant.root(), &["a.txt".into()], false).unwrap();
            let stat = paths::stat_of(&opened).unwrap();
            version_for(grant, "a.txt", &stat)
        };
        // More than one occurrence without `replace_all` is a caller mistake,
        // never a silent choice of which one to change.
        assert_eq!(
            edit(grant, "a.txt", "x", "y", false, &version).err(),
            Some(Code::InvalidParams)
        );
        assert_eq!(fixture.read("a.txt"), "x x x");

        let all = edit(grant, "a.txt", "x", "y", true, &version).unwrap();
        assert_eq!(all["bytes"], 5);
        assert_eq!(fixture.read("a.txt"), "y y y");
    }

    #[test]
    fn edit_refuses_text_that_is_not_there() {
        let fixture = Fixture::new("edit-missing-text");
        std::fs::write(fixture.root.join("a.txt"), b"hello").unwrap();
        let grant = fixture.grant();
        let version = {
            let (opened, _) =
                paths::open_relative(grant.root(), &["a.txt".into()], false).unwrap();
            let stat = paths::stat_of(&opened).unwrap();
            version_for(grant, "a.txt", &stat)
        };
        assert_eq!(
            edit(grant, "a.txt", "absent", "x", false, &version).err(),
            Some(Code::InvalidParams)
        );
        assert_eq!(fixture.read("a.txt"), "hello");
    }

    #[test]
    fn write_refuses_content_over_the_bound() {
        let fixture = Fixture::new("too-big");
        let grant = fixture.grant();
        let big = vec![b'a'; MAX_WRITE_BYTES + 1];
        assert_eq!(
            write(grant, "big.bin", &big, None, true).err(),
            Some(Code::InvalidParams)
        );
        assert!(!fixture.exists("big.bin"));
    }

    #[test]
    fn a_sensitive_target_is_refused_for_write_too() {
        let fixture = Fixture::new("sensitive-write");
        std::fs::create_dir_all(fixture.root.join(".ssh")).unwrap();
        let grant = fixture.grant();
        assert_eq!(
            write(grant, ".ssh/authorized_keys", b"key", None, true).err(),
            Some(Code::RejectedEntry)
        );
        assert!(!fixture.exists(".ssh/authorized_keys"));
    }

    #[test]
    fn a_write_through_a_replaced_directory_is_refused() {
        // F04 for the write path: the parent is re-opened through the held root
        // on every call, so a directory swapped for a link cannot redirect it.
        let fixture = Fixture::new("write-swap");
        std::fs::create_dir_all(fixture.root.join("sub")).unwrap();
        let grant = fixture.grant();

        // Make `sub` a link to somewhere outside the root, where the platform
        // allows it. On Windows an ordinary user cannot create a symlink but can
        // create a junction, and both are reparse points.
        let outside = std::env::temp_dir()
            .join(format!("fsguard-write-outside-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&outside);
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::remove_dir(&fixture.root.join("sub")).unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(&outside, fixture.root.join("sub")).unwrap();
        #[cfg(windows)]
        {
            let status = std::process::Command::new("cmd")
                .arg("/C")
                .arg("mklink")
                .arg("/J")
                .arg(fixture.root.join("sub"))
                .arg(&outside)
                .status()
                .expect("mklink");
            assert!(status.success(), "could not create the junction fixture");
        }

        assert!(
            write(grant, "sub/escaped.txt", b"nope", None, true).is_err(),
            "a write must not follow a replaced parent"
        );
        assert!(!outside.join("escaped.txt").exists());

        let _ = std::fs::remove_dir_all(&outside);
    }
}
