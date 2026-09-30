//! `fs-guard` — the root-anchored, read-only file helper.
//!
//! The desktop main process spawns this binary and speaks the private stdio
//! protocol in [`protocol`] to it. The helper is a separate process on purpose
//! (design decision D6): the main process never walks a user directory itself,
//! and this binary is the only thing holding the authorised root descriptor.
//!
//! ## Threading
//!
//! Two threads, for one reason: real cancellation. A request like `list` over a
//! large directory must be interruptible *while it runs*, so a cancel has to be
//! observed from outside the operation. The reader thread does that:
//!
//! * main thread — pulls requests off the channel and answers them, one at a
//!   time, so responses are naturally ordered and no operation runs twice;
//! * reader thread — reads frames, and records `cancel` targets in the shared
//!   flag *before* the main thread reaches them.
//!
//! Without the reader thread a `cancel` sitting behind the very operation it is
//! meant to stop would only be read after that operation finished, which is
//! exactly the deadlock-shaped behaviour the token is supposed to prevent.
//!
//! ## What this binary cannot do
//!
//! There is no `exec`, `shell`, `write`, `rename`, `unlink`, or network
//! operation, and the request type denies unknown fields, so no request can ask
//! for one. The op allow-list is an exhaustive `match`, not a lookup table.

mod grants;
mod ops;
mod paths;
mod protocol;

use std::sync::mpsc::{self, Sender};

use serde_json::{json, Value};

use crate::grants::GrantRegistry;
use crate::ops::CancelFlag;
use crate::protocol::{Code, Request, Response, MAX_FRAME_BYTES, MAX_PAGE, MAX_READ_BYTES};

/// What the reader thread hands to the main thread.
enum Incoming {
    Request(Request),
    /// A frame we could not turn into a request. Answered with the given code.
    Malformed(Code),
}

fn main() {
    let cancel = ops::new_cancel_flag();
    let (sender, receiver) = mpsc::channel::<Incoming>();

    let reader_cancel = cancel.clone();
    let reader = std::thread::spawn(move || read_loop(sender, reader_cancel));

    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    let mut registry = GrantRegistry::new();

    while let Ok(incoming) = receiver.recv() {
        let response = match incoming {
            Incoming::Malformed(code) => {
                // A frame we cannot attribute still gets an answer, so the
                // caller can correlate it rather than hanging.
                Response::err("(missing)", code, describe(code))
            }
            Incoming::Request(request) => {
                let id = request.reply_id().to_string();
                let result = dispatch(&mut registry, &request, &cancel);
                // Clear the cancel marker now that the operation is done, so
                // the set cannot grow without bound over a long session.
                clear_cancel(&cancel, &id);
                match result {
                    Ok(value) => Response::ok(id, value),
                    Err(code) => Response::err(id, code, describe(code)),
                }
            }
        };

        let value = serde_json::to_value(&response).unwrap_or_else(|_| {
            json!({ "id": response.id, "ok": false, "error": { "code": "internal", "message": "response serialisation failed" } })
        });
        if let Err(code) = protocol::write_frame(&mut out, &value) {
            // If the frame itself cannot be written the stream is gone; there
            // is nothing left to report to.
            let _ = code;
            break;
        }
    }

    // The reader thread only exits when stdin closes or the stream desyncs.
    let _ = reader.join();

    // Drop every root descriptor on shutdown. A grant is in-memory authority,
    // so exiting the helper must release it rather than leave it to the OS.
    registry.close_all();
}

