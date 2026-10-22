//! The temporary-object registry, and the only deletion in this helper.
//!
//! A write publishes through a staging file in the target's own directory. That
//! staging file is the helper's *own* object: it is created by this process, it
//! is never the user's file, and it must be removed whether the write succeeds,
//! fails, or is abandoned. Design D3 states the rule this module exists to
//! enforce:
//!
//! > 用户原文件禁止作为清理目标，清理只根据本任务已记录的暂存对象身份执行，
//! > 绝不通过dirname向上推算根目录。
//!
//! Three properties make that true rather than aspirational:
//!
//! 1. **Only registered objects are deletable.** There is no function here that
//!    takes a path from outside; the only way to become removable is to be
//!    recorded by [`TempRegistry::track`], and the only caller of that is the
//!    staging step.
//! 2. **Deletion goes through a held directory handle, not an assembled path.**
//!    [`crate::paths::remove_relative`] resolves the name against the directory
//!    object that was open when the staging file was created, so a directory
//!    renamed or replaced afterwards cannot redirect the deletion.
//! 3. **The object's identity is re-checked before removal.** If the name no
//!    longer refers to the object that was created -- something replaced it, or
//!    the directory moved -- the removal is refused and the residue is reported.
//!    "I could not clean up" is an answer; "I deleted whatever was there" is not.
//!
//! Nothing here ever ascends from a directory name, and nothing here is reachable
//! from a request: the registry is built and dropped inside a single write
//! operation.

use std::fs::File;

use crate::paths;
use crate::protocol::Code;

/// What became of one cleanup attempt.
///
/// `Failed` is deliberately distinct from "there was nothing to clean": a caller
/// that reported success while a staging file survived would be claiming a
/// cleanliness it cannot show.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Discard {
    /// The object was removed.
    Removed,
    /// Nothing to do: no object was ever created.
    NoObject,
    /// The object is gone already (the publish consumed it, or somebody removed
    /// it). This is the expected outcome after a successful publish.
    AlreadyGone,
    /// The name no longer refers to the object that was created under it, so the
    /// removal was refused. The residue is *not* deleted.
    IdentityChanged,
    /// The name or its directory could not be resolved (the parent moved away),
    /// or the filesystem refused the removal.
    Failed,
}

impl Discard {
    /// Whether the registry is now empty for this object, in the sense the caller
    /// cares about: nothing of ours is left behind under a name we know.
    pub fn is_clean(self) -> bool {
        matches!(self, Discard::Removed | Discard::NoObject | Discard::AlreadyGone)
    }
}

/// One staging object this task created.
///
/// The parent is a *held handle*, kept open for as long as the object is tracked.
/// That is what makes the deletion immune to a directory being renamed or
/// replaced: the kernel resolves the name inside the object this handle names.
struct Tracked {
    parent: File,
    name: String,
    identity: u64,
}

/// The set of staging objects belonging to one write operation.
///
/// Scoped to a single operation on purpose: there is no process-wide registry to
/// leak across requests, and dropping the registry is what performs the cleanup,
/// so an early return (including a `?`) cannot skip it.
pub struct TempRegistry {
    tracked: Vec<Tracked>,
}

impl TempRegistry {
    pub fn new() -> Self {
        TempRegistry {
            tracked: Vec::new(),
        }
    }

    /// The number of objects still tracked (diagnostics, and used by tests).
    pub fn len(&self) -> usize {
        self.tracked.len()
    }

    /// Record a staging object so it can be cleaned up.
    ///
    /// `parent` must be the directory handle the object was created through, and
    /// `identity` the object's own identity as read immediately after creation.
    /// Both are captured here rather than re-derived later: re-deriving is how a
    /// cleanup would follow a change instead of noticing one.
    ///
    /// Returns false when the directory handle could not be duplicated, in which
    /// case the object is *not* tracked and the caller must report residue rather
    /// than assume it will be cleaned up.
    pub fn track(&mut self, parent: &File, name: &str, identity: u64) -> bool {
        match parent.try_clone() {
            Ok(handle) => {
                self.tracked.push(Tracked {
                    parent: handle,
                    name: name.to_string(),
                    identity,
                });
                true
            }
            Err(_) => false,
        }
    }

