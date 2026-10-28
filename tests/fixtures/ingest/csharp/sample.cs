// Realistic C# fixture for the ingest extractor tests.
using System;
using Alias = System.Text.StringBuilder;

namespace Example
{
    interface IShape
    {
        int Area();
    }

    class Greeter : Base, IShape
    {
        int field;

        int Greet(int a)
        {
            return Helper(a);
        }

        public int Area()
        {
            return 0;
        }
    }

    enum Choice
    {
        A,
        B,
    }

    struct Point
    {
        int x;
    }
}