/// Read frames until EOF or a desynchronising error.
fn read_loop(sender: Sender<Incoming>, cancel: CancelFlag) {
    let stdin = std::io::stdin();
    let mut input = stdin.lock();
    loop {
        match protocol::read_frame(&mut input) {
            Ok(None) => return, // clean EOF: the main process closed the pipe
            Ok(Some(body)) => match protocol::parse_request(&body) {
                Ok(request) => {
                    // Record a cancel *before* forwarding it, so the operation
                    // it targets can see it while it is still running.
                    if request.op == "cancel" {
                        if let Some(target) = request
                            .params
                            .get("request_id")
                            .and_then(|value| value.as_str())
                        {
                            if let Ok(mut set) = cancel.lock() {
                                // Bound the set: a caller that only ever sends
                                // cancels must not grow memory without limit.
                                if set.len() >= 1024 {
                                    set.clear();
                                }
                                set.insert(target.to_string());
                            }
                        }
                    }
                    if sender.send(Incoming::Request(request)).is_err() {
                        return;
                    }
                }
                Err(code) => {
                    if sender.send(Incoming::Malformed(code)).is_err() {
                        return;
                    }
                }
            },
            Err(code) => {
                // The length prefix desynchronised the stream: report and stop,
                // because the next four bytes are not a length.
                let _ = sender.send(Incoming::Malformed(code));
                return;
            }
        }
    }
}

fn clear_cancel(cancel: &CancelFlag, request_id: &str) {
    if let Ok(mut set) = cancel.lock() {
        set.remove(request_id);
    }
}

/// The exhaustive operation table.
///
/// An `op` that is not one of these is refused as `unknown_op`; adding a
/// capability means adding a match arm here, which is the point.
fn dispatch(
    registry: &mut GrantRegistry,
    request: &Request,
    cancel: &CancelFlag,
) -> Result<Value, Code> {
    match request.op.as_str() {
        "open_root" => {
            check_keys(&request.params, &["path"])?;
            let path = string_param(&request.params, "path")?;
            let (grant, version, label) = registry.open_root(&path)?;
            Ok(json!({ "grant": grant, "grant_version": version, "label": label }))
        }
        "close_root" => {
            check_keys(&request.params, &[])?;
            registry.close(grant_id(request)?)?;
            Ok(json!({ "closed": true }))
        }
        "list" => {
            check_keys(
                &request.params,
                &["path", "cursor", "page_size", "include_hidden"],
            )?;
            let path = optional_string_param(&request.params, "path")?.unwrap_or_default();
            let cursor = optional_string_param(&request.params, "cursor")?;
            let page_size = optional_u32_param(&request.params, "page_size")?;
            if let Some(size) = page_size {
                if size == 0 || size > MAX_PAGE {
                    return Err(Code::InvalidParams);
                }
            }
            let include_hidden =
                optional_bool_param(&request.params, "include_hidden")?.unwrap_or(false);
            let grant = registry.get(grant_id(request)?)?;
            ops::list(
                grant,
                &path,
                cursor.as_deref(),
                page_size,
                include_hidden,
                request.reply_id(),
                cancel,
            )
        }
        "stat" => {
            check_keys(&request.params, &["path"])?;
            let path = string_param(&request.params, "path")?;
            let grant = registry.get(grant_id(request)?)?;
            ops::stat(grant, &path)
        }
        "read" => {
            check_keys(&request.params, &["path", "offset", "length"])?;
            let path = string_param(&request.params, "path")?;
            let offset = optional_u64_param(&request.params, "offset")?.unwrap_or(0);
            let length = optional_u64_param(&request.params, "length")?;
            if let Some(value) = length {
                if value == 0 || value > MAX_READ_BYTES {
                    return Err(Code::InvalidParams);
                }
            }
            let grant = registry.get(grant_id(request)?)?;
            ops::read(grant, &path, offset, length, request.reply_id(), cancel)
        }
        "search" => {
            check_keys(
                &request.params,
                &[
                    "path",
                    "name_contains",
                    "text_contains",
                    "max_results",
                    "deadline_ms",
                ],
            )?;
            let path = optional_string_param(&request.params, "path")?.unwrap_or_default();
            let name_contains = optional_string_param(&request.params, "name_contains")?;
            let text_contains = optional_string_param(&request.params, "text_contains")?;
            let max_results = optional_u32_param(&request.params, "max_results")?;
            let deadline_ms = optional_u64_param(&request.params, "deadline_ms")?;
            let grant = registry.get(grant_id(request)?)?;
            ops::search(
                grant,
                &ops::SearchQuery {
                    path: &path,
                    name_contains: name_contains.as_deref(),
                    text_contains: text_contains.as_deref(),
                    max_results,
                    deadline_ms,
                },
                request.reply_id(),
                cancel,
            )
        }
        "cancel" => {
            // The reader thread already recorded the target; acknowledge it so
            // the caller is not left waiting. (`params.request_id` is optional
            // here: a cancel with no target is a no-op, not an error.)
            check_keys(&request.params, &["request_id"])?;
            Ok(json!({
                "cancelled": request
                    .params
                    .get("request_id")
                    .and_then(|value| value.as_str())
                    .unwrap_or("")
            }))
        }
        _ => Err(Code::UnknownOp),
    }
}

