//! The ACL lifecycle an AppContainer needs to reach its own project (task 7.2).
//!
//! ## Why this exists
//!
//! An AppContainer reaches nothing by default -- that is its value, and it is also
//! why a confined child cannot open the project it was started in. Measured on this
//! host: `cmd /c type probe.txt` inside the AppContainer succeeded **0/5** times
//! without a grant on the directory, and **5/6** with one. The single failure was
//! the separate launch instability, not the ACL.
//!
//! ## What it does, and what it refuses
//!
//! It adds **one** ACE naming only the AppContainer SID to the project root, and
//! takes exactly that ACE back afterwards. Nothing else in the DACL is touched: no
//! `Everyone`, no other principal, no integrity label.
//!
//! It refuses rather than guesses in the two cases that could damage a directory:
//!
//! * **The SID is already in the DACL.** Then the grant is not ours to make, and
//!   removing it on the way out would revoke an authorization this run never
//!   created. A stale ACE from an earlier crash is exactly this case, so it is
//!   reported instead of silently adopted.
//! * **The DACL cannot be read or written.** A directory whose protection cannot be
//!   established is not one to modify.
//!
//! ## What is still open (deliberately)
//!
//! Restoration is in-process: the [`ActiveGrant`] guard revokes on every return path
//! of the run, but a hard kill of the launcher leaves the ACE behind. Task 7.2 also
//! asks for crash recovery (a recorded intent replayed next run) and for the
//! inherited/protected-DACL, hard-link, moved-root and large-directory cases; none
//! of that is here. The refusal above is what keeps the gap from compounding: a
//! leftover ACE is reported next time, not stacked on.

use std::ffi::c_void;
use std::ptr;

use windows_sys::Win32::Foundation::{GetLastError, LocalFree, ERROR_SUCCESS};
use windows_sys::Win32::Security::Authorization::{
    GetNamedSecurityInfoW, SetEntriesInAclW, SetNamedSecurityInfoW, EXPLICIT_ACCESS_W,
    GRANT_ACCESS, REVOKE_ACCESS, SE_FILE_OBJECT, TRUSTEE_IS_SID, TRUSTEE_IS_WELL_KNOWN_GROUP,
    TRUSTEE_W,
};
use windows_sys::Win32::Security::{
    AclSizeInformation, CopySid, EqualSid, GetAce, GetAclInformation, GetLengthSid, ACL,
    ACL_SIZE_INFORMATION, DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID,
};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_GENERIC_EXECUTE, FILE_GENERIC_READ, FILE_GENERIC_WRITE,
};

use crate::LaunchError;

/// `DELETE`, which is not among the generic file rights: a script replaces and
/// removes files inside its own project, and without this it cannot.
const DELETE: u32 = 0x0001_0000;
/// `OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE`: files and directories created
/// later are covered too, so a run does not have to re-grant for each new output.
const SUB_CONTAINERS_AND_OBJECTS_INHERIT: u32 = 0x03;
/// Where the SID sits inside an `ACCESS_ALLOWED_ACE`: `ACE_HEADER` (4 bytes) then
/// the access mask (4 bytes).
const OFFSETOF_ACE_SID: usize = 8;

/// One grant this process made, and therefore the only one it may take back.
pub struct ProjectGrant {
    path: String,
    /// A copy of the SID, because the derived AppContainer SID may be dropped
    /// before the revoke happens.
    sid: Vec<u8>,
}

/// Give the AppContainer SID read, write, execute and delete on `path`, inherited.
pub fn grant(path: &str, sid: PSID) -> Result<ProjectGrant, LaunchError> {
    unsafe {
        if sid.is_null() {
            return Err(LaunchError::Isolation("the AppContainer SID is null".into()));
        }
        let length = GetLengthSid(sid) as usize;
        if length == 0 {
            return Err(LaunchError::Isolation("the AppContainer SID is empty".into()));
        }

        let mut name: Vec<u16> = wide(path);
        let mut old_dacl: *mut ACL = ptr::null_mut();
        let mut descriptor: PSECURITY_DESCRIPTOR = ptr::null_mut();
        let status = GetNamedSecurityInfoW(
            name.as_ptr(),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            ptr::null_mut(),
            ptr::null_mut(),
            &mut old_dacl,
            ptr::null_mut(),
            &mut descriptor,
        );
        if status != ERROR_SUCCESS {
            return Err(LaunchError::Isolation(format!(
                "could not read the DACL of {path} (error {status})"
            )));
        }
        // The descriptor owns the DACL, so it has to outlive every use below.
        let _descriptor_guard = FreeGuard(descriptor);

        if dacl_names(old_dacl, sid) {
            return Err(LaunchError::Isolation(format!(
                "the project directory {path} already grants this AppContainer; a \
                 previous run may have left that behind, and removing it here would \
                 revoke an authorization this run did not create"
            )));
        }

        let mut entry: EXPLICIT_ACCESS_W = std::mem::zeroed();
        entry.grfAccessPermissions =
            FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE | DELETE;
        entry.grfAccessMode = GRANT_ACCESS;
        entry.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT;
        entry.Trustee = trustee(sid);

        let mut new_dacl: *mut ACL = ptr::null_mut();
        let status = SetEntriesInAclW(1, &entry, old_dacl, &mut new_dacl);
        if status != ERROR_SUCCESS {
            return Err(LaunchError::Isolation(format!(
                "could not build the new DACL for {path} (error {status})"
            )));
        }
        let _new_dacl_guard = FreeGuard(new_dacl as *mut c_void);

        let status = SetNamedSecurityInfoW(
            name.as_mut_ptr(),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            ptr::null_mut(),
            ptr::null_mut(),
            new_dacl,
            ptr::null_mut(),
        );
        if status != ERROR_SUCCESS {
            return Err(LaunchError::Isolation(format!(
                "could not apply the DACL to {path} (error {status})"
            )));
        }

        let mut copy = vec![0u8; length];
        if CopySid(length as u32, copy.as_mut_ptr() as PSID, sid) == 0 {
            // The grant is already applied; take it back rather than return a token
            // that cannot name what it added.
            let _ = revoke_sid(path, sid);
            return Err(LaunchError::Isolation(format!(
                "could not copy the AppContainer SID while granting {path} (error {})",
                GetLastError()
            )));
        }
        Ok(ProjectGrant { path: path.to_string(), sid: copy })
    }
}

