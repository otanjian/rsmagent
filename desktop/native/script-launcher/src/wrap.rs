//! Transparent wrapper mode: run a command under a Job Object and get out of the way.
//!
//! This is the shape the desktop actually needs. The worker the desktop launches is
//! a long-lived JSON-RPC peer: the parent writes requests to its stdin and reads
//! answers from its stdout for the whole session. So the wrapper must **not**
//! interpret those streams -- it inherits its own standard handles and hands them
//! to the child untouched, which leaves the existing protocol exactly as it was and
//! changes only what confines the process tree.
//!
//! What it replaces, and why that matters: the desktop currently kills a worker's
//! tree with `taskkill /T`, which walks the live process table. A worker that
//! spawned something which outlived it, or that died between the two, leaves
//! whatever `taskkill` could not reach. A Job Object does not have that failure
//! mode -- `KILL_ON_JOB_CLOSE` means the kernel ends every member when the last
//! handle closes, including when *this* process is killed and never runs cleanup.
//!
//! Deliberately not the same entry point as the JSON one in `main.rs`: that one
//! is a single request/response pair, and reusing it here would mean parsing a
//! protocol this mode exists to stay out of.

use std::ffi::{c_void, OsStr};
use std::mem::{size_of, zeroed};
use std::path::Path;

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows_sys::Win32::System::JobObjects::TerminateJobObject;
use windows_sys::Win32::System::Console::{
    GetStdHandle, STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE,
};
use windows_sys::Win32::System::Threading::{
    CreateProcessW, GetExitCodeProcess, ResumeThread, WaitForSingleObject, CREATE_NO_WINDOW,
    CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, PROCESS_INFORMATION, STARTUPINFOW,
};

use crate::windows::{create_job, environment_block, quote_argument, wide};
use crate::{JobSpec, LaunchError, Limits, RunResult, Status};

