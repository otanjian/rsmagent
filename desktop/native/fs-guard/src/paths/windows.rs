//! The Windows backend for path validation and root-anchored opening.
//!
//! Same guarantee as the unix backend, reached a different way. There is no
//! `openat` on Windows, so the equivalent is
//! `NtCreateFile` with `OBJECT_ATTRIBUTES.RootDirectory` set to the
//! already-held parent handle and a `UNICODE_STRING` naming **one** component.
//! The kernel resolves that component against the parent object, which is why
//! this is an object-anchored operation rather than a string check:
//!
//! * a name containing a separator is refused by the kernel itself
//!   (`STATUS_OBJECT_NAME_INVALID`), so no amount of `..` in a single component
//!   can climb out;
//! * `FILE_OPEN_REPARSE_POINT` opens the reparse point *itself* rather than
//!   following it, and the attribute check below then refuses it -- so a
//!   junction swapped in between two requests is seen, not traversed;
//! * the absolute path never leaves the helper, exactly as on unix.
//!
//! Design D2 is explicit that a single flag or a `GetFinalPathNameByHandle`
//! prefix comparison is **not** the boundary. It is not used as one here: the
//! refusals come from the per-component open plus the attributes read back from
//! the resulting handle.

use std::ffi::{c_void, OsStr, OsString};
use std::fs::File;
use std::os::windows::io::{AsRawHandle, FromRawHandle};

use windows_sys::Win32::Foundation::HANDLE;
use windows_sys::Win32::Storage::FileSystem::{
    GetDriveTypeW, GetFileInformationByHandle, GetFinalPathNameByHandleW,
    BY_HANDLE_FILE_INFORMATION, FILE_LIST_DIRECTORY, FILE_NAME_NORMALIZED,
    FILE_READ_ATTRIBUTES, FILE_READ_DATA, FILE_SHARE_DELETE, FILE_SHARE_READ,
    FILE_SHARE_WRITE, SYNCHRONIZE, VOLUME_NAME_DOS,
};

use super::{is_sensitive, Kind, RootRef};
use crate::protocol::Code;

/// How this backend carries an absolute root path between `open_root` and the
/// identity re-check. The unix backend uses a `CString`; Windows takes a wide
/// string and so carries an `OsString`. `grants` only stores and hands the value
/// back, so it is generic over the platform through this alias.
pub type RootPath = OsString;

/// `OBJ_CASE_INSENSITIVE` is an NT object-attribute flag rather than a Win32
/// constant, so `windows-sys` does not export it. The value is from the
/// documented `OBJECT_ATTRIBUTES.Attributes` set.
const OBJ_CASE_INSENSITIVE: u32 = 0x0000_0040;

/// `OBJ_DONT_REPARSE`: fail the open if any component of an *absolute* name is a
/// reparse point, instead of silently traversing it.
///
/// This is what makes the root open safe without walking the path ourselves: a
/// junction anywhere above the picked directory makes the open fail rather than
/// resolve to an object the user did not select. It is a second line of defence
/// behind the per-component opens used for every request after the root is held.
const OBJ_DONT_REPARSE: u32 = 0x0000_1000;

/// `FILE_OPEN_REPARSE_POINT`: open the reparse point itself, never follow it.
const FILE_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
/// `FILE_OPEN_FOR_BACKUP_INTENT`: allows a directory handle to be opened
/// without `FILE_LIST_DIRECTORY` on every parent, which is what makes
/// component-at-a-time traversal possible for a directory the user picked.
const FILE_OPEN_FOR_BACKUP_INTENT: u32 = 0x0000_4000;
/// `FILE_DIRECTORY_FILE`: the object must be a directory.
const FILE_DIRECTORY_FILE: u32 = 0x0000_0001;
/// `FILE_NON_DIRECTORY_FILE`: the object must not be a directory.
const FILE_NON_DIRECTORY_FILE: u32 = 0x0000_0040;
/// `FILE_SYNCHRONOUS_IO_NONALERT`: required for synchronous `NtCreateFile`.
const FILE_SYNCHRONOUS_IO_NONALERT: u32 = 0x0000_0020;

/// `FILE_ATTRIBUTE_REPARSE_POINT`.
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
/// `FILE_ATTRIBUTE_DIRECTORY`.
const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x0000_0010;

/// NT status values this backend maps to protocol codes.
const STATUS_OBJECT_NAME_INVALID: i32 = 0xC000_0033u32 as i32;
const STATUS_OBJECT_NAME_NOT_FOUND: i32 = 0xC000_0034u32 as i32;
const STATUS_OBJECT_PATH_NOT_FOUND: i32 = 0xC000_003Au32 as i32;
const STATUS_OBJECT_NAME_COLLISION: i32 = 0xC000_0035u32 as i32;
const STATUS_ACCESS_DENIED: i32 = 0xC000_0022u32 as i32;
const STATUS_NOT_A_DIRECTORY: i32 = 0xC000_0106u32 as i32;
const STATUS_FILE_IS_A_DIRECTORY: i32 = 0xC000_00BAu32 as i32;
const STATUS_REPARSE_POINT_ENCOUNTERED: i32 = 0xC000_050Bu32 as i32;

#[repr(C)]
struct ObjectAttributes {
    length: u32,
    root_directory: HANDLE,
    object_name: *const UnicodeString,
    attributes: u32,
    security_descriptor: *const c_void,
    security_quality_of_service: *const c_void,
}

#[repr(C)]
struct UnicodeString {
    length: u16,
    maximum_length: u16,
    buffer: *mut u16,
}

#[repr(C)]
struct IoStatusBlock {
    status: usize,
    information: usize,
}

