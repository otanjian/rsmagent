//! The AppContainer (LPAC) access boundary (design D8).
//!
//! This is the half the Job Object does not provide. A Job owns the process tree
//! and its resource ceilings; it says nothing about *what the process can reach*.
//! An AppContainer token says that, and by default an AppContainer reaches almost
//! nothing: no user profile, no other project, no credentials, no network unless a
//! capability is granted.
//!
//! ## What this does, and what it deliberately leaves out
//!
//! Implemented: derive a SID for a named AppContainer profile, create the profile,
//! and launch the child inside it.
//!
//! **[GAP] Nothing is granted back yet.** A confined process cannot read the user's
//! project until an ACE on that directory names the AppContainer SID, and that is
//! task 7.2's ACL lifecycle -- which D8 makes a hard prerequisite precisely because
//! a badly-restored ACE damages the user's directory. So with this module alone the
//! child starts confined and then fails to reach its own working directory.
//!
//! **[GAP] The launch is intermittent, and the trigger is the AppContainer itself.**
//! Measured, single-variable: with the *same* pipes, `access: inherited` succeeds
//! 20/20 and `access: app_container` succeeds about 16/20, failing with
//! `ERROR_NOT_APPCONTAINER` (4250) and `ERROR_GEN_FAILURE` (31). An earlier note
//! here blamed the pipes; that was a two-variable comparison (JSON+AppContainer
//! against wrap+inherited) and the attribution was wrong -- pipes are present in
//! the 20/20 row too.
//!
//! `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` is passed and is not the answer: with the
//! attribute count correct and the handle array held for the list lifetime, the
//! rate is unchanged. So the open question is not which handle setting is wrong
//! but **why the same `SECURITY_CAPABILITIES` attribute is sometimes honoured and
//! sometimes not**. See `docs/isolation-status.md`.
//!
//! **[GAP] No capability SIDs.** Network, and anything reached through a capability
//! rather than an ACE, is therefore unavailable -- the intended default, but it
//! means `internetClient` is never *granted* rather than explicitly denied.
//!
//! **[GAP] Profile lifetime.** Profiles are created per run and never deleted here;
//! repeated runs accumulate them. Task 7.3 owns cleanup.

use std::ffi::c_void;
use std::mem::{size_of, zeroed};
use std::os::windows::ffi::OsStrExt;
use std::ptr;

use windows_sys::Win32::Foundation::{GetLastError, LocalFree, HANDLE};
use windows_sys::Win32::Security::Isolation::{
    CreateAppContainerProfile, DeriveAppContainerSidFromAppContainerName,
};
use windows_sys::Win32::Security::{SECURITY_CAPABILITIES, SID_AND_ATTRIBUTES};
use windows_sys::Win32::System::Threading::{
    DeleteProcThreadAttributeList, InitializeProcThreadAttributeList,
    UpdateProcThreadAttribute, EXTENDED_STARTUPINFO_PRESENT, LPPROC_THREAD_ATTRIBUTE_LIST,
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
    STARTUPINFOEXW,
};

use crate::LaunchError;

/// The profile every run shares.
///
/// One name rather than one per run: the SID is what an ACE would name, so a fresh
/// SID per run would mean a fresh ACE per run -- and the whole of task 7.2 is about
/// not spraying ACEs across a user's directory. One profile keeps the eventual grant
/// to a single identity.
pub const PROFILE_NAME: &str = "cow-project-script";

/// A derived AppContainer SID, freed on drop.
pub struct AppContainerSid(*mut c_void);

impl Drop for AppContainerSid {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { LocalFree(self.0 as *mut c_void) };
        }
    }
}

impl AppContainerSid {
    pub fn as_ptr(&self) -> *mut c_void {
        self.0
    }
}

/// The SID of the project-script AppContainer, creating the profile if needed.
///
/// Creation is attempted first and its result is deliberately not fatal: the profile
/// exists after the first run, and `ERROR_ALREADY_EXISTS` is the normal case from
/// then on. The derive call is what has to succeed, because it produces the SID.
pub fn sid() -> Result<AppContainerSid, LaunchError> {
    unsafe {
        let name = wide(PROFILE_NAME);
        let display = wide("RongAI project script");
        let description = wide("Runs project scripts with no access outside them");
        let mut created: *mut c_void = ptr::null_mut();
        // The return value is ignored on purpose: a profile that already exists is
        // the expected steady state, and treating it as an error would fail the
        // second run of every session.
        let _ = CreateAppContainerProfile(
            name.as_ptr(),
            display.as_ptr(),
            description.as_ptr(),
            ptr::null_mut(),
            0,
            &mut created,
        );
        if !created.is_null() {
            LocalFree(created as *mut c_void);
        }

        let mut derived: *mut c_void = ptr::null_mut();
        let hr = DeriveAppContainerSidFromAppContainerName(name.as_ptr(), &mut derived);
        if hr != 0 || derived.is_null() {
            return Err(LaunchError::Isolation(format!(
                "DeriveAppContainerSidFromAppContainerName failed (0x{:08X})",
                hr as u32
            )));
        }
        Ok(AppContainerSid(derived))
    }
}

