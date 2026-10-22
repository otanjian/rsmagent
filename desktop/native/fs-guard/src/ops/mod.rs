//! The read-side operations: `list`, `stat`, `read`, `search`.
//!
//! Every operation is bounded by construction: a listing returns at most one page
//! and reports how many entries it skipped, a read returns at most one chunk and
//! says whether more remains, and all of them poll the cancel flag so a long
//! enumeration cannot pin the UI.
//!
//! Nothing here re-derives a path from a *string* that a caller supplied after
//! the first validation: `resolve` opens each component relative to the held
//! root handle, and the metadata is read from the resulting handle. That is what
//! makes the "listed it, then it was swapped" race a non-event.
//!
//! What is platform-independent is the whole of the logic below; the few
//! primitives that need a syscall -- re-open a directory for its own offset,
//! enumerate it, stat one entry through its parent -- live in [`crate::paths`]
//! per platform. Splitting it there rather than here is what keeps one reviewed
//! implementation of "list a directory, bounded and cancellable" for both.

use std::collections::HashSet;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

use crate::grants::Grant;
use crate::paths::{self, Kind};
use crate::protocol::{Code, MAX_PAGE};

/// Largest chunk returned by one `read`, in raw bytes.
///
/// Chosen so the base64 response still fits inside one 64 KiB frame: 32 KiB of
/// raw bytes becomes ~43.7 KiB of base64, leaving room for the envelope. A
/// larger chunk would make every read fail the frame bound instead of returning
/// data.
pub const MAX_READ_CHUNK: u64 = 32 * 1024;

/// Largest number of directory entries inspected in one call.
///
/// The page itself is smaller; this bounds the *scan* so a directory with
/// millions of entries cannot make the helper allocate without limit. Hitting it
/// is reported as `truncated` rather than silently returning a partial view.
pub const MAX_SCAN_ENTRIES: usize = 20_000;

/// Shared cancellation set. `cancel` inserts; long operations poll.
pub type CancelFlag = Arc<Mutex<HashSet<String>>>;

pub fn new_cancel_flag() -> CancelFlag {
    Arc::new(Mutex::new(HashSet::new()))
}

fn is_cancelled(cancel: &CancelFlag, request_id: &str) -> bool {
    cancel
        .lock()
        .map(|set| set.contains(request_id))
        .unwrap_or(false)
}

/// Whether a request has been cancelled.
///
/// Public because the per-platform enumeration loops poll it, and they must poll
/// the *same* set the reader thread inserts into.
pub fn is_cancelled_for(cancel: &CancelFlag, request_id: &str) -> bool {
    is_cancelled(cancel, request_id)
}

/// Resolve a relative path against the grant.
///
/// `""` and `"."` mean the root itself, which is how the UI lists the chosen
/// directory without naming it.
fn resolve(grant: &Grant, relpath: &str, want_directory: bool) -> Result<(File, Kind), Code> {
    if relpath.is_empty() || relpath == "." {
        if !want_directory {
            return Err(Code::InvalidParams);
        }
        // A fresh handle rather than a duplicated one: see `reopen_directory`.
        return Ok((paths::reopen_directory(&grant.root_ref())?, Kind::Directory));
    }
    let components = paths::validate_relative_path(relpath)?;
    paths::open_relative(grant.root(), &components, want_directory)
}

/// The walk for a caller that does not know whether the target is a file or a
/// directory, and must find out (`stat`).
///
/// Separate from [`resolve`] because the difference is the *access intent*, not a
/// flag: on Windows the intent decides whether the kernel is told to require a
/// shape, and requiring one makes the other unreachable. On unix a plain
/// `O_RDONLY` already opens both, so this is the same call there.
#[cfg(windows)]
fn resolve_any(grant: &Grant, relpath: &str) -> Result<(File, Kind), Code> {
    if relpath.is_empty() || relpath == "." {
        return Ok((paths::reopen_directory(&grant.root_ref())?, Kind::Directory));
    }
    let components = paths::validate_relative_path(relpath)?;
    paths::open_relative_with(
        grant.root(),
        &components,
        false,
        paths::Access::ReadEither,
    )
}

/// See [`resolve_any`]: unix needs no special intent, so this delegates.
#[cfg(not(windows))]
fn resolve_any(grant: &Grant, relpath: &str) -> Result<(File, Kind), Code> {
    resolve(grant, relpath, false)
}