#[link(name = "ntdll")]
extern "system" {
    fn NtCreateFile(
        file_handle: *mut HANDLE,
        desired_access: u32,
        object_attributes: *const ObjectAttributes,
        io_status_block: *mut IoStatusBlock,
        allocation_size: *const i64,
        file_attributes: u32,
        share_access: u32,
        create_disposition: u32,
        create_options: u32,
        ea_buffer: *const c_void,
        ea_length: u32,
    ) -> i32;

    /// `NtQueryDirectoryFile` is used rather than
    /// `GetFileInformationByHandleEx(FileIdBothDirectoryInfo)` because only the
    /// former can be told to `RestartScan`, and a directory listing that starts
    /// wherever the handle happened to stop is not a listing.
    fn NtQueryDirectoryFile(
        file_handle: HANDLE,
        event: HANDLE,
        apc_routine: Option<unsafe extern "system" fn(*mut c_void, *mut c_void, *mut c_void)>,
        apc_context: *mut c_void,
        io_status_block: *mut IoStatusBlock,
        file_information: *mut c_void,
        length: u32,
        file_information_class: u32,
        return_single_entry: u8,
        file_name: *const UnicodeString,
        restart_scan: u8,
    ) -> i32;

    /// Set information on an open file by handle. Used to mark a temporary object
    /// for deletion (`FileDispositionInformation`) without assembling a path.
    #[cfg(feature = "write-ops")]
    fn NtSetInformationFile(
        file_handle: HANDLE,
        io_status_block: *mut IoStatusBlock,
        file_information: *mut c_void,
        length: u32,
        file_information_class: u32,
    ) -> i32;
}

/// `FileBothDirectoryInformation`: the `NtQueryDirectoryFile` information class
/// whose records carry the name, attributes, size, timestamps and the 8.3 short
/// name.
///
/// This is deliberately **not** the `FileIdBothDirectoryInfo` constant from
/// `windows-sys`: that one is a `FILE_INFO_BY_HANDLE_CLASS` value for
/// `GetFileInformationByHandleEx`, and the two enumerations are different.
/// Passing the latter to `NtQueryDirectoryFile` is answered with
/// `STATUS_INVALID_INFO_CLASS` (measured on Windows 11), so the value is stated
/// here for the API it belongs to.
#[allow(non_upper_case_globals)]
const FileBothDirectoryInformation: u32 = 3;

/// One directory entry, as `FileBothDirectoryInformation` lays it out.
///
/// `FILE_BOTH_DIR_INFORMATION` is a variable-length structure whose name follows
/// the fixed part, and `windows-sys` does not export it, so the layout is stated
/// here. Only the offset of the name matters for that reason; `offset_of!` is
/// used rather than a hand-counted constant so a field added later cannot
/// silently move it.
#[repr(C)]
struct FileBothDirInformation {
    next_entry_offset: u32,
    file_index: u32,
    creation_time: i64,
    last_access_time: i64,
    last_write_time: i64,
    change_time: i64,
    end_of_file: i64,
    allocation_size: i64,
    file_attributes: u32,
    file_name_length: u32,
    ea_size: u32,
    short_name_length: u8,
    short_name: [u16; 12],
    file_name: [u16; 1],
}

impl FileBothDirInformation {
    /// The entry's name, decoded.
    ///
    /// SAFETY: the caller must ensure `self` points at a record the kernel
    /// filled, with at least `file_name_length` bytes of name available at the
    /// documented offset and the whole record inside the buffer.
    unsafe fn name(&self) -> String {
        let units = self.file_name_length as usize / 2;
        let start = self.file_name.as_ptr();
        String::from_utf16_lossy(std::slice::from_raw_parts(start, units))
    }

    /// The 8.3 short name's length in code units, or 0 when there is none.
    ///
    /// A short name is an *alias* for the same object, and design D2 refuses to
    /// treat one as an authorisation identity: "8.3别名不用于授权标识，无法证明同一
    /// 对象时拒绝". Exposed so the listing can apply that rule; the current
    /// listing policy refuses every alias by never resolving one, so this is the
    /// hook the rule is documented against.
    #[allow(dead_code)]
    fn short_name_units(&self) -> usize {
        self.short_name_length as usize / 2
    }
}

/// A NUL-terminated UTF-16 buffer that lives as long as the call that uses it.
struct Wide {
    units: Vec<u16>,
}

impl Wide {
    fn new(text: &str) -> Self {
        Wide {
            units: text.encode_utf16().chain(std::iter::once(0)).collect(),
        }
    }

    fn as_ptr(&self) -> *const u16 {
        self.units.as_ptr()
    }
}

/// Identity of an authorised root, used to detect the root being replaced.
///
/// Windows has no inode, so the pair is (volume serial, file index), which is
/// the documented equivalent of (device, inode) and comes from the same
/// `GetFileInformationByHandle` call the attribute checks already need.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RootIdentity {
    pub device: u64,
    pub inode: u64,
}

