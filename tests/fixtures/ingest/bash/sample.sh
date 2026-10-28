#!/bin/bash
# Realistic Bash fixture for the ingest extractor tests.
source ./lib.sh
. other.sh

K=1

format_name() {
  helper "$1"
}

function top_level {
  format_name "$1"
  helper "$1"
}
