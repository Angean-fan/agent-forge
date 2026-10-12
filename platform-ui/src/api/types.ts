// 平台 API 的响应类型（对齐 platform-api 的 responses/spec 契约）。

export interface AccountInfo {
  id: number;
  username: string;
}

export interface ProjectSummary {
  id: number;
  name: string;
  active_spec_id: number | null;
  created_at: string;
}

export interface ProjectDetail extends ProjectSummary {
  requirement: string;
}

/** 需求项来源：模型推断一律为 pending，需用户显式确认为 user_confirmed。 */
export type SpecSource = 'user_explicit' | 'user_confirmed' | 'pending';

export interface SpecField {
  name: string;
  type: string;
  required: boolean;
}

export interface SpecEntity {
  name: string;
  fields: SpecField[];
  source: SpecSource;
}

export interface SpecPermission {
  role: string;
  actions: string[];
  source: SpecSource;
}

export interface SpecBusinessRule {
  id: string;
  text: string;
  source: SpecSource;
}

export interface SpecAcceptanceCase {
  id: string;
  scenario: string;
  expected: string;
  source: SpecSource;
}

export interface TechnicalProfile {
  profile_id: string;
  profile_version: string;
  template_version: string;
}

export interface SoftwareSpecification {
  schema_version: string;
  project_type: string;
  goal: string;
  actors: string[];
  entities: SpecEntity[];
  relations: Record<string, unknown>[];
  pages: Record<string, unknown>[];
  permissions: SpecPermission[];
  business_rules: SpecBusinessRule[];
  acceptance_cases: SpecAcceptanceCase[];
  technical_profile: TechnicalProfile;
  constraints: string[];
  exclusions: string[];
  capability_requests: Record<string, unknown>[];
  recipe_requests: Record<string, unknown>[];
  integrations: Record<string, unknown>[];
  external_requirements: Record<string, unknown>[];
}

export interface SpecView {
  spec_id: number;
  version: number;
  draft_revision: number;
  confirmed: boolean;
  confirmed_at: string | null;
  hash: string;
  spec: SoftwareSpecification;
  pending: string[];
  issues: string[];
  summary: string | null;
  missing_fields: string[];
  questions: string[];
}

export interface SpecConfirmView {
  spec_id: number;
  version: number;
  draft_revision: number;
  hash: string;
  confirmed_at: string;
}

/** R02/R05：访谈问答（题型由后端给定，前端不猜测）。 */
export type QuestionType = 'single_choice' | 'multi_choice' | 'text';

export interface InterviewQuestion {
  question_id: string;
  version: number;
  type: QuestionType;
  prompt: string;
  options: string[];
  allow_custom: boolean;
  required: boolean;
  affects: string[];
}

export interface InterviewAnswer {
  question_id: string;
  question_version: number;
  selected?: string[];
  custom?: string | null;
  unsure?: boolean;
}

export interface InterviewView {
  project_id: number;
  draft_id: number;
  round: number;
  status: string; // NEW / CLARIFYING / READY_TO_CONFIRM / NEEDS_INPUT
  waiting: boolean;
  questions: InterviewQuestion[];
  pending: string[];
  issues: string[];
  error_code: string | null;
}

export interface RunView {
  id: number;
  project_id: number;
  spec_id: number;
  status: string;
  error_code: string | null;
  lease_owner: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskSummary {
  total: number;
  done: number;
}

export interface CostSummary {
  currency_unit: string;
  settled_micro: number;
  reserved_micro: number;
  unknown_calls: number;
}

export interface RunArtifact {
  id: number;
  kind: string;
  path: string;
  sha256: string;
  verified: boolean;
  created_at: string;
}

export interface VerificationCheck {
  name: string;
  status: string;
}

export interface VerificationReport {
  validation_scope?: string;
  verified?: boolean;
  label?: string;
  checks?: VerificationCheck[];
  acceptance_cases?: VerificationCheck[];
}

export interface RunDetail {
  id: number;
  project_id: number;
  spec_id: number;
  status: string;
  phase: string | null;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  tasks: TaskSummary;
  cost: CostSummary;
  artifacts: RunArtifact[];
  verification: VerificationReport | null;
}

export interface RunEvent {
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface RunEventsData {
  run_id: number;
  items: RunEvent[];
  latest_sequence: number;
}
