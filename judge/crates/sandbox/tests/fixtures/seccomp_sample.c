#include <pthread.h>
#include <stdio.h>
static void *thread(void *p){return p;}
int main(void){
    const unsigned char data[]={0,'K','F',255,10,13,0,7};
    unsigned char input[sizeof data];size_t n=fread(input,1,sizeof input,stdin);
    if(n&&n!=sizeof data)return 2;
    for(size_t i=0;i<n;i++)if(input[i]!=data[i])return 3;
    pthread_t t;if(pthread_create(&t,0,thread,(void*)17))return 4;
    void *p=0;if(pthread_join(t,&p)||p!=(void*)17)return 5;
    if(fwrite(data,1,sizeof data,stdout)!=sizeof data)return 6;
    return 7;
}
