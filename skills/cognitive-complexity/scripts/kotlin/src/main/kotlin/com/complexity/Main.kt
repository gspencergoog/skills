package com.complexity

import com.google.gson.GsonBuilder
import java.io.File
import kotlin.system.exitProcess

fun main(args: Array<String>) {
    var format = "text"
    var threshold = 15
    var verbose = false
    var sortKey = "complexity"
    var pathArg = "-"
    val excludes = mutableListOf<String>()

    var i = 0
    while (i < args.size) {
        when (val arg = args[i]) {
            "-f", "--format" -> { i++; if (i < args.size) format = args[i] }
            "-t", "--threshold" -> { i++; if (i < args.size) threshold = args[i].toIntOrNull() ?: 15 }
            "-v", "--verbose" -> verbose = true
            "-s", "--sort" -> { i++; if (i < args.size) sortKey = args[i] }
            "-e", "--exclude" -> { i++; if (i < args.size) excludes.add(args[i]) }
            "-h", "--help" -> {
                println("""Usage: cognitive-complexity-kt [OPTIONS] [PATH]

Calculate Cognitive Complexity for Kotlin code according to SonarSource standard.

Arguments:
  [PATH]                     Path to file or directory. If omitted or "-", reads from stdin.

Options:
  -f, --format <FORMAT>      Output format: text (default), json, table, summary
  -t, --threshold <INT>      Threshold for flagging high complexity (default: 15)
  -v, --verbose              Show detailed breakdown of increments
  -s, --sort <KEY>           Sort by: complexity (default), name, line, file
  -e, --exclude <PATTERN>    Glob patterns to exclude during directory traversal
  -h, --help                 Show help message
  -V, --version              Show version""")
                exitProcess(0)
            }
            "-V", "--version" -> {
                println("cognitive-complexity-kt 1.0.0")
                exitProcess(0)
            }
            else -> {
                if (!arg.startsWith("-")) {
                    pathArg = arg
                }
            }
        }
        i++
    }

    val analyzer = KotlinComplexityAnalyzer(threshold = threshold)
    val fileResults = mutableListOf<FileComplexity>()

    if (pathArg == "-") {
        val sourceCode = System.`in`.bufferedReader().readText()
        fileResults.add(analyzer.analyzeSource(sourceCode, "<stdin>"))
    } else {
        val target = File(pathArg)
        if (!target.exists()) {
            System.err.println("Error: Path does not exist: $pathArg")
            exitProcess(2)
        }

        if (target.isFile) {
            val sourceCode = target.readText()
            fileResults.add(analyzer.analyzeSource(sourceCode, target.path))
        } else if (target.isDirectory) {
            target.walkTopDown().filter { it.isFile && (it.extension == "kt" || it.extension == "kts") }.forEach { file ->
                try {
                    val sourceCode = file.readText()
                    fileResults.add(analyzer.analyzeSource(sourceCode, file.path))
                } catch (e: Exception) {
                    System.err.println("Error reading ${file.path}: ${e.message}")
                }
            }
        }
    }

    for (fileComp in fileResults) {
        when (sortKey) {
            "complexity" -> fileComp.functions.sortByDescending { it.complexity }
            "name" -> fileComp.functions.sortBy { it.name }
            "line" -> fileComp.functions.sortBy { it.lineNumber }
        }
    }

    val totalFiles = fileResults.size
    val totalFuncs = fileResults.sumOf { it.functions.size }
    val totalComplexity = fileResults.sumOf { it.totalComplexity }
    val avgComplexity = if (totalFuncs > 0) totalComplexity.toDouble() / totalFuncs else 0.0
    val highestComplexity = fileResults.maxOfOrNull { it.highestComplexity } ?: 0
    val exceedingCount = fileResults.sumOf { f -> f.functions.count { it.exceedsThreshold } }

    val report = ComplexityReport(
        version = "1.0.0",
        language = "kotlin",
        summary = ComplexitySummary(
            totalFiles = totalFiles,
            totalFunctions = totalFuncs,
            totalComplexity = totalComplexity,
            averageComplexity = (avgComplexity * 100).toInt() / 100.0,
            highestComplexity = highestComplexity,
            functionsExceedingThreshold = exceedingCount,
            threshold = threshold
        ),
        files = fileResults
    )

    if (format == "json") {
        val gson = GsonBuilder().setPrettyPrinting().create()
        println(gson.toJson(report))
    } else if (format == "summary") {
        println("Files: $totalFiles, Functions: $totalFuncs, Total Complexity: $totalComplexity, Avg: ${String.format("%.2f", avgComplexity)}, Over Threshold: $exceedingCount")
    } else {
        println("Cognitive Complexity Report (Language: ${report.language})")
        println("=".repeat(60))
        for (f in report.files) {
            println("\nFile: ${f.path}")
            if (f.functions.isEmpty()) {
                println("  (No functions or methods found)")
                continue
            }
            for (fn in f.functions) {
                val qName = if (fn.className != null) "${fn.className}.${fn.name}" else fn.name
                val status = if (fn.exceedsThreshold) "[EXCEEDS THRESHOLD $threshold]" else "[PASS]"
                println("  $qName (lines ${fn.lineNumber}-${fn.endLineNumber}) -> Complexity: ${fn.complexity} $status")
                if (verbose && fn.breakdown.isNotEmpty()) {
                    for (b in fn.breakdown) {
                        println("    Line ${String.format("%4d", b.line)}: ${b.reason}")
                    }
                }
            }
        }
        println("\n" + "-".repeat(60))
        println("Summary:")
        println("  Files analyzed:                $totalFiles")
        println("  Total functions:               $totalFuncs")
        println("  Total complexity:              $totalComplexity")
        println("  Average complexity:            ${String.format("%.2f", avgComplexity)}")
        println("  Highest complexity:            $highestComplexity")
        println("  Functions exceeding threshold: $exceedingCount (threshold: $threshold)")
        println("=".repeat(60))
    }

    exitProcess(if (exceedingCount > 0) 1 else 0)
}