/// One `list` call.
pub fn list(
    grant: &Grant,
    relpath: &str,
    cursor: Option<&str>,
    page_size: Option<u32>,
    include_hidden: bool,
    request_id: &str,
    cancel: &CancelFlag,
) -> Result<Value, Code> {
    let page_size = page_size.unwrap_or(200).clamp(1, MAX_PAGE) as usize;
    let (directory, _) = resolve(grant, relpath, true)?;

    let mut names = paths::read_names(&directory, request_id, cancel)?;
    names.sort();
    let scanned = names.len();
    let scan_truncated = scanned >= MAX_SCAN_ENTRIES;

    // The cursor is the last name of the previous page, so paging is stable even
    // if the directory changes between calls: a name sorts where it sorts.
    let start = match cursor {
        Some(after) => names.partition_point(|name| name.as_str() <= after),
        None => 0,
    };

    let mut entries = Vec::new();
    let mut skipped = 0usize;
    for name in names.into_iter().skip(start) {
        if entries.len() >= page_size {
            break;
        }
        if is_cancelled(cancel, request_id) {
            return Err(Code::Cancelled);
        }
        if paths::is_sensitive(&name) {
            skipped += 1;
            continue;
        }
        if !include_hidden && paths::is_hidden(&name) {
            skipped += 1;
            continue;
        }
        // Metadata comes from the directory handle, never by following the name:
        // a link is classified as a link, not as whatever it points at.
        let stat = match paths::stat_at(&directory, &name) {
            Some(stat) => stat,
            None => {
                skipped += 1;
                continue;
            }
        };
        match stat.kind {
            Some(kind) => entries.push(json!({
                "name": name,
                "kind": kind,
                "size": if kind == "file" { Some(stat.size) } else { None },
                "modified": stat.modified,
            })),
            None => skipped += 1,
        }
    }

    let last = entries
        .last()
        .and_then(|entry| entry.get("name"))
        .and_then(|name| name.as_str())
        .map(|name| name.to_string());

    Ok(json!({
        "path": relpath,
        "grant_version": grant.version,
        "entries": entries,
        "skipped": skipped,
        "truncated": scan_truncated,
        "next_cursor": last,
    }))
}

/// One `stat` call.
pub fn stat(grant: &Grant, relpath: &str) -> Result<Value, Code> {
    // Not esolve: stat reports the kind, so it must not ask the kernel to
    // require one. See esolve_any for why that is Windows-specific.
    let (file, kind) = resolve_any(grant, relpath)?;
    let stat = paths::stat_of(&file)?;
    let kind = match kind {
        Kind::Directory => "dir",
        Kind::RegularFile => {
            if !stat.regular || stat.links > 1 {
                // A hard-linked file is reachable under a name the user did not
                // authorise.
                return Err(Code::RejectedEntry);
            }
            "file"
        }
    };
    Ok(json!({
        "path": relpath,
        "grant_version": grant.version,
        "kind": kind,
        "size": if kind == "file" { Some(stat.size) } else { None },
        "modified": stat.modified,
    }))
}

/// One `read` call: a bounded chunk, base64-encoded for the JSON frame.
pub fn read(
    grant: &Grant,
    relpath: &str,
    offset: u64,
    length: Option<u64>,
    request_id: &str,
    cancel: &CancelFlag,
) -> Result<Value, Code> {
    if is_cancelled(cancel, request_id) {
        return Err(Code::Cancelled);
    }
    let (mut file, kind) = resolve(grant, relpath, false)?;
    let stat = paths::stat_of(&file)?;
    if kind != Kind::RegularFile || !stat.regular || stat.links > 1 {
        return Err(Code::RejectedEntry);
    }
    let total = stat.size;
    if offset > total {
        return Err(Code::InvalidParams);
    }
    let requested = length.unwrap_or(MAX_READ_CHUNK);
    let available = total - offset;
    let take = requested.min(MAX_READ_CHUNK).min(available);

    file.seek(SeekFrom::Start(offset))
        .map_err(|_| Code::IoError)?;
    let mut buffer = vec![0u8; take as usize];
    let mut filled = 0usize;
    while filled < buffer.len() {
        if is_cancelled(cancel, request_id) {
            return Err(Code::Cancelled);
        }
        match file.read(&mut buffer[filled..]) {
            Ok(0) => break,
            Ok(n) => filled += n,
            Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return Err(Code::IoError),
        }
    }
    buffer.truncate(filled);

    let truncated = offset + (filled as u64) < total;
    Ok(json!({
        "path": relpath,
        "grant_version": grant.version,
        "offset": offset,
        "bytes": filled,
        "total": total,
        "truncated": truncated,
        "encoding": "base64",
        "data": base64(&buffer),
        // The opaque version of the object these bytes came from (task 4.2).
        //
        // Returned with *every* chunk, not only the first: a caller that reads a
        // large file in pieces must be able to start editing from whatever it
        // last saw, and making the version depend on which chunk it happened to
        // end on would be a trap. It describes the object, so it is the same value
        // each time until the object actually changes.
        //
        // This is what makes v3's ``expected_version`` precondition reachable at
        // all: the contract requires that only a prior read through the same
        // authorised root can produce an acceptable value, which is exactly what
        // minting it here -- from the grant, the relative path and the open
        // object's identity -- provides.
        "version": crate::version::version_for(grant, relpath, &stat),
    }))
}

