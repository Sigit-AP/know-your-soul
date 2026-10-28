/* Realistic C fixture for the ingest extractor tests. */
#include <stdio.h>
#include "local.h"

#define K 1

struct Thing {
    int field;
};

enum Choice {
    A,
    B,
};

typedef int alias_t;

int helper(int x);

static int format_name(const char *value) {
    return helper((int)value[0]);
}

int top_level(int a) {
    printf("%d", a);
    return helper(a);
}