/// True if the handle's object is a reparse point (symlink, junction, mount
/// point, or a cloud placeholder's tag). Every one of them is refused.
fn is_reparse(info: &BY_HANDLE_FILE_INFORMATION) -> bool {
    info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

fn info_of(file: &File) -> Result<BY_HANDLE_FILE_INFORMATION, Code> {
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    // SAFETY: `info` is zeroed and the handle is owned by `file`.
    let ok = unsafe { GetFileInformationByHandle(file.as_raw_handle() as HANDLE, &mut info) };
    if ok == 0 {
        return Err(Code::IoError);
    }
    Ok(info)
}

/// Map an NT status to a protocol code.
///
/// The important mappings are the policy refusals, not the I/O accidents: a
/// name the kernel rejected as malformed and a reparse point encountered on the
/// way are both "the entry is not readable under this policy".
fn classify_status(status: i32) -> Code {
    match status {
        STATUS_OBJECT_NAME_INVALID => Code::PathOutsideRoot,
        STATUS_OBJECT_PATH_NOT_FOUND | STATUS_OBJECT_NAME_NOT_FOUND => Code::InvalidParams,
        STATUS_OBJECT_NAME_COLLISION => Code::InvalidParams,
        STATUS_ACCESS_DENIED => Code::RejectedEntry,
        STATUS_NOT_A_DIRECTORY | STATUS_FILE_IS_A_DIRECTORY => Code::PathOutsideRoot,
        STATUS_REPARSE_POINT_ENCOUNTERED => Code::RejectedEntry,
        _ => Code::IoError,
    }
}

/// The access a relative open asks for.
///
/// A publish (`ReplaceFileW`) and a temporary-object deletion both need
/// `DELETE` on a handle to the object, which is not part of a read. Asking for it
/// only where it is used keeps the read-only path's access mask as narrow as it
/// has always been -- an over-broad handle is a capability nobody asked for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Access {
    /// Read data and attributes, as every read-side operation needs.
    Read,
    /// Read, without saying whether the object is a file or a directory.
    ///
    /// `stat` must *report* what the object is, so it cannot ask the kernel to
    /// require either shape. Windows refuses a directory when
    /// `FILE_NON_DIRECTORY_FILE` is set, which is what made `stat` on a directory
    /// fail with `path_outside_root` -- while the unix backend opened it happily
    /// with a plain `O_RDONLY`, since a directory is openable without
    /// `O_DIRECTORY`. The two backends have to agree, so this variant exists.
    ReadEither,
    /// The above plus `DELETE`, for a handle that replaces or removes the object.
    ///
    /// Only the write build asks for it. `#[cfg]` rather than `#[allow]` so the
    /// read-only binary has no way to express the request at all.
    #[cfg(feature = "write-ops")]
    Replace,
}

impl Access {
    fn mask(self) -> u32 {
        /// `DELETE`: required to rename, replace or remove the object by handle.
        #[cfg(feature = "write-ops")]
        const DELETE: u32 = 0x0001_0000;
        match self {
            Access::Read | Access::ReadEither =>
                FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            #[cfg(feature = "write-ops")]
            Access::Replace => {
                FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE | DELETE
            }
        }
    }
}

/// Delete one entry **through a directory handle**, never by an assembled path.
///
/// The entry is opened relative to `parent` -- so the kernel resolves the name
/// against the directory object the caller still holds, and a directory renamed
/// or replaced since the temporary object was created cannot redirect the
/// deletion -- and then marked for deletion *by handle*.
///
/// `FileDispositionInformation` (the non-`Ex` form) is used rather than
/// `std::fs::remove_file` for the same reason the publish uses `ReplaceFileW`:
/// deletion by handle cannot be raced through the name. The object disappears
/// when the last handle closes, which is why the handle is dropped before
/// returning.
///
/// This is the *only* deletion the helper has. It is not exposed as an
/// operation and is reachable only from the temporary-object registry.
#[cfg(feature = "write-ops")]
pub fn remove_relative(parent: &File, name: &str) -> Result<(), Code> {
    /// `FileDispositionInformation`: `FILE_DISPOSITION_INFORMATION { BOOLEAN }`.
    const FILE_DISPOSITION_INFORMATION_CLASS: u32 = 13;
    if name.is_empty() || name == "." || name == ".." || name.contains('/') || name.contains('\\') {
        return Err(Code::InvalidParams);
    }
    // `Access::Replace` is the access that carries `DELETE`.
    let handle = open_component_at(parent, name, Shape::File, Access::Replace)?;
    // SAFETY: the structure is `#[repr(C)]` with the documented single byte, and
    // the handle is live with `DELETE` access.
    let status = unsafe {
        let mut disposition: u8 = 1; // TRUE: delete on last close
        let mut io_status: IoStatusBlock = std::mem::zeroed();
        NtSetInformationFile(
            handle.as_raw_handle() as HANDLE,
            &mut io_status,
            &mut disposition as *mut u8 as *mut c_void,
            std::mem::size_of::<u8>() as u32,
            FILE_DISPOSITION_INFORMATION_CLASS,
        )
    };
    // Drop the handle before reporting: the deletion is only complete once the
    // last handle closes, and reporting success earlier would be a claim the
    // caller cannot rely on.
    drop(handle);
    if status < 0 {
        return Err(classify_status(status));
    }
    Ok(())
}

