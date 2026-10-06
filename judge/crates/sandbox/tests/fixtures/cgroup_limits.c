/* Fixed VM-only resource probes, never submitted programs or host test commands. */
#include <errno.h>
#include <signal.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

int main(void) {
    if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() <= 1) return 40;
    raise(SIGSTOP);
#if KF_FIXTURE == 1
    pid_t pid = fork();
    if (pid == -1) return errno == EAGAIN ? 0 : 41;
    if (pid == 0) _exit(42);
    waitpid(pid, NULL, 0);
    return 42;
#elif KF_FIXTURE == 2
    size_t size = 128u * 1024u * 1024u;
    volatile unsigned char *data = malloc(size);
    if (data == NULL) return 43;
    for (size_t i = 0; i < size; i += 4096) data[i] = 1;
    free((void *) data);
    return 44;
#elif KF_FIXTURE == 3
    clock_t start = clock();
    volatile unsigned long count = 0;
    while (clock() - start < CLOCKS_PER_SEC * 3 / 20) ++count;
    return count == 0 ? 45 : 0;
#else
#error KF_FIXTURE must be 1, 2, or 3
#endif
}
