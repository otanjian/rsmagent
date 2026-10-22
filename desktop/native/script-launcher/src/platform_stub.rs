//! Non-Windows placeholder: the isolation this crate provides is Windows-specific.
//!
//! macOS and Linux already have their own confinement arrangements (the desktop
//! ships a POSIX file helper and its scripts run through the existing frozen
//! worker), and neither is what design D8 specifies. Rather than pretend to
//! isolate, this refuses -- so a POSIX caller asking for a Job-backed run is told
//! the truth instead of silently getting no confinement.

use crate::{JobSpec, LaunchError, RunResult};

pub fn run(_job: &JobSpec) -> Result<RunResult, LaunchError> {
    Err(LaunchError::Unsupported(
        "the native script launcher is implemented for Windows only".into(),
    ))
}
