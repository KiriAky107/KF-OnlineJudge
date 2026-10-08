#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <sys/sysmacros.h>
#include <unistd.h>
static void check(int yes, const char *why) {
    if (!yes) { fprintf(stderr,"ISOLATION_FAIL %s errno=%d\n",why,errno); exit(1); }
}
static void absent(const char *path) {
    int fd=open(path,O_RDONLY|O_CLOEXEC);
    check(fd<0&&(errno==ENOENT||errno==EACCES||errno==EPERM),path);
}
static void files(void) {
    check(getpid()==2 && getuid()>=100000 && geteuid()==getuid(),"namespace uid");
    check(prctl(PR_GET_NO_NEW_PRIVS,0,0,0,0)==1 && prctl(PR_GET_SECCOMP)==2,"NNP/filter");
    char cwd[128];check(getcwd(cwd,sizeof cwd)&&!strcmp(cwd,"/work"),"initial cwd");
    check(!getenv("KF_SANDBOX_INDEPENDENT_VM")&&!getenv("HOME")&&!getenv("LD_PRELOAD"),"cleared environment");
    const char *hidden[]={"/kf/outside-sentinel","/var/lib/kf-judge/tasks","/sys","/.old-root","/escape-image","/proc/sys","/proc/sysrq-trigger","/proc/kcore","/proc/keys","/proc/meminfo","/proc/cpuinfo","/proc/1/root","/proc/1/fd/3","/proc/1/environ","/proc/self/root/../../kf/outside-sentinel","/proc/self/cwd/../../kf/outside-sentinel","/work/stdin-001","/work/input-001"};
    for(size_t i=0;i<sizeof hidden/sizeof *hidden;i++)absent(hidden[i]);
    for(int fd=3;fd<64;fd++)check(fcntl(fd,F_GETFD)<0&&errno==EBADF,"only FD012 inherited");
    DIR *d=opendir("/proc");check(d!=NULL,"proc listing");struct dirent *e;
    while((e=readdir(d)))if(e->d_name[0]>='0'&&e->d_name[0]<='9')check(atoi(e->d_name)==2,"proc only user pid");
    closedir(d);
    d=opendir("/proc/self/fd");check(d!=NULL,"self FD listing");int scanner=dirfd(d);
    while((e=readdir(d)))if(e->d_name[0]>='0'&&e->d_name[0]<='9')check(atoi(e->d_name)<=2||atoi(e->d_name)==scanner,"no directory/control capability");
    closedir(d);
    struct statvfs v;check(!statvfs("/proc",&v)&&(v.f_flag&(ST_RDONLY|ST_NOSUID|ST_NODEV|ST_NOEXEC))==(ST_RDONLY|ST_NOSUID|ST_NODEV|ST_NOEXEC),"proc mount flags");
    const char *dev[]={"null","zero","random","urandom"};unsigned int minors[]={3,5,8,9};
    d=opendir("/dev");check(d!=NULL,"dev listing");int n=0;
    while((e=readdir(d)))if(e->d_name[0]!='.'){
        int found=0;for(int i=0;i<4;i++)if(!strcmp(e->d_name,dev[i]))found=1;
        check(found,"device whitelist");n++;
    }
    closedir(d);check(n==4,"only four devices");
    for(int i=0;i<4;i++){
        char path[64];snprintf(path,sizeof path,"/dev/%s",dev[i]);struct stat st;
        check(!stat(path,&st)&&S_ISCHR(st.st_mode)&&major(st.st_rdev)==1&&minor(st.st_rdev)==minors[i],"device type");
        int fd=open(path,O_RDWR|O_NONBLOCK|O_CLOEXEC);check(fd>=0,"device I/O open");
        if(i==0)check(write(fd,"x",1)==1,"null write");
        if(i==1){char zero[16];check(read(fd,zero,sizeof zero)==(ssize_t)sizeof zero,"zero read");for(int j=0;j<16;j++)check(!zero[j],"zero bytes");}
        if(i==3){char random[16];check(read(fd,random,sizeof random)==(ssize_t)sizeof random,"urandom read");}
        close(fd);
    }
    check(!statvfs("/dev",&v)&&(v.f_flag&(ST_RDONLY|ST_NOSUID|ST_NOEXEC))==(ST_RDONLY|ST_NOSUID|ST_NOEXEC),"dev readonly flags");
    int fd=open("/dev/new",O_CREAT|O_WRONLY,0600);check(fd<0&&errno==EROFS,"dev entries immutable");
    absent("/dev/mem");absent("/dev/sda");absent("/dev/tty");
    char data[32];fd=open("/work/source/input",O_RDONLY|O_CLOEXEC);check(fd>=0&&read(fd,data,sizeof data)==15&&!memcmp(data,"protected-input",15),"bound input contents");close(fd);
    check(!statvfs("/work/source/input",&v)&&(v.f_flag&(ST_RDONLY|ST_NOEXEC|ST_NOSUID|ST_NODEV))==(ST_RDONLY|ST_NOEXEC|ST_NOSUID|ST_NODEV),"data readonly noexec");
    fd=open("/work/source/input",O_WRONLY|O_TRUNC|O_CLOEXEC);check(fd<0&&(errno==EROFS||errno==EACCES),"input write refused");
    check(rename("/work/source/input","/work/source/stolen")<0,"input rename refused");
    check(rename("/work/source","/work/stolen-dir")<0,"input ancestor rename refused");
    char *args[]={"/work/source/input",NULL};char *env[]={NULL};
    check(execve(args[0],args,env)<0&&errno==EACCES,"data execution refused");
    fd=open("/image-sentinel",O_WRONLY|O_TRUNC|O_CLOEXEC);check(fd<0&&errno==EROFS,"image readonly");
    fd=open("/work/source/result",O_CREAT|O_WRONLY|O_EXCL,0600);check(fd>=0&&write(fd,"own-output",10)==10,"sibling output permitted");close(fd);
    ssize_t count=read(0,data,sizeof data);check(count==13&&!memcmp(data,"private-stdin",13)&&read(0,data,sizeof data)==0,"sealed hidden stdin EOF");
    fd=open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC);check(fd>=0&&!fchdir(fd)&&!chdir("../../../../"),"root traversal");close(fd);
    check(getcwd(cwd,sizeof cwd)&&!strcmp(cwd,"/"),"root cannot climb above");absent("/kf/outside-sentinel");
    errno=0;check(syscall(SYS_chroot,"/")==-1&&errno==EPERM,"chroot reconstruction refused");
    printf("FILES_OK proc dev input stdin fd root image\n");
}
int main(int argc,char **argv){check(argc>=2,"fixed mode");if(!strcmp(argv[1],"files")){files();return 0;}return 100;}
