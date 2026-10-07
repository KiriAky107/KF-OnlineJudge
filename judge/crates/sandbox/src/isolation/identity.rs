//! Irreversible credential reduction, called only by the fixed single-threaded init.
use rustix::process::{Gid, Signal, Uid};
use rustix::thread::{CapabilitiesSecureBits as Bits, CapabilitySet, CapabilitySets};
use std::io;

fn denied(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}
const SECURE: Bits = Bits::NO_ROOT
    .union(Bits::NO_ROOT_LOCKED)
    .union(Bits::NO_SETUID_FIXUP_LOCKED)
    .union(Bits::KEEP_CAPS_LOCKED)
    .union(Bits::NO_CAP_AMBIENT_RAISE)
    .union(Bits::NO_CAP_AMBIENT_RAISE_LOCKED);

pub(crate) fn valid(uid: u32, gid: u32) -> bool {
    uid >= 100_000 && gid >= 100_000 && uid != u32::MAX && gid != u32::MAX
}

// Probe every supported bit rather than silently dropping only constants known
// to this library. The 64-bit capability ABI is a deliberate fail-closed bound.
fn last_capability() -> io::Result<u32> {
    for bit in 0..64 {
        match rustix::thread::capability_is_in_bounding_set(CapabilitySet::from_bits_retain(
            1 << bit,
        )) {
            Ok(_) => {}
            Err(rustix::io::Errno::INVAL) if bit > 0 => return Ok(bit - 1),
            Err(error) => return Err(error.into()),
        }
    }
    Err(denied("unsupported capability ABI"))
}

pub(crate) fn reduce_current(uid: u32, gid: u32) -> io::Result<()> {
    crate::launcher::entry::ensure_single_threaded()?;
    if !valid(uid, gid) || !rustix::process::geteuid().is_root() {
        return Err(denied("task identity precondition"));
    }
    let last = last_capability()?;
    rustix::thread::set_thread_groups(&[])?;
    rustix::thread::set_keep_capabilities(false)?;
    rustix::thread::clear_ambient_capability_set()?;
    // Lock keepcaps and no-setuid-fixup OFF, not ON. Normal UID transition must
    // still clear privilege; root and ambient acquisition are locked off too.
    rustix::thread::set_capabilities_secure_bits(SECURE)?;
    for bit in 0..=last {
        rustix::thread::remove_capability_from_bounding_set(CapabilitySet::from_bits_retain(
            1 << bit,
        ))?;
    }
    rustix::thread::set_no_new_privs(true)?;
    let gid = Gid::from_raw(gid);
    let uid = Uid::from_raw(uid);
    rustix::thread::set_thread_res_gid(gid, gid, gid)?;
    rustix::thread::set_thread_res_uid(uid, uid, uid)?;
    // The trusted init retains control/capture capabilities. A same-UID user
    // child must not reopen them through /proc/1/fd or /proc/1/root. Don't rely
    // on fs.suid_dumpable's system default after the credential transition.
    rustix::process::set_dumpable_behavior(rustix::process::DumpableBehavior::NotDumpable)?;
    let empty = CapabilitySets {
        effective: CapabilitySet::empty(),
        permitted: CapabilitySet::empty(),
        inheritable: CapabilitySet::empty(),
    };
    rustix::thread::set_capabilities(None, empty)?;
    rustix::thread::clear_ambient_capability_set()?;
    // Linux clears PDEATHSIG on credential changes. Restore it before ACK;
    // the supervisor also checks the same held bootstrap/init parent relation.
    rustix::process::set_parent_process_death_signal(Some(Signal::KILL))?;
    if rustix::thread::capabilities(None)? != empty
        || !rustix::process::getgroups()?.is_empty()
        || rustix::process::getuid() != uid
        || rustix::process::geteuid() != uid
        || rustix::process::getgid() != gid
        || rustix::process::getegid() != gid
        || rustix::process::dumpable_behavior()? != rustix::process::DumpableBehavior::NotDumpable
        || rustix::thread::get_keep_capabilities()?
        || rustix::thread::capabilities_secure_bits()? != SECURE
        || !rustix::thread::no_new_privs()?
        || rustix::process::parent_process_death_signal()? != Some(Signal::KILL)
    {
        return Err(denied("task credential readback"));
    }
    for bit in 0..=last {
        let cap = CapabilitySet::from_bits_retain(1 << bit);
        if rustix::thread::capability_is_in_bounding_set(cap)?
            || rustix::thread::capability_is_in_ambient_set(cap)?
        {
            return Err(denied("task capability readback"));
        }
    }
    verify_status(
        &std::fs::read_to_string("/proc/self/status")?,
        uid.as_raw(),
        gid.as_raw(),
    )
}

pub(crate) fn reduce_init(uid: u32, gid: u32) -> io::Result<()> {
    if rustix::process::getpid().as_raw_pid() != 1 {
        return Err(denied("credential stage requires namespace init"));
    }
    reduce_current(uid, gid)
}

fn field<'a>(status: &'a str, name: &str) -> io::Result<&'a str> {
    status
        .lines()
        .find_map(|line| line.strip_prefix(name))
        .ok_or_else(|| denied("missing credential field"))
}

pub(crate) fn verify_status(status: &str, uid: u32, gid: u32) -> io::Result<()> {
    for (name, expected) in [("Uid:", uid), ("Gid:", gid)] {
        let ids: Vec<_> = field(status, name)?.split_whitespace().collect();
        if ids.len() != 4 || ids.iter().any(|s| s.parse::<u32>().ok() != Some(expected)) {
            return Err(denied("real/effective/saved/filesystem identity"));
        }
    }
    if !field(status, "Groups:")?.trim().is_empty() || field(status, "NoNewPrivs:")?.trim() != "1" {
        return Err(denied("groups/no_new_privs"));
    }
    for name in ["CapInh:", "CapPrm:", "CapEff:", "CapBnd:", "CapAmb:"] {
        if u64::from_str_radix(field(status, name)?.trim(), 16).ok() != Some(0) {
            return Err(denied("nonempty capability set"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    const STATUS: &str = "Uid:\t100000 100000 100000 100000\nGid:\t100001 100001 100001 100001\nGroups:\t\nNoNewPrivs:\t1\nCapInh:\t0000\nCapPrm:\t0000\nCapEff:\t0000\nCapBnd:\t0000\nCapAmb:\t0000\n";
    #[test]
    fn readback_rejects_saved_ids_groups_missing_fields_and_any_capability() {
        verify_status(STATUS, 100000, 100001).unwrap();
        for invalid in [
            STATUS.replace("100000 100000 100000 100000", "100000 100000 0 100000"),
            STATUS.replace("Groups:\t", "Groups:\t0"),
            STATUS.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"),
            STATUS.replace("CapBnd:\t0000", "CapBnd:\t0001"),
            STATUS.replace("CapAmb:\t0000\n", ""),
        ] {
            assert!(verify_status(&invalid, 100000, 100001).is_err());
        }
    }
}
