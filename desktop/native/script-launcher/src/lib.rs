//! Native isolated launcher for desktop project scripts (design D8).
//!
//! What this is for, in one sentence: running a script in the user's project must
//! not be able to reach anything outside that project, spawn something that
//! outlives the run, or consume the machine.
//!
//! Two mechanisms, and they are not interchangeable (D8):
//!
//! * a **Job Object** owns the process tree and its resource limits, so a cancel
//!   can end every descendant and a runaway cannot exhaust memory or processes;
//! * an **access boundary** (LPAC / AppContainer) owns *what the process can
//!   reach*. `docs/isolation-status.md` records that this half is not implemented
//!   yet -- see [`Access`].
//!
//! The launcher refuses rather than degrading. If the Job cannot be created, or
//! the process cannot be assigned to it before it runs, the run does not happen:
//! "isolation unavailable" must never silently become "ran it anyway", because
//! that is the one failure a user cannot detect from the result.
//!
//! ## Not implemented yet (recorded so it is not mistaken for done)
//!
//! * **LPAC / AppContainer access boundary.** [`Access`] exists as a type and is
//!   always [`Access::Inherited`]; no restricted token is created and no
//!   AppContainer is entered. Until that lands, containment is *process-tree and
//!   resource* only -- the script still runs with this user's own file access.
//! * **ACL lifecycle** (tasks 7.2/7.3): nothing here adds, tracks or restores an
//!   ACE, so there is nothing to restore.
//! * **Runtime pinning** (task 7.1): the caller names the executable. Choosing a
//!   bundled Python/PowerShell over one on `PATH` is a separate decision.

use serde::{Deserialize, Serialize};

#[cfg(windows)]
mod acl;
#[cfg(windows)]
mod appcontainer;
#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub mod wrap;

/// How the launched process is confined *for access* (not for resources).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Access {
    /// The process inherits this user's own access. **This is what runs today.**
    ///
    /// Named explicitly rather than being an unstated default: a caller that wants
    /// a boundary must be able to see that it did not get one.
    Inherited,
    /// Confine with an AppContainer (LPAC). Not implemented; asking for it is a
    /// refusal, not a silent downgrade to [`Access::Inherited`].
    AppContainer,
}

/// Resource ceilings for one run. Absent members are left at the Job's default.
#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize)]
pub struct Limits {
    /// Kill the tree after this many milliseconds of wall time.
    pub timeout_ms: Option<u64>,
    /// Maximum simultaneously active processes in the tree.
    pub max_processes: Option<u32>,
    /// Maximum committed memory for the whole tree, in bytes.
    pub max_memory_bytes: Option<u64>,
}

/// One script run the launcher is asked to perform.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobSpec {
    /// Absolute path of the interpreter to run. The caller resolves it; the
    /// launcher does not search `PATH`, because a launcher that searched would be
    /// the place an unknown executable could enter.
    pub executable: String,
    /// Arguments after the executable.
    #[serde(default)]
    pub args: Vec<String>,
    /// The project directory, used as the working directory.
    pub cwd: String,
    /// The complete environment for the child. Not merged with this process's
    /// environment: a whitelist that is unioned with everything the parent had is
    /// not a whitelist.
    #[serde(default)]
    pub env: Vec<(String, String)>,
    #[serde(default)]
    pub access: Option<Access>,
    #[serde(default)]
    pub limits: Limits,
    /// Standard input, if any.
    #[serde(default)]
    pub stdin: Option<String>,
}

/// How a run ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    /// The process exited on its own and its tree ended.
    Exited,
    /// The deadline passed and the tree was terminated.
    TimedOut,
    /// A ceiling was hit (process count or memory) and the tree was terminated.
    LimitExceeded,
}

/// The outcome of one run.
#[derive(Debug, Serialize, Deserialize)]
pub struct RunResult {
    pub status: Status,
    /// The process's exit code, when it exited rather than being terminated.
    pub exit_code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    /// True when a stream was cut off at [`MAX_OUTPUT_BYTES`] rather than ending.
    #[serde(default)]
    pub stdout_truncated: bool,
    #[serde(default)]
    pub stderr_truncated: bool,
    /// True when the tree was terminated because the deadline or a limit was hit,
    /// so a caller can tell "the script failed" from "we stopped it".
    pub terminated: bool,
}

impl RunResult {
    /// Whether this run produced a usable script result at all.
    pub fn produced_a_result(&self) -> bool {
        !self.terminated
    }
}

/// Why a run could not be started at all.
///
/// Separate from a non-zero exit code on purpose: this is "the launcher refused",
/// which is not a script result and must not be reported as one.
#[derive(Debug)]
pub enum LaunchError {
    /// The requested configuration cannot be honoured (e.g. an unimplemented
    /// access boundary, a missing working directory).
    Unsupported(String),
    /// The job could not be created or the process could not be confined.
    Isolation(String),
    /// Starting the process failed.
    Spawn(String),
    /// Reading the result failed.
    Io(String),
}

impl std::fmt::Display for LaunchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LaunchError::Unsupported(m) => write!(f, "unsupported: {m}"),
            LaunchError::Isolation(m) => write!(f, "isolation unavailable: {m}"),
            LaunchError::Spawn(m) => write!(f, "could not start: {m}"),
            LaunchError::Io(m) => write!(f, "io: {m}"),
        }
    }
}

impl std::error::Error for LaunchError {}

/// How much of each stream is kept.
///
/// Bounded because both are read into memory: a script that prints forever must
/// not be able to exhaust the machine through its *output* after being confined
/// for everything else.
pub const MAX_OUTPUT_BYTES: usize = 1 << 20;

/// Run one job under the strongest isolation this build can provide.
pub fn run(job: &JobSpec) -> Result<RunResult, LaunchError> {
    // No longer refused: the boundary is implemented (see `appcontainer`). What a
    // caller must still know is that **nothing is granted back yet** -- a confined
    // child cannot read its own working directory until task 7.2 adds the ACE -- so
    // `Access::AppContainer` is honest and also, today, unusable for a working run.
    // That state is recorded in `docs/isolation-status.md` rather than hidden behind
    // a refusal, because the refusal would now be a lie about the code.
    platform::run(job)
}

#[cfg(windows)]
use windows as platform;

#[cfg(not(windows))]
mod platform_stub;

#[cfg(not(windows))]
use platform_stub as platform;
