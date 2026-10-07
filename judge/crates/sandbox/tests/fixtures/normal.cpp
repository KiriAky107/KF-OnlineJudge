#include <iostream>
#include <string>
int main(int argc, char **argv) {
    if (argc != 2 || std::string(argv[1]) != "normal") return 100;
    std::size_t count = 0;
    char byte;
    while (std::cin.get(byte)) {
        if (static_cast<unsigned char>(byte) != 0xa5) return 101;
        ++count;
    }
    if (!std::cin.eof() || count != 65537) return 102;
    std::cout << "C++ bytes=" << count << '\n';
    return 0;
}
