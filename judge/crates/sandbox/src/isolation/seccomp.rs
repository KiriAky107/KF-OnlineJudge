//! Fixed x86_64 syscall policy, not request-supplied BPF or a user execution permit.
use crate::spec::ExecutionProfile;
use seccompiler::{
    BpfProgram, SeccompAction, SeccompCmpArgLen::Qword, SeccompCmpOp, SeccompCondition,
    SeccompFilter, SeccompRule, TargetArch, sock_filter,
};
use std::{collections::BTreeMap, io};

const AUDIT_X86_64: u32 = 0xc000_003e;
const MAX_INSTRUCTIONS: usize = 2048;

fn denied(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}
fn condition(arg: u8, op: SeccompCmpOp, value: u64) -> io::Result<SeccompCondition> {
    SeccompCondition::new(arg, Qword, op, value).map_err(io::Error::other)
}
fn rules(conditions: Vec<Vec<SeccompCondition>>) -> io::Result<Vec<SeccompRule>> {
    conditions
        .into_iter()
        .map(|c| SeccompRule::new(c).map_err(io::Error::other))
        .collect()
}

pub(crate) fn compile(profile: ExecutionProfile) -> io::Result<BpfProgram> {
    if !cfg!(target_arch = "x86_64") {
        return Err(denied("unsupported seccomp architecture"));
    }
    let mut allowed = BTreeMap::new();
    // Ordinary glibc, C/C++ I/O, loader, memory, clocks, synchronization and
    // child waiting. Paths/FDs still require the independent filesystem and
    // descriptor boundaries; seccomp cannot inspect pointed-to path strings.
    for nr in [
        libc::SYS_read,
        libc::SYS_write,
        libc::SYS_readv,
        libc::SYS_writev,
        libc::SYS_pread64,
        libc::SYS_pwrite64,
        libc::SYS_preadv,
        libc::SYS_pwritev,
        libc::SYS_open,
        libc::SYS_openat,
        libc::SYS_close,
        libc::SYS_close_range,
        libc::SYS_lseek,
        libc::SYS_stat,
        libc::SYS_lstat,
        libc::SYS_fstat,
        libc::SYS_newfstatat,
        libc::SYS_statx,
        libc::SYS_access,
        libc::SYS_faccessat,
        libc::SYS_faccessat2,
        libc::SYS_getdents64,
        libc::SYS_readlink,
        libc::SYS_readlinkat,
        libc::SYS_getcwd,
        libc::SYS_chdir,
        libc::SYS_fchdir,
        libc::SYS_dup,
        libc::SYS_dup2,
        libc::SYS_dup3,
        libc::SYS_pipe,
        libc::SYS_pipe2,
        libc::SYS_poll,
        libc::SYS_ppoll,
        libc::SYS_select,
        libc::SYS_pselect6,
        libc::SYS_epoll_create1,
        libc::SYS_epoll_ctl,
        libc::SYS_epoll_wait,
        libc::SYS_epoll_pwait,
        libc::SYS_eventfd2,
        libc::SYS_mmap,
        libc::SYS_mprotect,
        libc::SYS_munmap,
        libc::SYS_mremap,
        libc::SYS_madvise,
        libc::SYS_brk,
        libc::SYS_msync,
        libc::SYS_mincore,
        libc::SYS_futex,
        libc::SYS_set_tid_address,
        libc::SYS_set_robust_list,
        libc::SYS_get_robust_list,
        libc::SYS_rseq,
        libc::SYS_rt_sigaction,
        libc::SYS_rt_sigprocmask,
        libc::SYS_rt_sigpending,
        libc::SYS_rt_sigreturn,
        libc::SYS_sigaltstack,
        libc::SYS_rt_sigsuspend,
        libc::SYS_rt_sigtimedwait,
        libc::SYS_restart_syscall,
        libc::SYS_clock_gettime,
        libc::SYS_clock_getres,
        libc::SYS_clock_nanosleep,
        libc::SYS_nanosleep,
        libc::SYS_gettimeofday,
        libc::SYS_time,
        libc::SYS_sched_yield,
        libc::SYS_sched_getaffinity,
        libc::SYS_getrandom,
        libc::SYS_uname,
        libc::SYS_getpid,
        libc::SYS_getppid,
        libc::SYS_gettid,
        libc::SYS_getuid,
        libc::SYS_geteuid,
        libc::SYS_getgid,
        libc::SYS_getegid,
        libc::SYS_getresuid,
        libc::SYS_getresgid,
        libc::SYS_getgroups,
        libc::SYS_capget,
        libc::SYS_getrusage,
        libc::SYS_times,
        libc::SYS_getrlimit,
        libc::SYS_sysinfo,
        libc::SYS_umask,
        libc::SYS_fsync,
        libc::SYS_fdatasync,
        libc::SYS_ftruncate,
        libc::SYS_truncate,
        libc::SYS_fallocate,
        libc::SYS_mkdir,
        libc::SYS_mkdirat,
        libc::SYS_rmdir,
        libc::SYS_unlink,
        libc::SYS_unlinkat,
        libc::SYS_rename,
        libc::SYS_renameat,
        libc::SYS_renameat2,
        libc::SYS_utimensat,
        libc::SYS_statfs,
        libc::SYS_fstatfs,
        libc::SYS_fork,
        libc::SYS_wait4,
        libc::SYS_waitid,
        // Holding/waiting on visible children is read-only; signalling via
        // pidfds remains forbidden, as does pidfd_getfd.
        libc::SYS_pidfd_open,
        libc::SYS_execve,
        libc::SYS_exit,
        libc::SYS_exit_group,
    ] {
        allowed.insert(nr, vec![]);
    }
    // clone is the inspectable glibc fallback. Unknown/high bits, new
    // namespaces, ptrace, pidfd and parent reassignment never match.
    let child_flags = (libc::CLONE_CHILD_SETTID | libc::CLONE_CHILD_CLEARTID) as u64;
    let thread_required = (libc::CLONE_VM
        | libc::CLONE_FS
        | libc::CLONE_FILES
        | libc::CLONE_SIGHAND
        | libc::CLONE_THREAD) as u64;
    let thread_optional =
        child_flags | (libc::CLONE_SYSVSEM | libc::CLONE_SETTLS | libc::CLONE_PARENT_SETTID) as u64;
    let mut clones = vec![
        vec![condition(
            0,
            SeccompCmpOp::MaskedEq(!child_flags),
            libc::SIGCHLD as u64,
        )?],
        vec![condition(
            0,
            SeccompCmpOp::MaskedEq(!thread_optional),
            thread_required,
        )?],
    ];
    if profile.gnu_compiler() {
        clones.push(vec![condition(
            0,
            SeccompCmpOp::Eq,
            (libc::CLONE_VM | libc::CLONE_VFORK | libc::SIGCHLD) as u64,
        )?]);
        for nr in [
            libc::SYS_vfork,
            libc::SYS_chmod,
            libc::SYS_fchmod,
            libc::SYS_fchmodat,
        ] {
            allowed.insert(nr, vec![]);
        }
    }
    allowed.insert(libc::SYS_clone, rules(clones)?);
    // TLS setup is needed by dynamic/static glibc, but user code cannot mutate
    // GS or use arbitrary arch_prctl extensions.
    allowed.insert(
        libc::SYS_arch_prctl,
        rules(vec![vec![condition(
            0,
            SeccompCmpOp::Eq,
            0x1002, // ARCH_SET_FS
        )?]])?,
    );
    // Read-only process information; mutating privilege, dumpability and
    // seccomp settings is not part of the language contract.
    allowed.insert(
        libc::SYS_prctl,
        rules(
            [
                libc::PR_GET_NO_NEW_PRIVS,
                libc::PR_GET_DUMPABLE,
                libc::PR_GET_SECCOMP,
                libc::PR_GET_SECUREBITS,
                libc::PR_GET_KEEPCAPS,
                libc::PR_GET_PDEATHSIG,
                libc::PR_GET_NAME,
                libc::PR_SET_NAME,
            ]
            .into_iter()
            .map(|op| Ok(vec![condition(0, SeccompCmpOp::Eq, op as u64)?]))
            .collect::<io::Result<_>>()?,
        )?,
    );
    // glibc reads its own limit through prlimit64(pid=0,new_limit=NULL).
    allowed.insert(
        libc::SYS_prlimit64,
        rules(vec![vec![
            condition(0, SeccompCmpOp::Eq, 0)?,
            condition(2, SeccompCmpOp::Eq, 0)?,
        ]])?,
    );
    // File/pipe descriptor operations only. Async signal ownership, leases,
    // notify and arbitrary fcntl extensions are outside the language contract.
    let mut fcntl = [
        libc::F_GETFD,
        libc::F_GETFL,
        libc::F_DUPFD,
        libc::F_DUPFD_CLOEXEC,
    ]
    .into_iter()
    .map(|op| Ok(vec![condition(1, SeccompCmpOp::Eq, op as u64)?]))
    .collect::<io::Result<Vec<_>>>()?;
    fcntl.push(vec![
        condition(1, SeccompCmpOp::Eq, libc::F_SETFD as u64)?,
        condition(2, SeccompCmpOp::MaskedEq(!(libc::FD_CLOEXEC as u64)), 0)?,
    ]);
    // The kernel's O_LARGEFILE bit remains visible in F_GETFL on x86_64 even
    // though libc's O_LARGEFILE macro is zero for its 64-bit userspace ABI.
    let flags = (libc::O_ACCMODE
        | libc::O_APPEND
        | libc::O_NONBLOCK
        | libc::O_SYNC
        | libc::O_DSYNC
        | libc::O_NOATIME) as u64
        | 0x8000;
    fcntl.push(vec![
        condition(1, SeccompCmpOp::Eq, libc::F_SETFL as u64)?,
        condition(2, SeccompCmpOp::MaskedEq(!flags), 0)?,
    ]);
    allowed.insert(libc::SYS_fcntl, rules(fcntl)?);
    // Fixed tty/pipe queries. CPython's actual fopen path also needs FIOCLEX:
    // it only sets close-on-exec on a descriptor already owned by this task.
    // FIONCLEX, terminal injection and arbitrary device ioctls stay denied.
    let mut ioctls = vec![libc::TCGETS, libc::FIONREAD];
    if matches!(
        profile,
        ExecutionProfile::PythonCheck | ExecutionProfile::PythonRun
    ) {
        ioctls.extend([libc::TCGETS2, libc::FIOCLEX]);
    }
    allowed.insert(
        libc::SYS_ioctl,
        rules(
            ioctls
                .into_iter()
                // Linux request numbers occupy 32 bits. musl exposes them as
                // signed int (including TCGETS2's high bit), glibc as u64.
                // Reinterpret that fixed bit pattern, never sign-extend it into
                // an allowed high-word ioctl bypass.
                .map(|op| Ok(vec![condition(1, SeccompCmpOp::Eq, u64::from(op as u32))?]))
                .collect::<io::Result<_>>()?,
        )?,
    );
    // Initialization transport only. The FD is fixed, authenticated and
    // CLOEXEC; the final execution stage must additionally close/replace stdio.
    allowed.insert(
        libc::SYS_recvmsg,
        rules(vec![vec![
            condition(0, SeccompCmpOp::Eq, 3)?,
            condition(2, SeccompCmpOp::Eq, libc::MSG_CMSG_CLOEXEC as u64)?,
        ]])?,
    );
    // PID1 returns an owned main pidfd to its authenticated monitor. User
    // children never retain fd3's Unix endpoint: fork_exec closes/replaces it.
    allowed.insert(
        libc::SYS_sendmsg,
        rules(vec![vec![
            condition(0, SeccompCmpOp::Eq, 3)?,
            condition(2, SeccompCmpOp::Eq, libc::MSG_NOSIGNAL as u64)?,
        ]])?,
    );
    // UnixStream::write uses send(MSG_NOSIGNAL), encoded as sendto with no
    // destination on x86_64. It is not a network address exception.
    allowed.insert(
        libc::SYS_sendto,
        rules(vec![vec![
            condition(0, SeccompCmpOp::Eq, 3)?,
            condition(4, SeccompCmpOp::Eq, 0)?,
            condition(5, SeccompCmpOp::Eq, 0)?,
        ]])?,
    );
    allowed.insert(
        libc::SYS_shutdown,
        rules(vec![vec![condition(0, SeccompCmpOp::Eq, 3)?]])?,
    );
    allowed.insert(
        libc::SYS_setsockopt,
        rules(
            [libc::SO_RCVTIMEO, libc::SO_SNDTIMEO]
                .into_iter()
                .map(|opt| {
                    Ok(vec![
                        condition(0, SeccompCmpOp::Eq, 3)?,
                        condition(1, SeccompCmpOp::Eq, libc::SOL_SOCKET as u64)?,
                        condition(2, SeccompCmpOp::Eq, opt as u64)?,
                    ])
                })
                .collect::<io::Result<_>>()?,
        )?,
    );
    let base: BpfProgram = SeccompFilter::new(
        allowed,
        SeccompAction::Errno(libc::EPERM as u32),
        SeccompAction::Allow,
        TargetArch::x86_64,
    )
    .map_err(io::Error::other)?
    .try_into()
    .map_err(io::Error::other)?;
    // clone3 has pointer arguments that classic BPF cannot safely inspect.
    // Return ENOSYS to select glibc's argument-checked clone fallback. Guard
    // the ABI before this exception; the generated base also checks the ABI.
    let mut program = vec![
        sock_filter {
            code: 0x20,
            jt: 0,
            jf: 0,
            k: 4,
        },
        sock_filter {
            code: 0x15,
            jt: 1,
            jf: 0,
            k: AUDIT_X86_64,
        },
        sock_filter {
            code: 0x06,
            jt: 0,
            jf: 0,
            k: libc::SECCOMP_RET_KILL_PROCESS,
        },
        sock_filter {
            code: 0x20,
            jt: 0,
            jf: 0,
            k: 0,
        },
        sock_filter {
            code: 0x15,
            jt: 0,
            jf: 1,
            k: libc::SYS_clone3 as u32,
        },
        sock_filter {
            code: 0x06,
            jt: 0,
            jf: 0,
            k: libc::SECCOMP_RET_ERRNO | libc::ENOSYS as u32,
        },
    ];
    program.extend(base);
    if program.len() > MAX_INSTRUCTIONS {
        return Err(denied("seccomp instruction budget"));
    }
    Ok(program)
}

