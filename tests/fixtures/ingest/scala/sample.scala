// Realistic Scala fixture for the ingest extractor tests.
package com.example.demo

import scala.collection.mutable.ListBuffer
import java.util.Objects.requireNonNull

trait Greeter {
  def greet(name: String): String
}

class Demo(name: String) extends Greeter {
  override def greet(name: String): String = helper(name)

  def helper(x: String): String = s"hi $x"
}

object Singleton {
  val maxSize = 10
}

def topLevel(flag: Boolean): Int = if (flag) 1 else 2
