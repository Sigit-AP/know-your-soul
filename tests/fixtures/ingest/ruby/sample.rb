# Realistic Ruby fixture for the ingest extractor tests.
require "json"
require_relative "local"

K = 1

module Util
  class Greeter < Base
    def greet(name)
      format_name(name)
    end

    def self.build
      new
    end
  end
end

def format_name(value)
  value.strip
end

def top_level(a)
  helper(a)
end
