/* Fixed resource-limit programs: guarded whole-capped independent VM only. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
static void require(int yes,const char *why){if(!yes){fprintf(stderr,"rlimit: %s errno=%d\n",why,errno);exit(40);}}
static void verify(void){
    const int resources[]={RLIMIT_FSIZE,RLIMIT_NOFILE,RLIMIT_STACK,RLIMIT_CORE};
    const rlim_t limits[]={4096,16,262144,0};
    for(unsigned i=0;i<4;i++){struct rlimit r;require(getrlimit(resources[i],&r)==0&&r.rlim_cur==limits[i]&&r.rlim_max==limits[i],"finite soft/hard inheritance");}
    require(getuid()==100000&&geteuid()==100000&&prctl(PR_GET_SECCOMP)==2,"reduced filtered identity");
}
static void *thread(void *unused){(void)unused;verify();return NULL;}
static void recurse(unsigned n){volatile unsigned char bytes[8192];memset((void*)bytes,1,sizeof bytes);if(n)recurse(n-1);require(bytes[0]==1,"stack frame");}
int main(int argc,char **argv){
    require(argc==2,"fixed case");verify();
    if(!strcmp(argv[1],"inherited-exec")){puts("KF_RLIMIT_CHILD_EXEC_PROOF soft_hard=1");return 0;}
    if(!strcmp(argv[1],"inherit")){
        pthread_t t;require(pthread_create(&t,NULL,thread,NULL)==0&&pthread_join(t,NULL)==0,"pthread inheritance");
        pid_t p=fork();require(p>=0,"fork inheritance");
        if(!p){verify();execl("/kf/bin/rlimit-inherit-probe","rlimit-inherit-probe","inherited-exec",(char*)0);_exit(41);}
        int status;require(waitpid(p,&status,0)==p&&WIFEXITED(status)&&WEXITSTATUS(status)==0,"exec inheritance");
        struct rlimit raised={.rlim_cur=17,.rlim_max=17};errno=0;require(setrlimit(RLIMIT_NOFILE,&raised)==-1&&errno==EPERM,"no limit raising through filter");
        puts("KF_RLIMIT_PROOF mode=inherit fork=1 exec=1 pthread=1 four_limits=1 raise_denied=1");return 0;
    }
    if(!strcmp(argv[1],"files")){
        require(signal(SIGXFSZ,SIG_IGN)!=SIG_ERR,"SIGXFSZ ignore for EFBIG evidence");
        int fd=open("/kf/rlimit-work/limited-file",O_WRONLY|O_CREAT|O_EXCL,0600);require(fd>=0,"work file");
        char data[8192];memset(data,1,sizeof data);
        require(write(fd,data,sizeof data)==4096,"finite file partial write");errno=0;require(write(fd,data,1)==-1&&errno==EFBIG,"file limit EFBIG");require(close(fd)==0,"file close");
        puts("KF_RLIMIT_PROOF mode=files finite4096=1 efbig=1");return 0;
    }
    if(!strcmp(argv[1],"fds")){
        int fds[20],n=0;for(;n<20;n++){fds[n]=open("/dev/null",O_RDONLY);if(fds[n]<0)break;require(fds[n]<16,"descriptor above NOFILE");}
        require(n>0&&n<20&&errno==EMFILE,"FD limit EMFILE");for(int i=0;i<n;i++)require(close(fds[i])==0,"FD close");
        puts("KF_RLIMIT_PROOF mode=fds emfile=1 maxfd15=1");return 0;
    }
    if(!strcmp(argv[1],"stack")){puts("KF_RLIMIT_STACK_READY soft_hard262144=1 core0=1");fflush(stdout);recurse(128);return 42;}
    return 43;
}
