// Realistic Swift fixture for the ingest extractor tests.
import Foundation
import UIKit

protocol Greeter {
    func greet(name: String) -> String
}

struct Point {
    var x: Int
}

class Demo: Greeter {
    func greet(name: String) -> String {
        return helper(x: name)
    }

    func helper(x: String) -> String {
        return "hi \(x)"
    }
}

func topLevel(flag: Bool) -> Int {
    return flag ? 1 : 2
}
