/* Fixed VM-only mount-boundary fixture; no request-controlled paths. */
#define _GNU_SOURCE
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mount.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc < 2 || argc > 3) return 2;
    char marker[64] = {0};
    FILE *f = fopen("/kf/INDEPENDENT_VM", "r");
    if (!f || !fgets(marker, sizeof marker, f) || strcmp(marker, "isolated-test-guest\n")) return 3;
    fclose(f);
    char *end = NULL;
    long pid = strtol(argv[1], &end, 10);
    if (!end || *end || pid <= 1) return 4;
    char ns[64], root[64];
    snprintf(ns, sizeof ns, "/proc/%ld/ns/mnt", pid);
    snprintf(root, sizeof root, "/proc/%ld/root", pid);
    int fd = open(ns, O_RDONLY | O_CLOEXEC), dir = open(root, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0 || dir < 0 || setns(fd, CLONE_NEWNS) || fchdir(dir) || chroot(".") || chdir("/")) return 5;
    close(fd); close(dir);
    if (argc == 3) return umount("/work/nested/value") ? 6 : 0;
    return mount("/image-sentinel", "/work/nested/value", NULL, MS_BIND, NULL) ? 7 : 0;
}
