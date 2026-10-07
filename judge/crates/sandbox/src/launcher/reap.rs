//! Wait ownership belongs to one fresh single-threaded supervisor, never Worker.
use std::sync::atomic::{AtomicBool, Ordering};
static OWNED_SCOPE: AtomicBool = AtomicBool::new(false);
pub(crate) fn is_owned_scope() -> bool {
    OWNED_SCOPE.load(Ordering::Relaxed)
}
use rustix::process::{Pid, WaitOptions};
use std::{
    io,
    time::{Duration, Instant},
};
pub(crate) struct OwnedChildren {
    previous: Option<Pid>,
    finished: bool,
}
impl OwnedChildren {
    pub(crate) fn begin() -> io::Result<Self> {
        super::entry::ensure_single_threaded()?;
        if !std::fs::read_to_string(
            "/proc/self/task/".to_owned() + &std::process::id().to_string() + "/children",
        )?
        .trim()
        .is_empty()
        {
            return Err(io::Error::other("supervisor must start without children"));
        }
        let previous = rustix::process::child_subreaper()?;
        if OWNED_SCOPE
            .compare_exchange(false, true, Ordering::Relaxed, Ordering::Relaxed)
            .is_err()
        {
            return Err(io::Error::other("owned scope already active"));
        }
        if let Err(e) = rustix::process::set_child_subreaper(rustix::process::Pid::from_raw(1)) {
            OWNED_SCOPE.store(false, Ordering::Relaxed);
            return Err(e.into());
        }
        Ok(Self {
            previous,
            finished: false,
        })
    }
    /// Call only after the owned direct Child was waited and its cgroup empty.
    /// The precondition excludes unrelated children, including other tasks.
    pub(crate) fn finish(&mut self) -> io::Result<u32> {
        let count = reap_current()?;
        rustix::process::set_child_subreaper(self.previous)?;
        self.finished = true;
        OWNED_SCOPE.store(false, Ordering::Relaxed);
        Ok(count)
    }
}
pub(crate) fn reap_current() -> io::Result<u32> {
    if !is_owned_scope() || rustix::process::child_subreaper()?.is_none() {
        return Err(io::Error::other("owned subreaper required"));
    }
    let end = Instant::now() + Duration::from_secs(2);
    let mut count = 0;
    loop {
        match rustix::process::wait(WaitOptions::NOHANG) {
            Ok(Some(_)) => {
                count += 1;
                if count > 130 {
                    return Err(io::Error::other("reap ceiling"));
                }
            }
            Err(rustix::io::Errno::CHILD) => return Ok(count),
            Err(rustix::io::Errno::INTR) => {}
            Ok(None) => {
                if Instant::now() >= end {
                    return Err(io::Error::new(io::ErrorKind::TimedOut, "owned orphan wait"));
                }
                std::thread::sleep(Duration::from_millis(2));
            }
            Err(e) => return Err(e.into()),
        }
    }
}
impl Drop for OwnedChildren {
    fn drop(&mut self) {
        if !self.finished {
            let _ = self.finish();
        }
    }
}
