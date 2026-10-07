//! Bounded, nonblocking parent transport. Only pipe ends reach user FDs012.
//! Input is an immutable authorized snapshot; capture storage is anonymous in
//! an operator-owned private directory. No request pathname or unbounded Vec.
//! The supervisor must cancel the whole tree on limit/read/storage failure.
use crate::{artifacts::SnapshotFile, spec::ValidatedSpec};
use rustix::fd::{AsRawFd, OwnedFd};
use rustix::fs::{FileType, Mode, OFlags};
use std::io;

const CHUNK: usize = 4096;
fn denied(s: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, s)
}

/// Blocking, owned CLOEXEC ends; the exec primitive duplicates these to 012
/// and closes every other descriptor, including control and capture handles.
pub struct ChildPipes {
    pub input: OwnedFd,
    pub output: OwnedFd,
    pub error: OwnedFd,
}
impl ChildPipes {
    pub fn validate_for_exec(&self) -> io::Result<()> {
        let mut identities = [(0, 0); 3];
        for (i, (fd, access)) in [
            (&self.input, OFlags::RDONLY),
            (&self.output, OFlags::WRONLY),
            (&self.error, OFlags::WRONLY),
        ]
        .into_iter()
        .enumerate()
        {
            let stat = rustix::fs::fstat(fd)?;
            let flags = rustix::fs::fcntl_getfl(fd)?;
            if fd.as_raw_fd() < 4
                || FileType::from_raw_mode(stat.st_mode) != FileType::Fifo
                || flags.intersection(OFlags::ACCMODE) != access
                || flags.contains(OFlags::NONBLOCK)
                || !rustix::io::fcntl_getfd(fd)?.contains(rustix::io::FdFlags::CLOEXEC)
            {
                return Err(denied("owned blocking CLOEXEC child pipe end"));
            }
            let identity = (stat.st_dev, stat.st_ino);
            if identities[..i].contains(&identity) {
                return Err(denied("independent stdio pipes"));
            }
            identities[i] = identity;
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct CaptureStatus {
    /// A lower bound if the tree is cancelled or collection fails.
    pub observed: u64,
    pub retained: u64,
    pub exceeded: bool,
    pub eof: bool,
}
pub struct Capture {
    fd: OwnedFd,
    pipe: Option<OwnedFd>,
    limit: u64,
    status: CaptureStatus,
}
impl Capture {
    pub fn status(&self) -> CaptureStatus {
        self.status
    }
    /// Bounded caller buffer, no writable capture capability escapes.
    pub fn read_at(&self, buffer: &mut [u8], offset: u64) -> io::Result<usize> {
        let length = buffer
            .len()
            .min(self.status.retained.saturating_sub(offset) as usize);
        if length == 0 {
            return Ok(0);
        }
        Ok(rustix::io::pread(&self.fd, &mut buffer[..length], offset)?)
    }
    fn drain(&mut self) -> io::Result<()> {
        let Some(pipe) = self.pipe.as_ref() else {
            return Ok(());
        };
        let mut buffer = [0; CHUNK];
        // Observe one byte beyond the ceiling, retain only the allowed prefix.
        let length = CHUNK.min((self.limit - self.status.retained + 1) as usize);
        let n = match rustix::io::read(pipe, &mut buffer[..length]) {
            Ok(0) => {
                self.pipe = None;
                self.status.eof = true;
                return Ok(());
            }
            Ok(n) => n,
            Err(rustix::io::Errno::AGAIN | rustix::io::Errno::INTR) => return Ok(()),
            Err(e) => return Err(e.into()),
        };
        self.status.observed = self
            .status
            .observed
            .checked_add(n as u64)
            .ok_or_else(|| denied("output counter overflow"))?;
        let keep = n.min((self.limit - self.status.retained) as usize);
        let mut done = 0;
        while done < keep {
            match rustix::io::pwrite(&self.fd, &buffer[done..keep], self.status.retained) {
                Ok(0) => return Err(io::Error::new(io::ErrorKind::WriteZero, "capture storage")),
                Ok(n) => {
                    done += n;
                    self.status.retained += n as u64;
                }
                Err(rustix::io::Errno::INTR) => continue,
                Err(e) => return Err(e.into()),
            }
        }
        if keep < n {
            self.status.exceeded = true;
            self.pipe = None; // No further allocation/drain on this stream.
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PumpStatus {
    pub stdin_sent: u64,
    pub stdin_closed: bool,
    pub stdin_closed_early: bool,
    pub stdout: CaptureStatus,
    pub stderr: CaptureStatus,
}
impl PumpStatus {
    pub fn output_limit_exceeded(self) -> bool {
        self.stdout.exceeded || self.stderr.exceeded
    }
    pub fn outputs_eof(self) -> bool {
        self.stdout.eof && self.stderr.eof
    }
}
pub struct PipePump {
    input: Option<SnapshotFile>,
    writer: Option<OwnedFd>,
    pending: [u8; CHUNK],
    position: usize,
    length: usize,
    sent: u64,
    closed_early: bool,
    output: Capture,
    error: Capture,
}
impl PipePump {
    /// This capability comes from the operator, never from the request. It
    /// must be private to the current trusted monitor identity. Temporary
    /// captures have zero links and are automatically deleted on drop.
    pub fn prepare(
        input: Option<SnapshotFile>,
        spec: &ValidatedSpec,
        directory: OwnedFd,
    ) -> io::Result<(Self, ChildPipes)> {
        let stat = rustix::fs::fstat(&directory)?;
        if FileType::from_raw_mode(stat.st_mode) != FileType::Directory
            || stat.st_uid != rustix::process::geteuid().as_raw()
            || stat.st_mode & 0o7777 != 0o700
            || !rustix::io::fcntl_getfd(&directory)?.contains(rustix::io::FdFlags::CLOEXEC)
        {
            return Err(denied("capture directory capability"));
        }
        let limits = &spec.spec().limits;
        let total = limits
            .stdout_bytes
            .checked_add(limits.stderr_bytes)
            .ok_or_else(|| denied("capture budget overflow"))?;
        if limits.stdout_bytes > limits.file_bytes
            || limits.stderr_bytes > limits.file_bytes
            || total
                > limits
                    .workdir_bytes
                    .min(crate::artifacts::MAX_SNAPSHOT_BYTES)
        {
            return Err(denied("capture storage ceiling"));
        }
        let (input_end, input_write) = rustix::pipe::pipe_with(rustix::pipe::PipeFlags::CLOEXEC)?;
        let (output_read, output_end) = rustix::pipe::pipe_with(rustix::pipe::PipeFlags::CLOEXEC)?;
        let (error_read, error_end) = rustix::pipe::pipe_with(rustix::pipe::PipeFlags::CLOEXEC)?;
        for fd in [&input_write, &output_read, &error_read] {
            rustix::fs::fcntl_setfl(fd, OFlags::NONBLOCK)?;
        }
        let capture = |pipe, limit| -> io::Result<Capture> {
            let fd = rustix::fs::openat(
                &directory,
                ".",
                OFlags::TMPFILE | OFlags::RDWR | OFlags::CLOEXEC,
                Mode::RUSR | Mode::WUSR,
            )?;
            let stat = rustix::fs::fstat(&fd)?;
            if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile
                || stat.st_nlink != 0
                || stat.st_size != 0
                || stat.st_uid != rustix::process::geteuid().as_raw()
            {
                return Err(denied("anonymous capture storage"));
            }
            Ok(Capture {
                fd,
                pipe: Some(pipe),
                limit,
                status: CaptureStatus::default(),
            })
        };
        let writer = if input.as_ref().is_some_and(|f| f.bytes() > 0) {
            Some(input_write)
        } else {
            drop(input_write);
            None
        };
        Ok((
            Self {
                input,
                writer,
                pending: [0; CHUNK],
                position: 0,
                length: 0,
                sent: 0,
                closed_early: false,
                output: capture(output_read, limits.stdout_bytes)?,
                error: capture(error_read, limits.stderr_bytes)?,
            },
            ChildPipes {
                input: input_end,
                output: output_end,
                error: error_end,
            },
        ))
    }
    /// Fair bounded tick: one stdin write and one read from each output. The
    /// outer supervisor supplies poll/deadlines/cancellation; no busy wait here.
    pub fn poll(&mut self) -> io::Result<PumpStatus> {
        self.feed()?;
        self.output.drain()?;
        self.error.drain()?;
        Ok(self.status())
    }
    pub fn status(&self) -> PumpStatus {
        PumpStatus {
            stdin_sent: self.sent,
            stdin_closed: self.writer.is_none(),
            stdin_closed_early: self.closed_early,
            stdout: self.output.status(),
            stderr: self.error.status(),
        }
    }
    pub fn stdout(&self) -> &Capture {
        &self.output
    }
    pub fn stderr(&self) -> &Capture {
        &self.error
    }
    /// Cancelling closes the feed immediately; outputs can still be drained to
    /// EOF after the namespace/cgroup terminates. Snapshot stays immutable.
    pub fn close_input(&mut self) {
        self.writer = None;
    }
    fn feed(&mut self) -> io::Result<()> {
        let Some(writer) = self.writer.as_ref() else {
            return Ok(());
        };
        let input = self
            .input
            .as_ref()
            .ok_or_else(|| denied("stdin ownership"))?;
        if self.position == self.length {
            let remaining = input
                .bytes()
                .checked_sub(self.sent)
                .ok_or_else(|| denied("stdin accounting"))?;
            if remaining == 0 {
                self.writer = None;
                return Ok(());
            }
            let count = CHUNK.min(remaining as usize);
            self.length = match rustix::io::pread(input, &mut self.pending[..count], self.sent) {
                Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "sealed stdin")),
                Ok(n) => n,
                Err(rustix::io::Errno::INTR) => return Ok(()),
                Err(e) => return Err(e.into()),
            };
            self.position = 0;
        }
        match write_without_sigpipe(writer, &self.pending[self.position..self.length]) {
            Ok(0) => return Err(io::Error::new(io::ErrorKind::WriteZero, "stdin pipe")),
            Ok(n) => {
                self.position += n;
                self.sent += n as u64;
                if self.sent == input.bytes() {
                    self.writer = None;
                }
            }
            Err(rustix::io::Errno::PIPE) => {
                self.writer = None;
                self.closed_early = true;
            }
            Err(rustix::io::Errno::AGAIN | rustix::io::Errno::INTR) => {}
            Err(e) => return Err(e.into()),
        }
        Ok(())
    }
}

#[allow(unsafe_code)]
fn write_without_sigpipe(fd: &OwnedFd, bytes: &[u8]) -> rustix::io::Result<usize> {
    // SAFETY: valid owned pipe and borrowed bytes, initialized libc sigsets.
    // Block only this thread's SIGPIPE during write, consume a newly generated
    // EPIPE signal if none was pending, and restore the exact prior mask on
    // every path. Never change process-global disposition or steal a signal
    // which was already pending. No allocation/unwind while mask is changed.
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        let mut old: libc::sigset_t = std::mem::zeroed();
        let mut pending: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        libc::sigaddset(&mut set, libc::SIGPIPE);
        let rc = libc::pthread_sigmask(libc::SIG_BLOCK, &set, &mut old);
        if rc != 0 {
            return Err(rustix::io::Errno::from_raw_os_error(rc));
        }
        let result = if libc::sigpending(&mut pending) != 0 {
            Err(rustix::io::Errno::from_raw_os_error(
                *libc::__errno_location(),
            ))
        } else {
            let prior = libc::sigismember(&pending, libc::SIGPIPE) == 1;
            let n = libc::write(fd.as_raw_fd(), bytes.as_ptr().cast(), bytes.len());
            let written = if n < 0 {
                Err(rustix::io::Errno::from_raw_os_error(
                    *libc::__errno_location(),
                ))
            } else {
                Ok(n as usize)
            };
            if written == Err(rustix::io::Errno::PIPE) && !prior {
                let zero = libc::timespec {
                    tv_sec: 0,
                    tv_nsec: 0,
                };
                loop {
                    let signal = libc::sigtimedwait(&set, std::ptr::null_mut(), &zero);
                    if signal >= 0
                        || rustix::io::Errno::from_raw_os_error(*libc::__errno_location())
                            != rustix::io::Errno::INTR
                    {
                        break;
                    }
                }
            }
            written
        };
        let rc = libc::pthread_sigmask(libc::SIG_SETMASK, &old, std::ptr::null_mut());
        if rc != 0 {
            return Err(rustix::io::Errno::from_raw_os_error(rc));
        }
        result
    }
}