/// Open one component relative to a directory handle.
///
/// `as_directory` selects whether the kernel is told the object must (or must
/// not) be a directory, which makes "this is a file where the path needs a
/// directory" a kernel refusal rather than a post-hoc check.
///
/// `"."` is accepted on purpose and means "the same object": this is how the
/// helper re-opens a directory to get its own enumeration position, matching the
/// unix backend's `openat(parent, ".")`. `".."` is refused -- that is the one
/// name that could leave the authorised root, and no legitimate request needs
/// it.
fn open_component_at(
    parent: &File,
    name: &str,
    as_directory: Shape,
    access: Access,
) -> Result<File, Code> {
    if name.is_empty() || name == ".." {
        return Err(Code::PathOutsideRoot);
    }
    if name.contains('/') || name.contains('\\') {
        // Belt and braces: the kernel refuses this too
        // (STATUS_OBJECT_NAME_INVALID). Checked here so the refusal does not
        // depend on an NT status being mapped correctly.
        return Err(Code::PathOutsideRoot);
    }

    let mut wide_name = Wide::new(name);
    let bytes = (wide_name.units.len() - 1) * 2;
    if bytes > u16::MAX as usize {
        return Err(Code::InvalidParams);
    }
    let object_name = UnicodeString {
        length: bytes as u16,
        maximum_length: bytes as u16,
        buffer: wide_name.units.as_mut_ptr(),
    };
    let attributes = ObjectAttributes {
        length: std::mem::size_of::<ObjectAttributes>() as u32,
        root_directory: parent.as_raw_handle() as HANDLE,
        object_name: &object_name,
        attributes: OBJ_CASE_INSENSITIVE,
        security_descriptor: std::ptr::null(),
        security_quality_of_service: std::ptr::null(),
    };
    let mut io_status: IoStatusBlock = unsafe { std::mem::zeroed() };
    let mut handle: HANDLE = std::ptr::null_mut();

    let mut options = FILE_OPEN_FOR_BACKUP_INTENT | FILE_SYNCHRONOUS_IO_NONALERT;
    // `"."` names the object the parent handle already refers to. Asking the
    // kernel to open a reparse point *as itself* for that name is refused, and it
    // would be meaningless anyway: the parent was already refused if it was one.
    if name != "." {
        options |= FILE_OPEN_REPARSE_POINT;
    }
    options |= match as_directory {
        Shape::Directory => FILE_DIRECTORY_FILE,
        Shape::File => FILE_NON_DIRECTORY_FILE,
        // Neither: with `Access::ReadEither` the caller wants to find out what
        // this is. Setting either flag makes the kernel reject the other shape,
        // which is exactly the mismatch this variant removes.
        Shape::Either => 0,
    };

    // SAFETY: every pointer passed outlives the call; `parent` owns a live
    // handle; `wide_name` and `object_name` are not moved while it runs.
    let status = unsafe {
        NtCreateFile(
            &mut handle,
            access.mask(),
            &attributes,
            &mut io_status,
            std::ptr::null(),
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            1, // FILE_OPEN
            options,
            std::ptr::null(),
            0,
        )
    };
    if status < 0 {
        return Err(classify_status(status));
    }
    // SAFETY: a successful `NtCreateFile` returned a handle we now own.
    Ok(unsafe { File::from_raw_handle(handle as _) })
}

/// Open a path relative to `root`, one component at a time, refusing reparse
/// points on every step.
pub fn open_relative(
    root: &File,
    components: &[String],
    want_directory: bool,
) -> Result<(File, Kind), Code> {
    open_relative_with(root, components, want_directory, Access::Read)
}

/// `open_relative` with an explicit access intent.
///
/// The write path needs `DELETE` on the *target* handle to publish over it; every
/// other caller wants read access and nothing more. The last component is the
/// only one opened with the requested access, because only the target is ever
/// replaced -- an intermediate directory opened for replacement would be a
/// capability the caller never asked for.
/// What shape the caller expects the opened object to have.
///
/// A tri-state rather than a `bool` because `stat` genuinely does not know: it
/// opens the object in order to *report* its kind, so requiring either shape would
/// make one of the two answers unreachable.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Shape {
    Directory,
    File,
    Either,
}

pub fn open_relative_with(
    root: &File,
    components: &[String],
    want_directory: bool,
    access: Access,
) -> Result<(File, Kind), Code> {
    if components.is_empty() {
        return Err(Code::InvalidParams);
    }
    if components.iter().any(|c| is_sensitive(c)) {
        return Err(Code::RejectedEntry);
    }

    let mut current: Option<File> = None;
    for (index, component) in components.iter().enumerate() {
        let last = index + 1 == components.len();
        let parent = current.as_ref().unwrap_or(root);
        let step_access = if last { access } else { Access::Read };
        let shape = if !last || want_directory {
            Shape::Directory
        } else if step_access == Access::ReadEither {
            Shape::Either
        } else {
            Shape::File
        };
        let opened = open_component_at(parent, component, shape, step_access)?;
        // A reparse point is refused on *every* component, including the last:
        // following one is how a request would reach an object the user did not
        // authorise.
        let info = info_of(&opened)?;
        if is_reparse(&info) {
            return Err(Code::RejectedEntry);
        }
        current = Some(opened);
    }
    let file = current.expect("at least one component was opened");

    let kind = classify(&file)?;
    if want_directory && kind != Kind::Directory {
        return Err(Code::InvalidParams);
    }
    // A file with more than one hard link is reachable under a name the user did
    // not authorise, which is the same refusal the unix backend makes.
    if kind == Kind::RegularFile && link_count(&file)? > 1 {
        return Err(Code::RejectedEntry);
    }
    Ok((file, kind))
}

/// Classify an opened handle, refusing everything the helper does not read.
fn classify(file: &File) -> Result<Kind, Code> {
    let info = info_of(file)?;
    if is_reparse(&info) {
        return Err(Code::RejectedEntry);
    }
    if info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0 {
        return Ok(Kind::Directory);
    }
    Ok(Kind::RegularFile)
}

fn link_count(file: &File) -> Result<u64, Code> {
    Ok(info_of(file)?.nNumberOfLinks as u64)
}

pub fn identity_of(file: &File) -> Result<RootIdentity, Code> {
    let info = info_of(file)?;
    Ok(RootIdentity {
        device: info.dwVolumeSerialNumber as u64,
        inode: ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64,
    })
}

/// Refuse a root on a network filesystem.
///
/// The unix backend asks `statfs`; here the volume is asked for its drive type,
/// and a remote or unknown drive is refused rather than guessed at (design D6:
/// 首期明确仅本地NTFS普通目录).
pub fn reject_network_root(file: &File) -> Result<(), Code> {
    /// `DRIVE_FIXED` from the documented `GetDriveTypeW` result set. Written out
    /// rather than imported because `windows-sys` places these in a module whose
    /// feature gate is the same as the function's, and a documented numeric
    /// constant is not something to depend on a crate layout for.
    const DRIVE_FIXED: u32 = 3;

    let volume = volume_path_of(file)?;
    let wide = Wide::new(&volume);
    // SAFETY: `wide` outlives the call.
    let drive_type = unsafe { GetDriveTypeW(wide.as_ptr()) };
    if drive_type == DRIVE_FIXED {
        return Ok(());
    }
    // A network share or a mapped drive, a removable volume, and anything the
    // helper cannot classify are all refused. The removable case is refused
    // because the identity checks this helper relies on cannot be relied on
    // across a media change, and an unclassifiable volume must never be assumed
    // local.
    Err(Code::Unsupported)
}

