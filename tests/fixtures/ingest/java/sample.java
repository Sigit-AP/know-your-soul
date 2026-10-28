// Realistic Java fixture for the ingest extractor tests.
package com.example;

import java.util.List;
import static java.util.Objects.requireNonNull;

interface Shape {
    int area();
}

class Greeter extends Base implements Shape {
    int field;

    int greet(String name) {
        return formatName(name).length();
    }

    public int area() {
        return 0;
    }
}

enum Choice {
    A,
    B,
}

class Helper {
    static int formatNameLength(String value) {
        requireNonNull(value);
        return value.trim().length();
    }
}
