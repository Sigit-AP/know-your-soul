// Realistic Kotlin fixture for the ingest extractor tests.
package com.example.demo

import kotlin.collections.List
import java.util.Objects.requireNonNull

interface Greeter {
    fun greet(name: String): String
}

class Demo(val name: String) : Greeter {
    override fun greet(name: String): String = helper(name)

    fun helper(x: String): String = "hi $x"
}

fun topLevel(flag: Boolean): Int {
    return if (flag) 1 else 2
}
