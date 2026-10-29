// Realistic Dart fixture for the ingest extractor tests.
import 'dart:async';
import 'package:foo/foo.dart';

abstract class Greeter {
  String greet(String name);
}

mixin Walkable {
  void walk() {}
}

class Demo extends Greeter {
  @override
  String greet(String name) => helper(name);

  String helper(String x) => 'hi $x';
}

int topLevel(bool flag) {
  return flag ? 1 : 2;
}
