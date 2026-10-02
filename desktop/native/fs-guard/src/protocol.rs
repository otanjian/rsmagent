//! The private stdio protocol between the main process and the file helper.
//!
//! Design decision D6 ships a separate signed helper precisely so the main
//! process never walks a user directory itself. That makes this framing a
//! security boundary: the caller is the desktop app, but the helper is a
//! separate process reading a pipe, so it must not trust *anything* about the
//! shape or size of what arrives.
//!
//! Framing is a 4-byte big-endian length followed by that many bytes of UTF-8
//! JSON. The length is checked **before** the body is read, so an oversized (or
//! hostile) frame is refused without ever allocating for it -- task 7.1's
//! "reject unbounded messages". A frame whose length exceeds
//! [`MAX_FRAME_BYTES`] desynchronises the stream (the helper cannot know where
//! the next frame starts), so it is a fatal protocol error rather than a
//! per-request one.

use std::io::{self, Read, Write};

use serde::{Deserialize, Serialize};
/// Hard ceiling on one frame in either direction (64 KiB).
///
/// This is the same bound the WSS gateway uses for commands (design §D5), so a
/// value that survives the network cannot be rejected here merely for its size.
pub const MAX_FRAME_BYTES: usize = 64 * 1024;

/// Largest absolute path accepted by `open_root`, in bytes.
///
/// The root is the one place an absolute path is legitimate: it comes from the
/// user's own native directory picker, never from a page or a model.
pub const MAX_PATH_BYTES: usize = 4096;

/// Largest single `read` in bytes. Larger requests are answered `truncated`
/// with the prefix that fits, never by allocating the requested size.
pub const MAX_READ_BYTES: u64 = 1024 * 1024;

/// Largest `list` page. Enumeration is paged (task 7.5) so one directory with
/// a million entries cannot produce an unbounded response.
pub const MAX_PAGE: u32 = 1000;

/// Stable error codes. These travel to the main process and are user-visible in
/// diagnostics, so they are a fixed vocabulary rather than free text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Code {
    /// The frame could not be parsed as a request object.
    InvalidFrame,
    /// The frame exceeded [`MAX_FRAME_BYTES`]; the stream is desynchronised.
    FrameTooLarge,
    /// `op` is not in the allow-list. Anything unlisted is refused, so adding
    /// an operation requires touching this enum -- there is no dynamic dispatch
    /// to sneak one in.
    UnknownOp,
    /// A parameter was missing, mistyped, or outside its documented range.
    InvalidParams,
    /// The referenced grant does not exist, or was closed/revoked.
    NoSuchGrant,
    /// The path escaped the authorised root (traversal, absolute, drive/UNC).
    PathOutsideRoot,
    /// The entry is one V1 refuses (symlink, reparse point, device, `nlink>1`).
    RejectedEntry,
    /// The platform cannot enforce the required check; the capability closes.
    Unsupported,
    /// An OS error that is not itself a policy decision.
    IoError,
    /// The request was cancelled by a later `cancel`.
    Cancelled,
    /// A bug. Reported rather than propagated as a panic.
    Internal,
}

impl Code {
    pub fn as_str(self) -> &'static str {
        match self {
            Code::InvalidFrame => "invalid_frame",
            Code::FrameTooLarge => "frame_too_large",
            Code::UnknownOp => "unknown_op",
            Code::InvalidParams => "invalid_params",
            Code::NoSuchGrant => "no_such_grant",
            Code::PathOutsideRoot => "path_outside_root",
            Code::RejectedEntry => "rejected_entry",
            Code::Unsupported => "unsupported",
            Code::IoError => "io_error",
            Code::Cancelled => "cancelled",
            Code::Internal => "internal",
        }
    }
}

/// A request frame.
///
/// `deny_unknown_fields` is deliberate. Task 7.1 requires that the helper take
/// a fixed, strictly-typed input; silently ignoring an unexpected key is how a
/// "read this file" request grows an unadvertised "and also run this" field.
#[derive(Debug, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub id: String,
    pub op: String,
    #[serde(default)]
    pub grant: Option<String>,
    #[serde(default)]
    pub params: serde_json::Value,
}

impl Request {
    /// The request id as it appears in the response. A frame without a usable
    /// `id` still gets an answer, so the caller can correlate the failure.
    pub fn reply_id(&self) -> &str {
        if self.id.is_empty() {
            "(missing)"
        } else {
            &self.id
        }
    }
}

/// A response frame.
#[derive(Debug, Serialize)]
pub struct Response {
    pub id: String,
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ErrorBody>,
}

#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub code: &'static str,
    pub message: String,
}

impl Response {
    pub fn ok(id: impl Into<String>, result: serde_json::Value) -> Self {
        Response {
            id: id.into(),
            ok: true,
            result: Some(result),
            error: None,
        }
    }

    pub fn err(id: impl Into<String>, code: Code, message: impl Into<String>) -> Self {
        Response {
            id: id.into(),
            ok: false,
            result: None,
            error: Some(ErrorBody {
                code: code.as_str(),
                message: message.into(),
            }),
        }
    }
}

