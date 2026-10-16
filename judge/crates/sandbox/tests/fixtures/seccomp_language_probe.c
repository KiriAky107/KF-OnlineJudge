/* Fixed actual compiler/dynamic runtime compatibility; no arbitrary argv. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>
static void require(int yes,const char *why){if(!yes){fprintf(stderr,"language probe: %s errno=%d\n",why,errno);exit(1);}}
static void compile(int cpp){
    pid_t p=fork();require(p>=0,"compiler fork");
    if(!p){
        const char *compiler=cpp?"/usr/bin/g++":"/usr/bin/gcc";
        execl(compiler,compiler,"-O0","-pthread",cpp?"/kf/fixtures/seccomp_sample.cpp":"/kf/fixtures/seccomp_sample.c","-o",cpp?"/kf/seccomp-work/cpp.bin":"/kf/seccomp-work/c.bin",(char*)0);_exit(2);
    }
    int status;require(waitpid(p,&status,0)==p&&WIFEXITED(status)&&WEXITSTATUS(status)==0,"GNU compiler success");
}
static void run(int cpp,int empty){
    const unsigned char expected[]={0,'K','F',255,10,13,0,7};
    int input[2],output[2];require(pipe2(input,O_CLOEXEC)==0&&pipe2(output,O_CLOEXEC)==0,"bounded pipes");
    pid_t p=fork();require(p>=0,"dynamic program fork");
    if(!p){
        require(dup2(input[0],0)==0&&dup2(output[1],1)==1,"stdio binding");
        close(input[0]);close(input[1]);close(output[0]);close(output[1]);
        const char *bin=cpp?"/kf/seccomp-work/cpp.bin":"/kf/seccomp-work/c.bin";
        execl(bin,bin,(char*)0);_exit(3);
    }
    close(input[0]);close(output[1]);
    if(!empty)require(write(input[1],expected,sizeof expected)==sizeof expected,"binary input");
    close(input[1]);unsigned char actual[16];size_t n=0;ssize_t result;
    while((result=read(output[0],actual+n,sizeof actual-n))>0){n+=(size_t)result;require(n<sizeof actual,"bounded fixed output");}
    require(result==0&&n==sizeof expected&&!memcmp(expected,actual,n),"exact binary output and EOF");close(output[0]);
    int status;require(waitpid(p,&status,0)==p&&WIFEXITED(status)&&WEXITSTATUS(status)==7,"normal nonzero exit");
}
int main(int argc,char **argv){
    require(argc==2&&prctl(PR_GET_SECCOMP)==2,"filtered fixed fixture");
    require(chdir("/kf/seccomp-work")==0,"private VM fixture work");
    if(!strcmp(argv[1],"compile-c")){compile(0);run(0,0);run(0,1);}
    else if(!strcmp(argv[1],"compile-cpp")){compile(1);run(1,0);run(1,1);}
    else if(!strcmp(argv[1],"native")){run(0,0);run(0,1);run(1,0);run(1,1);}
    else return 2;
    printf("KF_SECCOMP_LANGUAGE_PROOF profile=%s real_gnu=1 dynamic=1 pthread=1 empty_eof=1 binary_io=1 normal_exit7=1\n",argv[1]);return 0;
}
