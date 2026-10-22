//! Write-path probes against the **write** binary (task 2.4).
//!
//! This file is compiled only when `write-ops` is on, because
//! `CARGO_BIN_EXE_project-fs-guard` only exists in that build. The read-only
//! binary has no write operations at all, so there is nothing here for it to run.
//!
//! The cases all have the same shape as the acceptance matrix's W02 sentinel
//! probe: put a distinguishable file **outside** the granted root, drive the
//! write path through refusals and through one real write, and then require the
//! outside file's bytes *and* metadata to be exactly what they were. A helper
//! that "cleaned up" by ascending from a directory name, or that deleted
//! whatever sat at a staging-looking name, would show up here as a changed
//! sentinel.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{json, Value};

struct Helper {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: ChildStdout,
}

impl Helper {
    fn start() -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_project-fs-guard"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("the write helper binary must be runnable");
        let stdin = child.stdin.take().expect("stdin is piped");
        let stdout = child.stdout.take().expect("stdout is piped");
        Helper {
            child,
            stdin: Some(stdin),
            stdout,
        }
    }

    fn stdin(&mut self) -> &mut ChildStdin {
        self.stdin.as_mut().expect("stdin is open until drop")
    }

    /// Send one request and read exactly one response.
    ///
    /// A frame is a 4-byte big-endian length followed by that many bytes of JSON;
    /// a response is the same shape. This mirrors `tests/stdio.rs` deliberately:
    /// the two entry points share one protocol, and a test that framed
    /// differently would be testing a different helper.
    fn call(&mut self, request: &Value) -> Value {
        let body = serde_json::to_vec(request).expect("request serialises");
        assert!(body.len() <= 64 * 1024, "test frames must respect the bound");
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
        assert!(len <= 64 * 1024, "the helper must not exceed the frame bound");
        let mut payload = vec![0u8; len];
        self.stdout
            .read_exact(&mut payload)
            .expect("a response body");
        serde_json::from_slice(&payload).expect("a JSON response")
    }

    fn ok(&mut self, request: &Value) -> Value {
        let response = self.call(request);
        assert_eq!(response["ok"], true, "expected success, got {response}");
        response["result"].clone()
    }

    fn err(&mut self, request: &Value) -> String {
        let response = self.call(request);
        assert_eq!(response["ok"], false, "expected a refusal, got {response}");
        response["error"]["code"]
            .as_str()
            .expect("a refusal carries a code")
            .to_string()
    }

    /// Open a root and return the grant id.
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
        // signal, so waiting with the pipe open would hang.
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

/// One directory holding an authorised root, and a sentinel beside it.
struct Sandbox {
    base: PathBuf,
    root: PathBuf,
}

impl Sandbox {
    fn new(tag: &str) -> Self {
        let base = std::env::temp_dir().join(format!(
            "fsguard-write-io-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&base);
        let root = base.join("root");
        std::fs::create_dir_all(&root).unwrap();
        Sandbox { base, root }
    }

    fn sentinel_path(&self) -> PathBuf {
        self.base.join("outside-sentinel.txt")
    }

    /// Write the outside sentinel and return its bytes and metadata.
    fn plant_sentinel(&self) -> (Vec<u8>, u64, i64) {
        let path = self.sentinel_path();
        let content = b"OUTSIDE-SENTINEL-DO-NOT-TOUCH".to_vec();
        std::fs::write(&path, &content).unwrap();
        let meta = std::fs::metadata(&path).unwrap();
        let modified = meta
            .modified()
            .unwrap()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        (content, meta.len(), modified)
    }

    /// Assert the sentinel is byte-identical and unmodified.
    fn assert_sentinel_intact(&self, before: &(Vec<u8>, u64, i64)) {
        let path = self.sentinel_path();
        let after = std::fs::read(&path)
            .unwrap_or_else(|error| panic!("the out-of-root sentinel vanished: {error}"));
        assert_eq!(after, before.0, "the out-of-root sentinel's bytes changed");
        let meta = std::fs::metadata(&path).unwrap();
        assert_eq!(meta.len(), before.1, "the sentinel's size changed");
        let modified = meta
            .modified()
            .unwrap()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        assert_eq!(modified, before.2, "the sentinel's mtime changed");
    }

    fn file(&self, relative: &str, content: &[u8]) {
        let path = self.root.join(relative);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(path, content).unwrap();
    }

    fn read(&self, relative: &str) -> String {
        std::fs::read_to_string(self.root.join(relative)).unwrap()
    }

    /// Names of any staging file left in the root or its subdirectories.
    fn staging_leftovers(&self) -> Vec<String> {
        let mut found = Vec::new();
        collect_leftovers(&self.root, &self.root, &mut found);
        found
    }
}

fn collect_leftovers(dir: &Path, root: &Path, found: &mut Vec<String>) {
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(_) => return,
    };
    for entry in entries.filter_map(|entry| entry.ok()) {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with(".fsguard-tmp-") {
            let relative = entry
                .path()
                .strip_prefix(root)
                .unwrap_or(&entry.path())
                .to_string_lossy()
                .into_owned();
            found.push(relative);
        }
        if entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false) {
            collect_leftovers(&entry.path(), root, found);
        }
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.base);
    }
}

