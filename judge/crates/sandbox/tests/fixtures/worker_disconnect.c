/* Fixed independent-VM controller only. Not installed in the ordinary CLI. */
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <stdio.h>
#include <stdint.h>
#include <fcntl.h>
#include <string.h>
int main(int argc,char **argv) {
    if(argc!=4 || getuid()!=0 || setgid(1000) || setuid(1000))return 100;
    int d=open(argv[2],O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    int f=open(argv[3],O_RDONLY|O_CLOEXEC);
    char body[66000];int n=(int)read(f,body,sizeof body);if(d<0||f<0||n<=0||n>=66000)return 101;close(f);
    int s=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);struct sockaddr_un a={0};a.sun_family=AF_UNIX;
    if(strlen(argv[1])>=sizeof a.sun_path){return 102;}
    strcpy(a.sun_path,argv[1]);
    if(connect(s,(struct sockaddr*)&a,sizeof a))return 103;
    unsigned char header[8]={'K','F',1,0,(unsigned char)((unsigned)n>>24),(unsigned char)((unsigned)n>>16),(unsigned char)((unsigned)n>>8),(unsigned char)n};
    if(write(s,header,8)!=8||write(s,body,(size_t)n)!=n)return 104;
    char marker=(char)0xa5;struct iovec i={&marker,1};union {struct cmsghdr align;char bytes[CMSG_SPACE(sizeof(int))];}control={0};
    struct msghdr m={0};m.msg_iov=&i;m.msg_iovlen=1;m.msg_control=control.bytes;m.msg_controllen=sizeof control.bytes;
    struct cmsghdr *c=CMSG_FIRSTHDR(&m);c->cmsg_level=SOL_SOCKET;c->cmsg_type=SCM_RIGHTS;c->cmsg_len=CMSG_LEN(sizeof(int));memcpy(CMSG_DATA(c),&d,sizeof d);
    if(sendmsg(s,&m,MSG_NOSIGNAL)!=1)return 105;
    close(d);sleep(2);close(s);return 0;
}
