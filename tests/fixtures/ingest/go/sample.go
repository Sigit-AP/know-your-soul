// Realistic Go fixture for the ingest extractor tests.
package main

import (
	"fmt"
	alias "example.com/mod"
)

const K = 1

var counter = 0

type Thing struct {
	Field int
}

type Iface interface {
	Do() int
}

func (t Thing) Method(x int) int {
	return helper(x)
}

func topLevel(a int) int {
	fmt.Println(a)
	return alias.Helper(a)
}