// ---------------------------------------------------------------------------
// The W02 sentinel probe, for the write path.
// ---------------------------------------------------------------------------

#[test]
fn no_write_path_touches_a_file_outside_the_root() {
    let sandbox = Sandbox::new("sentinel");
    let before = sandbox.plant_sentinel();
    sandbox.file("target.txt", b"original");
    sandbox.file("other.txt", b"other");

    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    // Every one of these is a refusal, and each takes a different route through
    // the write path: a path that escapes, a missing version, a stale version, an
    // ambiguous "create" over something that exists, and a hardening case.
    let refusals = [
        json!({ "id": "a", "op": "write", "grant": grant,
                "params": { "path": "../outside-sentinel.txt", "content": "clobber", "create": true } }),
        json!({ "id": "b", "op": "write", "grant": grant,
                "params": { "path": "target.txt", "content": "clobber" } }),
        json!({ "id": "c", "op": "write", "grant": grant,
                "params": { "path": "target.txt", "content": "clobber",
                            "expected_version": "fgv1:0000000000000000" } }),
        json!({ "id": "d", "op": "write", "grant": grant,
                "params": { "path": "target.txt", "content": "clobber", "create": true } }),
        json!({ "id": "e", "op": "edit", "grant": grant,
                "params": { "path": "target.txt", "old_string": "original",
                            "new_string": "clobber", "expected_version": "fgv1:deadbeefdeadbeef" } }),
        json!({ "id": "f", "op": "mkdir", "grant": grant,
                "params": { "path": "../escaped-dir" } }),
        json!({ "id": "g", "op": "write", "grant": grant,
                "params": { "path": "sub/../target.txt", "content": "clobber" } }),
    ];
    for request in refusals {
        let id = request["id"].as_str().unwrap().to_string();
        let code = helper.err(&request);
        assert!(
            !code.is_empty(),
            "request {id} must be refused with a named code"
        );
    }

    // The refusals must not have created anything beside the root either.
    assert!(
        !sandbox.base.join("escaped-dir").exists(),
        "mkdir escaped the root"
    );
    assert_eq!(
        sandbox.read("target.txt"),
        "original",
        "a refused write must leave the target untouched"
    );

    // Now one *real* write, so the publish path (staging file, ReplaceFileW,
    // cleanup) actually runs, and the sentinel still must not move.
    let stat = helper.ok(&json!({
        "id": "stat", "op": "stat", "grant": grant,
        "params": { "path": "target.txt" },
    }));
    assert_eq!(stat["kind"], "file");
    let created = helper.ok(&json!({
        "id": "w", "op": "write", "grant": grant,
        "params": { "path": "fresh.txt", "content": "brand new", "create": true },
    }));
    assert_eq!(created["created"], true);
    let first_version = created["version"].as_str().unwrap().to_string();
    assert!(
        first_version.starts_with("fgv1:"),
        "the write response carries an opaque version, got {first_version}"
    );

    let overwritten = helper.ok(&json!({
        "id": "w2", "op": "write", "grant": grant,
        "params": { "path": "fresh.txt", "content": "second take",
                    "expected_version": first_version },
    }));
    assert_eq!(overwritten["created"], false);
    assert_eq!(sandbox.read("fresh.txt"), "second take");

    let edited = helper.ok(&json!({
        "id": "e2", "op": "edit", "grant": grant,
        "params": { "path": "fresh.txt", "old_string": "second",
                    "new_string": "edited",
                    "expected_version": overwritten["version"].as_str().unwrap() },
    }));
    assert!(edited["version"].as_str().unwrap().starts_with("fgv1:"));
    assert_eq!(sandbox.read("fresh.txt"), "edited take");

    // The whole point: after real publishes, nothing outside moved.
    sandbox.assert_sentinel_intact(&before);
    assert_eq!(sandbox.read("target.txt"), "original");
    assert_eq!(sandbox.read("other.txt"), "other");
}