/// An attribute list carrying the AppContainer capability for `CreateProcess`.
pub struct AttributeList {
    /// Owned only: the list points into this, so it must outlive `list` and must
    /// never be moved. Never read directly.
    _buffer: Vec<u8>,
    /// The `SECURITY_CAPABILITIES` the attribute points at, boxed.
    ///
    /// Boxed rather than a field by value, and kept alive here rather than as a
    /// local, because `UpdateProcThreadAttribute` stores the *pointer*: a local would
    /// be a use-after-free that shows up only as `CreateProcess` failing with
    /// `ERROR_INVALID_PARAMETER`, since the kernel reads it after the function that
    /// built the list has already returned.
    _capabilities: Box<SECURITY_CAPABILITIES>,
    /// The handles named in `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`, kept alive for the
    /// same reason as `_capabilities`: the attribute stores the *pointer*, so the
    /// array must outlive the list or the kernel reads freed memory.
    _handles: Vec<HANDLE>,
    list: LPPROC_THREAD_ATTRIBUTE_LIST,
}

impl Drop for AttributeList {
    fn drop(&mut self) {
        if !self.list.is_null() {
            unsafe { DeleteProcThreadAttributeList(self.list) };
        }
    }
}

/// Build the `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` attribute.
///
/// Two passes, because the API requires it: the first asks for the buffer size, the
/// second fills it. Doing it in one pass is the classic way to get a list that
/// silently lacks the attribute.
pub fn attribute_list(
    sid: &AppContainerSid,
    handles: &[HANDLE],
) -> Result<AttributeList, LaunchError> {
    unsafe {
        // No capabilities: every capability is a thing the script may reach, and the
        // first batch needs none. Adding one later is a deliberate act.
        let mut capabilities = Box::new(SECURITY_CAPABILITIES {
            AppContainerSid: sid.as_ptr(),
            Capabilities: ptr::null_mut::<SID_AND_ATTRIBUTES>(),
            CapabilityCount: 0,
            Reserved: 0,
        });

        let mut size: usize = 0;
        // One attribute for the capabilities, one for the handle list when there is
        // one. **Both passes must use the same count**: sizing for a different number
        // than you set is how the second attribute silently fails to fit.
        let count: u32 = if handles.is_empty() { 1 } else { 2 };
        InitializeProcThreadAttributeList(ptr::null_mut(), count, 0, &mut size);
        if size == 0 {
            return Err(LaunchError::Isolation(format!(
                "InitializeProcThreadAttributeList could not size the list (error {})",
                GetLastError()
            )));
        }
        let mut buffer = vec![0u8; size];
        let list = buffer.as_mut_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST;
        if InitializeProcThreadAttributeList(list, count, 0, &mut size) == 0 {
            return Err(LaunchError::Isolation(format!(
                "InitializeProcThreadAttributeList failed (error {})",
                GetLastError()
            )));
        }
        if UpdateProcThreadAttribute(
            list,
            0,
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES as usize,
            capabilities.as_mut() as *mut SECURITY_CAPABILITIES as *const c_void,
            size_of::<SECURITY_CAPABILITIES>(),
            ptr::null_mut(),
            ptr::null_mut(),
        ) == 0
        {
            DeleteProcThreadAttributeList(list);
            return Err(LaunchError::Isolation(format!(
                "UpdateProcThreadAttribute failed (error {})",
                GetLastError()
            )));
        }
        // Only the handles the child needs. Without this list, `bInheritHandles`
        // offers it *every* inheritable handle in this process, and an AppContainer
        // cannot open most of them -- which is why this launch failed about a third
        // of the time and succeeded when there were no extra handles to inherit.
        if !handles.is_empty() {
            if UpdateProcThreadAttribute(
                list,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                handles.as_ptr() as *const c_void,
                handles.len() * size_of::<HANDLE>(),
                ptr::null_mut(),
                ptr::null_mut(),
            ) == 0
            {
                DeleteProcThreadAttributeList(list);
                return Err(LaunchError::Isolation(format!(
                    "UpdateProcThreadAttribute(HANDLE_LIST) failed (error {})",
                    GetLastError()
                )));
            }
        }
        Ok(AttributeList { _buffer: buffer, _capabilities: capabilities, _handles: handles.to_vec(), list })
    }
}

/// A `STARTUPINFOEXW` pointing at an attribute list.
///
/// Held by value so the caller keeps it alive across the whole `CreateProcess` call:
/// the OS reads the list through this pointer during that call, and a temporary
/// would be a use-after-free the compiler cannot see.
pub struct StartupInfoEx {
    pub info: STARTUPINFOEXW,
    _list: AttributeList,
    /// `lpDesktop` points here, so the string must outlive `info`.
    _desktop: Vec<u16>,
}

impl StartupInfoEx {
    pub fn new(sid: &AppContainerSid, std_handles: [HANDLE; 3], inherit: &[HANDLE]) -> Result<Self, LaunchError> {
        let list = attribute_list(sid, inherit)?;
        let mut info: STARTUPINFOEXW = unsafe { zeroed() };
        info.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        // The standard handles, named explicitly. Relying on inheritance alone is not
        // enough: the child only receives handles marked inheritable, and a caller's
        // pipe is not necessarily one of them.
        info.StartupInfo.dwFlags = 0x0000_0100; // STARTF_USESTDHANDLES
        info.StartupInfo.hStdInput = std_handles[0];
        info.StartupInfo.hStdOutput = std_handles[1];
        info.StartupInfo.hStdError = std_handles[2];
        info.lpAttributeList = list.list;
        // An AppContainer child cannot reach the parent's desktop, so one is named.
        let mut desktop: Vec<u16> = std::ffi::OsStr::new("WinSta0\\Default")
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        info.StartupInfo.lpDesktop = desktop.as_mut_ptr();
        Ok(StartupInfoEx { info, _list: list, _desktop: desktop })
    }

    pub fn flags(&self) -> u32 {
        EXTENDED_STARTUPINFO_PRESENT
    }
}

fn wide(value: &str) -> Vec<u16> {
    std::ffi::OsStr::new(value)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}