/// Read one frame, or `None` at a clean end of input.
///
/// The length prefix is validated before the body is read. A body that is
/// shorter than its prefix claims is an [`Code::InvalidFrame`], not a partial
/// parse: a truncated frame must never be interpreted as a smaller request.
pub fn read_frame<R: Read>(reader: &mut R) -> Result<Option<Vec<u8>>, Code> {
    let mut header = [0u8; 4];
    if !read_exact_or_eof(reader, &mut header)? {
        return Ok(None);
    }
    let len = u32::from_be_bytes(header) as usize;
    if len > MAX_FRAME_BYTES {
        return Err(Code::FrameTooLarge);
    }
    let mut body = vec![0u8; len];
    if !read_exact_or_eof(reader, &mut body)? {
        return Err(Code::InvalidFrame);
    }
    Ok(Some(body))
}

/// Fill `buf` completely; `Ok(false)` means EOF before any byte was read.
fn read_exact_or_eof<R: Read>(reader: &mut R, buf: &mut [u8]) -> Result<bool, Code> {
    let mut filled = 0;
    while filled < buf.len() {
        match reader.read(&mut buf[filled..]) {
            Ok(0) => {
                return if filled == 0 {
                    Ok(false)
                } else {
                    Err(Code::InvalidFrame)
                };
            }
            Ok(n) => filled += n,
            Err(ref e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => return Err(Code::IoError),
        }
    }
    Ok(true)
}

/// Write one frame. The body is length-checked first so a response can never
/// exceed the bound the reader will accept.
pub fn write_frame<W: Write>(writer: &mut W, value: &serde_json::Value) -> Result<(), Code> {
    let body = serde_json::to_vec(value).map_err(|_| Code::Internal)?;
    if body.len() > MAX_FRAME_BYTES {
        return Err(Code::FrameTooLarge);
    }
    writer
        .write_all(&(body.len() as u32).to_be_bytes())
        .and_then(|()| writer.write_all(&body))
        .and_then(|()| writer.flush())
        .map_err(|_| Code::IoError)
}

/// Parse a frame body into a [`Request`].
pub fn parse_request(body: &[u8]) -> Result<Request, Code> {
    // Reject anything that is not UTF-8 JSON before serde sees it, so invalid
    // bytes cannot reach a parser with different assumptions.
    if std::str::from_utf8(body).is_err() {
        return Err(Code::InvalidFrame);
    }
    serde_json::from_slice::<Request>(body).map_err(|_| Code::InvalidFrame)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn framed(body: &[u8]) -> Vec<u8> {
        let mut out = (body.len() as u32).to_be_bytes().to_vec();
        out.extend_from_slice(body);
        out
    }

    /// `read_frame` takes any `Read`; `&[u8]` is one, `Vec<u8>` is not (it is a
    /// writer). This wraps a byte vector as a reader without copying.
    fn reader(bytes: &[u8]) -> &[u8] {
        bytes
    }

    #[test]
    fn round_trips_a_frame() {
        let input = framed(b"{\"id\":\"1\",\"op\":\"ping\"}");
        let mut source = reader(&input);
        let body = read_frame(&mut source).unwrap().unwrap();
        assert_eq!(body, b"{\"id\":\"1\",\"op\":\"ping\"}");
        assert!(read_frame(&mut source).unwrap().is_none());
    }

    #[test]
    fn an_oversized_length_is_refused_without_reading_the_body() {
        // Claim a 1 GiB body but provide none: the helper must refuse on the
        // prefix alone rather than trying to allocate or block for the body.
        let mut input = (1024u32 * 1024 * 1024).to_be_bytes().to_vec();
        input.push(b'{');
        let mut source = reader(&input);
        assert_eq!(read_frame(&mut source), Err(Code::FrameTooLarge));
    }

    #[test]
    fn a_truncated_body_is_not_parsed_as_a_smaller_request() {
        let mut input = (10u32).to_be_bytes().to_vec();
        input.extend_from_slice(b"{\"id\":");
        let mut source = reader(&input);
        assert_eq!(read_frame(&mut source), Err(Code::InvalidFrame));
    }

    #[test]
    fn a_half_written_length_prefix_is_not_eof() {
        let input = vec![0u8, 0u8];
        let mut source = reader(&input);
        assert_eq!(read_frame(&mut source), Err(Code::InvalidFrame));
    }

    #[test]
    fn unknown_fields_are_rejected() {
        let body = br#"{"id":"1","op":"stat","grant":"g","params":{},"exec":"rm -rf /"}"#;
        assert_eq!(parse_request(body), Err(Code::InvalidFrame));
    }

    #[test]
    fn non_utf8_is_rejected_before_parsing() {
        assert_eq!(parse_request(&[0xff, 0xfe, 0x00]), Err(Code::InvalidFrame));
    }

    #[test]
    fn a_request_without_an_id_still_gets_a_correlatable_reply_id() {
        let request = parse_request(br#"{"id":"","op":"stat"}"#).unwrap();
        assert_eq!(request.reply_id(), "(missing)");
    }

    #[test]
    fn outgoing_frames_are_bounded_too() {
        let huge =
            serde_json::json!({ "id": "1", "ok": true, "result": "x".repeat(MAX_FRAME_BYTES) });
        let mut sink: Vec<u8> = Vec::new();
        assert_eq!(write_frame(&mut sink, &huge), Err(Code::FrameTooLarge));
        assert!(
            sink.is_empty(),
            "a rejected frame must not be partially written"
        );
    }
}
