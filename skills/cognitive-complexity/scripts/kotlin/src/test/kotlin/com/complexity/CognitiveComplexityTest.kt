package com.complexity

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class CognitiveComplexityTest {
    private val analyzer = KotlinComplexityAnalyzer(threshold = 15)

    private fun getComplexity(code: String, funcName: String): Int {
        val result = analyzer.analyzeSource(code, "Test.kt")
        val func = result.functions.find { it.name == funcName }
            ?: throw AssertionError("Function $funcName not found in ${result.functions.map { it.name }}")
        return func.complexity
    }

    @Test
    fun testTC01LinearCode() {
        val code = """
            fun linearFunction(a: Int, b: Int): Int {
                val x = a + b
                val y = x * 2
                return y
            }
        """.trimIndent()
        assertEquals(0, getComplexity(code, "linearFunction"))
    }

    @Test
    fun testTC02SingleIf() {
        val code = """
            fun singleIf(x: Int): Int {
                if (x > 0) {
                    return x
                }
                return -x
            }
        """.trimIndent()
        assertEquals(1, getComplexity(code, "singleIf"))
    }

    @Test
    fun testTC03NestedIfLoop() {
        val code = """
            fun nestedIfLoop(items: List<Int>): Int {
                var total = 0
                for (x in items) {
                    if (x > 0) {
                        total += x
                    }
                }
                return total
            }
        """.trimIndent()
        assertEquals(3, getComplexity(code, "nestedIfLoop"))
    }

    @Test
    fun testTC04WhenExpression() {
        val code = """
            fun processCommand(cmd: String): Int {
                when (cmd) {
                    "start" -> return 1
                    "stop" -> return 0
                    else -> return -1
                }
            }
        """.trimIndent()
        assertEquals(1, getComplexity(code, "processCommand"))
    }

    @Test
    fun testTC05BoolChain() {
        val code = """
            fun boolChain(a: Boolean, b: Boolean, c: Boolean): Boolean {
                if (a && b && c) {
                    return true
                }
                return false
            }
        """.trimIndent()
        assertEquals(2, getComplexity(code, "boolChain"))
    }

    @Test
    fun testTC06BoolSwitch() {
        val code = """
            fun boolSwitch(a: Boolean, b: Boolean, c: Boolean): Boolean {
                if ((a && b) || c) {
                    return true
                }
                return false
            }
        """.trimIndent()
        assertEquals(3, getComplexity(code, "boolSwitch"))
    }

    @Test
    fun testTC07ElseIfChain() {
        val code = """
            fun elseIfChain(x: Int): String {
                if (x == 1) {
                    return "one"
                } else if (x == 2) {
                    return "two"
                } else if (x == 3) {
                    return "three"
                } else {
                    return "other"
                }
            }
        """.trimIndent()
        assertEquals(3, getComplexity(code, "elseIfChain"))
    }

    @Test
    fun testTC08TripleLoop() {
        val code = """
            fun tripleLoop(matrix: List<List<List<Int>>>) {
                for (row in matrix) {
                    for (col in row) {
                        for (item in col) {
                            println(item)
                        }
                    }
                }
            }
        """.trimIndent()
        assertEquals(6, getComplexity(code, "tripleLoop"))
    }

    @Test
    fun testTC09Recursion() {
        val code = """
            fun factorial(n: Int): Int {
                if (n <= 1) {
                    return 1
                }
                return n * factorial(n - 1)
            }
        """.trimIndent()
        assertEquals(2, getComplexity(code, "factorial"))
    }

    @Test
    fun testTC10TryCatch() {
        val code = """
            fun safeDivide(a: Double, b: Double): Double {
                try {
                    return a / b
                } catch (e: Exception) {
                    return 0.0
                }
            }
        """.trimIndent()
        assertEquals(1, getComplexity(code, "safeDivide"))
    }

    @Test
    fun testTC11NestedClosure() {
        val code = """
            fun outerWithClosure(items: List<Int>): List<Int> {
                val f = { x: Int ->
                    if (x > 0) x * 2 else 0
                }
                return items.map(f)
            }
        """.trimIndent()
        assertEquals(2, getComplexity(code, "outerWithClosure"))
    }

    @Test
    fun testTC12TernaryNested() {
        val code = """
            fun ternaryNested(items: List<Int>) {
                for (x in items) {
                    val val_x = if (x > 0) 1 else -1
                }
            }
        """.trimIndent()
        assertEquals(3, getComplexity(code, "ternaryNested"))
    }

    @Test
    fun testTC13GuardClause() {
        val code = """
            fun guardClause(user: User?): String? {
                if (user == null) {
                    return null
                }
                if (!user.isActive) {
                    return null
                }
                return user.name
            }
        """.trimIndent()
        assertEquals(2, getComplexity(code, "guardClause"))
    }

    @Test
    fun testTC14ElvisOperator() {
        val code = """
            fun elvisBranch(user: User?): String {
                val name = user?.name ?: "Unknown"
                return name
            }
        """.trimIndent()
        assertEquals(1, getComplexity(code, "elvisBranch"))
    }

    @Test
    fun testTC15SonarSourceAppendixB() {
        val code = """
            fun getElement(matrix: Array<Array<Int?>>): Int? {
                for (i in 0 until matrix.size) {                          // +1 (nesting 0)
                    for (j in 0 until matrix[i].size) {                   // +2 (nesting 1)
                        if (matrix[i][j] != null) {                       // +3 (nesting 2)
                            if (matrix[i][j]!! > 0 && matrix[i][j]!! < 100) { // +4 (nesting 3) + 1 (bool) = +5
                                return matrix[i][j]
                            } else if (matrix[i][j] == 0) {               // +1 (else if base)
                                continue
                            }
                        }
                    }
                }
                return null
            }
        """.trimIndent()
        // 1 + 2 + 3 + (4 + 1) + 1 = 12
        assertEquals(12, getComplexity(code, "getElement"))
    }
}
