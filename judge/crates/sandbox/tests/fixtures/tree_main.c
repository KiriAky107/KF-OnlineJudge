#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <unistd.h>
static void require(int yes){if(!yes){fprintf(stderr,"tree user proof failure errno=%d\n",errno);_exit(2);}}
int main(int argc,char **argv){
    require(argc==2&&getpid()>1&&getppid()==1&&getuid()==100000&&prctl(PR_GET_SECCOMP)==2);
    DIR *dir=opendir("/proc/self/fd");require(dir!=NULL);int scanner=dirfd(dir);struct dirent *e;
    while((e=readdir(dir)))if(e->d_name[0]!='.'){int fd=atoi(e->d_name);require(fd<=2||fd==scanner);}
    closedir(dir);char data;require(read(0,&data,1)==0);
    puts("KF_TREE_USER_PROOF pid_gt1=1 parent_init=1 reduced=1 filtered=1 fd012_only=1 eof=1");fflush(stdout);
    if(!strcmp(argv[1],"normal"))return 7;
    pid_t child=fork();require(child>=0);
    if(child)return 7;
    if(!strcmp(argv[1],"orphan-short")){
        for(unsigned i=0;i<100&&getppid()!=1;i++)usleep(1000);
        require(getppid()==1);fputs("KF_ORPHAN_ADOPTED_PROOF\n",stderr);return 3;
    }
    for(;;)sleep(10);
}
