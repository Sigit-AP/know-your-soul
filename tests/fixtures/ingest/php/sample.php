<?php
// Realistic PHP fixture for the ingest extractor tests.
use Vendor\Pkg\Thing;
require "local.php";

const K = 1;

interface Shape
{
    public function area(): int;
}

class Greeter extends Base implements Shape
{
    public int $field;

    public function greet(int $a): int
    {
        return helper($a);
    }

    public function area(): int
    {
        return 0;
    }
}

function helper(int $v): int
{
    return $v + 1;
}

function top_level(int $a): int
{
    return helper($a);
}
