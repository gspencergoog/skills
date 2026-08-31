#!/usr/bin/env node
import * as fs from "fs";
import * as path from "path";
import { TypeScriptComplexityAnalyzer } from "./visitor";
import { ComplexityReport, FileComplexity } from "./types";

interface CliOptions {
  format: string;
  threshold: number;
  verbose: boolean;
  sortKey: string;
  pathArg: string;
  excludes: string[];
}

function parseArgs(args: string[]): CliOptions {
  let format = "text";
  let threshold = 15;
  let verbose = false;
  let sortKey = "complexity";
  let pathArg = "-";
  const excludes: string[] = [];

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "-f" || arg === "--format") {
      format = args[++i];
    } else if (arg === "-t" || arg === "--threshold") {
      threshold = parseInt(args[++i], 10);
    } else if (arg === "-v" || arg === "--verbose") {
      verbose = true;
    } else if (arg === "-s" || arg === "--sort") {
      sortKey = args[++i];
    } else if (arg === "-e" || arg === "--exclude") {
      excludes.push(args[++i]);
    } else if (arg === "-h" || arg === "--help") {
      printHelp();
      process.exit(0);
    } else if (arg === "-V" || arg === "--version") {
      console.log("cognitive-complexity-ts 1.0.0");
      process.exit(0);
    } else if (!arg.startsWith("-")) {
      pathArg = arg;
    }
  }

  return { format, threshold, verbose, sortKey, pathArg, excludes };
}

function printHelp(): void {
  console.log(`Usage: cognitive-complexity-ts [OPTIONS] [PATH]

Calculate Cognitive Complexity for TypeScript/JavaScript code according to SonarSource standard.

Arguments:
  [PATH]                     Path to file or directory. If omitted or "-", reads from stdin.

Options:
  -f, --format <FORMAT>      Output format: text (default), json, table, summary
  -t, --threshold <INT>      Threshold for flagging high complexity (default: 15)
  -v, --verbose              Show detailed breakdown of increments
  -s, --sort <KEY>           Sort by: complexity (default), name, line, file
  -e, --exclude <PATTERN>    Glob patterns to exclude during directory traversal
  -h, --help                 Show help message
  -V, --version              Show version`);
}

function collectFiles(dir: string, excludes: string[]): string[] {
  let results: string[] = [];
  const entries = fs.readdirSync(dir, { withFileTypes: true });

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules" && entry.name !== ".git" && entry.name !== "dist") {
        results = results.concat(collectFiles(fullPath, excludes));
      }
    } else if (
      entry.isFile() &&
      (entry.name.endsWith(".ts") ||
        entry.name.endsWith(".tsx") ||
        entry.name.endsWith(".js") ||
        entry.name.endsWith(".jsx"))
    ) {
      results.push(fullPath);
    }
  }
  return results;
}

function analyzeTarget(options: CliOptions, analyzer: TypeScriptComplexityAnalyzer): FileComplexity[] {
  const fileResults: FileComplexity[] = [];

  if (options.pathArg === "-" || !options.pathArg) {
    const sourceCode = fs.readFileSync(0, "utf-8");
    fileResults.push(analyzer.analyzeSource(sourceCode, "<stdin>"));
    return fileResults;
  }

  const targetPath = options.pathArg;
  if (!fs.existsSync(targetPath)) {
    console.error(`Error: Path does not exist: ${targetPath}`);
    process.exit(2);
  }

  const stat = fs.statSync(targetPath);
  if (stat.isFile()) {
    const sourceCode = fs.readFileSync(targetPath, "utf-8");
    fileResults.push(analyzer.analyzeSource(sourceCode, targetPath));
  } else if (stat.isDirectory()) {
    const files = collectFiles(targetPath, options.excludes);
    for (const file of files) {
      try {
        const sourceCode = fs.readFileSync(file, "utf-8");
        fileResults.push(analyzer.analyzeSource(sourceCode, file));
      } catch (e: any) {
        console.error(`Error reading ${file}: ${e.message}`);
      }
    }
  }

  return fileResults;
}

