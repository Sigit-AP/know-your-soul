-- Realistic Lua fixture for the ingest extractor tests.
local json = require("json")
local mod = require "mod"

local M = {}

function M.greet(name)
  return helper(name)
end

local function helper(x)
  return "hi " .. x
end

function top_level(flag)
  if flag then
    return 1
  else
    return 2
  end
end

return M
