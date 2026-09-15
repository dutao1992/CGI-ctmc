export type Report = {
  failure_mode: string;
  confidence: number;
  executive_summary: string;
  observations: string[];
  mechanisms: Array<{ name: string; probability: string; rationale: string }>;
  root_causes: Array<{ category: string; cause: string; evidence: string }>;
  evidence_matrix: Array<{ evidence: string; supports: string; strength: string }>;
  knowledge_references?: Array<{ source_title: string; used_for: string; caveat: string }>;
  actions: Array<{ priority: string; action: string; owner: string; verification: string }>;
  tests_required: Array<{ test: string; purpose: string; method: string }>;
  missing_information: string[];
  risk_statement: string;
  disclaimer: string;
};

export type Analysis = {
  id: string;
  created_at: string;
  case_no: string;
  part_name: string;
  machine_model: string;
  machine_position: string;
  model: string;
  status: string;
  failure_mode: string;
  confidence: number;
  knowledge_source_count: number;
  metadata?: Record<string, string>;
  images?: Array<{ name: string; url: string }>;
  report?: Report;
  preliminary_report?: {
    confidence: number;
    summary: string;
    knowledge_findings: Array<{ finding: string; source_title: string; applicability: string }>;
    initial_hypotheses: string[];
    verification_focus: string[];
  };
  knowledge_sources?: Array<{ title: string; snippet: string; matched_query?: string }>;
};

export type AnalysisJob = {
  id: string;
  status: "queued" | "searching" | "preliminary" | "reasoning" | "saving" | "completed" | "failed";
  progress: number;
  message: string;
  analysis_id?: string;
  error?: string;
  analysis?: Analysis;
};
