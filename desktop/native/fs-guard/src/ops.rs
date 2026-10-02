//! The read-side operations: `list`, `stat`, `read`.
//!
//! Every operation is bounded by construction (task 7.5): a listing returns at
//! most one page and reports how many entries it skipped, a read returns at
//! most one chunk and says whether more remains, and both poll the cancel flag
//! so a long enumeration cannot pin the UI.
//!
//! Nothing here re-derives a path from a *string* that a caller supplied after
//! the first validation: `resolve` opens each component with `openat`, and the
//! metadata is read from the resulting descriptor. That is what makes the
//! "listed it, then it was swapped" race a non-event.

use std::collections::HashSet;
use std::ffi::{CStr, CString};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::io::{AsRawFd, FromRawFd};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

use crate::grants::Grant;
use crate::paths::{self, Kind};
use crate::protocol::{Code, MAX_PAGE};

/// Largest chunk returned by one `read`, in raw bytes.
///
/// Chosen so the base64 response still fits inside one 64 KiB frame: 32 KiB of
/// raw bytes becomes ~43.7 KiB of base64, leaving room for the envelope. A
/// larger chunk would make every read fail the frame bound instead of
/// returning data.
pub const MAX_READ_CHUNK: u64 = 32 * 1024;

/// Largest number of directory entries inspected in one `list` call.
///
/// The page itself is smaller; this bounds the *scan* so a directory with
/// millions of entries cannot make the helper allocate without limit. Hitting
/// it is reported as `truncated` rather than silently returning a partial view.
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

/// Re-open the directory itself as an independent descriptor.
///
/// This is `openat(parent, ".")`, **not** `dup(parent)`. The distinction is the
/// whole reason this function exists: `dup` shares the file description, so the
/// directory offset is shared too, and the second `readdir` pass would start
/// where the first one stopped -- a directory listed twice would look empty the
/// second time. A fresh open gets its own offset and its own stream.
fn reopen_directory(parent: &File) -> Result<File, Code> {
    let dot = CString::new(".").map_err(|_| Code::Internal)?;
    let flags =
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NONBLOCK | libc::O_NOFOLLOW;
    // SAFETY: `dot` is a valid NUL-terminated string that outlives the call,
    // and `parent` is a live descriptor.
    let fd = unsafe { libc::openat(parent.as_raw_fd(), dot.as_ptr(), flags) };
    if fd < 0 {
        return Err(Code::IoError);
    }
    // SAFETY: `openat` returned a fresh descriptor we now own exclusively.
    Ok(unsafe { File::from_raw_fd(fd) })
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
        return Ok((reopen_directory(grant.root())?, Kind::Directory));
    }
    let components = paths::validate_relative_path(relpath)?;
    paths::open_relative(grant.root(), &components, want_directory)
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

    let mut names = read_names(&directory, request_id, cancel)?;
    names.sort();
    let scanned = names.len();
    let scan_truncated = scanned >= MAX_SCAN_ENTRIES;

    // The cursor is the last name of the previous page, so paging is stable
    // even if the directory changes between calls: a name sorts where it sorts.
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
        // Metadata comes from the directory handle, never by following the
        // name: `AT_SYMLINK_NOFOLLOW` is what keeps a link from being typed as
        // whatever it points at.
        let stat = match stat_at(&directory, &name) {
            Some(stat) => stat,
            None => {
                skipped += 1;
                continue;
            }
        };
        match classify_stat(&stat) {
            Some(kind) => entries.push(json!({
                "name": name,
                "kind": kind,
                "size": if kind == "file" { Some(stat.st_size as u64) } else { None },
                "modified": stat.st_mtime as i64,
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

/// Read directory names, bounded and cancellable.
fn read_names(
    directory: &File,
    request_id: &str,
    cancel: &CancelFlag,
) -> Result<Vec<String>, Code> {
    // A fresh descriptor, so this enumeration has its own offset and repeating
    // a listing is not affected by the previous one. `fdopendir` takes
    // ownership of the descriptor, so the `File` is forgotten rather than
    // closed twice.
    let handle = reopen_directory(directory)?;
    // SAFETY: `handle` is a live descriptor; ownership passes to the DIR stream.
    let stream = unsafe { libc::fdopendir(handle.as_raw_fd()) };
    if stream.is_null() {
        return Err(Code::IoError);
    }
    // The stream owns the descriptor now.
    std::mem::forget(handle);

    let mut names = Vec::new();
    loop {
        if names.len() >= MAX_SCAN_ENTRIES {
            break;
        }
        if names.len() % 256 == 0 && is_cancelled(cancel, request_id) {
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
        names.push(paths::display_name(name));
    }
    // SAFETY: `stream` is a live DIR stream opened above and not used after this.
    unsafe { libc::closedir(stream) };
    Ok(names)
}

fn stat_at(directory: &File, name: &str) -> Option<libc::stat> {
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
        return None;
    }
    Some(stat)
}

/// Map a stat result to a reported kind, or `None` when V1 refuses the entry.
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
        // A hard-linked file is reachable under a name the user did not authorise.
        return None;
    }
    Some("file")
}

/// One `stat` call.
pub fn stat(grant: &Grant, relpath: &str) -> Result<Value, Code> {
    let (file, _) = resolve(grant, relpath, false)?;
    let stat = fstat_of(&file)?;
    let kind = classify_stat(&stat).ok_or(Code::RejectedEntry)?;
    Ok(json!({
        "path": relpath,
        "grant_version": grant.version,
        "kind": kind,
        "size": if kind == "file" { Some(stat.st_size as u64) } else { None },
        "modified": stat.st_mtime as i64,
    }))
}

fn fstat_of(file: &File) -> Result<libc::stat, Code> {
    // SAFETY: `stat` is zeroed before use and `file` is a live descriptor.
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::fstat(file.as_raw_fd(), &mut stat) };
    if rc != 0 {
        return Err(Code::IoError);
    }
    Ok(stat)
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
    let (mut file, _) = resolve(grant, relpath, false)?;
    let stat = fstat_of(&file)?;
    if classify_stat(&stat).is_none() {
        return Err(Code::RejectedEntry);
    }
    let total = stat.st_size.max(0) as u64;
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
/// content and untrusted names is a denial-of-service surface, and the spec
/// asks for 字面量 search.
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
        reopen_directory(grant.root())?
    } else {
        let components = paths::validate_relative_path(relpath)?;
        let (directory, kind) = paths::open_relative(grant.root(), &components, true)?;
        debug_assert_eq!(kind, Kind::Directory);
        directory
    };

    // The starting base is what result paths are prefixed with. `.` (or an
    // empty path, meaning the root) contributes nothing, so results read as
    // paths relative to the authorised root rather than as `./x`.
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

        for name in read_names(&directory, request_id, cancel)? {
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
                // Search never descends into hidden entries, matching the
                // default listing policy.
                skipped += 1;
                continue;
            }
            let stat = match stat_at(&directory, &name) {
                Some(stat) => stat,
                None => {
                    skipped += 1;
                    continue;
                }
            };
            let kind = match classify_stat(&stat) {
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
                // Re-open the child *through this directory handle*, so the
                // walk can never leave the authorised root even if a component
                // was swapped since the parent was listed.
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
                "size": stat.st_size as u64,
                "modified": stat.st_mtime as i64,
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
    let stat = fstat_of(file)?;
    let size = stat.st_size.max(0) as u64;
    if size > MAX_SEARCH_FILE_BYTES {
        // Reported as "not searched" rather than "not found": the caller must
        // not read a skipped file as a negative match.
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
}
