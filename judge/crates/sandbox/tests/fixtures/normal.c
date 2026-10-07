#include <stdio.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc != 2) return 100;
    unsigned char bytes[4096];
    size_t count = 0;
    ssize_t n;
    while ((n = read(0, bytes, sizeof bytes)) > 0) {
        for (ssize_t i = 0; i < n; i++) if (bytes[i] != 0xa5) return 101;
        count += (size_t)n;
    }
    if (n < 0 || count != (strcmp(argv[1], "empty") ? 65537U : 0U)) return 102;
    printf("C bytes=%zu\n", count);
    return !strcmp(argv[1], "nonzero") ? 23 : 0;
}