/// The `grant` field is required by every operation except `open_root`.
fn grant_id(request: &Request) -> Result<&str, Code> {
    match request.grant.as_deref() {
        Some(id) if !id.is_empty() => Ok(id),
        _ => Err(Code::InvalidParams),
    }
}

/// Reject any parameter key the operation does not declare, which is how a
/// "read this file" request cannot smuggle in an extra directive.
fn check_keys(params: &Value, allowed: &[&str]) -> Result<(), Code> {
    if params.is_null() {
        return Ok(());
    }
    let object = params.as_object().ok_or(Code::InvalidParams)?;
    for key in object.keys() {
        if !allowed.contains(&key.as_str()) {
            return Err(Code::InvalidParams);
        }
    }
    Ok(())
}

fn string_param(params: &Value, key: &str) -> Result<String, Code> {
    optional_string_param(params, key)?.ok_or(Code::InvalidParams)
}

fn optional_string_param(params: &Value, key: &str) -> Result<Option<String>, Code> {
    match params.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.clone())),
        Some(_) => Err(Code::InvalidParams),
    }
}

fn optional_u64_param(params: &Value, key: &str) -> Result<Option<u64>, Code> {
    match params.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(number)) => number.as_u64().map(Some).ok_or(Code::InvalidParams),
        Some(_) => Err(Code::InvalidParams),
    }
}

fn optional_u32_param(params: &Value, key: &str) -> Result<Option<u32>, Code> {
    Ok(optional_u64_param(params, key)?.and_then(|value| u32::try_from(value).ok()))
}

fn optional_bool_param(params: &Value, key: &str) -> Result<Option<bool>, Code> {
    match params.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Bool(flag)) => Ok(Some(*flag)),
        Some(_) => Err(Code::InvalidParams),
    }
}

/// A stable, human-readable sentence per code. Deliberately free of host paths,
/// file names and any other data the caller did not already have.
fn describe(code: Code) -> &'static str {
    match code {
        Code::InvalidFrame => "the frame is not a valid request",
        Code::FrameTooLarge => "the frame exceeds the 64 KiB limit",
        Code::UnknownOp => "the operation is not supported",
        Code::InvalidParams => "the parameters are missing or out of range",
        Code::NoSuchGrant => "the grant is unknown or was closed",
        Code::PathOutsideRoot => "the path is outside the authorised root",
        Code::RejectedEntry => "the entry is not readable under this policy",
        Code::Unsupported => "the platform cannot enforce the required check",
        Code::IoError => "the filesystem reported an error",
        Code::Cancelled => "the request was cancelled",
        Code::Internal => "an internal error occurred",
    }
}

/// Kept so the constant is referenced from the binary as well as the tests; the
/// bound is what makes "unbounded messages" a rejection and not a policy note.
const _FRAME_BOUND_IS_ENFORCED: usize = MAX_FRAME_BYTES;

#[cfg(test)]
mod tests {
    use super::*;

    fn request(op: &str, params: Value) -> Request {
        Request {
            id: "req-1".to_string(),
            op: op.to_string(),
            grant: Some("g".to_string()),
            params,
        }
    }

