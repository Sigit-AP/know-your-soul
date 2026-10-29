-- Realistic Haskell fixture for the ingest extractor tests.
module Demo where

import Data.List (sort)
import qualified Data.Map as Map

data Point = Point { px :: Int, py :: Int }

class Greeter a where
  greet :: a -> String

greetName :: String -> String
greetName name = helper name

helper :: String -> String
helper x = "hi " ++ x

topLevel :: Bool -> Int
topLevel flag = if flag then 1 else 2