pub(crate) fn filter_count(status: &str) -> io::Result<u32> {
    let field = |name| {
        status
            .lines()
            .find_map(|line| line.strip_prefix(name))
            .and_then(|s| s.trim().parse::<u32>().ok())
    };
    if field("Seccomp:") != Some(2) || field("NoNewPrivs:") != Some(1) {
        return Err(denied("seccomp status readback"));
    }
    field("Seccomp_filters:").ok_or_else(|| denied("missing seccomp filter count"))
}

pub(crate) fn install(profile: ExecutionProfile) -> io::Result<()> {
    crate::launcher::entry::ensure_single_threaded()?;
    let uid = rustix::process::getuid().as_raw();
    let gid = rustix::process::getgid().as_raw();
    if !crate::isolation::identity::valid(uid, gid) {
        return Err(denied("seccomp task identity precondition"));
    }
    crate::isolation::identity::verify_status(
        &std::fs::read_to_string("/proc/self/status")?,
        uid,
        gid,
    )?;
    let program = compile(profile)?;
    seccompiler::apply_filter_all_threads(&program).map_err(|e| match e {
        seccompiler::Error::Prctl(e) | seccompiler::Error::Seccomp(e) => e,
        other => io::Error::other(other),
    })?;
    filter_count(&std::fs::read_to_string("/proc/self/status")?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    const PROFILES: [ExecutionProfile; 7] = [
        ExecutionProfile::Native,
        ExecutionProfile::CompileC,
        ExecutionProfile::CompileCpp,
        ExecutionProfile::PythonCheck,
        ExecutionProfile::PythonRun,
        ExecutionProfile::JavaCompile,
        ExecutionProfile::JavaRun,
    ];
    // Evaluate generated classic BPF against synthetic seccomp_data; this is
    // an ordinary policy regression, not evidence of kernel installation.
    fn evaluate(p: &[sock_filter], arch: u32, nr: u32, args: [u64; 6]) -> u32 {
        let mut data = [0_u8; 64];
        data[..4].copy_from_slice(&nr.to_le_bytes());
        data[4..8].copy_from_slice(&arch.to_le_bytes());
        for (i, arg) in args.into_iter().enumerate() {
            data[16 + i * 8..24 + i * 8].copy_from_slice(&arg.to_le_bytes());
        }
        let (mut pc, mut a) = (0_usize, 0_u32);
        for _ in 0..MAX_INSTRUCTIONS {
            let ins = &p[pc];
            pc += 1;
            match ins.code {
                0x20 => {
                    a = u32::from_le_bytes(
                        data[ins.k as usize..ins.k as usize + 4].try_into().unwrap(),
                    )
                }
                0x54 => a &= ins.k,
                0x05 => pc += ins.k as usize,
                0x15 => pc += if a == ins.k { ins.jt } else { ins.jf } as usize,
                0x25 => pc += if a > ins.k { ins.jt } else { ins.jf } as usize,
                0x35 => pc += if a >= ins.k { ins.jt } else { ins.jf } as usize,
                0x06 => return ins.k,
                _ => panic!("unexpected BPF opcode"),
            }
        }
        panic!("BPF budget exceeded");
    }
    #[test]
    fn abi_unknown_calls_and_clone_flags_fail_closed() {
        for profile in PROFILES {
            let p = compile(profile).unwrap();
            let deny = libc::SECCOMP_RET_ERRNO | libc::EPERM as u32;
            assert_eq!(
                evaluate(&p, 0x40000003, 20, [0; 6]),
                libc::SECCOMP_RET_KILL_PROCESS
            );
            assert_eq!(
                evaluate(&p, 0x40000003, libc::SYS_clone3 as u32, [0; 6]),
                libc::SECCOMP_RET_KILL_PROCESS
            );
            for nr in [0x40000000 | libc::SYS_getuid as u32, u32::MAX, 1023] {
                assert_eq!(evaluate(&p, AUDIT_X86_64, nr, [0; 6]), deny);
            }
            assert_eq!(
                evaluate(&p, AUDIT_X86_64, libc::SYS_clone3 as u32, [0; 6]),
                libc::SECCOMP_RET_ERRNO | libc::ENOSYS as u32
            );
            for flags in [
                libc::CLONE_NEWUSER as u64,
                libc::CLONE_NEWPID as u64,
                libc::CLONE_PARENT as u64,
                1_u64 << 32,
                libc::CLONE_PTRACE as u64,
            ] {
                let mut args = [0; 6];
                args[0] = flags | libc::SIGCHLD as u64;
                assert_eq!(
                    evaluate(&p, AUDIT_X86_64, libc::SYS_clone as u32, args),
                    deny
                );
            }
        }
    }
    #[test]
    fn compile_profile_exception_does_not_widen_native() {
        let flags = (libc::CLONE_VM | libc::CLONE_VFORK | libc::SIGCHLD) as u64;
        for profile in PROFILES {
            let p = compile(profile).unwrap();
            let expected = if profile.gnu_compiler() {
                libc::SECCOMP_RET_ALLOW
            } else {
                libc::SECCOMP_RET_ERRNO | libc::EPERM as u32
            };
            assert_eq!(
                evaluate(
                    &p,
                    AUDIT_X86_64,
                    libc::SYS_clone as u32,
                    [flags, 0, 0, 0, 0, 0]
                ),
                expected
            );
            assert_eq!(
                evaluate(&p, AUDIT_X86_64, libc::SYS_chmod as u32, [0; 6]),
                expected
            );
        }
    }

    #[test]
    fn dangerous_calls_and_pointer_or_high_bit_argument_bypasses_are_denied() {
        for profile in PROFILES {
            let p = compile(profile).unwrap();
            let deny = libc::SECCOMP_RET_ERRNO | libc::EPERM as u32;
            for nr in [
                libc::SYS_socket,
                libc::SYS_socketpair,
                libc::SYS_connect,
                libc::SYS_ptrace,
                libc::SYS_process_vm_readv,
                libc::SYS_unshare,
                libc::SYS_setns,
                libc::SYS_mount,
                libc::SYS_chroot,
                libc::SYS_bpf,
                libc::SYS_perf_event_open,
                libc::SYS_io_uring_setup,
                libc::SYS_userfaultfd,
                libc::SYS_keyctl,
                libc::SYS_capset,
                libc::SYS_setuid,
                libc::SYS_setgroups,
                libc::SYS_kill,
                libc::SYS_tgkill,
                libc::SYS_seccomp,
                libc::SYS_sendmsg,
            ] {
                assert_eq!(evaluate(&p, AUDIT_X86_64, nr as u32, [0; 6]), deny);
            }
            for (nr, args) in [
                (libc::SYS_ioctl, [4, libc::TIOCSTI, 0, 0, 0, 0]),
                (libc::SYS_ioctl, [4, (1 << 32) | libc::FIONREAD, 0, 0, 0, 0]),
                (libc::SYS_fcntl, [4, libc::F_SETOWN as u64, 0, 0, 0, 0]),
                (
                    libc::SYS_fcntl,
                    [4, libc::F_SETFL as u64, libc::O_ASYNC as u64, 0, 0, 0],
                ),
                (
                    libc::SYS_prctl,
                    [libc::PR_SET_DUMPABLE as u64, 0, 0, 0, 0, 0],
                ),
                (libc::SYS_prlimit64, [0, 0, 16, 0, 0, 0]),
                (libc::SYS_sendto, [3, 0, 0, 0, 16, 0]),
                (libc::SYS_recvmsg, [3, 0, 0, 0, 0, 0]),
            ] {
                assert_eq!(evaluate(&p, AUDIT_X86_64, nr as u32, args), deny);
            }
            for (nr, args) in [
                (libc::SYS_ioctl, [4, libc::FIONREAD, 0, 0, 0, 0]),
                (
                    libc::SYS_fcntl,
                    [4, libc::F_SETFL as u64, libc::O_NONBLOCK as u64, 0, 0, 0],
                ),
                (libc::SYS_prctl, [libc::PR_SET_NAME as u64, 16, 0, 0, 0, 0]),
                (
                    libc::SYS_recvmsg,
                    [3, 0, libc::MSG_CMSG_CLOEXEC as u64, 0, 0, 0],
                ),
            ] {
                assert_eq!(
                    evaluate(&p, AUDIT_X86_64, nr as u32, args),
                    libc::SECCOMP_RET_ALLOW
                );
            }
        }
    }

    #[test]
    fn python_descriptor_compatibility_never_grants_other_ioctl_operations() {
        for profile in PROFILES {
            let program = compile(profile).unwrap();
            let denied = libc::SECCOMP_RET_ERRNO | libc::EPERM as u32;
            let expected = if matches!(
                profile,
                ExecutionProfile::PythonCheck | ExecutionProfile::PythonRun
            ) {
                libc::SECCOMP_RET_ALLOW
            } else {
                denied
            };
            for request in [libc::FIOCLEX, libc::TCGETS2] {
                assert_eq!(
                    evaluate(
                        &program,
                        AUDIT_X86_64,
                        libc::SYS_ioctl as u32,
                        [3, request, 0, 0, 0, 0]
                    ),
                    expected
                );
                assert_eq!(
                    evaluate(
                        &program,
                        AUDIT_X86_64,
                        libc::SYS_ioctl as u32,
                        [3, (1 << 32) | request, 0, 0, 0, 0]
                    ),
                    denied
                );
            }
            for request in [libc::FIONCLEX, libc::TIOCSTI] {
                assert_eq!(
                    evaluate(
                        &program,
                        AUDIT_X86_64,
                        libc::SYS_ioctl as u32,
                        [3, request, 0, 0, 0, 0]
                    ),
                    denied
                );
            }
        }
    }
}
