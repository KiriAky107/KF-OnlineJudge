#include <unistd.h>
int main(void) {
    unsigned char b[4096];
    ssize_t n=read(0,b,sizeof b);
    if(n<0)return 8;
    if(n>0 && b[0]=='#')return 7;
    if(n>0 && b[0]=='!'){if(write(1,"live",4)!=4)return 9;sleep(10);return 0;}
    do {if(n>0 && write(1,b,(size_t)n)!=n)return 10;}while((n=read(0,b,sizeof b))>0);
    return n<0 ? 11 : 0;
}
