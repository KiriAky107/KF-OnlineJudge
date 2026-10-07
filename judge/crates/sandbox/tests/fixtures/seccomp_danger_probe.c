/* Fixed syscall boundary checks; only run in the capped credential-free VM. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/sched.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>
static void require(int yes,const char *why){if(!yes){fprintf(stderr,"danger probe: %s errno=%d\n",why,errno);exit(1);}}
static unsigned denied;
#define DENY(call) do {errno=0;long result=(call);require(result==-1&&errno==EPERM,#call);denied++;}while(0)
static void *thread(void *p){
    require(pthread_setname_np(pthread_self(),"kf-language")==0,"pthread naming");
    require(prctl(PR_GET_SECCOMP)==2,"thread filtered");return p;
}
int main(int argc,char **argv){
    require(argc==2,"fixed args");
    if(!strcmp(argv[1],"baseline")) {
        require(prctl(PR_GET_SECCOMP)==0,"baseline no filter");
        int fd=socket(AF_INET,SOCK_STREAM,0);require(fd>=0,"baseline inet socket");close(fd);
        int pair[2];require(socketpair(AF_UNIX,SOCK_STREAM,0,pair)==0,"baseline unix socketpair");close(pair[0]);close(pair[1]);
        require(prctl(PR_SET_DUMPABLE,1)==0,"baseline dumpability setter");
        pid_t child=fork();require(child>=0,"baseline fork");
        if(!child){_exit(ptrace(PTRACE_TRACEME,0,0,0)==0?0:4);}
        int status=0;require(waitpid(child,&status,0)==child&&WIFEXITED(status)&&WEXITSTATUS(status)==0,"baseline ptrace usable");
        puts("KF_SECCOMP_BASELINE_PROOF same_reduced_identity=1 no_filter=1 socket=1 socketpair=1 ptrace=1 dumpability=1");return 0;
    }
    require(prctl(PR_GET_SECCOMP)==2,"production filter");
    int pair[2]={-1,-1};
    DENY(socket(AF_INET,SOCK_STREAM,0));DENY(socket(AF_INET6,SOCK_STREAM,0));
    DENY(socket(AF_UNIX,SOCK_STREAM,0));DENY(socketpair(AF_UNIX,SOCK_STREAM,0,pair));
    DENY(syscall(SYS_connect,-1,NULL,0));DENY(syscall(SYS_bind,-1,NULL,0));
    DENY(syscall(SYS_listen,-1,1));DENY(syscall(SYS_accept4,-1,NULL,NULL,0));
    DENY(syscall(SYS_sendto,1,"x",1,0,NULL,0));DENY(syscall(SYS_recvmsg,1,NULL,0));
    DENY(syscall(SYS_ptrace,PTRACE_TRACEME,0,0,0));
    DENY(syscall(SYS_process_vm_readv,getpid(),NULL,0,NULL,0,0));
    DENY(syscall(SYS_process_vm_writev,getpid(),NULL,0,NULL,0,0));
    DENY(syscall(SYS_unshare,CLONE_NEWUSER));DENY(syscall(SYS_unshare,CLONE_NEWNET));
    DENY(syscall(SYS_setns,-1,CLONE_NEWNET));
    for(unsigned long flags=0, i=0;i<4;i++){
        const unsigned long rejected[]={CLONE_NEWUSER,CLONE_NEWPID,CLONE_PARENT,CLONE_PTRACE};
        flags=rejected[i]|SIGCHLD;DENY(syscall(SYS_clone,flags,NULL,NULL,NULL,0));
    }
    DENY(syscall(SYS_clone,(1UL<<32)|SIGCHLD,NULL,NULL,NULL,0));
    DENY(syscall(SYS_mount,"none","/not-existent","tmpfs",0,NULL));
    DENY(syscall(SYS_umount2,"/not-existent",0));DENY(syscall(SYS_chroot,"/"));
    DENY(syscall(SYS_pivot_root,"/","/not-existent"));
    DENY(syscall(SYS_bpf,0,NULL,0));DENY(syscall(SYS_perf_event_open,NULL,0,-1,-1,0));
    DENY(syscall(SYS_userfaultfd,0));DENY(syscall(SYS_io_uring_setup,1,NULL));
    DENY(syscall(SYS_keyctl,0,0,0,0,0));DENY(syscall(SYS_kexec_load,0,0,NULL,0));
    DENY(syscall(SYS_init_module,NULL,0,""));DENY(syscall(SYS_reboot,0,0,0,NULL));
    DENY(syscall(SYS_mknod,"/not-existent",0,0));DENY(syscall(SYS_setuid,0));
    DENY(syscall(SYS_setgid,0));DENY(syscall(SYS_setgroups,0,NULL));
    DENY(syscall(SYS_capset,NULL,NULL));DENY(syscall(SYS_seccomp,0,0,NULL));
    DENY(prctl(PR_SET_DUMPABLE,1));DENY(prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0));
    DENY(syscall(SYS_kill,getpid(),0));DENY(syscall(SYS_tgkill,getpid(),syscall(SYS_gettid),0));
    DENY(syscall(SYS_rt_tgsigqueueinfo,getpid(),syscall(SYS_gettid),0,NULL));
    require(pipe2(pair,O_CLOEXEC)==0,"language pipe");
    require(write(pair[1],"abc",3)==3,"pipe write");
    int available=0;require(ioctl(pair[0],FIONREAD,&available)==0&&available==3,"bounded ioctl query");
    require(fcntl(pair[0],F_SETFL,O_NONBLOCK)==0,"nonblocking pipe");
    DENY(fcntl(pair[0],F_SETFL,O_ASYNC));DENY(fcntl(pair[0],F_SETOWN,getpid()));
    DENY(syscall(SYS_fcntl,pair[0],F_SETFL,1UL<<40));
    DENY(ioctl(pair[0],TIOCSTI,"x"));DENY(syscall(SYS_ioctl,pair[0],(1UL<<32)|FIONREAD,&available));
    close(pair[0]);close(pair[1]);
    pthread_t t;require(pthread_create(&t,NULL,thread,(void*)23)==0,"bounded language thread");
    void *result=NULL;require(pthread_join(t,&result)==0&&result==(void*)23,"language join");
    printf("KF_SECCOMP_DANGER_PROOF profile=%s denied=%u network=1 ptrace=1 namespaces=1 privilege=1 signal=1 device=1 async_fcntl=1 language_thread_name=1 pipe_query=1\n",argv[1],denied);
    return 0;
}