function sortFunctions(fileResults: FileComplexity[], sortKey: string): void {
  for (const fileComp of fileResults) {
    if (sortKey === "complexity") {
      fileComp.functions.sort((a, b) => b.complexity - a.complexity);
    } else if (sortKey === "name") {
      fileComp.functions.sort((a, b) => a.name.localeCompare(b.name));
    } else if (sortKey === "line") {
      fileComp.functions.sort((a, b) => a.line_number - b.line_number);
    }
  }
}

function buildReport(fileResults: FileComplexity[], threshold: number): ComplexityReport {
  const totalFiles = fileResults.length;
  const totalFuncs = fileResults.reduce((sum, f) => sum + f.functions.length, 0);
  const totalComplexity = fileResults.reduce((sum, f) => sum + f.total_complexity, 0);
  const avgComplexity = totalFuncs > 0 ? totalComplexity / totalFuncs : 0;
  const highestComplexity = fileResults.reduce((max, f) => Math.max(max, f.highest_complexity), 0);
  const exceedingCount = fileResults.reduce(
    (sum, f) => sum + f.functions.filter((fn) => fn.exceeds_threshold).length,
    0
  );

  return {
    version: "1.0.0",
    language: "typescript",
    summary: {
      total_files: totalFiles,
      total_functions: totalFuncs,
      total_complexity: totalComplexity,
      average_complexity: Math.round(avgComplexity * 100) / 100,
      highest_complexity: highestComplexity,
      functions_exceeding_threshold: exceedingCount,
      threshold,
    },
    files: fileResults,
  };
}

function renderReport(report: ComplexityReport, format: string, verbose: boolean): void {
  if (format === "json") {
    console.log(JSON.stringify(report, null, 2));
    return;
  }
  if (format === "summary") {
    console.log(
      `Files: ${report.summary.total_files}, Functions: ${report.summary.total_functions}, Total Complexity: ${report.summary.total_complexity}, Avg: ${report.summary.average_complexity}, Over Threshold: ${report.summary.functions_exceeding_threshold}`
    );
    return;
  }

  console.log(`Cognitive Complexity Report (Language: ${report.language})`);
  console.log("=".repeat(60));
  for (const f of report.files) {
    console.log(`\nFile: ${f.path}`);
    if (f.functions.length === 0) {
      console.log("  (No functions or methods found)");
      continue;
    }
    for (const fn of f.functions) {
      const qName = fn.class_name ? `${fn.class_name}.${fn.name}` : fn.name;
      const status = fn.exceeds_threshold ? `[EXCEEDS THRESHOLD ${report.summary.threshold}]` : "[PASS]";
      console.log(`  ${qName} (lines ${fn.line_number}-${fn.end_line_number}) -> Complexity: ${fn.complexity} ${status}`);
      if (verbose && fn.breakdown.length > 0) {
        for (const b of fn.breakdown) {
          console.log(`    Line ${String(b.line).padStart(4, " ")}: ${b.reason}`);
        }
      }
    }
  }
  console.log("\n" + "-".repeat(60));
  console.log("Summary:");
  console.log(`  Files analyzed:                ${report.summary.total_files}`);
  console.log(`  Total functions:               ${report.summary.total_functions}`);
  console.log(`  Total complexity:              ${report.summary.total_complexity}`);
  console.log(`  Average complexity:            ${report.summary.average_complexity.toFixed(2)}`);
  console.log(`  Highest complexity:            ${report.summary.highest_complexity}`);
  console.log(`  Functions exceeding threshold: ${report.summary.functions_exceeding_threshold} (threshold: ${report.summary.threshold})`);
  console.log("=".repeat(60));
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  const analyzer = new TypeScriptComplexityAnalyzer(options.threshold);
  const fileResults = analyzeTarget(options, analyzer);

  sortFunctions(fileResults, options.sortKey);
  const report = buildReport(fileResults, options.threshold);
  renderReport(report, options.format, options.verbose);

  process.exit(report.summary.functions_exceeding_threshold > 0 ? 1 : 0);
}

main();