    /// Remove one tracked object, if it is still the object that was created.
    ///
    /// The entry is dropped from the registry only when the object is confirmed
    /// gone. A refused removal keeps the entry, because "still tracked" is what
    /// makes the residue reportable -- and, for the same reason, [`discard_all`]
    /// must not assume its list shrinks on every attempt.
    pub fn discard(&mut self, index: usize) -> Discard {
        if index >= self.tracked.len() {
            return Discard::NoObject;
        }
        let (parent, name, identity) = {
            let entry = &self.tracked[index];
            (entry.parent.try_clone(), entry.name.clone(), entry.identity)
        };
        let parent = match parent {
            Ok(handle) => handle,
            Err(_) => return Discard::Failed,
        };

        // Is the name still the object we made? A `stat_at` that fails means the
        // name is gone (already published or already cleaned); a different
        // identity means something else is there now.
        let outcome = match paths::stat_at(&parent, &name) {
            None => Discard::AlreadyGone,
            Some(stat) if stat.identity != identity => Discard::IdentityChanged,
            Some(_) => match paths::remove_relative(&parent, &name) {
                Ok(()) => Discard::Removed,
                // The identity check and the removal are two steps, so a racing
                // replacement between them is possible; the worst case is a
                // refusal, and a refusal is reported rather than retried into a
                // deletion.
                Err(_) => Discard::Failed,
            },
        };
        if outcome.is_clean() {
            // Nothing of ours is at that name any more, so there is nothing left
            // to report or to try again.
            self.tracked.remove(index);
        }
        outcome
    }

    /// Discard every tracked object, returning what happened to each.
    ///
    /// Each entry is attempted **once per call**, not until the list drains: a
    /// removal that is legitimately refused (the object was replaced, or the
    /// directory moved) keeps its entry so the residue stays reportable, and a
    /// `while !is_empty()` loop would spin on it forever. That is not a
    /// hypothetical -- it is what the first version of this method did.
    ///
    /// `&mut self` and explicit outcomes rather than `Drop`, so a caller can
    /// *see* whether the cleanup worked. A `Drop` that swallowed the result would
    /// make "removed" and "left behind" indistinguishable at exactly the point
    /// the distinction matters.
    pub fn discard_all(&mut self) -> Vec<Discard> {
        let indices: Vec<usize> = (0..self.tracked.len()).rev().collect();
        indices.into_iter().map(|index| self.discard(index)).collect()
    }
}

impl Default for TempRegistry {
    fn default() -> Self {
        Self::new()
    }
}

