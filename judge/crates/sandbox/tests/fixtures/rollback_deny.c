/* Fixed independent-guest syscall failure injector, never an operator helper. */
#define _GNU_SOURCE
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <errno.h>
#include <stddef.h>
#include <string.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>
int main(int argc,char **argv){
 const char *vm=getenv("KF_SANDBOX_INDEPENDENT_VM");if(argc!=2||!vm||strcmp(vm,"1")||access("/kf/INDEPENDENT_VM",R_OK))return 2;
 long nr=0;int special=0;char *mode=argv[1];
 if(!strcmp(mode,"namespace")){nr=SYS_unshare;special=1;}
 else if(!strcmp(mode,"root"))nr=SYS_pivot_root;
 else if(!strcmp(mode,"proc"))nr=SYS_mknodat;
 else if(!strcmp(mode,"rlimits")){nr=SYS_prlimit64;special=2;}
 else if(!strcmp(mode,"identity"))nr=SYS_setgroups;
 else if(!strcmp(mode,"seccomp"))nr=SYS_seccomp;
 else return 3;
 struct sock_filter code[16]={
 BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,arch)),
 BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,AUDIT_ARCH_X86_64,1,0),
 BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS),
 BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr)),
 BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,(unsigned)nr,0,1),
 BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM),
 BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW)};
 unsigned len=7;
 if(special){
  code[4]=(struct sock_filter)BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,(unsigned)nr,0,3);
  code[5]=(struct sock_filter)BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args)+(special==1?0:16));
  code[6]=(struct sock_filter)BPF_JUMP(BPF_JMP|BPF_JSET|BPF_K,special==1?0x20000000U:0xffffffffU,0,1);
  code[7]=(struct sock_filter)BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EPERM);
  code[8]=(struct sock_filter)BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW);len=9;
 }
 struct sock_fprog program={.len=(unsigned short)len,.filter=code};
 if(prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0)||prctl(PR_SET_SECCOMP,SECCOMP_MODE_FILTER,&program))return 4;
 char c[32];if(strlen(mode)>15)return 5;strcpy(c,"fail-");strcat(c,mode);
 execl("/kf/bin/kf-sandbox","kf-sandbox","vm-case",c,(char*)0);return 6;
}
