/* Fixed dangerous resource fixture: independent capped VM only. */
#define _GNU_SOURCE
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
static void require(int yes) { if (!yes) _exit(40); }
static __attribute__((unused)) uint64_t cpu_ns(void) { struct timespec t; require(clock_gettime(CLOCK_THREAD_CPUTIME_ID,&t)==0);return (uint64_t)t.tv_sec*1000000000+(uint64_t)t.tv_nsec; }
static __attribute__((unused)) void burn(uint64_t ns) { uint64_t start=cpu_ns();volatile unsigned n=0;while(cpu_ns()-start<ns)++n;(void)n; }
static __attribute__((unused)) void touch(size_t bytes) { volatile unsigned char *data=malloc(bytes);require(data!=NULL);for(size_t i=0;i<bytes;i+=4096)data[i]=1; }
static __attribute__((unused)) void *busy(void *unused) { (void)unused;
#if KF_ACCOUNT_CASE == 1
    for(;;)burn(10000000);
#else
    burn(30000000);return NULL;
#endif
    return NULL;
}
int main(void) {
    require(prctl(PR_SET_PDEATHSIG,SIGKILL)==0 && getppid()>1);
    require(raise(SIGSTOP)==0);
#if KF_ACCOUNT_CASE == 1
    burn(30000000);touch(8u<<20);require(raise(SIGSTOP)==0);
#endif
    pid_t child=fork();require(child>=0);
    if(child==0) {
#if KF_ACCOUNT_CASE == 4
        touch(128u<<20);_exit(41);
#elif KF_ACCOUNT_CASE == 2
        for(;;)sleep(10);
#else
        touch(8u<<20);pthread_t threads[2];
        for(unsigned i=0;i<2;i++)require(pthread_create(&threads[i],NULL,busy,NULL)==0);
        for(unsigned i=0;i<2;i++)require(pthread_join(threads[i],NULL)==0);
        _exit(0);
#endif
    }
#if KF_ACCOUNT_CASE == 3
    int status;require(waitpid(child,&status,0)==child&&WIFEXITED(status)&&WEXITSTATUS(status)==0);return 7;
#else
    for(;;)sleep(10);
#endif
}
