#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>
static void sleep_forever(void) { for (;;) usleep(10000); }
static void *busy(void *unused) {
    (void)unused;
    volatile unsigned long value = 1;
    for (;;) value = value * 1664525 + 1013904223;
    return NULL;
}
static void *sleeper(void *unused) { (void)unused; sleep_forever(); return NULL; }
int main(int argc, char **argv) {
    if (argc != 2) return 100;
    if (!strcmp(argv[1], "cpu")) {
        pid_t p = fork();
        if (p < 0) return 101;
        if (p == 0) {
            pthread_t threads[2];
            for (int i = 0; i < 2; ++i) if (pthread_create(&threads[i], NULL, busy, NULL)) _exit(102);
            if (write(1, "CPU_CHILD_THREADS_READY\n", 24) != 24) _exit(103);
            busy(NULL);
        }
        sleep_forever();
    }
    if (!strcmp(argv[1], "oom")) {
        pid_t p = fork();
        if (p < 0) return 104;
        if (p == 0) {
            /* Finite 128 MiB request in a 32 MiB task; no host allocator. */
            for (int i = 0; i < 128; ++i) {
                volatile unsigned char *block = malloc(1 << 20);
                if (!block) _exit(105);
                for (int j = 0; j < (1 << 20); j += 4096) block[j] = (unsigned char)i;
            }
            _exit(106);
        }
        sleep_forever();
    }
    if (!strcmp(argv[1], "fork") || !strcmp(argv[1], "threads")) {
        int thread_mode = !strcmp(argv[1], "threads");
        pthread_t threads[32];
        for (int i = 0; i < 32; ++i) {
            int error;
            if (thread_mode) error = pthread_create(&threads[i], NULL, sleeper, NULL);
            else {
                pid_t p = fork();
                if (p == 0) sleep_forever();
                error = p < 0 ? errno : 0;
            }
            if (error) {
                if (error != EAGAIN) return 107;
                sleep_forever();
            }
        }
        return 108; /* Kernel cap must reject well before this finite ceiling. */
    }
    if (!strcmp(argv[1], "fds")) {
        struct rlimit limit;
        if (getrlimit(RLIMIT_NOFILE, &limit) || limit.rlim_cur != 64 || limit.rlim_max != 64) return 109;
        int fds[128], n = 0, saved = 0;
        for (; n < 128; ++n) {
            fds[n] = open("/dev/null", O_RDONLY | O_CLOEXEC);
            if (fds[n] < 0) { saved = errno; break; }
        }
        for (int i = 0; i < n; ++i) close(fds[i]);
        printf("FD_PROOF soft=64 hard=64 opened=%d errno=%d\n", n, saved);
        return n == 61 && saved == EMFILE ? 0 : 110;
    }
    return 111;
}
