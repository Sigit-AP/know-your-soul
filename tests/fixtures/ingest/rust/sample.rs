// Realistic Rust fixture for the ingest extractor tests.
use std::collections::HashMap;
use crate::mod2::thing;

const K: i32 = 1;
static S: i32 = 2;

struct Thing {
    field: i32,
}

enum Choice {
    A,
    B,
}

trait T {
    fn t(&self);
}

impl Thing {
    fn method(&self, x: i32) -> i32 {
        helper(x)
    }
}

fn helper(v: i32) -> i32 {
    v + 1
}

fn top_level(a: i32) -> i32 {
    let map: HashMap<i32, i32> = HashMap::new();
    helper(a)
}