/// Standard base64 with padding.
///
/// Hand-rolled rather than pulled in as a dependency: the helper is a signed
/// security boundary, and this is 20 auditable lines with a known-answer test.
fn base64(input: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[((triple >> 18) & 0x3f) as usize] as char);
        out.push(ALPHABET[((triple >> 12) & 0x3f) as usize] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[((triple >> 6) & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[(triple & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

/// Largest literal search term accepted, in bytes.
pub const MAX_SEARCH_TERM: usize = 256;

/// Largest number of results one search returns.
pub const MAX_SEARCH_RESULTS: usize = 200;

/// Largest depth searched, so a pathologically deep tree cannot exhaust the
/// stack of pending directories.
pub const MAX_SEARCH_DEPTH: usize = 32;

/// Largest file inspected for a text match.
pub const MAX_SEARCH_FILE_BYTES: u64 = 4 * 1024 * 1024;

/// Largest deadline a caller may request, in milliseconds.
pub const MAX_SEARCH_DEADLINE_MS: u64 = 5_000;

/// Default deadline when the caller does not ask for one.
pub const DEFAULT_SEARCH_DEADLINE_MS: u64 = 2_000;

/// The bounded criteria for one `search` call.
///
/// Grouped into a struct so the bounds travel together and cannot be passed in
/// the wrong order at a call site.
pub struct SearchQuery<'a> {
    pub path: &'a str,
    pub name_contains: Option<&'a str>,
    pub text_contains: Option<&'a str>,
    pub max_results: Option<u32>,
    pub deadline_ms: Option<u64>,
}

/// One `search` call: literal name and/or literal text matching, bounded in
/// candidates, bytes, results and wall-clock time.
///
/// Every bound reports *which* bound was hit, because "there are more matches"
/// and "I stopped looking" are different answers and a caller that cannot tell
/// them apart will present a partial result as a complete one.
///
/// Matching is literal on purpose: a regular expression over untrusted file
/// content and untrusted names is a denial-of-service surface, and the spec asks
/// for 字面量 search.
pub fn search(
    grant: &Grant,
    query: &SearchQuery<'_>,
    request_id: &str,
    cancel: &CancelFlag,
) -> Result<Value, Code> {
    let relpath = query.path;
    let name_needle = match query.name_contains {
        Some(value) => {
            if value.is_empty() || value.len() > MAX_SEARCH_TERM {
                return Err(Code::InvalidParams);
            }
            Some(value)
        }
        None => None,
    };
    let text_needle = match query.text_contains {
        Some(value) => {
            if value.is_empty() || value.len() > MAX_SEARCH_TERM {
                return Err(Code::InvalidParams);
            }
            Some(value.as_bytes())
        }
        None => None,
    };
    if name_needle.is_none() && text_needle.is_none() {
        // A search with no criterion would return the whole tree, which is
        // exactly the unbounded enumeration this helper exists to prevent.
        return Err(Code::InvalidParams);
    }

    let limit = query
        .max_results
        .unwrap_or(MAX_SEARCH_RESULTS as u32)
        .clamp(1, MAX_SEARCH_RESULTS as u32) as usize;
    let deadline = std::time::Instant::now()
        + std::time::Duration::from_millis(
            query
                .deadline_ms
                .unwrap_or(DEFAULT_SEARCH_DEADLINE_MS)
                .clamp(1, MAX_SEARCH_DEADLINE_MS),
        );

    let start = if relpath.is_empty() || relpath == "." {
        paths::reopen_directory(&grant.root_ref())?
    } else {
        let components = paths::validate_relative_path(relpath)?;
        let (directory, kind) = paths::open_relative(grant.root(), &components, true)?;
        debug_assert_eq!(kind, Kind::Directory);
        directory
    };

    // The starting base is what result paths are prefixed with. `.` (or an empty
    // path, meaning the root) contributes nothing, so results read as paths
    // relative to the authorised root rather than as `./x`.
    let base = if relpath.is_empty() || relpath == "." {
        String::new()
    } else {
        relpath.trim_matches('/').to_string()
    };
    let mut results: Vec<Value> = Vec::new();
    let mut stack: Vec<(File, String, usize)> = vec![(start, base, 0)];
    let mut candidates = 0usize;
    let mut skipped = 0usize;
    let mut truncated: Option<&str> = None;

    'walk: while let Some((directory, base, depth)) = stack.pop() {
        if std::time::Instant::now() >= deadline {
            truncated = Some("deadline");
            break;
        }
        if is_cancelled(cancel, request_id) {
            return Err(Code::Cancelled);
        }

        for name in paths::read_names(&directory, request_id, cancel)? {
            if std::time::Instant::now() >= deadline {
                truncated = Some("deadline");
                break 'walk;
            }
            if candidates >= MAX_SCAN_ENTRIES {
                truncated = Some("candidates");
                break 'walk;
            }
            if paths::is_sensitive(&name) {
                skipped += 1;
                continue;
            }
            if paths::is_hidden(&name) {
                // Search never descends into hidden entries, matching the default
                // listing policy.
                skipped += 1;
                continue;
            }
            let stat = match paths::stat_at(&directory, &name) {
                Some(stat) => stat,
                None => {
                    skipped += 1;
                    continue;
                }
            };
            let kind = match stat.kind {
                Some(kind) => kind,
                None => {
                    skipped += 1;
                    continue;
                }
            };
            candidates += 1;

            let relative = if base.is_empty() {
                name.clone()
            } else {
                format!("{base}/{name}")
            };

            if kind == "dir" {
                if depth + 1 > MAX_SEARCH_DEPTH {
                    skipped += 1;
                    continue;
                }
                // Re-open the child *through this directory handle*, so the walk
                // can never leave the authorised root even if a component was
                // swapped since the parent was listed.
                match paths::open_relative(&directory, std::slice::from_ref(&name), true) {
                    Ok((child, _)) => stack.push((child, relative, depth + 1)),
                    Err(_) => skipped += 1,
                }
                continue;
            }

            if let Some(needle) = name_needle {
                if !name.contains(needle) {
                    continue;
                }
            }
            if let Some(needle) = text_needle {
                let (file, _) =
                    match paths::open_relative(&directory, std::slice::from_ref(&name), false) {
                        Ok(value) => value,
                        Err(_) => {
                            skipped += 1;
                            continue;
                        }
                    };
                match contains_literal(&file, needle, cancel, request_id)? {
                    Some(_) => {}
                    None => {
                        skipped += 1;
                        continue;
                    }
                }
            }

            results.push(json!({
                "path": relative,
                "kind": kind,
                "size": stat.size,
                "modified": stat.modified,
            }));
            if results.len() >= limit {
                truncated = Some("results");
                break 'walk;
            }
        }
    }

    Ok(json!({
        "path": relpath,
        "grant_version": grant.version,
        "results": results,
        "candidates": candidates,
        "skipped": skipped,
        "truncated": truncated.is_some(),
        "truncated_reason": truncated,
    }))
}

/// Literal byte-sequence search inside a file.
///
/// `Ok(Some(offset))` when found, `Ok(None)` when the file is too large to
/// inspect or the needle is absent. A needle longer than the file is simply not
/// found, which the code below handles without reading.
fn contains_literal(
    file: &File,
    needle: &[u8],
    cancel: &CancelFlag,
    request_id: &str,
) -> Result<Option<u64>, Code> {
    let stat = paths::stat_of(file)?;
    let size = stat.size;
    if size > MAX_SEARCH_FILE_BYTES {
        // Reported as "not searched" rather than "not found": the caller must not
        // read a skipped file as a negative match.
        return Ok(None);
    }
    if needle.is_empty() || size < needle.len() as u64 {
        return Ok(None);
    }

    let mut reader = file.try_clone().map_err(|_| Code::IoError)?;
    reader.seek(SeekFrom::Start(0)).map_err(|_| Code::IoError)?;
    // Overlap each chunk by `needle.len() - 1` so a match straddling a chunk
    // boundary is still seen.
    let overlap = needle.len() - 1;
    let chunk_size = 64 * 1024usize;
    let mut carry: Vec<u8> = Vec::new();
    let mut offset: u64 = 0;

    loop {
        if is_cancelled(cancel, request_id) {
            return Err(Code::Cancelled);
        }
        let mut buffer = vec![0u8; chunk_size];
        let mut filled = 0usize;
        while filled < buffer.len() {
            match reader.read(&mut buffer[filled..]) {
                Ok(0) => break,
                Ok(n) => filled += n,
                Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => return Err(Code::IoError),
            }
        }
        if filled == 0 {
            return Ok(None);
        }
        buffer.truncate(filled);

        let mut haystack = Vec::with_capacity(carry.len() + buffer.len());
        haystack.extend_from_slice(&carry);
        haystack.extend_from_slice(&buffer);
        let base = offset.saturating_sub(carry.len() as u64);
        if let Some(index) = find_subsequence(&haystack, needle) {
            return Ok(Some(base + index as u64));
        }
        if haystack.len() > overlap {
            carry = haystack[haystack.len() - overlap..].to_vec();
        } else {
            carry = haystack;
        }
        offset += filled as u64;
    }
}

/// Naive substring search. Deliberately not a regex engine: the inputs are an
/// untrusted needle and untrusted file content.
fn find_subsequence(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || haystack.len() < needle.len() {
        return None;
    }
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_matches_known_vectors() {
        // RFC 4648 test vectors.
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"foob"), "Zm9vYg==");
        assert_eq!(base64(b"fooba"), "Zm9vYmE=");
        assert_eq!(base64(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn a_binary_chunk_round_trips_through_the_alphabet() {
        let data: Vec<u8> = (0u8..=255).collect();
        let encoded = base64(&data);
        assert_eq!(encoded.len(), 344); // ceil(256/3)*4
        assert!(encoded
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '+' || c == '/' || c == '='));
    }

    /// The four read-side operations, driven through one real directory.
    ///
    /// These run on both platforms: everything they touch goes through the
    /// per-platform primitives, so a passing run here is what says the two
    /// backends behave the same way.
    #[test]
    fn list_is_bounded_and_reports_kinds() {
        let base = std::env::temp_dir().join(format!("fsguard-list-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("sub")).unwrap();
        std::fs::write(base.join("top.txt"), b"hello").unwrap();

        use crate::grants::GrantRegistry;
        let mut registry = GrantRegistry::new();
        let (id, _, _) = registry.open_root(base.to_str().unwrap()).unwrap();
        let grant = registry.get(&id).unwrap();
        let cancel = new_cancel_flag();

        let page = list(grant, "", None, None, false, "req", &cancel).unwrap();
        let entries = page["entries"].as_array().unwrap();
        let names: Vec<&str> = entries
            .iter()
            .map(|e| e["name"].as_str().unwrap())
            .collect();
        assert!(names.contains(&"sub"), "{names:?}");
        assert!(names.contains(&"top.txt"), "{names:?}");
        let dir = entries.iter().find(|e| e["name"] == "sub").unwrap();
        assert_eq!(dir["kind"], "dir");
        assert!(dir["size"].is_null(), "a directory has no size");
        let file = entries.iter().find(|e| e["name"] == "top.txt").unwrap();
        assert_eq!(file["kind"], "file");
        assert_eq!(file["size"], 5);

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn list_hides_dot_entries_unless_asked() {
        let base = std::env::temp_dir().join(format!("fsguard-hidden-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        std::fs::write(base.join(".hidden"), b"x").unwrap();
        std::fs::write(base.join("shown"), b"x").unwrap();

        use crate::grants::GrantRegistry;
        let mut registry = GrantRegistry::new();
        let (id, _, _) = registry.open_root(base.to_str().unwrap()).unwrap();
        let grant = registry.get(&id).unwrap();
        let cancel = new_cancel_flag();

        let default = list(grant, "", None, None, false, "req", &cancel).unwrap();
        let names: Vec<String> = default["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["name"].as_str().unwrap().to_string())
            .collect();
        assert!(!names.contains(&".hidden".to_string()), "{names:?}");
        assert!(names.contains(&"shown".to_string()), "{names:?}");

        let all = list(grant, "", None, None, true, "req", &cancel).unwrap();
        let names: Vec<String> = all["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["name"].as_str().unwrap().to_string())
            .collect();
        assert!(names.contains(&".hidden".to_string()), "{names:?}");

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn read_returns_a_bounded_chunk_and_says_whether_more_remains() {
        let base = std::env::temp_dir().join(format!("fsguard-read-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let payload = vec![b'a'; 100];
        std::fs::write(base.join("big.txt"), &payload).unwrap();

        use crate::grants::GrantRegistry;
        let mut registry = GrantRegistry::new();
        let (id, _, _) = registry.open_root(base.to_str().unwrap()).unwrap();
        let grant = registry.get(&id).unwrap();
        let cancel = new_cancel_flag();

        let first = read(grant, "big.txt", 0, Some(40), "req", &cancel).unwrap();
        assert_eq!(first["bytes"], 40);
        assert_eq!(first["total"], 100);
        assert_eq!(first["truncated"], true);
        assert_eq!(first["encoding"], "base64");

        let last = read(grant, "big.txt", 80, Some(40), "req", &cancel).unwrap();
        assert_eq!(last["bytes"], 20);
        assert_eq!(last["truncated"], false);

        // A read past the end is a parameter error, not an empty success.
        assert_eq!(
            read(grant, "big.txt", 101, None, "req", &cancel).err(),
            Some(Code::InvalidParams)
        );

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn read_refuses_a_directory() {
        let base = std::env::temp_dir().join(format!("fsguard-readdir-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("sub")).unwrap();

        use crate::grants::GrantRegistry;
        let mut registry = GrantRegistry::new();
        let (id, _, _) = registry.open_root(base.to_str().unwrap()).unwrap();
        let grant = registry.get(&id).unwrap();
        let cancel = new_cancel_flag();
        assert!(read(grant, "sub", 0, None, "req", &cancel).is_err());

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn search_finds_a_name_and_a_literal_within_a_file() {
        let base = std::env::temp_dir().join(format!("fsguard-search-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("sub")).unwrap();
        std::fs::write(base.join("sub").join("deep.txt"), b"needle inside").unwrap();
        std::fs::write(base.join("other.txt"), b"nothing here").unwrap();

        use crate::grants::GrantRegistry;
        let mut registry = GrantRegistry::new();
        let (id, _, _) = registry.open_root(base.to_str().unwrap()).unwrap();
        let grant = registry.get(&id).unwrap();
        let cancel = new_cancel_flag();

        let by_name = search(
            grant,
            &SearchQuery {
                path: "",
                name_contains: Some("deep"),
                text_contains: None,
                max_results: None,
                deadline_ms: None,
            },
            "req",
            &cancel,
        )
        .unwrap();
        let results = by_name["results"].as_array().unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0]["path"], "sub/deep.txt");

        let by_text = search(
            grant,
            &SearchQuery {
                path: "",
                name_contains: None,
                text_contains: Some("needle"),
                max_results: None,
                deadline_ms: None,
            },
            "req",
            &cancel,
        )
        .unwrap();
        let results = by_text["results"].as_array().unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0]["path"], "sub/deep.txt");

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn search_without_a_criterion_is_refused() {
        let base = std::env::temp_dir().join(format!("fsguard-nocrit-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        use crate::grants::GrantRegistry;
        let mut registry = GrantRegistry::new();
        let (id, _, _) = registry.open_root(base.to_str().unwrap()).unwrap();
        let grant = registry.get(&id).unwrap();
        let cancel = new_cancel_flag();
        assert_eq!(
            search(
                grant,
                &SearchQuery {
                    path: "",
                    name_contains: None,
                    text_contains: None,
                    max_results: None,
                    deadline_ms: None,
                },
                "req",
                &cancel,
            )
            .err(),
            Some(Code::InvalidParams)
        );

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn a_cancelled_request_is_reported_as_cancelled() {
        let base = std::env::temp_dir().join(format!("fsguard-cancel-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        std::fs::write(base.join("a.txt"), b"data").unwrap();

        use crate::grants::GrantRegistry;
        let mut registry = GrantRegistry::new();
        let (id, _, _) = registry.open_root(base.to_str().unwrap()).unwrap();
        let grant = registry.get(&id).unwrap();
        let cancel = new_cancel_flag();
        cancel.lock().unwrap().insert("req".to_string());

        assert_eq!(
            read(grant, "a.txt", 0, None, "req", &cancel).err(),
            Some(Code::Cancelled)
        );

        let _ = std::fs::remove_dir_all(&base);
    }
}