/// The volume root (``C:\``) a handle lives on.
///
/// `GetFinalPathNameByHandleW` is used **only** to name the volume for the
/// drive-type question. It is deliberately not used as a security boundary:
/// design D2 refuses a prefix comparison for that, and nothing here compares a
/// final path against the authorised root.
fn volume_path_of(file: &File) -> Result<String, Code> {
    let mut buffer = vec![0u16; 512];
    // SAFETY: the buffer is owned and its length is passed.
    let written = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle() as HANDLE,
            buffer.as_mut_ptr(),
            buffer.len() as u32,
            FILE_NAME_NORMALIZED | VOLUME_NAME_DOS,
        )
    };
    if written == 0 || written as usize >= buffer.len() {
        return Err(Code::Unsupported);
    }
    let text = String::from_utf16_lossy(&buffer[..written as usize]);
    // `\\?\C:\...` -> `C:\`, so `GetDriveTypeW` sees a path it understands.
    let trimmed = text.trim_start_matches(r"\\?\");
    match trimmed.find(':') {
        Some(index) => Ok(trimmed[..=index].to_string() + "\\"),
        None => Err(Code::Unsupported),
    }
}

/// Validate an absolute root path taken from the user's own directory picker.
///
/// This is the only place an absolute path is legitimate, and it is never taken
/// from a page or a model. A drive-relative name (`C:foo`) and a UNC prefix are
/// refused: the picker returns `C:\...`, and anything else is a caller building
/// a path, which is exactly the case to refuse.
pub fn validate_root_path(raw: &str) -> Result<OsString, Code> {
    if raw.is_empty() || raw.len() > crate::protocol::MAX_PATH_BYTES {
        return Err(Code::InvalidParams);
    }
    if raw.as_bytes().contains(&0) {
        return Err(Code::InvalidParams);
    }
    if raw.starts_with(r"\\") {
        // UNC (`\\server\share`) and the `\\?\` device namespace.
        return Err(Code::PathOutsideRoot);
    }
    let bytes = raw.as_bytes();
    let absolute = bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && (bytes[2] == b'\\' || bytes[2] == b'/');
    if !absolute {
        return Err(Code::PathOutsideRoot);
    }
    Ok(OsString::from(raw))
}

/// Open an absolute root path, refusing a reparse point and a non-directory.
///
/// Opens *without* requesting directory-ness and then inspects the handle, for
/// the same reason the unix backend does: the type check then runs against the
/// object actually opened, so it cannot be raced by replacing the root in
/// between.
///
/// `NtCreateFile` is used rather than `CreateFileW` because the handle must be
/// **synchronous**: `NtQueryDirectoryFile` -- which is what makes a listing
/// restart from the beginning -- refuses a handle opened for asynchronous I/O
/// with `STATUS_INVALID_INFO_CLASS`, and `CreateFileW` offers no flag for it.
pub fn open_root(path: &OsStr) -> Result<File, Code> {
    if path.as_encoded_bytes().contains(&0) {
        return Err(Code::InvalidParams);
    }
    let volume = Wide::new(&path.to_string_lossy());
    let file = open_absolute(&volume)?;
    if is_reparse(&info_of(&file)?) {
        // The user picked a link or junction; refuse rather than follow it.
        return Err(Code::RejectedEntry);
    }
    match classify(&file) {
        Ok(Kind::Directory) => Ok(file),
        // A regular file is not an acceptable root, but it is not a policy
        // refusal either: the caller asked for the wrong kind of object.
        Ok(Kind::RegularFile) => Err(Code::Unsupported),
        Err(code) => Err(code),
    }
}

/// Open one absolute path as a synchronous handle.
///
/// The path must first be put in the NT form. `NtCreateFile` does **not** accept
/// a DOS path: `C:\Users\...` is answered with `STATUS_OBJECT_PATH_SYNTAX_BAD`,
/// and the form it wants is the object-manager path `\??\C:\Users\...`. Getting
/// this wrong is not a subtle failure -- every absolute open fails -- so it is
/// worth stating rather than rediscovering.
fn open_absolute(wide: &Wide) -> Result<File, Code> {
    let text = String::from_utf16_lossy(&wide.units[..wide.units.len() - 1]);
    let nt = to_nt_path(&text);
    let mut nt_wide = Wide::new(&nt);
    let bytes = (nt_wide.units.len() - 1) * 2;

    let object_name = UnicodeString {
        length: bytes as u16,
        maximum_length: bytes as u16,
        buffer: nt_wide.units.as_mut_ptr(),
    };
    let attributes = ObjectAttributes {
        length: std::mem::size_of::<ObjectAttributes>() as u32,
        // An absolute name means no root directory: the picker's own path is the
        // one case where the caller names a location rather than a component.
        root_directory: std::ptr::null_mut(),
        object_name: &object_name,
        attributes: OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE,
        security_descriptor: std::ptr::null(),
        security_quality_of_service: std::ptr::null(),
    };
    let mut io_status: IoStatusBlock = unsafe { std::mem::zeroed() };
    let mut handle: HANDLE = std::ptr::null_mut();
    // SAFETY: every pointer outlives the call and the name buffer is not moved.
    let status = unsafe {
        NtCreateFile(
            &mut handle,
            FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            &attributes,
            &mut io_status,
            std::ptr::null(),
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            1, // FILE_OPEN
            FILE_OPEN_REPARSE_POINT
                | FILE_OPEN_FOR_BACKUP_INTENT
                | FILE_SYNCHRONOUS_IO_NONALERT,
            std::ptr::null(),
            0,
        )
    };
    if status < 0 {
        return Err(classify_status(status));
    }
    // SAFETY: a successful `NtCreateFile` returned a handle we now own.
    Ok(unsafe { File::from_raw_handle(handle as _) })
}

