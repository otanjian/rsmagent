//! The opaque file version the file tools exchange (design D3).
//!
//! Shared, not `write-ops`-gated, because **both** builds mint it: the read helper
//! returns it with the contents it served, and the write helper checks it before
//! publishing. One implementation is the point -- the contract forbids a client
//! from computing this value, so if the two sides each had their own recipe the
//! precondition could never be satisfied and a stale edit would be refused forever
//! (or, worse, accepted).
//!
//! The version is a *change detector*, not a cryptographic commitment: it binds
//! the project, the relative path, the object identity and the content stamp so a
//! caller cannot satisfy the precondition by hashing whatever it happens to hold.
//! It is deliberately not a keyed MAC -- there is no key both ends already share,
//! and the helper is not defending against a forged version so much as refusing to
//! overwrite something that moved since it was read.

use crate::grants::Grant;
use crate::paths::FileStat;

/// Marks the recipe so a future change to it is visible in the value itself.
const VERSION_PREFIX: &str = "fgv1:";

/// The opaque version of one file, bound to the read that produced it.
///
/// Design D3: "绑定项目/相对路径、对象身份及内容版本，不仅用mtime/size". So the
/// digest covers the grant **id**, the relative path, the object identity
/// (device+inode / volume serial+file index), the size and the modification time.
/// The grant's incrementing version *counter* is deliberately not used: it is
/// reused across sessions, so a value minted under one grant could otherwise match
/// another. A caller that only has the bytes on disk -- a different project, a
/// copy, a file that happened to be the same length -- cannot produce this value,
/// which is what the contract's ``note_not_checksum`` requires.
pub fn version_for(grant: &Grant, relpath: &str, stat: &FileStat) -> String {
    let material = format!(
        "{}|{}|{}|{}|{}",
        grant.id, relpath, stat.identity, stat.size, stat.modified
    );
    format!("{VERSION_PREFIX}{:016x}", fnv1a64(material.as_bytes()))
}

/// A 64-bit FNV-1a digest.
///
/// Used instead of pulling in a hash crate: this value is a *change detector*,
/// not a cryptographic commitment, and an extra dependency in a signed security
/// boundary is a liability (same reasoning as the hand-rolled base64 in `ops`).
fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in bytes {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

/// Whether a caller-supplied version is one this build could have minted.
///
/// A shape check only: it exists so an obviously hand-written value is refused
/// with a clear code instead of being compared and reported as "the file changed",
/// which would send the caller looking for an edit that never happened.
///
/// Gated to the write build because *checking* a caller's version is a write-build
/// concern -- the read build only ever mints one, and a warning about an unused
/// checker is noise in a security boundary where the warning list should stay
/// meaningful.
#[cfg(feature = "write-ops")]
pub fn looks_like_version(value: &str) -> bool {
    match value.strip_prefix(VERSION_PREFIX) {
        Some(body) => body.len() == 16 && body.chars().all(|c| c.is_ascii_hexdigit()),
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The material format, so the digest below can be pinned without a `Grant`
    /// (whose root handle has private fields and cannot be built outside the
    /// registry).
    fn material(id: &str, relpath: &str, stat: &FileStat) -> String {
        format!(
            "{}|{}|{}|{}|{}",
            id, relpath, stat.identity, stat.size, stat.modified
        )
    }

    fn stat(identity: u64, size: u64, modified: i64) -> FileStat {
        FileStat {
            identity,
            size,
            modified,
            regular: true,
            links: 1,
        }
    }

    #[test]
    fn fnv1a64_matches_its_published_test_vector() {
        // FNV-1a 64 over the empty string is the offset basis, and over "a" it is
        // the documented one. Pinned because this value is exchanged between two
        // builds: a silently different hash would make every read unusable by the
        // write that follows it.
        assert_eq!(fnv1a64(b""), 0xcbf2_9ce4_8422_2325);
        assert_eq!(fnv1a64(b"a"), 0xaf63_dc4c_8601_ec8c);
        assert_eq!(fnv1a64(b"foobar"), 0x85944171f73967e8);
    }

    #[test]
    fn each_covered_fact_changes_the_material() {
        // The whole point of covering all five: a value that only moved with size
        // could be satisfied by a copy, and one that only moved with mtime could be
        // satisfied by a touch.
        let base = material("g1", "a.txt", &stat(1, 2, 3));
        assert_ne!(base, material("g2", "a.txt", &stat(1, 2, 3)), "grant id");
        assert_ne!(base, material("g1", "b.txt", &stat(1, 2, 3)), "relative path");
        assert_ne!(base, material("g1", "a.txt", &stat(9, 2, 3)), "object identity");
        assert_ne!(base, material("g1", "a.txt", &stat(1, 20, 3)), "size");
        assert_ne!(base, material("g1", "a.txt", &stat(1, 2, 30)), "modified time");
    }

    #[test]
    #[cfg(feature = "write-ops")]
    fn the_version_is_prefixed_and_sixteen_hex_digits() {
        let version = format!("{VERSION_PREFIX}{:016x}", fnv1a64(b"anything"));
        assert!(looks_like_version(&version), "{version}");
        assert_eq!(version.len(), VERSION_PREFIX.len() + 16);
    }

    #[test]
    #[cfg(feature = "write-ops")]
    fn a_hand_written_version_is_recognised_as_the_wrong_shape() {
        assert!(!looks_like_version(""));
        assert!(!looks_like_version("0123456789abcdef"));
        assert!(!looks_like_version("fgv1:0123456789abcde")); // 15 digits
        assert!(!looks_like_version("fgv1:0123456789abcdef0")); // 17 digits
        assert!(!looks_like_version("fgv1:0123456789abcdeg")); // not hex
        assert!(!looks_like_version("fgv2:0123456789abcdef")); // different recipe
        assert!(looks_like_version("fgv1:0123456789abcdef"));
    }
}
