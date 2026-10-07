/* Fixed independent-VM probe; never installed as a user execution endpoint. */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>
static void require(int yes,const char *why){if(!yes){fprintf(stderr,"seccomp probe: %s errno=%d\n",why,errno);exit(1);}}
static void filtered(void){
    require(prctl(PR_GET_SECCOMP)==2,"mode");
    require(prctl(PR_GET_NO_NEW_PRIVS,0,0,0,0)==1,"nnp");
    FILE *f=fopen("/proc/self/status","r");require(f!=NULL,"status");
    char line[256];unsigned count=0;
    while(fgets(line,sizeof line,f))if(sscanf(line,"Seccomp_filters: %u",&count)==1)break;
    fclose(f);require(count==1,"one production filter");
    errno=0;require(syscall(0x40000000|SYS_getuid)==-1&&errno==EPERM,"x32 rejected");
    errno=0;require(syscall(SYS_clone3,NULL,0)==-1&&errno==ENOSYS,"clone3 fallback");
    errno=0;require(syscall(1023)==-1&&errno==EPERM,"unknown rejected");
}
static void *thread(void *p){filtered();return p;}
int main(int argc,char **argv){
    require(argc==2,"fixed args");filtered();
    if(!strcmp(argv[1],"--exec-child")){puts("KF_SECCOMP_EXEC_INHERIT_PROOF");return 0;}
    pthread_t t;require(pthread_create(&t,NULL,thread,(void*)17)==0,"pthread create");
    void *result=NULL;require(pthread_join(t,&result)==0&&result==(void*)17,"pthread join");
    pid_t p=fork();require(p>=0,"fork");
    if(!p){filtered();execl("/kf/bin/seccomp-inherit-probe","seccomp-inherit-probe","--exec-child",(char*)0);_exit(3);}
    int status=0;require(waitpid(p,&status,0)==p&&WIFEXITED(status)&&WEXITSTATUS(status)==0,"fork exec wait");
    p=fork();require(p>=0,"i386 fork");
    if(!p){long value;__asm__ volatile("int $0x80":"=a"(value):"0"(20):"memory");_exit(value?4:5);}
    require(waitpid(p,&status,0)==p&&WIFSIGNALED(status)&&WTERMSIG(status)==SIGSYS,"i386 killed");
    printf("KF_SECCOMP_INHERIT_PROOF profile=%s mode=2 nnp=1 filters=1 fork_exec_thread=1 x32_eperm=1 i386_sigsys=1 clone3_enosys=1 unknown_eperm=1\n",argv[1]);
    return 0;
}