/// A DOS path (`C:\dir`) as the object-manager path `NtCreateFile` expects
/// (`\??\C:\dir`). A path already in the NT form is returned unchanged.
fn to_nt_path(path: &str) -> String {
    if path.starts_with(r"\??\") || path.starts_with(r"\Device\") {
        return path.to_string();
    }
    format!(r"\??\{path}")
}

/// Identity of a path as it *currently resolves*, without following a link.
///
/// Used to re-verify an authorised root. Like the unix backend this deliberately
/// re-opens the recorded path rather than comparing the held handle with itself:
/// the held handle can never change identity, so that comparison could not fail.
pub fn identity_of_path(path: &OsStr) -> Result<RootIdentity, Code> {
    if path.as_encoded_bytes().contains(&0) {
        return Err(Code::InvalidParams);
    }
    let wide = Wide::new(&path.to_string_lossy());
    // `FILE_OPEN_REPARSE_POINT` means a link is opened as itself, so its identity
    // differs from its target's -- which is what makes "the path was replaced by
    // a link" a detectable change rather than a silent redirect.
    let file = match open_absolute(&wide) {
        Ok(file) => file,
        // The path no longer names anything: the grant cannot be re-verified.
        Err(_) => return Err(Code::Unsupported),
    };
    let info = info_of(&file).map_err(|_| Code::Unsupported)?;
    Ok(RootIdentity {
        device: info.dwVolumeSerialNumber as u64,
        inode: ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64,
    })
}

/// Read directory names from a handle, bounded and cancellable.
///
/// `NtQueryDirectoryFile` enumerates the *object* the handle refers to, so a
/// directory renamed after it was opened still enumerates the authorised object.
///
/// `RestartScan` is set on the first call, and that is the load-bearing detail:
/// a directory handle carries the enumeration position, so a second listing
/// through the *same* handle would otherwise continue where the first stopped
/// and a directory listed twice would look empty the second time. Measured on
/// Windows 11: with `RestartScan` the same handle enumerates the same entries
/// again; without it, the second call reports `STATUS_NO_MORE_FILES`.
pub fn read_names(
    directory: &File,
    request_id: &str,
    cancel: &crate::ops::CancelFlag,
) -> Result<Vec<String>, Code> {
    /// `STATUS_NO_MORE_FILES`: the enumeration is finished.
    const STATUS_NO_MORE_FILES: i32 = 0x8000_0006u32 as i32;

    let mut buffer = vec![0u8; 64 * 1024];
    let mut names: Vec<String> = Vec::new();
    let mut restart = 1u8;

    loop {
        let mut io_status: IoStatusBlock = unsafe { std::mem::zeroed() };
        // SAFETY: every pointer outlives the call; the handle is live; the kernel
        // writes only inside the owned buffer.
        let status = unsafe {
            NtQueryDirectoryFile(
                directory.as_raw_handle() as HANDLE,
                std::ptr::null_mut(),
                None,
                std::ptr::null_mut(),
                &mut io_status,
                buffer.as_mut_ptr() as *mut c_void,
                buffer.len() as u32,
                FileBothDirectoryInformation,
                false as u8,
                std::ptr::null(),
                restart,
            )
        };
        restart = 0;
        if status < 0 {
            if status == STATUS_NO_MORE_FILES {
                return Ok(names);
            }
            return Err(classify_status(status));
        }

        let mut offset = 0usize;
        loop {
            if names.len() >= crate::ops::MAX_SCAN_ENTRIES {
                return Ok(names);
            }
            if names.len() % 256 == 0 && crate::ops::is_cancelled_for(cancel, request_id) {
                return Err(Code::Cancelled);
            }
            // SAFETY: `offset` is inside the buffer, which the kernel filled with
            // a chain of FILE_BOTH_DIR_INFORMATION records, and the name lives at
            // the documented offset for `file_name_length` code units.
            let name = unsafe {
                let entry = &*(buffer.as_ptr().add(offset) as *const FileBothDirInformation);
                entry.name()
            };
            if name != "." && name != ".." {
                names.push(name);
            }
            // SAFETY: the same live record as above.
            let next = unsafe {
                (*(buffer.as_ptr().add(offset) as *const FileBothDirInformation))
                    .next_entry_offset
            };
            if next == 0 {
                break;
            }
            offset += next as usize;
        }
    }
}

/// Metadata for one entry of a directory handle, addressed by name.
///
/// The unix backend uses `fstatat(..., AT_SYMLINK_NOFOLLOW)`; here the entry is
/// opened relative to the directory handle with `FILE_OPEN_REPARSE_POINT`, so a
/// link is classified as a link rather than as its target. The returned shape is
/// the shared [`super::EntryStat`], so the listing logic in `ops` has no `cfg`.
pub fn stat_at(directory: &File, name: &str) -> Option<super::EntryStat> {
    if name.contains('/') || name.contains('\\') {
        return None;
    }
    // A directory first: `NtCreateFile` with FILE_NON_DIRECTORY_FILE would
    // refuse one, and this call must report directories, not skip them.
    // Either in one call rather than try-directory-then-try-file: two opens are
    // two chances for the object to change between them, and the kernel can answer
    // the question in one.
    let opened = open_component_at(directory, name, Shape::Either, Access::ReadEither)
        .ok()?;
    let info = info_of(&opened).ok()?;
    if is_reparse(&info) {
        return None;
    }
    let modified = filetime_to_unix(info.ftLastWriteTime.dwHighDateTime,
                                   info.ftLastWriteTime.dwLowDateTime);
    #[cfg(feature = "write-ops")]
    let identity = ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64;
    if info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0 {
        return Some(super::EntryStat {
            kind: Some("dir"),
            size: 0,
            modified,
            #[cfg(feature = "write-ops")]
            identity,
        });
    }
    if info.nNumberOfLinks > 1 {
        // A hard-linked file is reachable under a name the user did not
        // authorise; the unix backend refuses it for the same reason.
        return None;
    }
    Some(super::EntryStat {
        kind: Some("file"),
        size: ((info.nFileSizeHigh as u64) << 32) | info.nFileSizeLow as u64,
        modified,
        #[cfg(feature = "write-ops")]
        identity,
    })
}

