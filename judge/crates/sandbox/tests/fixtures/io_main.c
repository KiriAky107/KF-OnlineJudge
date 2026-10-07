#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>
static void require(int yes){if(!yes)_exit(2);}
static void write_all(int fd,const unsigned char *bytes,size_t n){while(n){ssize_t k=write(fd,bytes,n);if(k<0&&errno==EINTR)continue;require(k>0);bytes+=k;n-=(size_t)k;}}
static void pattern(int fd,unsigned char xor){unsigned char bytes[4096];for(unsigned offset=0;offset<262144;offset+=sizeof bytes){for(unsigned i=0;i<sizeof bytes;i++)bytes[i]=(unsigned char)((offset+i)%251)^xor;write_all(fd,bytes,sizeof bytes);}}
int main(int argc,char **argv){
    require(argc==2&&getpid()>1&&getppid()==1&&getuid()==100000&&prctl(PR_GET_SECCOMP)==2);
    DIR *dir=opendir("/proc/self/fd");require(dir!=NULL);int scanner=dirfd(dir);struct dirent *entry;
    while((entry=readdir(dir))){if(entry->d_name[0]!='.'){int fd=atoi(entry->d_name);require(fd<=2||fd==scanner);}}
    closedir(dir);
    require(open("/proc/1/fd/3",O_RDONLY|O_CLOEXEC)<0&&(errno==EACCES||errno==ENOENT));
    require(open("/proc/1/root",O_RDONLY|O_DIRECTORY|O_CLOEXEC)<0&&(errno==EACCES||errno==ENOENT));
    if(!strcmp(argv[1],"empty")){char byte;require(read(0,&byte,1)==0);return 7;}
    if(!strcmp(argv[1],"early-close")){require(close(0)==0);const unsigned char out[]={'C','L','O','S','E','D',0,255};write_all(1,out,sizeof out);return 7;}
    if(!strcmp(argv[1],"stdout-limit")||!strcmp(argv[1],"stderr-limit")){
        pid_t child=fork();require(child>=0);if(!child){for(;;)sleep(10);}
        unsigned char bytes[4096];memset(bytes,0x5a,sizeof bytes);int fd=!strcmp(argv[1],"stdout-limit")?1:2;
        // Fixed bounded flood in the independent VM. The descendant holding
        // both pipe writers must also disappear when the monitor cancels.
        for(unsigned i=0;i<32;i++){write_all(fd,bytes,sizeof bytes);}
        for(;;){sleep(10);}
    }
    require(!strcmp(argv[1],"duplex")||!strcmp(argv[1],"storage-fail"));pid_t child=fork();require(child>=0);
    if(!child){pattern(2,0xa5);return 0;}
    pattern(1,0);unsigned char input[4096];unsigned total=0;
    for(;;){ssize_t n=read(0,input,sizeof input);if(n<0&&errno==EINTR)continue;require(n>=0);if(!n)break;for(ssize_t i=0;i<n;i++)require(input[i]==(unsigned char)((total+(unsigned)i)%251));total+=(unsigned)n;require(total<=262144);}
    require(total==262144);int status;require(waitpid(child,&status,0)==child&&WIFEXITED(status)&&WEXITSTATUS(status)==0);return 7;
}