#[test]
fn a_successful_write_leaves_no_staging_object_behind() {
    let sandbox = Sandbox::new("residue");
    let before = sandbox.plant_sentinel();
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    // The parent must exist: a write is not a mkdir, so `mkdir` is a separate,
    // explicit step. This also exercises `mkdir` over the real protocol.
    let made = helper.ok(&json!({
        "id": "m", "op": "mkdir", "grant": grant, "params": { "path": "data" },
    }));
    assert_eq!(made["created"], true);

    let created = helper.ok(&json!({
        "id": "w", "op": "write", "grant": grant,
        "params": { "path": "data/one.txt", "content": "first", "create": true },
    }));
    // A nested target exercises the parent-directory handle the registry keeps.
    let overwritten = helper.ok(&json!({
        "id": "w2", "op": "write", "grant": grant,
        "params": { "path": "data/one.txt", "content": "second",
                    "expected_version": created["version"].as_str().unwrap() },
    }));
    assert_eq!(overwritten["bytes"], 6);

    assert!(
        sandbox.staging_leftovers().is_empty(),
        "staging objects left behind: {:?}",
        sandbox.staging_leftovers()
    );
    sandbox.assert_sentinel_intact(&before);
}

#[test]
fn the_write_binary_refuses_a_second_root_and_unlisted_operations() {
    let sandbox = Sandbox::new("surface");
    let before = sandbox.plant_sentinel();
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    // Deleting, renaming and starting a process are not operations this helper
    // has, in either build. They are refused by name rather than by a check that
    // could be relaxed.
    for op in ["unlink", "remove", "rename", "chmod", "shell", "spawn", "exec"] {
        assert_eq!(
            helper.err(&json!({ "id": "x", "op": op, "grant": grant, "params": {} })),
            "unknown_op",
            "{op} must not exist in the write build either"
        );
    }

    // A write may not name a second root, a cwd, a module or an interpreter.
    for extra in ["root_path", "cwd", "module", "argv_path", "absolute_path"] {
        let mut params = json!({ "path": "target.txt", "content": "x", "create": true });
        params[extra] = json!("anything");
        assert_eq!(
            helper.err(&json!({ "id": "x", "op": "write", "grant": grant, "params": params })),
            "invalid_params",
            "a write carrying {extra} must be refused"
        );
    }

    sandbox.assert_sentinel_intact(&before);
}

#[test]
fn an_edit_that_loses_the_race_changes_nothing() {
    let sandbox = Sandbox::new("race");
    let before = sandbox.plant_sentinel();
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    let created = helper.ok(&json!({
        "id": "w", "op": "write", "grant": grant,
        "params": { "path": "shared.txt", "content": "the model's view", "create": true },
    }));
    let stale = created["version"].as_str().unwrap().to_string();

    // The user edits the same file by hand after the model read it.
    sandbox.file("shared.txt", b"the user's newer text");

    assert_eq!(
        helper.err(&json!({
            "id": "e", "op": "edit", "grant": grant,
            "params": { "path": "shared.txt", "old_string": "model's",
                        "new_string": "model's", "expected_version": stale },
        })),
        "file_changed",
        "a stale edit must be refused as a conflict"
    );
    assert_eq!(
        sandbox.read("shared.txt"),
        "the user's newer text",
        "the user's newer content must survive a refused edit"
    );
    assert_eq!(
        helper.err(&json!({
            "id": "w2", "op": "write", "grant": grant,
            "params": { "path": "shared.txt", "content": "clobber", "expected_version": stale },
        })),
        "file_changed",
        "a stale overwrite must be refused as a conflict"
    );
    assert_eq!(sandbox.read("shared.txt"), "the user's newer text");
    assert!(
        sandbox.staging_leftovers().is_empty(),
        "a refused write must not have staged anything: {:?}",
        sandbox.staging_leftovers()
    );

    sandbox.assert_sentinel_intact(&before);
}

/// The error vocabulary the write path adds, as the caller sees it.
#[test]
fn the_conflict_code_is_stable_and_distinct() {
    let sandbox = Sandbox::new("codes");
    sandbox.file("here.txt", b"present");
    let mut helper = Helper::start();
    let grant = helper.open_root(&sandbox.root);

    // `file_changed` is the contract's `conflict_code` for write and edit: a
    // caller branches on it, so it must not be folded into `invalid_params`.
    let conflict = helper.err(&json!({
        "id": "c", "op": "write", "grant": grant,
        "params": { "path": "here.txt", "content": "x", "create": true },
    }));
    assert_eq!(conflict, "file_changed");

    // A genuinely malformed request is a *different* code, so the two are
    // distinguishable at the call site.
    let malformed = helper.err(&json!({
        "id": "m", "op": "write", "grant": grant, "params": { "content": "x" },
    }));
    assert_eq!(malformed, "invalid_params");
    assert_ne!(conflict, malformed);

    // And an unknown grant is neither.
    assert_eq!(
        helper.err(&json!({
            "id": "g", "op": "write", "grant": "fg-0000000000000000",
            "params": { "path": "x.txt", "content": "x", "create": true },
        })),
        "no_such_grant"
    );
}