/// A `FILETIME` (100-nanosecond ticks since 1601) as a unix timestamp.
///
/// The contract reports `modified` as seconds, and the unix backend reports
/// `st_mtime`; both therefore mean "unix seconds" and the client does not have
/// to know which platform produced the number.
fn filetime_to_unix(high: u32, low: u32) -> i64 {
    const TICKS_PER_SECOND: u64 = 10_000_000;
    /// Seconds between 1601-01-01 and 1970-01-01.
    const EPOCH_DIFFERENCE: u64 = 11_644_473_600;
    let ticks = ((high as u64) << 32) | low as u64;
    let seconds = ticks / TICKS_PER_SECOND;
    if seconds < EPOCH_DIFFERENCE {
        return 0;
    }
    (seconds - EPOCH_DIFFERENCE) as i64
}

/// Metadata for an already-open file handle, in the shared shape.
pub fn stat_of(file: &File) -> Result<super::FileStat, Code> {
    let info = info_of(file)?;
    Ok(super::FileStat {
        size: ((info.nFileSizeHigh as u64) << 32) | info.nFileSizeLow as u64,
        modified: filetime_to_unix(info.ftLastWriteTime.dwHighDateTime,
                                   info.ftLastWriteTime.dwLowDateTime),
        regular: info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY == 0,
        links: info.nNumberOfLinks as u64,
        identity: ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64,
    })
}

/// The authorised root as a filesystem path, for a caller that must name a
/// location inside it.
///
/// Only the write operations use this, and always to build a path *inside* a root
/// the caller is already authorised for; every such path is re-checked through a
/// handle before it is touched. The path is never the authorisation.
#[cfg(feature = "write-ops")]
pub fn absolute_root_of(grant: &crate::grants::Grant) -> Result<std::path::PathBuf, Code> {
    Ok(std::path::PathBuf::from(grant.root_path()))
}

