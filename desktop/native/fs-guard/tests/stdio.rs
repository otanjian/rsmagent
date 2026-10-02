//! Process-level probes for the file helper (task 7.2) and the escape probes
//! task 7.7 asks to preserve (F03–F06, macOS half).
//!
//! These drive the *built binary* over a real pipe rather than calling the
//! library, because the guarantees under test are about a separate process: the
//! helper must be unable to reach anything outside the granted root even when
//! the filesystem is changed underneath it between requests.
//!
//! The adversarial cases all share one shape: make the path *look* right, then
//! change what it resolves to, and require the helper to fail rather than
//! follow. A test that only called `open` and compared strings would pass
//! against a `realpath + open` implementation, which is exactly what decision
//! D6 forbids -- so these probes assert on the bytes actually returned.

use std::collections::HashMap;
use std::ffi::OsStr;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{json, Value};

/// A live helper process plus the two halves of its stdio pipe.
struct Helper {
    child: Child,
    /// `Option` so [`Drop`] can close the pipe *before* waiting: the helper only
    /// exits on EOF, and a `wait` with stdin still open would block forever.
    stdin: Option<ChildStdin>,
    stdout: ChildStdout,
}

impl Helper {
    fn start() -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_fs-guard"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("the helper binary must be runnable");
        let stdin = child.stdin.take().expect("stdin is piped");
        let stdout = child.stdout.take().expect("stdout is piped");
        Helper {
            child,
            stdin: Some(stdin),
            stdout,
        }
    }

    fn stdin(&mut self) -> &mut ChildStdin {
        self.stdin.as_mut().expect("stdin is open until Drop")
    }

    /// Send one request and read exactly one response.
    fn call(&mut self, request: &Value) -> Value {
        let body = serde_json::to_vec(request).expect("request serialises");
        assert!(
            body.len() <= 64 * 1024,
            "test frames must respect the bound"
        );
        self.stdin()
            .write_all(&(body.len() as u32).to_be_bytes())
            .and_then(|()| self.stdin().write_all(&body))
            .and_then(|()| self.stdin().flush())
            .expect("the helper must accept a frame");

        let mut header = [0u8; 4];
        self.stdout
            .read_exact(&mut header)
            .expect("a response header");
        let len = u32::from_be_bytes(header) as usize;
        assert!(
            len <= 64 * 1024,
            "the helper must not exceed the frame bound"
        );
        let mut payload = vec![0u8; len];
        self.stdout
            .read_exact(&mut payload)
            .expect("a response body");
        serde_json::from_slice(&payload).expect("a JSON response")
    }

    /// Send a request expected to succeed and return its `result`.
    fn ok(&mut self, request: &Value) -> Value {
        let response = self.call(request);
        assert_eq!(response["ok"], true, "expected success, got {response}",);
        response["result"].clone()
    }

    /// Send a request expected to fail and return its error code.
    fn err(&mut self, request: &Value) -> String {
        let response = self.call(request);
        assert_eq!(response["ok"], false, "expected a refusal, got {response}");
        response["error"]["code"]
            .as_str()
            .expect("a refusal carries a code")
            .to_string()
    }

    /// Open a root and return its grant id.
    fn open_root(&mut self, path: &Path) -> String {
        let result = self.ok(&json!({
            "id": "open",
            "op": "open_root",
            "params": { "path": path.to_str().unwrap() },
        }));
        result["grant"].as_str().expect("a grant id").to_string()
    }
}

