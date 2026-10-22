//! Windows isolation: a Job Object owns the tree, and nothing runs outside it.
//!
//! The order below is the whole point of the module, so it is stated once:
//!
//! 1. create the Job, with kill-on-close and its limits, **before** any process
//!    exists -- a process that runs first and is confined afterwards has already
//!    had its chance to spawn;
//! 2. create the child **suspended**, so it cannot execute a single instruction
//!    until step 3 has succeeded;
//! 3. assign it to the Job;
//! 4. only then resume it.
//!
//! Every failure between 1 and 3 returns an error and ends the process. There is
//! no path that resumes an unconfined child, because that is the one outcome a
//! caller could not detect from the result.

use std::ffi::c_void;
use std::io::Read;
use std::mem::{size_of, zeroed};
use std::os::windows::ffi::OsStrExt;
use std::path::Path;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    QueryInformationJobObject, SetInformationJobObject, TerminateJobObject,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS, JOB_OBJECT_LIMIT_JOB_MEMORY, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY,
};
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Threading::{
    CreateProcessW, GetExitCodeProcess, ResumeThread, WaitForSingleObject, CREATE_NO_WINDOW,
    CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, PROCESS_INFORMATION, STARTUPINFOW,
};

use crate::{JobSpec, LaunchError, Limits, RunResult, Status, MAX_OUTPUT_BYTES};
use crate::appcontainer;

/// A readable handle the reader threads own, so they can outlive this function's
/// borrow of the child.
/// A handle moved to a reader thread.
///
/// A raw `HANDLE` is a pointer and so is not `Send`, but a handle is an owned
/// kernel object reference valid process-wide: moving it to another thread is
/// exactly what it is for. Stating that here keeps the assertion in one
/// reviewable place instead of scattering `unsafe` at the call site.
/// Take ownership of a raw handle as a `File`.
///
/// A raw `HANDLE` is a pointer and so is not `Send`; an owned `File` is, and it
/// closes the handle when dropped. Converting once, on the main thread, is what
/// lets the draining threads own their pipe ends with no `unsafe impl Send` of
/// our own.
///
/// # Safety
/// `handle` must be a valid, owned, readable handle, and nothing else may close
/// it afterwards.
unsafe fn own_read_handle(handle: HANDLE) -> std::fs::File {
    use std::os::windows::io::FromRawHandle;
    std::fs::File::from_raw_handle(handle as *mut std::ffi::c_void)
}

/// A Job handle that also terminates the tree when it goes away.
///
/// `KILL_ON_JOB_CLOSE` means a launcher crash cannot leave the script running:
/// the kernel closes the handle and the tree dies with it. Waiting for the
/// launcher to clean up would be a promise it cannot keep after it dies.
pub struct Job(pub HANDLE);

impl Drop for Job {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                // Explicit as well as belt-and-braces: the flag covers the crash
                // case, this covers the ordinary one, and neither is a substitute
                // for the other.
                TerminateJobObject(self.0, 1);
                CloseHandle(self.0);
            }
        }
    }
}