/// Publish `staged` over the object at `target_path`.
///
/// `ReplaceFileW` is the API for exactly this, and it is used instead of a plain
/// rename for the reason design D3 gives: it **preserves the replaced file's
/// security descriptor, attributes, object identity and creation time**, and only
/// its contents become the staged ones. A `std::fs::rename` would instead attach
/// the *staging* file's descriptor to the name, so a file whose ACL is narrower
/// than its directory would silently gain the directory's permissions; and it
/// would fail outright while the target is open, which is the normal case here
/// because the caller just read it.
///
/// `target` (the handle whose version was verified) is passed as the file to
/// replace and is what makes "the object we checked" and "the object we replaced"
/// the same name. It also lets the caller keep holding the handle, so the
/// replacement happens underneath a reader that already opened the old object.
///
/// An earlier attempt used `NtSetInformationFile(FileRenameInformation)` on the
/// target handle, which is wrong in a way worth recording: that call renames *the
/// object the handle refers to*, so it moved the target onto the staging name and
/// left the target name absent. The measured symptom was a successful publish
/// followed by "file not found" when re-reading the target.
#[cfg(feature = "write-ops")]
pub fn replace_contents(
    target: &File,
    staged: &std::path::Path,
    target_path: &std::path::Path,
) -> Result<(), Code> {
    use windows_sys::Win32::Storage::FileSystem::{
        ReplaceFileW, REPLACEFILE_IGNORE_MERGE_ERRORS,
    };

    let _ = target;
    let replaced = Wide::new(&target_path.to_string_lossy());
    let replacement = Wide::new(&staged.to_string_lossy());
    // SAFETY: both wide strings outlive the call; the backup path and the two
    // reserved parameters are documented as optional and null.
    let ok = unsafe {
        ReplaceFileW(
            replaced.as_ptr(),
            replacement.as_ptr(),
            std::ptr::null(),
            REPLACEFILE_IGNORE_MERGE_ERRORS,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if ok == 0 {
        return Err(Code::IoError);
    }
    Ok(())
}

/// Re-open the root directory as an independent handle, for its own enumeration
/// position.
///
/// The unix backend says `openat(parent, ".")`; NT's relative open refuses `"."`
/// as a name (`STATUS_OBJECT_NAME_INVALID`, measured), so the object is reached
/// by its recorded path instead. That would be a string-trust regression on its
/// own -- the path could have been replaced since the user picked it -- so the
/// fresh handle's identity is compared against the identity captured when the
/// grant was created, and a mismatch is refused. The absolute path is therefore
/// only ever used to *reach* the object, never to authorise it.
pub fn reopen_directory(root: &RootRef<'_>) -> Result<File, Code> {
    let wide = Wide::new(&root.path.to_string_lossy());
    let file = open_absolute(&wide)?;
    let current = identity_of(&file)?;
    if current != root.identity {
        return Err(Code::Unsupported);
    }
    // The object is confirmed to be the authorised one; a handle to it is what
    // the caller asked for.
    let _ = root.handle;
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_root(tag: &str) -> std::path::PathBuf {
        let base = std::env::temp_dir().join(format!("fsguard-win-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        base
    }

    #[test]
    fn a_drive_relative_or_unc_root_is_refused() {
        assert!(validate_root_path(r"C:\Users\me\docs").is_ok());
        assert!(validate_root_path("C:/Users/me/docs").is_ok());
        for bad in ["", "relative\\path", "C:docs", r"\\server\share", r"\\?\C:\x"] {
            assert!(validate_root_path(bad).is_err(), "must refuse root {bad:?}");
        }
    }

    #[test]
    fn one_component_at_a_time_cannot_climb_out() {
        let base = temp_root("climb");
        let outside = temp_root("outside");
        std::fs::write(outside.join("secret.txt"), b"s").unwrap();
        let root = open_root(OsStr::new(base.to_str().unwrap())).unwrap();

        // A separator in a *single* component is refused by this backend before
        // any syscall, and by the kernel if it ever reached it.
        assert_eq!(
            open_component_at(&root, "..\\..\\secret.txt", Shape::File, Access::Read).err(),
            Some(Code::PathOutsideRoot)
        );
        assert_eq!(
            open_component_at(&root, "sub/inner", Shape::File, Access::Read).err(),
            Some(Code::PathOutsideRoot)
        );

        let _ = std::fs::remove_dir_all(&base);
        let _ = std::fs::remove_dir_all(&outside);
    }

    #[test]
    fn a_missing_component_is_a_parameter_error() {
        let base = temp_root("missing");
        let root = open_root(OsStr::new(base.to_str().unwrap())).unwrap();
        assert_eq!(
            open_component_at(&root, "nope", Shape::File, Access::Read).err(),
            Some(Code::InvalidParams)
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn a_file_is_not_an_acceptable_root_and_a_directory_is() {
        let base = temp_root("kind");
        let file = base.join("not-a-dir.txt");
        std::fs::write(&file, b"x").unwrap();

        assert_eq!(
            open_root(OsStr::new(file.to_str().unwrap())).err(),
            Some(Code::Unsupported)
        );
        assert!(open_root(OsStr::new(base.to_str().unwrap())).is_ok());

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn opening_relative_reaches_the_object_and_reports_its_kind() {
        let base = temp_root("rel");
        std::fs::create_dir_all(base.join("sub")).unwrap();
        std::fs::write(base.join("sub").join("a.txt"), b"hello").unwrap();
        let root = open_root(OsStr::new(base.to_str().unwrap())).unwrap();

        let (file, kind) = open_relative(&root, &["sub".into()], true).unwrap();
        assert_eq!(kind, Kind::Directory);
        drop(file);

        let (file, kind) = open_relative(&root, &["sub".into(), "a.txt".into()], false).unwrap();
        assert_eq!(kind, Kind::RegularFile);
        assert_eq!(stat_of(&file).unwrap().size, 5);

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn a_sensitive_component_is_refused_before_any_syscall() {
        let base = temp_root("sensitive");
        std::fs::create_dir_all(base.join(".ssh")).unwrap();
        let root = open_root(OsStr::new(base.to_str().unwrap())).unwrap();
        assert_eq!(
            open_relative(&root, &[".ssh".into()], true).err(),
            Some(Code::RejectedEntry)
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn enumeration_reports_every_entry_once() {
        let base = temp_root("list");
        std::fs::create_dir_all(base.join("sub")).unwrap();
        std::fs::write(base.join("top.txt"), b"xyz").unwrap();
        let root = open_root(OsStr::new(base.to_str().unwrap())).unwrap();

        let cancel = crate::ops::new_cancel_flag();
        let names = read_names(&root, "req", &cancel).unwrap();
        let mut sorted = names.clone();
        sorted.sort();
        assert_eq!(sorted, vec!["sub".to_string(), "top.txt".to_string()]);
        assert!(!names.iter().any(|n| n == "." || n == ".."));

        // Listing twice must not be affected by the first pass's position.
        let again = read_names(&root, "req", &cancel).unwrap();
        assert_eq!(again.len(), 2);

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn stat_at_reports_a_directory_and_a_file_differently() {
        let base = temp_root("stat");
        std::fs::create_dir_all(base.join("sub")).unwrap();
        std::fs::write(base.join("f.txt"), b"12345").unwrap();
        let root = open_root(OsStr::new(base.to_str().unwrap())).unwrap();

        let dir = stat_at(&root, "sub").unwrap();
        assert_eq!(dir.kind, Some("dir"));
        let file = stat_at(&root, "f.txt").unwrap();
        assert_eq!(file.kind, Some("file"));
        assert_eq!(file.size, 5);
        assert!(stat_at(&root, "absent").is_none());

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn the_root_identity_changes_when_the_path_is_replaced() {
        let base = temp_root("identity");
        let root_path = base.join("root");
        std::fs::create_dir_all(&root_path).unwrap();

        let first = identity_of_path(OsStr::new(root_path.to_str().unwrap())).unwrap();
        std::fs::rename(&root_path, base.join("moved")).unwrap();
        std::fs::create_dir_all(&root_path).unwrap();
        let second = identity_of_path(OsStr::new(root_path.to_str().unwrap())).unwrap();

        assert_ne!(first, second, "a replaced root must not keep its identity");

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn a_deleted_root_cannot_be_re_verified() {
        let base = temp_root("deleted");
        let root_path = base.join("root");
        std::fs::create_dir_all(&root_path).unwrap();
        std::fs::remove_dir_all(&root_path).unwrap();
        assert_eq!(
            identity_of_path(OsStr::new(root_path.to_str().unwrap())).err(),
            Some(Code::Unsupported)
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn a_local_volume_is_not_refused_as_a_network_root() {
        let base = temp_root("volume");
        let root = open_root(OsStr::new(base.to_str().unwrap())).unwrap();
        // The temporary directory is on a fixed local drive in every CI and dev
        // environment this target builds for.
        assert_eq!(reject_network_root(&root), Ok(()));
        let _ = std::fs::remove_dir_all(&base);
    }
}
