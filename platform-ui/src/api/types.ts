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
  cancel_requested: boolean;
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

export interface CancellationView {
  requested: boolean;
  notice: string | null;
}

// E04：外部服务的脱敏就绪快照（Mock/Live/配置分开呈现，不含真实值）。
export interface ExternalServiceView {
  service_id: string;
  name: string;
  mode: string;
  required: boolean;
  required_for_live: boolean;
  secret: boolean;
  env_name: string;
  config_present: boolean;
  mock_status: string;
  live_status: string;
  reason: string | null;
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
  cancellation: CancellationView | null;
  code_delivery_status: string;
  external_readiness: string;
  external_notice: string | null;
  external_services: ExternalServiceView[];
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

export interface ProfileOption {
  profile_id: string;
  profile_version: string;
  template_version: string;
  frontend_language: string | null;
  orm: string | null;
  recommended: boolean;
  notes: string[];
}

export interface ProfileExplanation {
  profile_id: string;
  explanation: string;
  thinking_mode: string;
  reasoning_effort: string | null;
}

/** FP10：运行模式选择（标准装配模式 / 异步工厂模式）。 */
export type RunMode = 'standard' | 'factory';

export interface FactoryBlueprintSummary {
  phase: string;
  version: number;
  status: string;
  blueprint_hash: string;
  module_count: number;
  module_ids: string[];
  sandbox_integrations: Array<Record<string, unknown>>;
}

export interface FactoryDepartmentLane {
  phase: string;
  dept_id: string;
  status: string;
  current_round: number;
  max_rounds: number;
  duration_ms: number | null;
  test_status: string | null;
  test_executed: boolean;
  test_exit_code: number | null;
  verifier_kind: string | null;
  error_code: string | null;
  cost_micro: number;
  calls: number;
  input_tokens: number;
  output_tokens: number;
}

export interface FactoryDefectSummary {
  id: number;
  phase: string;
  dept_id: string;
  scope: string;
  round: number;
  error_class: string;
  summary: string;
  log_excerpt: string;
  affected_paths: string[];
  resolved: boolean;
  created_at: string;
  resolved_at: string | null;
}

export interface FactoryRoutingRole {
  role: string;
  provider: string;
  model: string;
  base_url_host: string;
  api_key_env: string;
  api_key_present: boolean;
  enabled: boolean;
  supports_tools: boolean;
}

/** 左侧「模型配置」面板：自定义模型库预设项（严禁明文密钥）。 */
export interface CustomModelPreset {
  id: string;
  name: string;
  provider: string;
  model: string;
  base_url: string;
  base_url_host: string;
  api_key_env: string;
  api_key_present: boolean;
  supports_tools: boolean;
  max_output_tokens: number | null;
  timeout_seconds: number | null;
  removable: boolean;
}

export interface CustomModelPresetCreatePayload {
  id?: string | null;
  name: string;
  provider: string;
  model: string;
  base_url: string;
  api_key_env: string;
  supports_tools: boolean;
  max_output_tokens?: number | null;
  timeout_seconds?: number | null;
}

/** 左侧「模型配置」面板：全角色脱敏模型路由与受控更新结构。 */
export interface ModelRoleConfig {
  role: string;
  label: string;
  provider: string;
  model: string;
  base_url: string;
  base_url_host: string;
  api_key_env: string;
  api_key_present: boolean;
  enabled: boolean;
  supports_tools: boolean;
  max_output_tokens: number | null;
  timeout_seconds: number | null;
  source: string;
}

export interface ModelConfigView {
  config_source: string;
  writable: boolean;
  roles: ModelRoleConfig[];
  custom_models?: CustomModelPreset[];
}

export interface ModelRoleUpdateItem {
  role: string;
  provider: string;
  model: string;
  base_url: string;
  api_key_env: string;
  enabled: boolean;
  supports_tools: boolean;
  max_output_tokens?: number | null;
  timeout_seconds?: number | null;
}

export interface FactoryRunView {
  run_id: number;
  project_id: number;
  run_status: string;
  factory_active: boolean;
  current_phase: string;
  integration_rounds: Record<string, number>;
  blueprints: FactoryBlueprintSummary[];
  backend_departments: FactoryDepartmentLane[];
  frontend_departments: FactoryDepartmentLane[];
  open_defects: FactoryDefectSummary[];
  resolved_defects: FactoryDefectSummary[];
  cost_by_department: Record<string, unknown>;
  model_routing_snapshot: FactoryRoutingRole[];
}
