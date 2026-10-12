//! Single-threaded factory termination notice; handlers perform one lock-free store.
use std::io;
use std::sync::atomic::{AtomicBool, Ordering};

static STOP_REQUESTED: AtomicBool = AtomicBool::new(false);
extern "C" fn notice(_: libc::c_int) {
    STOP_REQUESTED.store(true, Ordering::Relaxed);
}
pub(crate) struct StopSignals {
    previous: Vec<(libc::c_int, libc::sigaction)>,
}
impl StopSignals {
    #[allow(unsafe_code)] // Fixed signal installation belongs to the Sandbox boundary.
    pub(crate) fn install() -> io::Result<Self> {
        // Factory authorization has already proved a single-threaded process.
        // Caught handlers reset across exec, so fresh helper executables retain
        // their own signal/guardian semantics; no signal mask is inherited.
        STOP_REQUESTED.store(false, Ordering::Relaxed);
        let mut guard = Self {
            previous: Vec::new(),
        };
        for signal in [libc::SIGTERM, libc::SIGINT] {
            // SAFETY: initialized action storage, an async-signal-safe handler,
            // valid signal numbers, and process-global use in the fixed factory.
            unsafe {
                let mut action: libc::sigaction = std::mem::zeroed();
                action.sa_sigaction = notice as *const () as usize;
                action.sa_flags = libc::SA_RESTART;
                if libc::sigemptyset(&mut action.sa_mask) != 0 {
                    return Err(io::Error::last_os_error());
                }
                let mut previous: libc::sigaction = std::mem::zeroed();
                if libc::sigaction(signal, &action, &mut previous) != 0 {
                    return Err(io::Error::last_os_error());
                }
                guard.previous.push((signal, previous));
            }
        }
        Ok(guard)
    }
    pub(crate) fn requested(&self) -> bool {
        STOP_REQUESTED.load(Ordering::Relaxed)
    }
}
impl Drop for StopSignals {
    #[allow(unsafe_code)] // Restore only this invocation's saved signal dispositions.
    fn drop(&mut self) {
        for (signal, previous) in self.previous.iter().rev() {
            // SAFETY: restore only dispositions saved by this fixed invocation.
            unsafe {
                libc::sigaction(*signal, previous, std::ptr::null_mut());
            }
        }
    }
}
