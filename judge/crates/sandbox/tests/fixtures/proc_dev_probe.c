/* Fixed bounded filesystem probe, compiled/run only in the capped test VM. */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
static int absent(const char *p) {
    struct stat st;
    return stat(p,&st)<0 && (errno==ENOENT || errno==EACCES);
}
static int probe(int mnt,int pidns,int root,const char *peer) {
    alarm(8);
    if(fchdir(root)||chroot(".")||chdir("/"))return 10;
    close(mnt);close(pidns);close(root);
    int created=open("/dev/new-file",O_CREAT|O_WRONLY|O_CLOEXEC,0600);
    if(created>=0||errno!=EROFS)return 24;
    /* Test-only identity, not a production identity implementation. */
    if(setgroups(0,NULL)||setresgid(100000,100000,100000)||setresuid(100000,100000,100000))return 11;
    if(getpid()!=2 || !absent("/proc/1/root") || !absent("/proc/1/fd"))return 12;
    DIR *d=opendir("/proc");if(!d)return 13;struct dirent *e;
    while((e=readdir(d)))if(e->d_name[0]>='0'&&e->d_name[0]<='9'&&strcmp(e->d_name,"2"))return 14;
    closedir(d);
    const char *global[]={"/proc/sys","/proc/sysrq-trigger","/proc/kcore","/proc/keys","/proc/cpuinfo","/proc/meminfo"};
    for(unsigned i=0;i<sizeof global/sizeof *global;i++)if(!absent(global[i]))return 15;
    char path[128];snprintf(path,sizeof path,"/proc/%s",peer);if(!absent(path))return 16;
    int fd=open("/proc/self/status",O_RDONLY|O_CLOEXEC);char buf[1024];
    if(fd<0 || read(fd,buf,sizeof buf)<=0)return 17;
    close(fd);
    fd=open("/dev/null",O_WRONLY|O_CLOEXEC);if(fd<0||write(fd,"discard",7)!=7)return 18;close(fd);
    unsigned char zero[16]={1};fd=open("/dev/zero",O_RDONLY|O_CLOEXEC);
    if(fd<0||read(fd,zero,sizeof zero)!=(ssize_t)sizeof zero)return 19;
    close(fd);for(unsigned i=0;i<sizeof zero;i++)if(zero[i])return 20;
    fd=open("/dev/urandom",O_RDONLY|O_CLOEXEC);if(fd<0||read(fd,zero,sizeof zero)!=(ssize_t)sizeof zero)return 21;close(fd);
    fd=open("/dev/random",O_RDONLY|O_NONBLOCK|O_CLOEXEC);if(fd<0)return 22;close(fd);
    if(!absent("/dev/mem")||!absent("/dev/sda")||!absent("/dev/tty")||!absent("/sys")||!absent("/kf")||!absent("/var/lib/kf-judge"))return 23;
    fd=open("/image-sentinel",O_WRONLY|O_TRUNC|O_CLOEXEC);if(fd>=0||errno!=EROFS)return 25;
    if(!absent("/proc/self/root/../../kf")||!absent("/proc/self/cwd/../../sys")||!absent("/escape-image/outside-sentinel"))return 26;
    d=opendir("/proc/self/fd");if(!d)return 27;int scanner=dirfd(d);
    while((e=readdir(d))) {
        if(e->d_name[0]=='.'||atoi(e->d_name)==scanner)continue;
        snprintf(path,sizeof path,"/proc/self/fd/%d",atoi(e->d_name));
        struct stat st;if(stat(path,&st)||S_ISDIR(st.st_mode))return 28;
    }
    closedir(d);return 0;
}
int main(int argc,char **argv) {
    alarm(15);if(argc!=3)return 2;
    for(int i=1;i<3;i++)for(char *p=argv[i];*p;p++)if(*p<'0'||*p>'9')return 2;
    char path[128];snprintf(path,sizeof path,"/proc/%s/ns/mnt",argv[1]);int mnt=open(path,O_RDONLY|O_CLOEXEC);
    snprintf(path,sizeof path,"/proc/%s/ns/pid",argv[1]);int pidns=open(path,O_RDONLY|O_CLOEXEC);
    snprintf(path,sizeof path,"/proc/%s/root",argv[1]);int root=open(path,O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    if(mnt<0||pidns<0||root<0||setns(mnt,CLONE_NEWNS)||setns(pidns,CLONE_NEWPID))return 3;
    pid_t child=fork();if(child==0)_exit(probe(mnt,pidns,root,argv[2]));
    close(mnt);close(pidns);close(root);int status=0;
    if(child<0||waitpid(child,&status,0)!=child||!WIFEXITED(status)||WEXITSTATUS(status)){fprintf(stderr,"proc/dev probe status=%d\n",status);return 4;}
    puts("KF_PROC_DEV_PROBE pid_subset=1 hidepid=1 devices=1 old_root_absent=1 readonly=1");return 0;
}
