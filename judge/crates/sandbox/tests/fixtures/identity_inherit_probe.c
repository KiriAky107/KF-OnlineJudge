#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <linux/capability.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>
static int check(void) {
    uid_t r,e,s; gid_t gr,ge,gs;
    if(getresuid(&r,&e,&s)||getresgid(&gr,&ge,&gs)) return 1;
    if(r!=100000||e!=r||s!=r||gr!=r||ge!=r||gs!=r||getgroups(0,NULL)!=0) return 2;
    struct __user_cap_header_struct h={_LINUX_CAPABILITY_VERSION_3,0};
    struct __user_cap_data_struct d[2]={{0}};
    if(syscall(SYS_capget,&h,d)) return 3;
    for(int i=0;i<2;i++) if(d[i].effective||d[i].permitted||d[i].inheritable) return 4;
    if(prctl(PR_GET_NO_NEW_PRIVS,0,0,0,0)!=1||prctl(PR_GET_SECUREBITS,0,0,0,0)!=0xeb) return 5;
    for(int cap=0;cap<64;cap++) {
        int b=prctl(PR_CAPBSET_READ,cap,0,0,0);
        if(b==-1 && errno==EINVAL) break;
        if(b!=0||prctl(PR_CAP_AMBIENT,PR_CAP_AMBIENT_IS_SET,cap,0,0)!=0) return 6;
    }
    if(setuid(0)!=-1||errno!=EPERM||setgid(0)!=-1||errno!=EPERM) return 7;
    gid_t group=0;
    if(setgroups(1,&group)!=-1||errno!=EPERM) return 8;
    if(mount("tmpfs","/tmp", "tmpfs",0,NULL)!=-1||errno!=EPERM) return 9;
    d[0].permitted=d[0].effective=1U<<CAP_SYS_ADMIN;
    if(syscall(SYS_capset,&h,d)!=-1||errno!=EPERM) return 10;
    if(prctl(PR_SET_SECUREBITS,0,0,0,0)!=-1||errno!=EPERM) return 11;
    return 0;
}
static void *thread_check(void *unused) { (void)unused; return (void *)(long)check(); }
int main(int argc,char **argv) {
    if(argc==2 && !strcmp(argv[1],"--suid-control-child")) {
        if(access("/kf/INDEPENDENT_VM",R_OK)||geteuid()!=0||prctl(PR_GET_NO_NEW_PRIVS,0,0,0,0)!=0) return 30;
        if(setgroups(0,NULL)||setresgid(100000,100000,100000)||setresuid(100000,100000,100000))return 31;
        execl("/kf/bin/identity-suid-probe","identity-suid-probe","--suid-positive",(char *)0);return 32;
    }
    if(argc==2 && !strcmp(argv[1],"--suid-positive")) {
        if(getuid()!=100000||geteuid()!=0) return 33;
        puts("KF_SUID_CONTROL_PROOF works_without_nnp=1");return 0;
    }
    int result=check(); if(result){fprintf(stderr,"credential check=%d\n",result);return result;}
    pthread_t thread; void *thread_result;
    if(pthread_create(&thread,NULL,thread_check,NULL)||pthread_join(thread,&thread_result)||thread_result) return 20;
    pid_t child=fork();if(child<0)return 21;if(!child)_exit(check());
    int status;if(waitpid(child,&status,0)!=child||!WIFEXITED(status)||WEXITSTATUS(status))return 22;
    if(argc==2) {
        pid_t peer=(pid_t)strtol(argv[1],NULL,10); if(peer<=1)return 23;
        if(kill(peer,0)!=-1||errno!=EPERM)return 24;
        if(ptrace(PTRACE_ATTACH,peer,NULL,NULL)!=-1||errno!=EPERM)return 25;
        char path[64];snprintf(path,sizeof path,"/proc/%d/root/work/main",peer);
        int fd=open(path,O_RDONLY|O_CLOEXEC);if(fd>=0){close(fd);return 26;}
        if(errno!=EACCES&&errno!=EPERM)return 27;
    } else if(argc!=1) return 28;
    puts("KF_IDENTITY_INHERIT_PROBE ids=1 groups=0 caps=0 nnp=1 securebits=1 escalation_denied=1 fork_thread_inherit=1 peer_denied=1");
    return 0;
}
