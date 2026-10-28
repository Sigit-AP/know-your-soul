// Realistic TypeScript fixture for the ingest extractor tests.
import { helper } from "./mod";

interface Shape {
  area(): number;
}

type Alias = string | number;

class Greeter extends Base implements Shape {
  greet(name: string): string {
    return formatName(name);
  }

  area(): number {
    return 1;
  }
}

function formatName(value: string): string {
  return value.trim();
}

function topLevel(a: number): number {
  return helper(a);
}
