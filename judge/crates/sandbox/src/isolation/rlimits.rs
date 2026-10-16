//! Finite inherited per-process guardrails; task totals stay in cgroup/monitor.
//! Apply in the trusted single-threaded init before credentials/seccomp. Never
//! raise an inherited hard limit, use RLIMIT_CPU for aggregate CPU, or use
//! RLIMIT_AS/NPROC as substitutes for cgroup memory/pids. CORE is always zero.
use crate::spec::ValidatedSpec;
use rustix::process::{Resource, Rlimit};
use serde::{Deserialize, Serialize};
use std::io;
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RlimitPlan {
    file_bytes: u64,
    stack_bytes: u64,
    open_files: u32,
}
fn denied(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}
impl RlimitPlan {
    pub(crate) fn from_spec(s: &ValidatedSpec) -> Self {
        let l = &s.spec().limits;
        Self {
            file_bytes: l.file_bytes,
            stack_bytes: l.stack_bytes,
            open_files: l.open_files,
        }
    }
    pub(crate) fn valid(&self) -> bool {
        self.file_bytes > 0
            && self.file_bytes <= 64 << 20
            && self.stack_bytes > 0
            && self.stack_bytes <= 256 << 20
            && (3..=1024).contains(&self.open_files)
    }
    fn limits(self) -> [(Resource, u64); 4] {
        [
            (Resource::Fsize, self.file_bytes),
            (Resource::Nofile, self.open_files.into()),
            (Resource::Stack, self.stack_bytes),
            (Resource::Core, 0),
        ]
    }
    pub(crate) fn apply_current(self) -> io::Result<()> {
        crate::launcher::entry::ensure_single_threaded()?;
        if !self.valid() || !rustix::process::geteuid().is_root() {
            return Err(denied("rlimit initialization precondition"));
        }
        // Preflight every hard ceiling before the first irreversible mutation.
        for (r, n) in self.limits() {
            if rustix::process::getrlimit(r)
                .maximum
                .is_some_and(|old| old < n)
            {
                return Err(denied("rlimit would raise inherited hard ceiling"));
            }
        }
        for (r, n) in self.limits() {
            let expected = Rlimit {
                current: Some(n),
                maximum: Some(n),
            };
            rustix::process::setrlimit(r, expected)?;
            if rustix::process::getrlimit(r) != expected {
                return Err(denied("rlimit kernel readback"));
            }
        }
        Ok(())
    }
    pub(crate) fn apply_init(self) -> io::Result<()> {
        if rustix::process::getpid() != rustix::process::Pid::INIT {
            return Err(denied("rlimit stage requires namespace init"));
        }
        self.apply_current()
    }
    pub(crate) fn verify_proc(self, text: &str) -> io::Result<()> {
        if !self.valid() || text.len() > 4096 {
            return Err(denied("rlimit readback size/plan"));
        }
        for (name, n, unit) in [
            ("Max file size", self.file_bytes, "bytes"),
            ("Max open files", self.open_files.into(), "files"),
            ("Max stack size", self.stack_bytes, "bytes"),
            ("Max core file size", 0, "bytes"),
        ] {
            let mut lines = text.lines().filter_map(|line| line.strip_prefix(name));
            let line = lines
                .next()
                .ok_or_else(|| denied("missing rlimit readback"))?;
            if lines.next().is_some() {
                return Err(denied("duplicate rlimit readback"));
            }
            let parts: Vec<_> = line.split_whitespace().collect();
            if parts.len() != 3
                || parts[0].parse::<u64>().ok() != Some(n)
                || parts[1].parse::<u64>().ok() != Some(n)
                || parts[2] != unit
            {
                return Err(denied("rlimit readback mismatch"));
            }
        }
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn readback_requires_both_finite_limits_correct_units_and_all_fields() {
        let p = RlimitPlan {
            file_bytes: 4096,
            stack_bytes: 262144,
            open_files: 16,
        };
        let good = "Limit Soft Limit Hard Limit Units\nMax file size 4096 4096 bytes\nMax open files 16 16 files\nMax stack size 262144 262144 bytes\nMax core file size 0 0 bytes\n";
        p.verify_proc(good).unwrap();
        for bad in [
            good.replace("4096 4096", "4096 unlimited"),
            good.replace("16 16 files", "16 32 files"),
            good.replace("262144 262144 bytes", "262144 262144 files"),
            good.replace("Max core file size 0 0 bytes\n", ""),
            format!("{good}Max file size 4096 4096 bytes\n"),
        ] {
            assert!(p.verify_proc(&bad).is_err());
        }
        for bad in [
            RlimitPlan { file_bytes: 0, ..p },
            RlimitPlan {
                stack_bytes: 257 << 20,
                ..p
            },
            RlimitPlan { open_files: 2, ..p },
        ] {
            assert!(!bad.valid());
        }
    }
}