/// Run `executable args...` in `cwd` under a Job, passing stdio straight through.
///
/// Returns the child's exit code, and also fills in a [`RunResult`] so a caller
/// that wants the same shape as the JSON mode can have it. The streams are **not**
/// captured here: they went to this process's own stdout/stderr, which is the point.
pub fn run_wrapped(
    executable: &str,
    args: &[String],
    cwd: &str,
    env: &[(String, String)],
    limits: Limits,
) -> Result<(RunResult, u32), LaunchError> {
    if !Path::new(cwd).is_dir() {
        return Err(LaunchError::Unsupported(format!(
            "the working directory does not exist: {cwd}"
        )));
    }

    // Step 1: the Job exists before the process does, so there is no instant at
    // which an unconfined child could run.
    let job = create_job(&limits)?;

    unsafe {
        let mut start: STARTUPINFOW = zeroed();
        start.cb = size_of::<STARTUPINFOW>() as u32;
        // Hand our standard handles to the child **explicitly**.
        //
        // Relying on plain inheritance is not enough: a child only receives handles
        // that are marked inheritable, and this process's stdout is not necessarily
        // one of them -- a caller that launched us with a non-inheritable pipe
        // (measured: PowerShell) leaves the child with a dead stdout, and the output
        // disappears while the exit code still looks correct. Naming them and
        // setting STARTF_USESTDHANDLES is what makes the pass-through unconditional.
        start.dwFlags = 0x0000_0100; // STARTF_USESTDHANDLES
        start.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
        start.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
        start.hStdError = GetStdHandle(STD_ERROR_HANDLE);

        let mut command = build_command_line(executable, args);
        let cwd_wide = wide(cwd);
        let mut environment = environment_block(env, cwd);
        let environment = environment.as_mut_ptr() as *const c_void;

        let mut info: PROCESS_INFORMATION = zeroed();
        // Step 2: suspended.
        let created = CreateProcessW(
            std::ptr::null(),
            command.as_mut_ptr(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            1, // inherit handles: the child needs ours
            CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
            environment,
            cwd_wide.as_ptr(),
            &start,
            &mut info,
        );
        if created == 0 {
            return Err(LaunchError::Spawn(format!(
                "CreateProcess failed (error {})",
                GetLastError()
            )));
        }

        // Step 3: confine before it runs.
        if windows_sys::Win32::System::JobObjects::AssignProcessToJobObject(job.0, info.hProcess)
            == 0
        {
            let error = GetLastError();
            ResumeThread(info.hThread);
            TerminateJobObject(job.0, 1);
            CloseHandle(info.hThread);
            CloseHandle(info.hProcess);
            return Err(LaunchError::Isolation(format!(
                "AssignProcessToJobObject failed (error {error})"
            )));
        }

        // Step 4: and only now may it run.
        if ResumeThread(info.hThread) == u32::MAX {
            TerminateJobObject(job.0, 1);
            CloseHandle(info.hThread);
            CloseHandle(info.hProcess);
            return Err(LaunchError::Isolation(format!(
                "ResumeThread failed (error {})",
                GetLastError()
            )));
        }
        CloseHandle(info.hThread);

        // The child owns its own lifetime: wait for it however long it takes, or
        // until the deadline. No output is read here -- it went to the inherited
        // handles -- so there is nothing that could block.
        let deadline = limits.timeout_ms.map(std::time::Duration::from_millis);
        let started = std::time::Instant::now();
        let mut status = Status::Exited;
        let mut terminated = false;
        loop {
            let waited = WaitForSingleObject(info.hProcess, 100);
            if waited == WAIT_OBJECT_0 {
                break;
            }
            if waited == WAIT_TIMEOUT {
                if let Some(limit) = deadline {
                    if started.elapsed() >= limit {
                        TerminateJobObject(job.0, 1);
                        status = Status::TimedOut;
                        terminated = true;
                        break;
                    }
                }
                continue;
            }
            TerminateJobObject(job.0, 1);
            CloseHandle(info.hProcess);
            return Err(LaunchError::Isolation(format!(
                "WaitForSingleObject failed (error {})",
                GetLastError()
            )));
        }

        let mut exit_code: u32 = 0;
        GetExitCodeProcess(info.hProcess, &mut exit_code);
        CloseHandle(info.hProcess);
        // End the tree explicitly as well as via `KILL_ON_JOB_CLOSE`: a descendant
        // that outlived the child would otherwise keep running until this process
        // exits, which for a wrapper is the whole session.
        TerminateJobObject(job.0, 1);

        Ok((
            RunResult {
                status,
                exit_code: if terminated { None } else { Some(exit_code as i32) },
                stdout: String::new(),
                stderr: String::new(),
                stdout_truncated: false,
                stderr_truncated: false,
                terminated,
            },
            exit_code,
        ))
    }
}

/// The environment a wrapped command runs with: the caller's list, plus the system
/// variables Windows needs to start a process at all.
///
/// Same reasoning as the JSON mode: a parent that passes a whitelist still cannot
/// express `SystemRoot`, and without it the child does not start. Kept in one place
/// so the two modes cannot disagree about what "the environment" means.
pub fn wrap_environment(caller: &[(String, String)]) -> Vec<(String, String)> {
    let mut pairs = caller.to_vec();
    for name in ["SystemRoot", "windir", "PATH"] {
        if pairs.iter().any(|(existing, _)| existing.eq_ignore_ascii_case(name)) {
            continue;
        }
        if let Ok(value) = std::env::var(name) {
            pairs.push((name.to_string(), value));
        }
    }
    pairs
}

fn build_command_line(executable: &str, args: &[String]) -> Vec<u16> {
    let mut line = quote_argument(executable);
    for argument in args {
        line.push(' ');
        line.push_str(&quote_argument(argument));
    }
    wide(&line)
}

/// The job spec a wrapper invocation is equivalent to, for logging and reuse.
pub fn spec_for(executable: &str, args: &[String], cwd: &str, limits: Limits) -> JobSpec {
    JobSpec {
        executable: executable.to_string(),
        args: args.to_vec(),
        cwd: cwd.to_string(),
        env: wrap_environment(&[]),
        access: Some(crate::Access::Inherited),
        limits,
        stdin: None,
    }
}

// Kept so the module compiles on a non-Windows host for the type-checking build.
#[allow(dead_code)]
fn _unused(_: &OsStr) {}
