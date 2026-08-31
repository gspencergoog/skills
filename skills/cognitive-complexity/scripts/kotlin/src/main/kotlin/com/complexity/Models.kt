package com.complexity

import com.google.gson.annotations.SerializedName

data class ComplexityIncrement(
    val line: Int,
    val column: Int,
    val type: String,
    val increment: Int,
    val nesting: Int,
    val reason: String
)

data class FunctionComplexity(
    val name: String,
    @SerializedName("class_name") val className: String?,
    @SerializedName("line_number") val lineNumber: Int,
    @SerializedName("end_line_number") val endLineNumber: Int,
    val complexity: Int,
    @SerializedName("exceeds_threshold") val exceedsThreshold: Boolean,
    val breakdown: List<ComplexityIncrement>
)

data class FileComplexity(
    val path: String,
    @SerializedName("total_complexity") val totalComplexity: Int,
    @SerializedName("average_complexity") val averageComplexity: Double,
    @SerializedName("highest_complexity") val highestComplexity: Int,
    val functions: MutableList<FunctionComplexity>
)

data class ComplexitySummary(
    @SerializedName("total_files") val totalFiles: Int,
    @SerializedName("total_functions") val totalFunctions: Int,
    @SerializedName("total_complexity") val totalComplexity: Int,
    @SerializedName("average_complexity") val averageComplexity: Double,
    @SerializedName("highest_complexity") val highestComplexity: Int,
    @SerializedName("functions_exceeding_threshold") val functionsExceedingThreshold: Int,
    val threshold: Int
)

data class ComplexityReport(
    val version: String = "1.0.0",
    val language: String = "kotlin",
    val summary: ComplexitySummary,
    val files: List<FileComplexity>
)
