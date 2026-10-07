#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static void emit(int fd, size_t total) {
    unsigned char b[4096]; for(size_t i=0;i<sizeof b;i++)b[i]=(unsigned char)i;
    for(size_t offset=0;offset<total;) {size_t n=total-offset;if(n>sizeof b)n=sizeof b;ssize_t w=write(fd,b,n);if(w<=0)_exit(111);offset+=(size_t)w;}
}
static void child(void) {pid_t p=fork();if(p<0)_exit(112);if(p==0){for(;;)usleep(10000);}}
static int fill(const char *dir) {
    unsigned char b[4096]={0};int saw=0;
    for(int i=0;i<128&&!saw;i++){char name[80];snprintf(name,sizeof name,"%s/file-%d",dir,i);int fd=open(name,O_WRONLY|O_CREAT|O_EXCL,0600);if(fd<0){if(errno==ENOSPC){saw=1;break;}return 1;}ssize_t n=write(fd,b,sizeof b);if(n<0&&errno==ENOSPC)saw=1;else if(n<0)return 2;close(fd);}
    return saw?0:3;
}
int main(int argc,char **argv){
    if(argc!=2)return 100;
    if(!strcmp(argv[1],"binary")){
        emit(1,65537);emit(2,8193);unsigned char b[4096];size_t count=0;ssize_t n;while((n=read(0,b,sizeof b))>0){for(ssize_t i=0;i<n;i++)if(b[i]!=0xa5)return 101;count+=(size_t)n;}if(n<0||count!=65537)return 102;
        int fd=open("result.bin",O_WRONLY|O_CREAT|O_EXCL,0600);if(fd<0)return 103;emit(fd,32769);close(fd);return 7;
    }
    if(!strcmp(argv[1],"stdout")||!strcmp(argv[1],"stderr")){child();emit(!strcmp(argv[1],"stdout")?1:2,4<<20);for(;;)usleep(10000);}
    if(!strcmp(argv[1],"wall")||!strcmp(argv[1],"cancel")||!strcmp(argv[1],"monitor")){child();for(;;)usleep(10000);}
    if(!strcmp(argv[1],"leftover")){child();emit(1,17);return 7;}
    if(!strcmp(argv[1],"early")){close(0);emit(1,2);return 0;}
    if(!strcmp(argv[1],"disk")){
        signal(SIGXFSZ,SIG_IGN);int fd=open("single",O_WRONLY|O_CREAT,0600);if(fd<0)return 104;int ebig=0;unsigned char b[4096]={0};for(int i=0;i<64;i++)if(write(fd,b,sizeof b)<0){if(errno==EFBIG)ebig=1;else if(errno==ENOSPC)break;else return 105;}close(fd);
        unlink("single");int work=fill("/work"),tmp=fill("/tmp");
        printf("KF_DISK_PROOF file=%d work_enospc=%d tmp_enospc=%d\n",ebig,work==0,tmp==0);return (ebig&&work==0&&tmp==0)?0:106;
    }
    if(!strcmp(argv[1],"export")){int fd=open("valid.bin",O_CREAT|O_WRONLY,0600);if(fd<0)return 107;emit(fd,17);close(fd);if(symlink("/proc/self/status","escape"))return 108;return 0;}
    return 109;
}
