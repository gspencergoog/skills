export interface ComplexityIncrement {
  line: number;
  column: number;
  type: string;
  increment: number;
  nesting: number;
  reason: string;
}

export interface FunctionComplexity {
  name: string;
  class_name: string | null;
  line_number: number;
  end_line_number: number;
  complexity: number;
  exceeds_threshold: boolean;
  breakdown: ComplexityIncrement[];
}

export interface FileComplexity {
  path: string;
  total_complexity: number;
  average_complexity: number;
  highest_complexity: number;
  functions: FunctionComplexity[];
}

export interface ComplexitySummary {
  total_files: number;
  total_functions: number;
  total_complexity: number;
  average_complexity: number;
  highest_complexity: number;
  functions_exceeding_threshold: number;
  threshold: number;
}

export interface ComplexityReport {
  version: string;
  language: string;
  summary: ComplexitySummary;
  files: FileComplexity[];
}
