/* VM-only kernel failure injector. It execs one fixed trusted test case. */
#define _GNU_SOURCE
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <errno.h>
#include <stddef.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>
int main(void) {
    const char *vm=getenv("KF_SANDBOX_INDEPENDENT_VM");
    if(!vm||vm[0]!='1'||vm[1]||access("/kf/INDEPENDENT_VM",R_OK))return 2;
    struct sock_filter filter[]={
        BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,arch)),
        BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,AUDIT_ARCH_X86_64,1,0),
        BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr)),
        BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,__NR_mknodat,0,1),
        BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM),
        BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW)
    };
    struct sock_fprog program={.len=sizeof filter/sizeof *filter,.filter=filter};
    if(prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0)||prctl(PR_SET_SECCOMP,SECCOMP_MODE_FILTER,&program))return 3;
    setenv("KF_SANDBOX_DENY_MKNOD","1",1);
    execl("/kf/bin/proc-dev-vm-tests","proc-dev-vm-tests","--ignored","--test-threads=1","--exact","kernel_mknod_failure_rolls_back_proc_dev_and_root","--nocapture",(char*)0);
    return 4;
}
