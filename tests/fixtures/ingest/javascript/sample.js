// Realistic JavaScript fixture for the ingest extractor tests.
import { helper } from "./mod.js";
const fs = require("fs");

const VAL = 3;

class Greeter extends Base {
  greet(name) {
    return formatName(name);
  }

  static build() {
    return new Greeter();
  }
}

function formatName(value) {
  return value.trim();
}

function topLevel(a) {
  return helper(a);
}

const arrow = (b) => topLevel(b);