    #[test]
    fn an_unknown_operation_is_refused() {
        let mut registry = GrantRegistry::new();
        let cancel = ops::new_cancel_flag();
        assert_eq!(
            dispatch(&mut registry, &request("exec", json!({})), &cancel),
            Err(Code::UnknownOp),
        );
        for op in ["write", "unlink", "rename", "shell", "chmod", "spawn"] {
            assert_eq!(
                dispatch(&mut registry, &request(op, json!({})), &cancel),
                Err(Code::UnknownOp),
                "{op} must not exist",
            );
        }
    }

    #[test]
    fn undeclared_parameters_are_refused() {
        let mut registry = GrantRegistry::new();
        let cancel = ops::new_cancel_flag();
        let with_extra = Request {
            id: "req-1".to_string(),
            op: "stat".to_string(),
            grant: Some("g".to_string()),
            params: json!({ "path": "a.txt", "follow": true }),
        };
        assert_eq!(
            dispatch(&mut registry, &with_extra, &cancel),
            Err(Code::InvalidParams)
        );
    }

    #[test]
    fn a_missing_grant_is_refused_for_every_operation() {
        let mut registry = GrantRegistry::new();
        let cancel = ops::new_cancel_flag();
        for op in ["list", "stat", "read"] {
            let mut req = request(op, json!({ "path": "a.txt" }));
            req.grant = None;
            assert_eq!(
                dispatch(&mut registry, &req, &cancel),
                Err(Code::InvalidParams),
                "{op}"
            );
        }
    }

    #[test]
    fn an_unknown_grant_is_refused() {
        let mut registry = GrantRegistry::new();
        let cancel = ops::new_cancel_flag();
        for op in ["list", "stat", "read"] {
            assert_eq!(
                dispatch(
                    &mut registry,
                    &request(op, json!({ "path": "a.txt" })),
                    &cancel
                ),
                Err(Code::NoSuchGrant),
                "{op}"
            );
        }
    }

    #[test]
    fn a_page_size_beyond_the_bound_is_refused() {
        let mut registry = GrantRegistry::new();
        let cancel = ops::new_cancel_flag();
        let dir = std::env::temp_dir();
        let (grant, _, _) = registry.open_root(dir.to_str().unwrap()).unwrap();
        // The range check runs before the grant lookup, so an unknown grant
        // cannot mask an out-of-range parameter.
        let mut unknown = request("list", json!({ "path": ".", "page_size": MAX_PAGE + 1 }));
        unknown.grant = Some("g".to_string());
        assert_eq!(
            dispatch(&mut registry, &unknown, &cancel),
            Err(Code::InvalidParams)
        );

        let mut oversized = request("list", json!({ "path": ".", "page_size": MAX_PAGE + 1 }));
        oversized.grant = Some(grant.clone());
        assert_eq!(
            dispatch(&mut registry, &oversized, &cancel),
            Err(Code::InvalidParams)
        );
        let mut zero = request("list", json!({ "path": ".", "page_size": 0 }));
        zero.grant = Some(grant);
        assert_eq!(
            dispatch(&mut registry, &zero, &cancel),
            Err(Code::InvalidParams)
        );
    }

    #[test]
    fn a_read_length_beyond_the_bound_is_refused() {
        let mut registry = GrantRegistry::new();
        let cancel = ops::new_cancel_flag();
        let dir = std::env::temp_dir();
        let (grant, _, _) = registry.open_root(dir.to_str().unwrap()).unwrap();
        let mut req = request(
            "read",
            json!({ "path": "a.txt", "length": MAX_READ_BYTES + 1 }),
        );
        req.grant = Some(grant.clone());
        assert_eq!(
            dispatch(&mut registry, &req, &cancel),
            Err(Code::InvalidParams)
        );
        let mut zero = request("read", json!({ "path": "a.txt", "length": 0 }));
        zero.grant = Some(grant);
        assert_eq!(
            dispatch(&mut registry, &zero, &cancel),
            Err(Code::InvalidParams)
        );
    }
}