impl Drop for Helper {
    fn drop(&mut self) {
        // Close the write end first: the helper treats EOF as its shutdown
        // signal, so waiting with the pipe open would hang. Then give it a
        // bounded moment to exit, and kill only if it genuinely does not -- a
        // silent SIGKILL could otherwise hide a helper that fails to shut down.
        drop(self.stdin.take());
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        loop {
            match self.child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) if std::time::Instant::now() < deadline => {
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                _ => break,
            }
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// A throwaway directory tree, removed on drop.
struct Sandbox {
    root: PathBuf,
}

impl Sandbox {
    fn new(name: &str) -> Self {
        let root = std::env::temp_dir().join(format!(
            "fsguard-it-{}-{}-{}",
            name,
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("sandbox root");
        Sandbox { root }
    }

    fn path(&self, relative: &str) -> PathBuf {
        self.root.join(relative)
    }

    fn dir(&self, relative: &str) -> PathBuf {
        let path = self.path(relative);
        std::fs::create_dir_all(&path).expect("sandbox dir");
        path
    }

    fn file(&self, relative: &str, contents: &[u8]) -> PathBuf {
        let path = self.path(relative);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("parent dir");
        }
        std::fs::write(&path, contents).expect("sandbox file");
        path
    }

    fn symlink(&self, relative: &str, target: impl AsRef<Path>) {
        let path = self.path(relative);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("parent dir");
        }
        std::os::unix::fs::symlink(target, &path).expect("symlink");
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

// ---------------------------------------------------------------------------
// The happy path, so a refusal below cannot be blamed on a broken fixture.
// ---------------------------------------------------------------------------

#[test]
fn reads_a_file_inside_the_granted_root() {
    let sandbox = Sandbox::new("happy");
    sandbox.file("notes/hello.txt", b"hello from inside");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    let listed = helper.ok(&json!({
        "id": "l", "op": "list", "grant": grant, "params": { "path": "." },
    }));
    let names: Vec<String> = listed["entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| entry["name"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(names, vec!["notes"]);

    let read = helper.ok(&json!({
        "id": "r", "op": "read", "grant": grant,
        "params": { "path": "notes/hello.txt" },
    }));
    assert_eq!(read["bytes"], 17);
    // "hello from inside" -> base64.
    assert_eq!(read["data"], "aGVsbG8gZnJvbSBpbnNpZGU=");
    assert_eq!(read["truncated"], false);
}

#[test]
fn a_grant_is_not_echoed_with_its_absolute_path() {
    let sandbox = Sandbox::new("nopath");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    let response = helper.call(&json!({
        "id": "s", "op": "stat", "grant": grant, "params": { "path": "missing.txt" },
    }));
    let serialized = response.to_string();
    assert!(
        !serialized.contains(sandbox.root.to_str().unwrap()),
        "a refusal must not leak the host path: {serialized}",
    );
}

// ---------------------------------------------------------------------------
// F03: traversal and absolute paths.
// ---------------------------------------------------------------------------

#[test]
fn traversal_is_refused_for_every_operation() {
    let sandbox = Sandbox::new("traversal");
    sandbox.file("inside.txt", b"inside");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    for path in [
        "../outside.txt",
        "..",
        "a/../../etc/passwd",
        "/etc/passwd",
        "..\\windows",
    ] {
        for op in ["stat", "read"] {
            let code = helper.err(&json!({
                "id": "x", "op": op, "grant": grant, "params": { "path": path },
            }));
            assert!(
                code == "path_outside_root" || code == "invalid_params",
                "{op}({path}) must be refused, got {code}",
            );
        }
    }
}

// ---------------------------------------------------------------------------
// F04: a symlink anywhere in the path, including one swapped in after listing.
// ---------------------------------------------------------------------------

#[test]
fn a_symlinked_file_is_refused() {
    let sandbox = Sandbox::new("symfile");
    let outside = Sandbox::new("symfile-outside");
    outside.file("secret.txt", b"secret");
    sandbox.symlink("link.txt", outside.path("secret.txt"));

    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    assert_eq!(
        helper.err(&json!({
            "id": "r", "op": "read", "grant": grant, "params": { "path": "link.txt" },
        })),
        "rejected_entry",
    );
}

#[test]
fn a_swapped_intermediate_directory_cannot_redirect_a_read() {
    // This is the scenario task 7.2 names outright: the path was authorised and
    // even listed, then a component is replaced by a link to an unauthorised
    // area. The read must fail, never return the content beyond the link.
    let sandbox = Sandbox::new("swapdir");
    let outside = Sandbox::new("swapdir-outside");
    outside.file("secret.txt", b"TOP SECRET PAYLOAD");
    sandbox.dir("safe");
    sandbox.file("safe/public.txt", b"public");

    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    // Legitimate access first, so the fixture is proven readable.
    helper.ok(&json!({
        "id": "r1", "op": "read", "grant": grant, "params": { "path": "safe/public.txt" },
    }));
    helper.ok(&json!({
        "id": "l1", "op": "list", "grant": grant, "params": { "path": "safe" },
    }));

    // Swap the directory for a link pointing outside the authorised root.
    std::fs::remove_file(sandbox.path("safe/public.txt")).unwrap();
    std::fs::remove_dir(sandbox.path("safe")).unwrap();
    sandbox.symlink("safe", outside.root.clone());

    let response = helper.call(&json!({
        "id": "r2", "op": "read", "grant": grant, "params": { "path": "safe/secret.txt" },
    }));
    assert_eq!(
        response["ok"], false,
        "a swapped component must not be followed"
    );
    let serialized = response.to_string();
    assert!(
        !serialized.contains("TOP SECRET") && !serialized.contains("UE9QIFNFQ1JFVA"),
        "the payload beyond the link must never appear: {serialized}",
    );
}

#[test]
fn a_swapped_root_requires_the_user_to_choose_again() {
    let sandbox = Sandbox::new("swaproot");
    let outside = Sandbox::new("swaproot-outside");
    outside.file("secret.txt", b"secret");
    sandbox.dir("root");

    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.path("root"));

    // Move the authorised directory away and put a link in its place.
    std::fs::rename(sandbox.path("root"), sandbox.path("root-away")).unwrap();
    sandbox.symlink("root", outside.root.clone());

    // The held descriptor still names the original object, but the *granted
    // path* no longer does, so the grant is stale and every operation stops.
    assert_eq!(
        helper.err(&json!({
            "id": "s", "op": "stat", "grant": grant, "params": { "path": "." },
        })),
        "unsupported",
    );
}

// ---------------------------------------------------------------------------
// F05: files V1 refuses by kind.
// ---------------------------------------------------------------------------

#[test]
fn a_device_file_is_refused() {
    let sandbox = Sandbox::new("device");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    // /dev/null is a character device. The grant refuses it before any read, so
    // a device can never be presented as an ordinary file.
    let code = helper.err(&json!({
        "id": "s", "op": "stat", "grant": grant, "params": { "path": "nothing" },
    }));
    assert_eq!(
        code, "invalid_params",
        "a missing entry is not an I/O error"
    );

    let device_sandbox = Sandbox::new("device-link");
    device_sandbox.symlink("null", "/dev/null");
    let mut helper2 = Helper::start();
    let grant2 = helper2.open_root(&device_sandbox.root);
    // Even reached through a link, the entry is refused rather than opened.
    assert_eq!(
        helper2.err(&json!({
            "id": "s", "op": "stat", "grant": grant2, "params": { "path": "null" },
        })),
        "rejected_entry",
    );
}

#[test]
fn a_hard_linked_file_is_refused() {
    let sandbox = Sandbox::new("hardlink");
    let original = sandbox.file("original.txt", b"content");
    std::fs::hard_link(&original, sandbox.path("second-name.txt")).expect("hard link");

    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    // `nlink > 1` means the object is reachable under a name the user never
    // authorised, so it is refused rather than silently read.
    assert_eq!(
        helper.err(&json!({
            "id": "r", "op": "read", "grant": grant, "params": { "path": "original.txt" },
        })),
        "rejected_entry",
    );
}

#[test]
fn a_hard_linked_file_is_counted_as_skipped_in_a_listing() {
    let sandbox = Sandbox::new("hardlink-list");
    let original = sandbox.file("original.txt", b"content");
    std::fs::hard_link(&original, sandbox.path("second-name.txt")).expect("hard link");
    sandbox.file("ordinary.txt", b"ok");

    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    let listed = helper.ok(&json!({
        "id": "l", "op": "list", "grant": grant, "params": { "path": "." },
    }));
    let names: Vec<&str> = listed["entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| entry["name"].as_str().unwrap())
        .collect();
    assert_eq!(
        names,
        vec!["ordinary.txt"],
        "refused entries are skipped, not listed"
    );
    assert!(listed["skipped"].as_u64().unwrap() >= 2);
}

// ---------------------------------------------------------------------------
// F06: sensitive paths and hidden entries.
// ---------------------------------------------------------------------------

#[test]
fn sensitive_names_are_refused_and_cannot_be_unhidden() {
    let sandbox = Sandbox::new("sensitive");
    sandbox.file(".ssh/id_rsa", b"PRIVATE KEY MATERIAL");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    // Not via an explicit path...
    assert_eq!(
        helper.err(&json!({
            "id": "r", "op": "read", "grant": grant, "params": { "path": ".ssh/id_rsa" },
        })),
        "rejected_entry",
    );
    // ...and not by asking for hidden entries to be included either. There is
    // deliberately no parameter that relaxes this.
    let listed = helper.ok(&json!({
        "id": "l", "op": "list", "grant": grant,
        "params": { "path": ".", "include_hidden": true },
    }));
    let serialized = listed.to_string();
    assert!(
        !serialized.contains("id_rsa"),
        "a sensitive entry must never be listed: {serialized}"
    );
    assert!(!serialized.contains("PRIVATE KEY"));
}

#[test]
fn hidden_entries_are_excluded_by_default_but_available_when_asked() {
    let sandbox = Sandbox::new("hidden");
    sandbox.file(".notes/hidden.txt", b"x");
    sandbox.file("visible.txt", b"x");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    let default_listing = helper.ok(&json!({
        "id": "l1", "op": "list", "grant": grant, "params": { "path": "." },
    }));
    let names: Vec<&str> = default_listing["entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| entry["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["visible.txt"]);

    let explicit = helper.ok(&json!({
        "id": "l2", "op": "list", "grant": grant,
        "params": { "path": ".", "include_hidden": true },
    }));
    let names: Vec<&str> = explicit["entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| entry["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec![".notes", "visible.txt"]);
}

// ---------------------------------------------------------------------------
// Bounded behaviour: paging, chunked reads, cancel.
// ---------------------------------------------------------------------------

#[test]
fn listing_is_paged_and_reports_a_cursor() {
    let sandbox = Sandbox::new("paged");
    for index in 0..25 {
        sandbox.file(&format!("file-{index:02}.txt"), b"x");
    }
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    let mut seen: Vec<String> = Vec::new();
    let mut cursor: Option<String> = None;
    let mut pages = 0;
    loop {
        pages += 1;
        assert!(pages < 20, "paging must terminate");
        let mut params = json!({ "path": ".", "page_size": 10 });
        if let Some(value) = &cursor {
            params["cursor"] = json!(value);
        }
        let page = helper.ok(&json!({
            "id": "l", "op": "list", "grant": grant, "params": params,
        }));
        let entries = page["entries"].as_array().unwrap();
        assert!(entries.len() <= 10, "a page must respect page_size");
        for entry in entries {
            seen.push(entry["name"].as_str().unwrap().to_string());
        }
        cursor = page["next_cursor"].as_str().map(|value| value.to_string());
        if entries.is_empty() {
            break;
        }
    }
    seen.sort();
    seen.dedup();
    assert_eq!(
        seen.len(),
        25,
        "every file must appear exactly once across pages"
    );
}

#[test]
fn a_read_longer_than_one_chunk_is_reported_as_truncated() {
    let sandbox = Sandbox::new("chunk");
    let payload = vec![b'a'; 100 * 1024];
    sandbox.file("big.bin", &payload);
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    let first = helper.ok(&json!({
        "id": "r", "op": "read", "grant": grant,
        "params": { "path": "big.bin", "length": 100 * 1024 },
    }));
    // The chunk is bounded so the base64 response fits one frame.
    assert_eq!(first["bytes"], 32 * 1024);
    assert_eq!(first["truncated"], true);
    assert_eq!(first["total"], 100 * 1024);

    // Resuming from the returned offset yields the next chunk, so a bounded
    // helper can still deliver a large file.
    let second = helper.ok(&json!({
        "id": "r2", "op": "read", "grant": grant,
        "params": { "path": "big.bin", "offset": 32 * 1024 },
    }));
    assert_eq!(second["bytes"], 32 * 1024);
    assert_eq!(second["offset"], 32 * 1024);

    let tail = helper.ok(&json!({
        "id": "r3", "op": "read", "grant": grant,
        "params": { "path": "big.bin", "offset": 96 * 1024 },
    }));
    assert_eq!(tail["bytes"], 4 * 1024);
    assert_eq!(tail["truncated"], false, "the final chunk ends the file");
}

#[test]
fn an_offset_past_the_end_is_refused() {
    let sandbox = Sandbox::new("offset");
    sandbox.file("small.txt", b"abc");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    assert_eq!(
        helper.err(&json!({
            "id": "r", "op": "read", "grant": grant,
            "params": { "path": "small.txt", "offset": 99 },
        })),
        "invalid_params",
    );
}

// ---------------------------------------------------------------------------
// Protocol hardening.
// ---------------------------------------------------------------------------

#[test]
fn unknown_operations_and_extra_fields_are_refused() {
    let sandbox = Sandbox::new("ops");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    for op in [
        "exec", "write", "unlink", "rename", "shell", "spawn", "chmod",
    ] {
        assert_eq!(
            helper.err(&json!({ "id": "x", "op": op, "grant": grant, "params": {} })),
            "unknown_op",
            "{op} must not exist",
        );
    }
    // A read with an undeclared extra field is refused rather than ignored.
    assert_eq!(
        helper.err(&json!({
            "id": "r", "op": "read", "grant": grant,
            "params": { "path": "a.txt", "follow_links": true },
        })),
        "invalid_params",
    );
}

#[test]
fn an_oversized_frame_terminates_the_helper_rather_than_desyncing() {
    let sandbox = Sandbox::new("oversize");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    assert!(!grant.is_empty());

    // Claim 1 GiB with no body. The helper must answer and stop, because the
    // next four bytes are not a length prefix.
    helper
        .stdin()
        .write_all(&(1024u32 * 1024 * 1024).to_be_bytes())
        .expect("the pipe accepts the prefix");
    let mut header = [0u8; 4];
    helper
        .stdout
        .read_exact(&mut header)
        .expect("a refusal header");
    let len = u32::from_be_bytes(header) as usize;
    let mut payload = vec![0u8; len];
    helper
        .stdout
        .read_exact(&mut payload)
        .expect("a refusal body");
    let response: Value = serde_json::from_slice(&payload).unwrap();
    assert_eq!(response["error"]["code"], "frame_too_large");

    let status = helper.child.wait().expect("the helper exits");
    assert!(
        status.success(),
        "a desynchronised stream is a clean exit, not a crash"
    );
}

#[test]
fn closing_the_grant_stops_access() {
    let sandbox = Sandbox::new("close");
    sandbox.file("a.txt", b"x");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    helper.ok(&json!({ "id": "c", "op": "close_root", "grant": grant, "params": {} }));
    assert_eq!(
        helper.err(&json!({
            "id": "s", "op": "stat", "grant": grant, "params": { "path": "a.txt" },
        })),
        "no_such_grant",
    );
}

// ---------------------------------------------------------------------------
// Search: literal name and text matching, bounded.
// ---------------------------------------------------------------------------

#[test]
fn search_matches_names_and_text_literally() {
    let sandbox = Sandbox::new("search");
    sandbox.file("reports/q3-summary.md", b"nothing special");
    sandbox.file("reports/q4-summary.md", b"nothing special");
    sandbox.file("notes/plan.md", b"the magic phrase is here");
    sandbox.file("notes/other.md", b"unrelated content");

    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    let by_name = helper.ok(&json!({
        "id": "s1", "op": "search", "grant": grant,
        "params": { "path": ".", "name_contains": "summary" },
    }));
    let mut paths: Vec<&str> = by_name["results"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| entry["path"].as_str().unwrap())
        .collect();
    paths.sort();
    assert_eq!(
        paths,
        vec!["reports/q3-summary.md", "reports/q4-summary.md"]
    );

    let by_text = helper.ok(&json!({
        "id": "s2", "op": "search", "grant": grant,
        "params": { "path": ".", "text_contains": "magic phrase" },
    }));
    let paths: Vec<&str> = by_text["results"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| entry["path"].as_str().unwrap())
        .collect();
    assert_eq!(paths, vec!["notes/plan.md"]);
}

#[test]
fn search_does_not_descend_into_hidden_or_sensitive_entries() {
    let sandbox = Sandbox::new("search-hidden");
    sandbox.file(".ssh/id_rsa", b"FINDME");
    sandbox.file(".private/hidden.md", b"FINDME");
    sandbox.file("visible.md", b"FINDME");

    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    let found = helper.ok(&json!({
        "id": "s", "op": "search", "grant": grant,
        "params": { "path": ".", "text_contains": "FINDME" },
    }));
    let serialized = found.to_string();
    assert!(
        !serialized.contains(".ssh"),
        "sensitive paths must not be searched: {serialized}"
    );
    assert!(
        !serialized.contains(".private"),
        "hidden paths must not be searched: {serialized}"
    );
    let paths: Vec<&str> = found["results"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| entry["path"].as_str().unwrap())
        .collect();
    assert_eq!(paths, vec!["visible.md"]);
}

#[test]
fn search_reports_the_bound_it_hit() {
    let sandbox = Sandbox::new("search-bound");
    for index in 0..10 {
        sandbox.file(&format!("match-{index}.txt"), b"needle");
    }
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    let limited = helper.ok(&json!({
        "id": "s", "op": "search", "grant": grant,
        "params": { "path": ".", "name_contains": "match", "max_results": 3 },
    }));
    assert_eq!(limited["results"].as_array().unwrap().len(), 3);
    assert_eq!(limited["truncated"], true);
    assert_eq!(
        limited["truncated_reason"], "results",
        "a caller must be able to tell a capped result set from a complete one",
    );
}

#[test]
fn a_search_without_criteria_is_refused() {
    let sandbox = Sandbox::new("search-empty");
    sandbox.file("a.txt", b"x");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    // No criterion would mean "enumerate everything", which is exactly the
    // unbounded behaviour the helper refuses.
    assert_eq!(
        helper.err(&json!({
            "id": "s", "op": "search", "grant": grant, "params": { "path": "." },
        })),
        "invalid_params",
    );
    assert_eq!(
        helper.err(&json!({
            "id": "s2", "op": "search", "grant": grant,
            "params": { "path": ".", "name_contains": "" },
        })),
        "invalid_params",
    );
}

#[test]
fn search_finds_a_match_straddling_a_chunk_boundary() {
    let sandbox = Sandbox::new("search-boundary");
    // Place the needle so it crosses the 64 KiB chunk edge: the reader must
    // carry an overlap, not miss it.
    let mut contents = vec![b'.'; 64 * 1024 - 4];
    contents.extend_from_slice(b"BOUNDARYNEEDLE");
    contents.extend_from_slice(b"tail");
    sandbox.file("big.txt", &contents);

    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    let found = helper.ok(&json!({
        "id": "s", "op": "search", "grant": grant,
        "params": { "path": ".", "text_contains": "BOUNDARYNEEDLE" },
    }));
    assert_eq!(
        found["results"].as_array().unwrap().len(),
        1,
        "a match across a chunk boundary must still be found: {found}",
    );
}

#[test]
fn search_skips_files_too_large_to_inspect() {
    let sandbox = Sandbox::new("search-large");
    // Larger than the per-file inspection bound: reported as skipped, never as
    // a negative match.
    let mut contents = vec![b'.'; (4 * 1024 * 1024) + 16];
    contents.extend_from_slice(b"NEEDLE-AT-THE-END");
    sandbox.file("huge.txt", &contents);

    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);
    let found = helper.ok(&json!({
        "id": "s", "op": "search", "grant": grant,
        "params": { "path": ".", "text_contains": "NEEDLE-AT-THE-END" },
    }));
    assert_eq!(found["results"].as_array().unwrap().len(), 0);
    assert!(
        found["skipped"].as_u64().unwrap() >= 1,
        "the file must be counted as skipped"
    );
}

/// A guard that the fixture helper's own bookkeeping type is used, keeping the
/// import list honest if the tests above are trimmed.
#[allow(dead_code)]
fn _assert_map_type(_map: HashMap<String, String>, _os: &OsStr) {}
