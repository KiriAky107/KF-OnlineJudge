#include <iostream>
#include <thread>
int main(){
    const char data[]={0,'K','F',char(255),10,13,0,7};char input[sizeof data];
    std::cin.read(input,sizeof input);auto n=std::cin.gcount();
    if(n&&n!=sizeof data)return 2;
    for(std::streamsize i=0;i<n;i++)if(input[i]!=data[i])return 3;
    int value=0;std::thread t([&]{value=17;});t.join();if(value!=17)return 4;
    std::cout.write(data,sizeof data);if(!std::cout)return 6;
    return 7;
}
