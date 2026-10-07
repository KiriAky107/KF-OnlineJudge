/* Fixed, bounded probe: compile/run only in the independently capped VM. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/wait.h>
#include <unistd.h>
static int probe(int ns,int root){
    if(setns(ns,CLONE_NEWNS)||fchdir(root)||chroot(".")||chdir("/"))return 10;
    close(ns);close(root);
    int fd=open("/image-sentinel",O_WRONLY|O_TRUNC|O_CLOEXEC);if(fd!=-1||errno!=EROFS)return 11;
    fd=open("/image-sentinel",O_RDONLY|O_CLOEXEC);char value[32]={0};if(fd<0||read(fd,value,31)!=15||strcmp(value,"immutable-image"))return 12;close(fd);
    if(access("/kf",F_OK)==0||access("/sys",F_OK)==0||access("/var/lib/kf-judge",F_OK)==0)return 13;
    struct statfs work,tmp;if(statfs("/work",&work)||statfs("/tmp",&tmp)||work.f_type!=0x01021994||tmp.f_type!=0x01021994||!memcmp(&work.f_fsid,&tmp.f_fsid,sizeof work.f_fsid))return 14;
    fd=open("/work/isolated-sentinel",O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);if(fd<0||write(fd,"task",4)!=4)return 15;close(fd);
    fd=open("/tmp/bounded",O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);if(fd<0)return 16;
    char bytes[65536]={0};unsigned long total=0;int exhausted=0;
    while(total<=20UL*1024*1024){ssize_t n=write(fd,bytes,sizeof bytes);if(n<0){if(errno==ENOSPC)exhausted=1;break;}total+=(unsigned long)n;}
    close(fd);unlink("/tmp/bounded");if(!exhausted||total!=16UL*1024*1024)return 17;
    return 0;
}
int main(int argc,char **argv){
    alarm(8);if(argc!=2)return 2;for(char *p=argv[1];*p;p++)if(*p<'0'||*p>'9')return 2;
    char path[128];if(snprintf(path,sizeof path,"/proc/%s/ns/mnt",argv[1])>=(int)sizeof path)return 2;
    int ns=open(path,O_RDONLY|O_CLOEXEC);
    if(snprintf(path,sizeof path,"/proc/%s/root",argv[1])>=(int)sizeof path)return 2;
    int root=open(path,O_RDONLY|O_DIRECTORY|O_CLOEXEC);if(ns<0||root<0)return 3;
    pid_t child=fork();if(child==0)_exit(probe(ns,root));close(ns);close(root);int status=0;
    if(child<0||waitpid(child,&status,0)!=child||!WIFEXITED(status)||WEXITSTATUS(status)){fprintf(stderr,"root probe status=%d\n",status);return 4;}
    puts("KF_ROOT_PROBE readonly=1 old_root_absent=1 scratch=1 tmp_enospc=1");return 0;
}