/// Take back the one ACE [`grant`] added.
pub fn revoke(grant: ProjectGrant) -> Result<(), LaunchError> {
    unsafe { revoke_sid(&grant.path, grant.sid.as_ptr() as PSID) }
}

unsafe fn revoke_sid(path: &str, sid: PSID) -> Result<(), LaunchError> {
    let mut name: Vec<u16> = wide(path);
    let mut old_dacl: *mut ACL = ptr::null_mut();
    let mut descriptor: PSECURITY_DESCRIPTOR = ptr::null_mut();
    let status = GetNamedSecurityInfoW(
        name.as_ptr(),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        ptr::null_mut(),
        ptr::null_mut(),
        &mut old_dacl,
        ptr::null_mut(),
        &mut descriptor,
    );
    if status != ERROR_SUCCESS {
        return Err(LaunchError::Isolation(format!(
            "could not read the DACL of {path} to revoke (error {status})"
        )));
    }
    let _descriptor_guard = FreeGuard(descriptor);

    if !dacl_names(old_dacl, sid) {
        // Already removed, or the directory is gone. Nothing to undo.
        return Ok(());
    }

    let mut entry: EXPLICIT_ACCESS_W = std::mem::zeroed();
    entry.grfAccessMode = REVOKE_ACCESS;
    entry.grfInheritance = 0; // NO_INHERITANCE
    entry.Trustee = trustee(sid);

    let mut new_dacl: *mut ACL = ptr::null_mut();
    let status = SetEntriesInAclW(1, &entry, old_dacl, &mut new_dacl);
    if status != ERROR_SUCCESS {
        return Err(LaunchError::Isolation(format!(
            "could not build the revoked DACL for {path} (error {status})"
        )));
    }
    let _new_dacl_guard = FreeGuard(new_dacl as *mut c_void);

    let status = SetNamedSecurityInfoW(
        name.as_mut_ptr(),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        ptr::null_mut(),
        ptr::null_mut(),
        new_dacl,
        ptr::null_mut(),
    );
    if status != ERROR_SUCCESS {
        return Err(LaunchError::Isolation(format!(
            "could not revoke the grant on {path} (error {status})"
        )));
    }
    Ok(())
}

/// Whether the DACL already has an ACE naming `sid`.
///
/// Walked rather than asked of the access-check API: the question is not "would
/// this SID be allowed" but "is there an entry we did not write".
unsafe fn dacl_names(dacl: *mut ACL, sid: PSID) -> bool {
    if dacl.is_null() {
        return false;
    }
    let mut info: ACL_SIZE_INFORMATION = std::mem::zeroed();
    if GetAclInformation(
        dacl,
        &mut info as *mut ACL_SIZE_INFORMATION as *mut c_void,
        std::mem::size_of::<ACL_SIZE_INFORMATION>() as u32,
        AclSizeInformation,
    ) == 0
    {
        return false;
    }
    for index in 0..info.AceCount {
        let mut ace: *mut c_void = ptr::null_mut();
        if GetAce(dacl, index, &mut ace) == 0 {
            continue;
        }
        let ace_sid = (ace as *mut u8).add(OFFSETOF_ACE_SID) as PSID;
        if EqualSid(ace_sid, sid) != 0 {
            return true;
        }
    }
    false
}

fn trustee(sid: PSID) -> TRUSTEE_W {
    let mut trustee: TRUSTEE_W = unsafe { std::mem::zeroed() };
    trustee.TrusteeForm = TRUSTEE_IS_SID;
    trustee.TrusteeType = TRUSTEE_IS_WELL_KNOWN_GROUP;
    trustee.ptstrName = sid as *mut u16;
    trustee
}

struct FreeGuard(PSECURITY_DESCRIPTOR);

impl Drop for FreeGuard {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { LocalFree(self.0) };
        }
    }
}

fn wide(value: &str) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    std::ffi::OsStr::new(value)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

/// A grant that takes itself back when it goes out of scope.
///
/// A guard rather than a `revoke()` at the end of the run because the run has many
/// early returns -- a refusal, a spawn failure, a failed assign, a failed wait --
/// and every one of them leaves the child unstarted or dead while the ACE is still
/// on the user's directory. Making revocation a destructor is what makes "always"
/// true rather than "on the paths someone remembered".
pub struct ActiveGrant(Option<ProjectGrant>);

pub fn activate(path: &str, sid: PSID) -> Result<ActiveGrant, LaunchError> {
    Ok(ActiveGrant(Some(grant(path, sid)?)))
}

impl Drop for ActiveGrant {
    fn drop(&mut self) {
        if let Some(grant) = self.0.take() {
            if let Err(err) = revoke(grant) {
                // Reported, not swallowed: a leftover ACE is visible to the next run
                // (which refuses rather than adopting it), and silence here would
                // make that refusal look unexplained.
                eprintln!("script-launcher: could not revoke the AppContainer grant: {err}");
            }
        }
    }
}
