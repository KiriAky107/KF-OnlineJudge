#define _GNU_SOURCE
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <grp.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
static int absent(const char *p) { int fd=open(p,O_RDONLY|O_CLOEXEC); if(fd>=0){close(fd);return 0;} return errno==ENOENT; }
int main(int argc,char **argv) {
    if(argc!=2)return 2;
    char marker[64]={0};FILE *f=fopen("/kf/INDEPENDENT_VM","r");
    if(!f||!fgets(marker,sizeof marker,f)||strcmp(marker,"isolated-test-guest\n"))return 3;
    fclose(f);
    char *end=NULL;long pid=strtol(argv[1],&end,10);if(!end||*end||pid<=1)return 4;
    char ns[64],root[64];snprintf(ns,sizeof ns,"/proc/%ld/ns/mnt",pid);snprintf(root,sizeof root,"/proc/%ld/root",pid);
    int n=open(ns,O_RDONLY|O_CLOEXEC),d=open(root,O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    if(n<0||d<0||setns(n,CLONE_NEWNS)||fchdir(d)||chroot(".")||chdir("/"))return 5;
    close(n);close(d);
    if(setgroups(0,NULL)||setresgid(100000,100000,100000)||setresuid(100000,100000,100000))return 6;
    char bytes[32]={0};int fd=open("/work/source/input",O_RDONLY|O_CLOEXEC);if(fd<0||read(fd,bytes,sizeof bytes)!=15||memcmp(bytes,"protected-input",15))return 7;close(fd);
    fd=open("/work/source/input",O_WRONLY|O_TRUNC|O_CLOEXEC);if(fd>=0||(errno!=EROFS&&errno!=EACCES))return 8;
    if(!rename("/work/source/input","/work/source/stolen"))return 9;
    if(!rename("/work/source","/work/stolen-dir"))return 10;
    struct stat s;struct statvfs v;
    if(stat("/work/main",&s)||(s.st_mode&07777)!=0555||statvfs("/work/main",&v)||(v.f_flag&ST_NOEXEC))return 11;
    if(statvfs("/work/source/input",&v)||!(v.f_flag&ST_RDONLY)||!(v.f_flag&ST_NOEXEC))return 12;
    if(!absent("/work/input-001")||!absent("/image-outside-sentinel")||!absent("/kf/INDEPENDENT_VM"))return 13;
    fd=open("/work/source/result",O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);if(fd<0||write(fd,"output",6)!=6)return 14;close(fd);
    puts("KF_INPUT_BIND_PROBE data_ro=1 program_exec=1 data_noexec=1 ancestor_sticky=1 sibling_output=1 stdin_hidden=1 outside_absent=1");return 0;
}
