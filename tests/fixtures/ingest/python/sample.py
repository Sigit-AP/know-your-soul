"""Realistic Python fixture for the ingest extractor tests."""

import os
from pkg.mod import thing

CONSTANT = 42
counter = 0


class Greeter(Base):
    """A class with methods."""

    def greet(self, name):
        return format_name(name)

    @staticmethod
    def build():
        return Greeter()


def format_name(value):
    return value.strip()


def top_level(a, b=2):
    result = helper(a)
    return result