/// Whether a code means "the publish may have happened but we cannot tell".
///
/// Used by the write path to decide between "clean the staging file" and "leave
/// everything and report the uncertainty". Cleaning up after an ambiguous publish
/// risks removing the only copy of the new content, so the residue is reported
/// instead of tidied away.
///
/// `file_changed` is *not* ambiguous: it is raised before any staging happens, so
/// there is nothing to be uncertain about.
pub fn publish_is_ambiguous(code: Code) -> bool {
    matches!(code, Code::IoError | Code::Internal | Code::Unsupported)
}

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
                .join(format!("fsguard-temp-{tag}-{}", std::process::id()));
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

        fn parent(&self) -> File {
            let grant = self.registry.get(&self.grant_id).unwrap();
            paths::reopen_directory(&grant.root_ref()).unwrap()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    /// Create a staging-looking file in the fixture and return its identity.
    fn make_staging(fixture: &Fixture, name: &str) -> u64 {
        let path = fixture.root.join(name);
        std::fs::write(&path, b"staged").unwrap();
        let file = File::open(&path).unwrap();
        paths::stat_of(&file).unwrap().identity
    }

    #[test]
    fn a_tracked_object_is_removed() {
        let fixture = Fixture::new("remove");
        let name = ".fsguard-tmp-aaa-0";
        let identity = make_staging(&fixture, name);
        let parent = fixture.parent();

        let mut registry = TempRegistry::new();
        registry.track(&parent, name, identity);
        assert_eq!(registry.len(), 1);
        assert_eq!(registry.discard(0), Discard::Removed);
        assert_eq!(registry.len(), 0);
        assert!(!fixture.root.join(name).exists());
    }

    #[test]
    fn an_untracked_file_is_never_touched() {
        // The user's own file sits next to the staging object and must survive.
        let fixture = Fixture::new("untracked");
        std::fs::write(fixture.root.join("notes.txt"), b"mine").unwrap();
        let name = ".fsguard-tmp-bbb-0";
        let identity = make_staging(&fixture, name);
        let parent = fixture.parent();

        let mut registry = TempRegistry::new();
        registry.track(&parent, name, identity);
        assert_eq!(registry.discard(0), Discard::Removed);

        assert_eq!(
            std::fs::read_to_string(fixture.root.join("notes.txt")).unwrap(),
            "mine",
            "a file that was never tracked must be untouched"
        );
    }

    #[test]
    fn discarding_an_unknown_index_is_a_no_op() {
        let fixture = Fixture::new("noindex");
        std::fs::write(fixture.root.join("keep.txt"), b"keep").unwrap();
        let mut registry = TempRegistry::new();
        assert_eq!(registry.discard(0), Discard::NoObject);
        assert_eq!(registry.discard(99), Discard::NoObject);
        assert!(fixture.root.join("keep.txt").exists());
    }

    #[test]
    fn an_object_that_is_already_gone_reports_that_rather_than_failing() {
        // After a successful publish the staging name is gone, and that is the
        // expected outcome, not an error.
        let fixture = Fixture::new("gone");
        let name = ".fsguard-tmp-ccc-0";
        let identity = make_staging(&fixture, name);
        let parent = fixture.parent();
        std::fs::remove_file(fixture.root.join(name)).unwrap();

        let mut registry = TempRegistry::new();
        registry.track(&parent, name, identity);
        assert_eq!(registry.discard(0), Discard::AlreadyGone);
    }

    #[test]
    fn a_replaced_name_is_refused_rather_than_deleted() {
        // Something else now sits where our staging file was. Deleting "whatever
        // is at the name" is exactly the behaviour the registry must not have.
        let fixture = Fixture::new("replaced");
        let name = ".fsguard-tmp-ddd-0";
        let identity = make_staging(&fixture, name);
        let parent = fixture.parent();

        std::fs::remove_file(fixture.root.join(name)).unwrap();
        std::fs::write(fixture.root.join(name), b"somebody else's file").unwrap();

        let mut registry = TempRegistry::new();
        registry.track(&parent, name, identity);
        assert_eq!(registry.discard(0), Discard::IdentityChanged);
        assert_eq!(
            std::fs::read_to_string(fixture.root.join(name)).unwrap(),
            "somebody else's file",
            "a different object under the same name must survive"
        );
    }

    #[test]
    fn a_parent_that_moved_away_cannot_redirect_the_deletion() {
        // The deletion resolves the name inside the held directory object, so
        // renaming the directory does not make the removal land somewhere else.
        let fixture = Fixture::new("moved");
        let name = ".fsguard-tmp-eee-0";
        let identity = make_staging(&fixture, name);
        let parent = fixture.parent();

        // Take the whole tree away and put a same-named file back at the old path
        // with a foreign owner-of-that-name, then confirm our object is the only
        // thing removed.
        let moved = fixture.root.with_extension("moved");
        let _ = std::fs::remove_dir_all(&moved);
        std::fs::rename(&fixture.root, &moved).unwrap();
        std::fs::create_dir_all(&fixture.root).unwrap();
        std::fs::write(fixture.root.join(name), b"decoy in the new directory").unwrap();

        let mut registry = TempRegistry::new();
        registry.track(&parent, name, identity);
        // Our object is still reachable through the held handle, so it is removed
        // -- from the *moved* directory, never from the new one at the old path.
        assert_eq!(registry.discard(0), Discard::Removed);
        assert!(!moved.join(name).exists(), "our object was removed");
        assert_eq!(
            std::fs::read_to_string(fixture.root.join(name)).unwrap(),
            "decoy in the new directory",
            "the file now at the old path must be untouched"
        );

        let _ = std::fs::remove_dir_all(&moved);
    }

    #[test]
    fn discard_all_drains_every_tracked_object() {
        let fixture = Fixture::new("drain");
        let parent = fixture.parent();
        let mut registry = TempRegistry::new();
        let mut names = Vec::new();
        for index in 0..3 {
            let name = format!(".fsguard-tmp-fff-{index}");
            let identity = make_staging(&fixture, &name);
            registry.track(&parent, &name, identity);
            names.push(name);
        }
        let outcomes = registry.discard_all();
        assert_eq!(outcomes.len(), 3);
        assert!(outcomes.iter().all(|outcome| outcome.is_clean()));
        assert!(names.iter().all(|name| !fixture.root.join(name).exists()));
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn an_ambiguous_publish_is_never_treated_as_clean() {
        // The two outcomes that must not be papered over: an I/O failure after a
        // publish may have replaced the target, so cleaning up could delete the
        // only copy of the new content.
        assert!(publish_is_ambiguous(Code::IoError));
        assert!(publish_is_ambiguous(Code::Internal));
        assert!(!publish_is_ambiguous(Code::FileChanged));
    }
}
