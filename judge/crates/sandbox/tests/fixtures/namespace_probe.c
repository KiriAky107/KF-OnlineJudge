/* Fixed probe, compiled/executed only by the independently capped VM harness. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/ipc.h>
#include <sys/mount.h>
#include <sys/msg.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

static int enter(const char *pid, const char *name, int flag) {
    char path[128];
    if (snprintf(path,sizeof path,"/proc/%s/ns/%s",pid,name)>=(int)sizeof path) return -1;
    int fd=open(path,O_RDONLY|O_CLOEXEC);
    if(fd<0) return -1;
    int result=setns(fd,flag); close(fd); return result;
}
static int probe(const char *pid, int queue) {
    char hostname[64]={0};
    if(enter(pid,"uts",CLONE_NEWUTS)||gethostname(hostname,sizeof hostname)||strcmp(hostname,"kf-sandbox"))return 10;
    if(enter(pid,"ipc",CLONE_NEWIPC))return 11;
    struct msqid_ds ds;
    if(msgctl(queue,IPC_STAT,&ds)!=-1 || (errno!=EINVAL&&errno!=EIDRM))return 12;
    if(enter(pid,"net",CLONE_NEWNET))return 13;
    struct if_nameindex *names=if_nameindex();
    if(!names || strcmp(names[0].if_name,"lo") || names[1].if_index)return 14;
    if_freenameindex(names);
    int sock=socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC,0);
    struct ifreq iface={0};strcpy(iface.ifr_name,"lo");
    if(sock<0||ioctl(sock,SIOCGIFFLAGS,&iface)||iface.ifr_flags&IFF_UP)return 15;
    close(sock);
    if(enter(pid,"mnt",CLONE_NEWNS)||mount("tmpfs","/kf/ns-mount","tmpfs",MS_NOSUID|MS_NODEV|MS_NOEXEC,"size=1m,mode=0700"))return 16;
    int file=open("/kf/ns-mount/sentinel",O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
    if(file<0||write(file,"isolated",8)!=8)return 17;
    close(file);
    if(enter(pid,"pid",CLONE_NEWPID))return 18;
    pid_t child=fork();
    if(child==0)_exit(getpid()>1&&getppid()==0&&kill(1,0)==0?0:19);
    int status;
    if(child<0||waitpid(child,&status,0)!=child||!WIFEXITED(status)||WEXITSTATUS(status))return 20;
    return 0;
}
int main(int argc,char **argv){
    alarm(4);
    if(argc!=2)return 2;
    for(char *p=argv[1];*p;p++)if(*p<'0'||*p>'9')return 2;
    int queue=msgget(IPC_PRIVATE,IPC_CREAT|0600);if(queue<0)return 3;
    pid_t child=fork();if(child==0)_exit(probe(argv[1],queue));
    int status=0;int ok=child>0&&waitpid(child,&status,0)==child&&WIFEXITED(status)&&WEXITSTATUS(status)==0;
    if(msgctl(queue,IPC_RMID,NULL))return 4;
    if(!ok){fprintf(stderr,"namespace probe status=%d\n",status);return 5;}
    puts("KF_NAMESPACE_PROBE uts=1 ipc=1 net_lo_down=1 mount_private=1 pid_child=1");return 0;
}
