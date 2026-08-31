import { test } from "node:test";
import * as assert from "node:assert";
import { TypeScriptComplexityAnalyzer } from "../src/visitor";

const analyzer = new TypeScriptComplexityAnalyzer(15);

function getComplexity(code: string, funcName: string): number {
  const result = analyzer.analyzeSource(code, "test.ts");
  const func = result.functions.find((f) => f.name === funcName);
  if (!func) {
    throw new Error(`Function ${funcName} not found in ${result.functions.map((f) => f.name).join(", ")}`);
  }
  return func.complexity;
}

// TC01: Flat linear code -> 0
test("TC01: Flat linear code", () => {
  const code = `
function linearFunction(a: number, b: number): number {
  const x = a + b;
  const y = x * 2;
  return y;
}`;
  assert.strictEqual(getComplexity(code, "linearFunction"), 0);
});

// TC02: Single if statement -> 1
test("TC02: Single if statement", () => {
  const code = `
function singleIf(x: number): number {
  if (x > 0) {
    return x;
  }
  return -x;
}`;
  assert.strictEqual(getComplexity(code, "singleIf"), 1);
});

// TC03: Nested if inside for loop -> 3 (Loop +1, If +2)
test("TC03: Nested if inside for loop", () => {
  const code = `
function nestedIfLoop(items: number[]): number {
  let total = 0;
  for (const x of items) {
    if (x > 0) {
      total += x;
    }
  }
  return total;
}`;
  assert.strictEqual(getComplexity(code, "nestedIfLoop"), 3);
});

// TC04: Switch statement -> 1
test("TC04: Switch statement", () => {
  const code = `
function processCommand(cmd: string): number {
  switch (cmd) {
    case "start": return 1;
    case "stop": return 0;
    default: return -1;
  }
}`;
  assert.strictEqual(getComplexity(code, "processCommand"), 1);
});

// TC05: Boolean chain a && b && c -> 2 (if + 1, bool + 1)
test("TC05: Boolean chain same operator", () => {
  const code = `
function boolChain(a: boolean, b: boolean, c: boolean): boolean {
  if (a && b && c) {
    return true;
  }
  return false;
}`;
  assert.strictEqual(getComplexity(code, "boolChain"), 2);
});

// TC06: Boolean switch operator -> 3 (if + 1, and + 1, or + 1)
test("TC06: Boolean switch operator", () => {
  const code = `
function boolSwitch(a: boolean, b: boolean, c: boolean): boolean {
  if ((a && b) || c) {
    return true;
  }
  return false;
}`;
  assert.strictEqual(getComplexity(code, "boolSwitch"), 3);
});

// TC07: else if chain -> 1 per branch, 0 nesting penalty
test("TC07: else if chain", () => {
  const code = `
function elseIfChain(x: number): string {
  if (x === 1) {
    return "one";
  } else if (x === 2) {
    return "two";
  } else if (x === 3) {
    return "three";
  } else {
    return "other";
  }
}`;
  assert.strictEqual(getComplexity(code, "elseIfChain"), 3);
});

// TC08: 3-level nested loop -> 1 + 2 + 3 = 6
test("TC08: 3-level nested loop", () => {
  const code = `
function tripleLoop(matrix: number[][][]): void {
  for (const row of matrix) {
    for (const col of row) {
      for (const item of col) {
        console.log(item);
      }
    }
  }
}`;
  assert.strictEqual(getComplexity(code, "tripleLoop"), 6);
});

// TC09: Direct recursion -> +1
test("TC09: Direct recursion", () => {
  const code = `
function factorial(n: number): number {
  if (n <= 1) {
    return 1;
  }
  return n * factorial(n - 1);
}`;
  assert.strictEqual(getComplexity(code, "factorial"), 2);
});

// TC10: try/catch block -> +1 for catch
test("TC10: try/catch block", () => {
  const code = `
function safeDivide(a: number, b: number): number {
  try {
    return a / b;
  } catch (e) {
    return 0;
  }
}`;
  assert.strictEqual(getComplexity(code, "safeDivide"), 1);
});

// TC11: Nested closure / arrow function with branch
test("TC11: Nested closure with branch", () => {
  const code = `
function outerWithClosure(items: number[]): number[] {
  const f = (x: number) => {
    if (x > 0) return x * 2;
    return 0;
  };
  return items.map(f);
}`;
  assert.strictEqual(getComplexity(code, "outerWithClosure"), 2);
});

// TC12: Ternary nested inside loop
test("TC12: Ternary nested inside loop", () => {
  const code = `
function ternaryNested(items: number[]): void {
  for (const x of items) {
    const val = x > 0 ? 1 : -1;
  }
}`;
  assert.strictEqual(getComplexity(code, "ternaryNested"), 3);
});

// TC13: Guard clause early returns
test("TC13: Guard clause", () => {
  const code = `
function guardClause(user: any): string | null {
  if (!user) {
    return null;
  }
  if (!user.isActive) {
    return null;
  }
  return user.name;
}`;
  assert.strictEqual(getComplexity(code, "guardClause"), 2);
});

// TC14: Nullish coalescing sequence ??
test("TC14: Nullish coalescing sequence", () => {
  const code = `
function nullishSeq(a: any, b: any, c: any): any {
  if (a ?? b ?? c) {
    return true;
  }
  return false;
}`;
  assert.strictEqual(getComplexity(code, "nullishSeq"), 2);
});

// TC15: SonarSource Whitepaper Appendix B Example
test("TC15: SonarSource Appendix B", () => {
  const code = `
function getElement(matrix: number[][]): number | null {
  for (let i = 0; i < matrix.length; i++) {              // +1 (nesting 0)
    for (let j = 0; j < matrix[i].length; j++) {         // +2 (nesting 1)
      if (matrix[i][j] !== null) {                       // +3 (nesting 2)
        if (matrix[i][j] > 0 && matrix[i][j] < 100) {   // +4 (nesting 3) + 1 (bool) = +5
          return matrix[i][j];
        } else if (matrix[i][j] === 0) {                 // +1 (else if base)
          continue;
        }
      }
    }
  }
  return null;
}`;
  assert.strictEqual(getComplexity(code, "getElement"), 12);
});

// Classes, getters, setters, constructors, TSX
test("Classes, getters, setters, constructors, labeled jumps, and TSX", () => {
  const code = `
class Service {
  private _val = 0;
  constructor(initVal: number) {
    if (initVal > 0) this._val = initVal;
  }
  get val(): number {
    return this._val > 0 ? this._val : 0;
  }
  set val(v: number) {
    if (v >= 0) this._val = v;
  }
  public doWork(): void {
    outer: for (let i = 0; i < 5; i++) {
      while (i < 3) {
        break outer;
      }
    }
  }
}
const arrowFunc = (x: number) => x > 0 ? 1 : 0;
`;
  const res = analyzer.analyzeSource(code, "component.tsx");
  assert.strictEqual(res.functions.length, 5);
  const constructorFn = res.functions.find(f => f.name === "constructor");
  assert.strictEqual(constructorFn?.complexity, 1);

  const getAccessor = res.functions.find(f => f.name === "val" && f.complexity === 1);
  assert.ok(getAccessor);

  const doWorkFn = res.functions.find(f => f.name === "doWork");
  // for (1) + while (2) + labeled break (1) = 4
  assert.strictEqual(doWorkFn?.complexity, 4);
});
