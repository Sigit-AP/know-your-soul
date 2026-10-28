// Realistic C++ fixture for the ingest extractor tests.
#include <vector>
#include "local.hpp"

namespace ns {

class Greeter : public Base {
public:
    int greet(int a) {
        return helper(a);
    }
};

struct Thing {
    int field;
};

enum class Choice {
    A,
    B,
};

int helper(int v) {
    return v + 1;
}

int top_level(int a) {
    std::vector<int> xs;
    return helper(a);
}

}
