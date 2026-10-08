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
#define main isolated_danger_main
#include "seccomp_danger_probe.c"
#undef main
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
static void identity(void) {
    check(getuid()>=100000&&getuid()==geteuid()&&getgid()==getegid()&&getgroups(0,NULL)==0,"identity and groups");
    check(prctl(PR_GET_NO_NEW_PRIVS,0,0,0,0)==1&&prctl(PR_GET_SECCOMP)==2,"identity NNP/filter");
    FILE *f=fopen("/proc/self/status","r");check(f!=NULL,"status for caps");char line[256];int caps=0;
    while(fgets(line,sizeof line,f))if(!strncmp(line,"Cap",3)){
        char name[32];unsigned long long value;
        check(sscanf(line,"%31s %llx",name,&value)==2&&value==0,"all capability sets zero");caps++;
    }
    fclose(f);check(caps==5,"five capability sets");
}
static void peer(void) {
    identity();int fd=open("peer-secret",O_WRONLY|O_CREAT|O_EXCL,0600);check(fd>=0,"peer secret create");
    check(write(fd,"peer-private-content",20)==20,"peer secret write");close(fd);
    fd=open("peer-ready",O_WRONLY|O_CREAT|O_EXCL,0600);check(fd>=0&&write(fd,"ready",5)==5,"peer readiness");close(fd);
    struct stat st;while(stat("peer-stop",&st)<0){check(errno==ENOENT,"peer stop state");usleep(10000);}
    fd=open("peer-secret",O_RDONLY|O_CLOEXEC);char data[32];check(fd>=0&&read(fd,data,sizeof data)==20&&!memcmp(data,"peer-private-content",20),"peer secret preserved");close(fd);
    printf("PEER_OK uid=%u\n",getuid());
}
static void cross(void) {
    identity();FILE *f=fopen("/work/peer","r");check(f!=NULL,"peer metadata");
    int pid,init,parent;unsigned uid;unsigned long long ns[6];
    check(fscanf(f,"%d %d %d %u %llu %llu %llu %llu %llu %llu",&pid,&init,&parent,&uid,&ns[0],&ns[1],&ns[2],&ns[3],&ns[4],&ns[5])==10,"peer frame");fclose(f);
    check(pid>2&&init>2&&parent>2&&uid>=100000&&uid!=getuid(),"live peer distinct identity");
    const char *names[]={"mnt","pid","net","ipc","uts","cgroup"};char path[160];
    for(int i=0;i<6;i++){
        snprintf(path,sizeof path,"/proc/self/ns/%s",names[i]);struct stat st;check(!stat(path,&st)&&(unsigned long long)st.st_ino!=ns[i],"distinct peer namespace");
    }
    snprintf(path,sizeof path,"/proc/%d/root/work/peer-secret",pid);absent(path);
    snprintf(path,sizeof path,"/proc/%d/mem",pid);absent(path);
    snprintf(path,sizeof path,"/proc/%d/fd/3",pid);absent(path);
    snprintf(path,sizeof path,"/proc/%d/ns/mnt",init);absent(path);
    snprintf(path,sizeof path,"/proc/%d/root/kf/isolation-peer-frame",parent);absent(path);
    absent("/var/lib/kf-judge/tasks/kf-isolation-peer/root/work/peer-secret");
    DENY(syscall(SYS_kill,pid,0));DENY(syscall(SYS_kill,-1,SIGTERM));
    DENY(syscall(SYS_ptrace,PTRACE_ATTACH,pid,NULL,NULL));
    DENY(syscall(SYS_process_vm_readv,pid,NULL,0,NULL,0,0));
    DENY(syscall(SYS_process_vm_writev,pid,NULL,0,NULL,0,0));
    errno=0;check(syscall(SYS_pidfd_open,pid,0)==-1&&errno==ESRCH,"peer invisible to pidfd");
    int fd=(int)syscall(SYS_pidfd_open,1,0);check(fd>=3,"own visible init pidfd");
    DENY(syscall(SYS_pidfd_getfd,fd,3,0));
    /* GCC may pass its own harmless descriptors to a wrapper. Reuse FD3
       explicitly to test the number-based sendmsg exception on a non-socket. */
    if(fd!=3){check(dup3(fd,3,O_CLOEXEC)==3,"reuse FD3 without control capability");close(fd);fd=3;}
    struct msghdr msg={0};errno=0;check(sendmsg(fd,&msg,MSG_NOSIGNAL)==-1&&errno==ENOTSOCK,"FD3 cannot regain control endpoint");close(fd);
    DENY(syscall(SYS_symlink,"/proc/1/root","/work/escape"));DENY(syscall(SYS_link,"/work/peer","/work/stolen"));
    printf("CROSS_OK peer=%u self=%u denied=%u\n",uid,getuid(),denied);
}
int main(int argc,char **argv){
    check(argc>=2,"fixed mode");
    if(!strcmp(argv[1],"files")){files();return 0;}
    if(!strcmp(argv[1],"peer")){peer();return 0;}
    if(!strcmp(argv[1],"danger")){char *args[]={argv[0],argc>=3?argv[2]:"native",NULL};return isolated_danger_main(2,args);}
    if(!strcmp(argv[1],"cross")){cross();return 0;}
    if(!strcmp(argv[1],"identity")){
        identity();struct stat st;struct statvfs v;
        check(!stat("/usr/bin/kf-isolation-setuid",&st)&&st.st_uid==0&&(st.st_mode&06000)==06000,"actual setuid/setgid file");
        check(!statvfs("/usr/bin/kf-isolation-setuid",&v)&&(v.f_flag&ST_NOSUID),"image nosuid");
        char *args[]={"/usr/bin/kf-isolation-setuid","post-setuid",NULL};char *env[]={NULL};execve(args[0],args,env);check(0,"setuid exec");
    }
    if(!strcmp(argv[1],"post-setuid")){identity();printf("IDENTITY_OK uid=%u euid=%u\n",getuid(),geteuid());return 0;}
    return 100;
}