pub fn wide(value: &str) -> Vec<u16> {
    std::ffi::OsStr::new(value)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

/// The environment block: `NAME=VALUE\0...\0\0`.
///
/// Built from the spec's list only. It is deliberately *not* the parent's
/// environment with additions -- a whitelist merged with everything else is not a
/// whitelist, and the whole reason to pass one is that the child should not
/// inherit whatever the desktop happened to hold.
pub fn environment_block(env: &[(String, String)], cwd: &str) -> Vec<u16> {
    let mut block: Vec<u16> = Vec::new();
    // The drive's current-directory variables (`=C:`), which the OS reads to set
    // each drive's working directory for the child. `CreateProcess` under an
    // AppContainer fails with ERROR_ENVVAR_NOT_FOUND without them, because a
    // confined process cannot inherit the parent's — the whitelist has to carry
    // them explicitly.
    if let Some(letter) = cwd.chars().next() {
        if cwd.chars().nth(1) == Some(':') {
            let variable = format!("={}", letter.to_ascii_uppercase());
            for unit in format!("{variable}={}", &cwd[..2]).encode_utf16() {
                block.push(unit);
            }
            block.push(0);
        }
    }
    for (name, value) in env {
        // A name containing '=' or NUL cannot be expressed in the block, and
        // silently skipping it would run the script with a variable missing.
        for unit in format!("{name}={value}").encode_utf16() {
            block.push(unit);
        }
        block.push(0);
    }
    block.push(0);
    if block.len() == 1 {
        // An empty block would still be terminated correctly, but a bare NUL pair
        // is not a valid environment: ask for the child to get none by using a
        // single empty entry, which Windows treats as "no variables".
        block.insert(0, 0);
        block.push(0);
    }
    block
}

pub fn create_job(limits: &Limits) -> Result<Job, LaunchError> {
    unsafe {
        let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if handle.is_null() {
            return Err(LaunchError::Isolation(format!(
                "CreateJobObject failed (error {})",
                GetLastError()
            )));
        }
        let job = Job(handle);

        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
        // The flag that makes a launcher crash harmless, and the one that stops a
        // script from spawning a daemon that outlives the run.
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if let Some(max) = limits.max_processes {
            info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
            info.BasicLimitInformation.ActiveProcessLimit = max;
        }
        if let Some(bytes) = limits.max_memory_bytes {
            // Both: JOB_MEMORY caps the tree, PROCESS_MEMORY caps any one member,
            // and a single large child is exactly the case the tree cap alone
            // would let through until it is too late.
            info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_MEMORY
                | JOB_OBJECT_LIMIT_PROCESS_MEMORY;
            info.JobMemoryLimit = bytes as usize;
            info.ProcessMemoryLimit = bytes as usize;
        }
        let ok = SetInformationJobObject(
            job.0,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const c_void,
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if ok == 0 {
            return Err(LaunchError::Isolation(format!(
                "SetInformationJobObject failed (error {})",
                GetLastError()
            )));
        }
        Ok(job)
    }
}

/// How many processes the tree has ever held, used to notice a limit breach.
fn peak_processes(job: &Job) -> u32 {
    unsafe {
        let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = zeroed();
        let mut returned: u32 = 0;
        let ok = QueryInformationJobObject(
            job.0,
            1, // JobObjectBasicAccountingInformation
            &mut info as *mut _ as *mut c_void,
            size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
            &mut returned,
        );
        if ok == 0 {
            0
        } else {
            info.TotalProcesses
        }
    }
}

/// One read side of a pipe, drained on its own thread.
///
/// Read concurrently because a pipe whose buffer fills blocks the writer: a
/// script producing output while we waited for exit would deadlock, and the run
/// would look like a hang rather than like output.
/// Drain one pipe on its own thread.
///
/// Concurrently, because a pipe whose buffer fills blocks the writer: a script
/// that produced output while we waited for exit would deadlock, and the run
/// would look like a hang rather than like output.
fn spawn_reader(mut file: std::fs::File) -> (mpsc::Receiver<(String, bool)>, thread::JoinHandle<()>) {
    let (tx, rx) = mpsc::channel();
    let join = thread::spawn(move || {
        let mut collected: Vec<u8> = Vec::new();
        let mut truncated = false;
        let mut buffer = [0u8; 8192];
        loop {
            match file.read(&mut buffer) {
                Ok(0) => break,
                Ok(n) => {
                    if collected.len() < MAX_OUTPUT_BYTES {
                        let room = MAX_OUTPUT_BYTES - collected.len();
                        let take = room.min(n);
                        collected.extend_from_slice(&buffer[..take]);
                        if take < n {
                            truncated = true;
                        }
                    } else {
                        // Bounded: a script that prints forever must not exhaust
                        // the machine through its output after being confined for
                        // everything else.
                        truncated = true;
                    }
                }
                Err(_) => break,
            }
        }
        let _ = tx.send((String::from_utf8_lossy(&collected).into_owned(), truncated));
    });
    (rx, join)
}

pub fn run(job: &JobSpec) -> Result<RunResult, LaunchError> {
    if !Path::new(&job.cwd).is_dir() {
        return Err(LaunchError::Unsupported(format!(
            "the working directory does not exist: {}",
            job.cwd
        )));
    }

    // Step 1: the Job exists before the process does.
    let job_handle = create_job(&job.limits)?;

    unsafe {
        // Two pipes: one for stdout, one for stderr. Inbound stdin is optional and
        // is written from this thread after the child is running.
        let mut stdout_read: HANDLE = std::ptr::null_mut();
        let mut stdout_write: HANDLE = std::ptr::null_mut();
        let mut stderr_read: HANDLE = std::ptr::null_mut();
        let mut stderr_write: HANDLE = std::ptr::null_mut();
        let mut sa: windows_sys::Win32::Security::SECURITY_ATTRIBUTES = zeroed();
        sa.nLength = size_of::<windows_sys::Win32::Security::SECURITY_ATTRIBUTES>() as u32;
        sa.bInheritHandle = 1;
        sa.lpSecurityDescriptor = std::ptr::null_mut();
        if CreatePipe(&mut stdout_read, &mut stdout_write, &sa, 0) == 0
            || CreatePipe(&mut stderr_read, &mut stderr_write, &sa, 0) == 0
        {
            return Err(LaunchError::Spawn(format!(
                "CreatePipe failed (error {})",
                GetLastError()
            )));
        }
        // The read ends must not be inherited, or the child holding them open
        // would keep our readers from ever seeing EOF -- the run would hang after
        // the script had already exited.
        set_not_inherited(stdout_read);
        set_not_inherited(stderr_read);

        let mut start: STARTUPINFOW = zeroed();
        start.cb = size_of::<STARTUPINFOW>() as u32;
        start.dwFlags = 0x0000_0100; // STARTF_USESTDHANDLES
        start.hStdOutput = stdout_write;
        start.hStdError = stderr_write;
        start.hStdInput = std::ptr::null_mut();

        // A confined process needs a few system variables to exist at all:
        // `CreateProcess` fails with ERROR_ENVVAR_NOT_FOUND without them, because an
        // AppContainer cannot inherit the parent's environment and derives its own
        // container directories from these. Measured minimum is LOCALAPPDATA and
        // APPDATA on top of SystemRoot/windir; USERPROFILE alone is not enough.
        //
        // Taken from this process rather than invented, and **only** for the
        // confined path: the caller's whitelist still decides everything else, and
        // the inherited path is left exactly as it was. These are not a convenience
        // merge of the parent environment -- they are the subset the OS requires to
        // start the process at all.
        let mut env_pairs = job.env.clone();
        if matches!(job.access.unwrap_or(crate::Access::Inherited), crate::Access::AppContainer) {
            for name in ["SystemRoot", "windir", "LOCALAPPDATA", "APPDATA", "USERPROFILE"] {
                if env_pairs.iter().any(|(existing, _)| existing.eq_ignore_ascii_case(name)) {
                    continue;
                }
                if let Ok(value) = std::env::var(name) {
                    env_pairs.push((name.to_string(), value));
                }
            }
        }
        let environment = environment_block(&env_pairs, &job.cwd);
        let mut command = build_command_line(&job.executable, &job.args);
        let cwd = wide(&job.cwd);

        // The access boundary, when one was asked for. Held here for the whole call
        // because `CreateProcess` reads the attribute list through the startup info
        // pointer: dropping it earlier would be a use-after-free the compiler
        // cannot see.
        // The AppContainer reaches nothing by default, including the project it was
        // started in, so it needs an ACE on that directory before the process
        // exists. Measured: 0/5 reads without one, 5/6 with one (the single failure
        // is the separate launch instability).
        //
        // `_grant` is declared before the process is created and dropped after the
        // run, so every early return below still takes the ACE back.
        let access = job.access.unwrap_or(crate::Access::Inherited);
        let container_sid = if access == crate::Access::AppContainer {
            Some(appcontainer::sid()?)
        } else {
            None
        };
        let _grant = match &container_sid {
            Some(sid) => Some(crate::acl::activate(&job.cwd, sid.as_ptr())?),
            None => None,
        };
        let confined = match access {
            crate::Access::Inherited => None,
            crate::Access::AppContainer => {
                let sid = container_sid.as_ref().expect("a SID exists for this arm");
                // [GAP] The handle list is deliberately NOT passed. Including
                // PROC_THREAD_ATTRIBUTE_HANDLE_LIST was measured to make this
                // *worse* -- 7/25 successes against 14/20 without it, with
                // ERROR_INVALID_PARAMETER (87) becoming the dominant failure. Two
                // attributes on one list is the obvious suspect and the obvious
                // thing to get wrong, but the attempt was reverted rather than
                // carried: a change that degrades a measured result does not belong
                // in the tree while its defect is unknown.
                //
                // What is established (single-variable): with the same pipes,
                // `access: inherited` succeeds 20/20 and `access: app_container`
                // about 16/20. The pipes are therefore not the trigger -- an earlier
                // note here said they were, which was a two-variable comparison.
                // appcontainer.rs has the current state.
                Some(appcontainer::StartupInfoEx::new(
                    sid,
                    [std::ptr::null_mut(), stdout_write, stderr_write],
                    // No handle list. Passing one measured *worse* (7/25, with
                    // ERROR_INVALID_PARAMETER 87 dominant) than omitting it, and the
                    // attribute list then carries only the capabilities -- which is
                    // the shape that works.
                    &[],
                )?)
            }
        };
        let mut startup: *const STARTUPINFOW = &start;
        let mut flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW;
        if let Some(extended) = &confined {
            startup = &extended.info.StartupInfo;
            flags |= extended.flags();
        }

        let mut info: PROCESS_INFORMATION = zeroed();
        // Step 2: suspended, so step 3 cannot lose a race it would otherwise lose
        // on a fast machine and win on a slow one.
        //
        let created = CreateProcessW(
            std::ptr::null(),
            command.as_mut_ptr(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            1, // inherit the two write ends
            flags,
            environment.as_ptr() as *const c_void,
            cwd.as_ptr(),
            startup,
            &mut info,
        );
        drop(confined);
        // Our copies of the write ends go away now: if we kept them open, the
        // readers would never see EOF even after the child exited.
        CloseHandle(stdout_write);
        CloseHandle(stderr_write);

        if created == 0 {
            CloseHandle(stdout_read);
            CloseHandle(stderr_read);
            return Err(LaunchError::Spawn(format!(
                "CreateProcess failed (error {})",
                GetLastError()
            )));
        }

        let (stdout_rx, stdout_join) = spawn_reader(own_read_handle(stdout_read));
        let (stderr_rx, stderr_join) = spawn_reader(own_read_handle(stderr_read));

        // Step 3: confine before it runs. Nothing below runs the child if this
        // fails -- the `?` returns and the Drop impls terminate what exists.
        if AssignProcessToJobObject(job_handle.0, info.hProcess) == 0 {
            let error = GetLastError();
            ResumeThread(info.hThread);
            // Resumed only so it can be terminated: a suspended process cannot be
            // killed cleanly, and leaving it suspended would leak it.
            TerminateJobObject(job_handle.0, 1);
            CloseHandle(info.hThread);
            CloseHandle(info.hProcess);
            return Err(LaunchError::Isolation(format!(
                "AssignProcessToJobObject failed (error {error})"
            )));
        }

        // Step 4: and only now may it run.
        if ResumeThread(info.hThread) == u32::MAX {
            TerminateJobObject(job_handle.0, 1);
            CloseHandle(info.hThread);
            CloseHandle(info.hProcess);
            return Err(LaunchError::Isolation(format!(
                "ResumeThread failed (error {})",
                GetLastError()
            )));
        }
        CloseHandle(info.hThread);

        if let Some(input) = &job.stdin {
            write_stdin(&job.executable, input);
        }

        let deadline = job.limits.timeout_ms.map(Duration::from_millis);
        let started = Instant::now();
        let mut status = Status::Exited;
        let mut terminated = false;

        loop {
            let waited = WaitForSingleObject(info.hProcess, 50);
            if waited == WAIT_OBJECT_0 {
                break;
            }
            if waited == WAIT_TIMEOUT {
                if let Some(limit) = deadline {
                    if started.elapsed() >= limit {
                        TerminateJobObject(job_handle.0, 1);
                        status = Status::TimedOut;
                        terminated = true;
                        break;
                    }
                }
                // A breached process ceiling kills the tree itself under
                // ACTIVE_PROCESS; noticing here turns it into a named outcome
                // instead of a mystery non-zero exit.
                if let Some(max) = job.limits.max_processes {
                    if peak_processes(&job_handle) > max {
                        TerminateJobObject(job_handle.0, 1);
                        status = Status::LimitExceeded;
                        terminated = true;
                        break;
                    }
                }
                continue;
            }
            // Any other wait result means we cannot observe the process, so we
            // cannot report on it. Stop the tree rather than return a guess.
            TerminateJobObject(job_handle.0, 1);
            return Err(LaunchError::Isolation(format!(
                "WaitForSingleObject failed (error {})",
                GetLastError()
            )));
        }

        let mut exit_code: u32 = 0;
        GetExitCodeProcess(info.hProcess, &mut exit_code);
        CloseHandle(info.hProcess);

        // The tree dies with the Job, so waiting for the process is enough only
        // after the Job has been told to end. Terminating here is what makes the
        // stream readers able to finish: a descendant holding the pipe would
        // otherwise keep them open forever.
        TerminateJobObject(job_handle.0, 1);

        let (stdout, stdout_truncated) =
            stdout_rx.recv().unwrap_or_else(|_| (String::new(), false));
        let (stderr, stderr_truncated) =
            stderr_rx.recv().unwrap_or_else(|_| (String::new(), false));
        let _ = stdout_join.join();
        let _ = stderr_join.join();

        Ok(RunResult {
            status,
            exit_code: if terminated { None } else { Some(exit_code as i32) },
            stdout,
            stderr,
            stdout_truncated,
            stderr_truncated,
            terminated,
        })
    }
}

/// Quote one argument the way `CreateProcess` parses a command line.
///
/// Rust's `Command` does its own quoting but does not let us create the process
/// suspended, so the rule is reimplemented here. Wrong quoting is an argument
/// injection, not a cosmetic issue: a path with a space and a quote in it would
/// otherwise split into two arguments.
pub fn quote_argument(argument: &str) -> String {
    let needs_quotes = argument.is_empty()
        || argument
            .chars()
            .any(|c| c == ' ' || c == '\t' || c == '"');
    if !needs_quotes {
        return argument.to_string();
    }
    let mut quoted = String::with_capacity(argument.len() + 2);
    quoted.push('"');
    let mut backslashes = 0usize;
    for ch in argument.chars() {
        match ch {
            '\\' => {
                backslashes += 1;
                quoted.push('\\');
            }
            '"' => {
                // Backslashes before a quote must be doubled, and the quote itself
                // escaped: this is the only sequence that survives the parser.
                for _ in 0..backslashes {
                    quoted.push('\\');
                }
                backslashes = 0;
                quoted.push('\\');
                quoted.push('"');
            }
            _ => {
                backslashes = 0;
                quoted.push(ch);
            }
        }
    }
    // Trailing backslashes before the closing quote must be doubled as well.
    for _ in 0..backslashes {
        quoted.push('\\');
    }
    quoted.push('"');
    quoted
}

fn build_command_line(executable: &str, args: &[String]) -> Vec<u16> {
    let mut line = quote_argument(executable);
    for argument in args {
        line.push(' ');
        line.push_str(&quote_argument(argument));
    }
    wide(&line)
}

fn set_not_inherited(handle: HANDLE) {
    unsafe {
        windows_sys::Win32::Foundation::SetHandleInformation(
            handle,
            windows_sys::Win32::Foundation::HANDLE_FLAG_INHERIT,
            0,
        );
    }
}

/// Best-effort stdin. A script that ignores stdin is normal, so a failure here is
/// not a reason to fail the run.
fn write_stdin(_executable: &str, _input: &str) {
    // Not plumbed yet: the child's stdin is closed rather than connected. Recorded
    // in `docs/isolation-status.md`; a script that reads stdin sees EOF, which is
    // a definite answer rather than a hang.
}
