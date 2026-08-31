package com.complexity

import java.io.File
import java.util.regex.Pattern

class KotlinComplexityAnalyzer(val threshold: Int = 15) {

    fun analyzeSource(sourceCode: String, filePath: String = "<stdin>"): FileComplexity {
        val functions = collectFunctions(sourceCode, filePath)
        val totalComplexity = functions.sumOf { it.complexity }
        val avgComplexity = if (functions.isNotEmpty()) totalComplexity.toDouble() / functions.size else 0.0
        val highestComplexity = functions.maxOfOrNull { it.complexity } ?: 0

        return FileComplexity(
            path = filePath,
            totalComplexity = totalComplexity,
            averageComplexity = (avgComplexity * 100).toInt() / 100.0,
            highestComplexity = highestComplexity,
            functions = functions.toMutableList()
        )
    }

    private fun collectFunctions(sourceCode: String, filePath: String): List<FunctionComplexity> {
        val lines = sourceCode.lines()
        val functionPattern = Pattern.compile("""(?:fun|override\s+fun)\s+(?:<[^>]+>\s+)?(?:[a-zA-Z0-9_]+\.)?([a-zA-Z0-9_]+)\s*\(""")
        val classPattern = Pattern.compile("""(?:class|interface|object)\s+([a-zA-Z0-9_]+)""")

        val result = mutableListOf<FunctionComplexity>()
        var currentClass: String? = null

        var i = 0
        while (i < lines.size) {
            val line = lines[i]
            val classMatcher = classPattern.matcher(line)
            if (classMatcher.find()) {
                currentClass = classMatcher.group(1)
            }

            val matcher = functionPattern.matcher(line)
            if (matcher.find()) {
                val funcName = matcher.group(1)
                val startLine = i + 1

                // Extract function body by tracking curly braces
                var braceCount = 0
                var foundOpenBrace = false
                val bodyLines = mutableListOf<Pair<Int, String>>()
                var endLine = startLine

                for (j in i until lines.size) {
                    val currentLine = lines[j]
                    bodyLines.add(Pair(j + 1, currentLine))
                    val openCount = currentLine.count { it == '{' }
                    val closeCount = currentLine.count { it == '}' }

                    if (openCount > 0) {
                        foundOpenBrace = true
                    }
                    braceCount += openCount - closeCount

                    if (foundOpenBrace && braceCount <= 0) {
                        endLine = j + 1
                        i = j
                        break
                    }
                }

                val funcComplexity = analyzeFunctionBody(funcName, currentClass, startLine, endLine, bodyLines)
                result.add(funcComplexity)
            }
            i++
        }

        return result
    }

    private fun analyzeFunctionBody(
        functionName: String,
        className: String?,
        startLine: Int,
        endLine: Int,
        linesWithNumbers: List<Pair<Int, String>>
    ): FunctionComplexity {
        var complexity = 0
        val breakdown = mutableListOf<ComplexityIncrement>()
        var currentNesting = 0

        fun addIncrement(lineNum: Int, type: String, baseInc: Int, nestingPenalty: Boolean, reason: String) {
            val penalty = if (nestingPenalty) currentNesting else 0
            val totalInc = baseInc + penalty
            complexity += totalInc
            val detail = "$reason (+$baseInc${if (penalty > 0) " + nesting $penalty" else ""} = +$totalInc)"
            breakdown.add(
                ComplexityIncrement(
                    line = lineNum,
                    column = 1,
                    type = type,
                    increment = totalInc,
                    nesting = currentNesting,
                    reason = detail
                )
            )
        }

        // Tokenized line analysis with nesting tracking
        for ((lineNum, rawLine) in linesWithNumbers) {
            val line = rawLine.trim()
            if (line.startsWith("//") || line.startsWith("/*") || line.startsWith("*")) continue

            // Check if / else if
            if (line.contains("else if") || line.contains("else  if")) {
                addIncrement(lineNum, "else_if", 1, false, "else if branch")
            } else if (line.matches(Regex(""".*\bif\s*\(.*"""))) {
                addIncrement(lineNum, "if", 1, true, "if expression")
            }

            // Check when expression
            if (line.matches(Regex(""".*\bwhen\s*(\(.*\))?\s*\{?.*"""))) {
                addIncrement(lineNum, "when", 1, true, "when expression")
            }

            // Check loops
            if (line.matches(Regex(""".*\bfor\s*\(.*"""))) {
                addIncrement(lineNum, "for_loop", 1, true, "for loop")
            }
            if (line.matches(Regex(""".*\bwhile\s*\(.*"""))) {
                addIncrement(lineNum, "while_loop", 1, true, "while loop")
            }
            if (line.matches(Regex(""".*\bdo\s*\{.*"""))) {
                addIncrement(lineNum, "do_while_loop", 1, true, "do-while loop")
            }

            // Check catch
            if (line.matches(Regex(""".*\bcatch\s*\(.*"""))) {
                addIncrement(lineNum, "catch", 1, true, "catch clause")
            }

            // Check ternary / elvis ?:
            if (line.contains("?:")) {
                addIncrement(lineNum, "elvis_op", 1, false, "elvis operator (?:)")
            }

            // Check boolean operators
            if (line.contains("&&") && line.contains("||")) {
                addIncrement(lineNum, "bool_op_sequence", 1, false, "boolean operator sequence (&&)")
                addIncrement(lineNum, "bool_op_switch", 1, false, "boolean operator switch to (||)")
            } else if (line.contains("&&")) {
                addIncrement(lineNum, "bool_op_sequence", 1, false, "boolean operator sequence (&&)")
            } else if (line.contains("||")) {
                addIncrement(lineNum, "bool_op_sequence", 1, false, "boolean operator sequence (||)")
            }

            // Check recursion
            if (line.contains("$functionName(") && !line.contains("fun $functionName")) {
                addIncrement(lineNum, "recursion", 1, false, "direct recursion call")
            }

            // Check labeled break / continue
            if (line.matches(Regex(""".*\b(break|continue)@[a-zA-Z0-9_]+.*"""))) {
                addIncrement(lineNum, "labeled_jump", 1, false, "labeled jump")
            }

            // Track nesting
            val opens = line.count { it == '{' }
            val closes = line.count { it == '}' }
            if (opens > closes) {
                currentNesting += (opens - closes)
            } else if (closes > opens) {
                currentNesting = maxOf(0, currentNesting - (closes - opens))
            }
        }

        return FunctionComplexity(
            name = functionName,
            className = className,
            lineNumber = startLine,
            endLineNumber = endLine,
            complexity = complexity,
            exceedsThreshold = complexity > threshold,
            breakdown = breakdown
        )
    }
}
