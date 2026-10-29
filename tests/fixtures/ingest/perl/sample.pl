# Realistic Perl fixture for the ingest extractor tests.
package Demo;

use strict;
use JSON::PP qw(decode_json);

sub greet {
    my ($name) = @_;
    return helper($name);
}

sub helper {
    my ($x) = @_;
    return "hi $x";
}

sub top_level {
    my ($flag) = @_;
    return $flag ? 1 : 2;
}

1;
