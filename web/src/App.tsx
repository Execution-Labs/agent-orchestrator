import { CSSProperties, FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { buildApiUrl, buildAuthHeaders } from './api'
import { ImportJobPanel } from './components/AppPanels/ImportJobPanel'
import { TerminalPanel } from './components/AppPanels/TerminalPanel'
import HITLModeSelector from './components/HITLModeSelector/HITLModeSelector'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { humanizeLabel } from './ui/labels'
import { ReviewMode, ReviewDecisionType, REVIEW_MODE_OPTIONS, REVIEW_DECISION_OPTIONS } from './types/review'
import './styles/orchestrator.css'

type ThemeMode = 'light' | 'dark' | 'system'
type RouteKey = 'board' | 'execution' | 'settings'
type SettingsTab = 'providers' | 'execution' | 'advanced'
type CreateTab = 'task' | 'import' | 'review'
type PullRequestItem = {
  number: number
  title: string
  author: string
  head_ref: string
  base_ref: string
  url: string
  has_review_task: boolean
  review_task_id: string | null
}
type TaskDetailTab = 'overview' | 'plan' | 'workdoc' | 'logs' | 'activity' | 'dependencies' | 'configuration' | 'changes'
type TaskActionKey = 'save' | 'run' | 'retry' | 'cancel' | 'transition' | 'delete' | 'clear' | 'approve_gate' | 'review_commit' | 'queue-backlog' | 'generate_follow_ups' | 'post_review_comments'
type GeneratedTaskStatus = 'backlog' | 'queued'
type GeneratedTaskHitlSelection = 'inherit_parent' | 'autopilot' | 'supervised' | 'review_only'

type TaskGateContext = {
  is_waiting: boolean
  gate: string | null
  display: string | null
  step: string | null
  status_kind: 'approval_wait' | 'intervention_wait' | null
}

type TaskWaitState = {
  kind: 'approval_wait' | 'intervention_wait' | 'auto_recovery_wait' | string
  step?: string | null
  reason_code?: string | null
  recoverable?: boolean
  attempt?: number
  max_attempts?: number
  next_retry_at?: string | null
  updated_at?: string | null
}

type TaskRecord = {
  id: string
  title: string
  description?: string
  task_type?: string
  current_step?: string | null
  current_agent_id?: string | null
  worker_model?: string | null
  worker_provider?: string | null
  step_timeout_seconds?: number | null
  priority: string
  status: string
  labels?: string[]
  blocked_by?: string[]
  blocks?: string[]
  parent_id?: string | null
  children_ids?: string[]
  source?: string
  pipeline_template?: string[]
  retry_count?: number
  hitl_mode?: string
  dependency_policy?: 'permissive' | 'prudent' | 'strict'
  pending_gate?: string | null
  gate_context?: TaskGateContext | null
  wait_state?: TaskWaitState | null
  quality_gate?: Record<string, number>
  metadata?: Record<string, unknown>
  human_blocking_issues?: HumanBlockingIssue[]
  human_review_actions?: ReviewAction[]
  can_skip_to_precommit?: boolean
  skip_to_precommit_reason_code?: string | null
  can_finalize_merge_conflict?: boolean
  finalize_merge_conflict_reason_code?: string | null
  recommended_action?: string | null
  error?: string | null
  timing_summary?: TaskTimingSummary | null
  execution_summary?: ExecutionSummary | null
  project_commands?: Record<string, Record<string, string>> | null
}

type ReviewAction = {
  action: 'approve' | 'request_changes' | 'retry' | 'finalize_merge_conflict'
  ts: string
  guidance: string
  previous_error?: string
}

type ExecutionStepSummary = {
  step: string
  status: string
  summary: string
  open_counts?: Record<string, number>
  commit?: string
  duration_seconds?: number | null
}

type ExecutionSummary = {
  run_id: string
  run_status: string
  run_summary: string
  started_at: string | null
  finished_at: string | null
  duration_seconds?: number | null
  steps: ExecutionStepSummary[]
}

type TaskTimingSummary = {
  total_completed_seconds?: number | null
  active_run_started_at?: string | null
  is_running?: boolean
  first_started_at?: string | null
  last_finished_at?: string | null
}

type BoardResponse = {
  columns: Record<string, TaskRecord[]>
}

type PreviewNode = {
  id: string
  title: string
  priority: string
}

type PreviewEdge = {
  from: string
  to: string
}

type PrdPreview = {
  nodes: PreviewNode[]
  edges: PreviewEdge[]
  chunk_count?: number
  strategy?: string
  ambiguity_warnings?: string[]
}

type OrchestratorStatus = {
  status: string
  queue_depth: number
  in_progress: number
  draining: boolean
  run_branch?: string | null
  scheduler_attached?: boolean
  scheduler_stale?: boolean
  last_tick_at?: string | null
  last_dispatch_at?: string | null
  tick_lag_seconds?: number | null
  consecutive_tick_failures?: number
  last_tick_error?: string | null
  dispatch_blocked_reason?: string | null
  last_reconcile_at?: string | null
  reconcile_repairs?: number
  integration_health?: {
    status?: string | null
    last_check_at?: string | null
    last_check_task_id?: string | null
    merge_count_since_check?: number
    failure_summary?: string | null
    fix_task_id?: string | null
  } | null
}

type AgentRecord = {
  id: string
  role: string
  status: string
  capacity: number
  override_provider?: string | null
}

type WorkerHealthRecord = {
  name: string
  type: string
  configured: boolean
  healthy: boolean
  status: 'connected' | 'unavailable' | 'not_configured'
  detail: string
  checked_at: string
  command?: string | null
  endpoint?: string | null
  model?: string | null
}

type WorkerRoutingRow = {
  step: string
  provider: string
  provider_type?: string | null
  source: 'default' | 'explicit'
  configured: boolean
}

type ProjectRef = {
  id: string
  path: string
  source: string
  is_git: boolean
}

type PinnedProjectRef = {
  id: string
  path: string
  pinned_at?: string
}

type TaskLogsSnapshot = {
  mode: 'active' | 'last' | 'history' | 'none'
  task_status?: string
  step?: string
  run_id?: string
  stdout: string
  stderr: string
  stdout_offset?: number
  stderr_offset?: number
  stdout_chunk_start?: number
  stderr_chunk_start?: number
  stdout_tail_start?: number
  stderr_tail_start?: number
  started_at?: string | null
  finished_at?: string | null
  log_id?: string
  progress?: Record<string, unknown>
  available_steps?: string[]
  step_execution_counts?: Record<string, number>
  step_latest_run?: Record<string, string>
  step_history?: Array<{ step: string; run_id: string; attempt?: number; started_at?: string | null; finished_at?: string | null }>
  current_step?: string
}

type LogAccumPhase = 'init' | 'backfill' | 'forward'

type LogAccumState = {
  taskId: string
  logId: string
  stdoutOffset: number
  stderrOffset: number
  stdoutBackfillOffset: number
  stderrBackfillOffset: number
  stdoutRaw: string
  stderrRaw: string
  stdoutRendered: string
  stderrRendered: string
  stdoutParsedLines: number
  stderrParsedLines: number
  stdoutStreamEvents: number
  stderrStreamEvents: number
  stdoutHasTextDelta: boolean
  stderrHasTextDelta: boolean
  phase: LogAccumPhase
  stdoutTailStart: number
  stderrTailStart: number
}

type LogPaneKey = 'stdout' | 'stderr'

type ImportJobRecord = {
  id: string
  project_id?: string
  title?: string
  status?: string
  created_at?: string
  created_task_ids?: string[]
  tasks?: Array<{ title?: string; priority?: string }>
}

type BrowseDirectoryEntry = {
  name: string
  path: string
  is_git: boolean
}

type BrowseProjectsResponse = {
  path: string
  parent: string | null
  current_is_git: boolean
  directories: BrowseDirectoryEntry[]
  truncated: boolean
}

type WorkerProviderSettings = {
  type: 'codex' | 'ollama' | 'claude'
  command?: string
  reasoning_effort?: 'low' | 'medium' | 'high'
  execution_mode?: 'sandboxed' | 'host_access'
  endpoint?: string
  model?: string
  temperature?: number
  num_ctx?: number
}

type LanguageCommandSettings = Record<string, string>

type SystemSettings = {
  orchestrator: {
    concurrency: number
    auto_deps: boolean
    auto_fix_push: boolean
    max_review_attempts: number
    max_merge_conflict_attempts: number
    step_timeout_seconds: number
  }
  agent_routing: {
    default_role: string
    task_type_roles: Record<string, string>
    role_provider_overrides: Record<string, string>
  }
  defaults: {
    quality_gate: {
      critical: number
      high: number
      medium: number
      low: number
    }
    dependency_policy: string
    hitl_mode: 'autopilot' | 'supervised' | 'review_only'
    task_generation: {
      child_status: GeneratedTaskStatus
      child_hitl_mode: GeneratedTaskHitlSelection
      infer_deps: boolean
    }
  }
  workers: {
    default: string
    default_model: string
    routing: Record<string, string>
    providers: Record<string, WorkerProviderSettings>
  }
  project: {
    commands: Record<string, LanguageCommandSettings>
    prompt_overrides: Record<string, string>
    prompt_injections: Record<string, string>
    prompt_defaults: Record<string, string>
  }
}

type MetricsSnapshot = {
  tokens_used: number
  estimated_cost_usd: number
  cost_available: boolean
  worker_time_seconds: number
  tasks_completed: number
  queue_depth: number
  in_progress: number
}

type RootSnapshot = {
  project_id?: string
}


type CollaborationTimelineEvent = {
  id: string
  type: string
  timestamp: string
  actor: string
  actor_type: string
  summary: string
  details: string
  human_blocking_issues?: HumanBlockingIssue[]
}

type HumanBlockingIssue = {
  summary: string
  details?: string
  category?: string
  action?: string
  blocking_on?: string
  severity?: string
}

type PlanRevisionRecord = {
  id: string
  task_id: string
  created_at: string
  source: 'worker_plan' | 'worker_refine' | 'human_edit' | 'import'
  parent_revision_id?: string | null
  step?: string | null
  feedback_note?: string | null
  provider?: string | null
  model?: string | null
  content: string
  content_hash: string
  status: 'draft' | 'committed'
}

type PlanRefineJobRecord = {
  id: string
  task_id: string
  base_revision_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  feedback: string
  instructions?: string | null
  priority?: 'normal' | 'high'
  result_revision_id?: string | null
  error?: string | null
}

type TaskPlanDocument = {
  task_id: string
  latest_revision_id?: string | null
  committed_revision_id?: string | null
  revisions: PlanRevisionRecord[]
  active_refine_job?: PlanRefineJobRecord | null
  plan_mutable?: boolean
}

type TaskWorkdocDocument = {
  task_id: string
  content: string
  exists: boolean
}

const STORAGE_PROJECT = 'overdrive-project'
const STORAGE_ROUTE = 'overdrive-route'
const ADD_REPO_VALUE = '__add_new_repo__'
const WS_RELOAD_CHANNELS = new Set(['tasks', 'queue', 'agents', 'review', 'terminal', 'notifications'])
const LOG_CHUNK_CHARS = 200_000
const LOG_HISTORY_MAX_CHARS = 5_000_000
const LOG_NEAR_BOTTOM_PX = 120

function createEmptyLogAccum(taskId = ''): LogAccumState {
  return {
    taskId,
    logId: '',
    stdoutOffset: 0,
    stderrOffset: 0,
    stdoutBackfillOffset: 0,
    stderrBackfillOffset: 0,
    stdoutRaw: '',
    stderrRaw: '',
    stdoutRendered: '',
    stderrRendered: '',
    stdoutParsedLines: 0,
    stderrParsedLines: 0,
    stdoutStreamEvents: 0,
    stderrStreamEvents: 0,
    stdoutHasTextDelta: false,
    stderrHasTextDelta: false,
    phase: 'init',
    stdoutTailStart: 0,
    stderrTailStart: 0,
  }
}

const ROUTES: Array<{ key: RouteKey; label: string }> = [
  { key: 'board', label: 'Board' },
  { key: 'execution', label: 'Execution' },
  { key: 'settings', label: 'Settings' },
]

const SETTINGS_TABS: Array<{ key: SettingsTab; label: string }> = [
  { key: 'providers', label: 'Providers' },
  { key: 'execution', label: 'Execution' },
  { key: 'advanced', label: 'Advanced' },
]

const TASK_TYPE_OPTIONS = [
  'auto',
  'feature',
  'initiative_plan',
  'bug',
  'refactor',
  'chore',
  'hotfix',
  'research',
  'spike',
  'test',
  'docs',
  'review',
  'commit_review',
  'pr_review',
  'mr_review',
  'security',
  'performance',
  'verify_only',
]

function isPlanningTaskType(taskType: string | undefined | null): boolean {
  return taskType === 'initiative_plan' || taskType === 'plan_only' || taskType === 'plan'
}

function normalizeGeneratedTaskStatus(raw: unknown): GeneratedTaskStatus {
  const value = String(raw || '').trim().toLowerCase()
  return value === 'queued' ? 'queued' : 'backlog'
}

function normalizeGeneratedTaskHitlSelection(raw: unknown): GeneratedTaskHitlSelection {
  const value = String(raw || '').trim().toLowerCase()
  if (value === 'collaborative') return 'supervised'
  if (value === 'autopilot' || value === 'supervised' || value === 'review_only' || value === 'inherit_parent') {
    return value
  }
  return 'inherit_parent'
}

function taskSupportsGenerateTasks(task: TaskRecord | null | undefined): boolean {
  if (!task) return false
  const explicitPipeline = Array.isArray(task.pipeline_template) ? task.pipeline_template.map((step) => String(step || '').trim()) : []
  if (explicitPipeline.length > 0) {
    return explicitPipeline.includes('generate_tasks')
  }
  const taskType = String(task.task_type || '').trim()
  return new Set(['initiative_plan', 'plan_only', 'plan', 'decompose', 'repo_review', 'security', 'security_audit']).has(taskType)
}

const POST_COMPLETION_GENERATION_PIPELINES = new Set(['research', 'review', 'spike', 'verify_only'])

function taskSupportsPostCompletionGeneration(task: TaskRecord | null | undefined): boolean {
  if (!task) return false
  if (task.status !== 'done') return false
  if ((task.children_ids?.length ?? 0) > 0) return false
  const taskType = String(task.task_type || '').trim()
  return POST_COMPLETION_GENERATION_PIPELINES.has(taskType)
}

function resolveTaskGenerationDefaults(
  task: TaskRecord | null | undefined,
  systemDefaults?: Partial<SystemSettings['defaults']['task_generation']> | null,
): {
  child_status: GeneratedTaskStatus
  child_hitl_mode: GeneratedTaskHitlSelection
  infer_deps: boolean
} {
  const metadata = task?.metadata && typeof task.metadata === 'object' ? task.metadata : {}
  const raw = metadata && typeof metadata === 'object'
    ? (metadata as Record<string, unknown>).task_generation_defaults
    : null
  const defaults = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
  const system = systemDefaults && typeof systemDefaults === 'object'
    ? systemDefaults
    : DEFAULT_SETTINGS.defaults.task_generation
  const inferRaw = defaults.infer_deps
  const systemInferRaw = system.infer_deps
  const inferDeps = typeof inferRaw === 'boolean'
    ? inferRaw
    : (
      typeof inferRaw === 'string'
        ? ['true', '1', 'yes', 'on'].includes(inferRaw.trim().toLowerCase())
        : (
          typeof systemInferRaw === 'boolean'
            ? systemInferRaw
            : true
        )
    )
  return {
    child_status: normalizeGeneratedTaskStatus(
      defaults.child_status ?? system.child_status ?? DEFAULT_SETTINGS.defaults.task_generation.child_status,
    ),
    child_hitl_mode: normalizeGeneratedTaskHitlSelection(
      defaults.child_hitl_mode ?? system.child_hitl_mode ?? DEFAULT_SETTINGS.defaults.task_generation.child_hitl_mode,
    ),
    infer_deps: inferDeps,
  }
}

const DEFAULT_SETTINGS: SystemSettings = {
  orchestrator: {
    concurrency: 2,
    auto_deps: true,
    auto_fix_push: false,
    max_review_attempts: 10,
    max_merge_conflict_attempts: 3,
    step_timeout_seconds: 0,
  },
  agent_routing: {
    default_role: 'general',
    task_type_roles: {},
    role_provider_overrides: {},
  },
  defaults: {
    quality_gate: {
      critical: 0,
      high: 0,
      medium: 0,
      low: 0,
    },
    dependency_policy: 'prudent',
    hitl_mode: 'supervised',
    task_generation: {
      child_status: 'backlog',
      child_hitl_mode: 'inherit_parent',
      infer_deps: true,
    },
  },
  workers: {
    default: 'codex',
    default_model: '',
    routing: {},
    providers: {
      codex: { type: 'codex', command: 'codex exec', execution_mode: 'sandboxed' },
    },
  },
  project: {
    commands: {},
    prompt_overrides: {},
    prompt_injections: {},
    prompt_defaults: {},
  },
}

const TASK_TYPE_ROLE_MAP_EXAMPLE = `{
  "bug": "debugger",
  "docs": "researcher"
}`

const ROLE_PROVIDER_OVERRIDES_EXAMPLE = `{
  "reviewer": "codex"
}`

const WORKER_ROUTING_EXAMPLE = `{
  "plan": "codex",
  "implement": "ollama",
  "review": "codex"
}`

const WORKER_PROVIDERS_EXAMPLE = `{
  "review-fastlane": {
    "type": "claude",
    "command": "claude -p",
    "model": "sonnet",
    "reasoning_effort": "high"
  },
  "local-lab-8b": {
    "type": "ollama",
    "endpoint": "http://localhost:11434",
    "model": "llama3.1:8b"
  }
}`

const PROJECT_COMMANDS_EXAMPLE = `python:
  test: .venv/bin/pytest -n auto
  lint: .venv/bin/ruff check .
typescript:
  test: npm test
  lint: npm run lint`

function RenderedMarkdown({ content, className, style }: { content: string; className?: string; style?: CSSProperties }): JSX.Element {
  return (
    <div className={`rendered-markdown ${className || ''}`} style={style}>
      <Markdown remarkPlugins={[remarkGfm]}>{content}</Markdown>
    </div>
  )
}

function statusPillClass(status: string): string {
  switch (status) {
    case 'in_progress': return 'status-running'
    case 'in_review': return 'status-review'
    case 'done': return 'status-done'
    case 'blocked': return 'status-blocked'
    case 'cancelled': return 'status-failed'
    default: return 'status-paused'
  }
}

const GATE_DISPLAY_MAP: Record<string, string> = {
  before_implement: 'Plan ready',
  before_generate_tasks: 'Generate tasks',
  before_done: 'Work complete',
  before_commit: 'Review implementation',
  human_intervention: 'Needs intervention',
}

function normalizeHitlMode(raw: string | null | undefined): 'autopilot' | 'supervised' | 'review_only' {
  const value = String(raw || '').trim().toLowerCase()
  if (value === 'collaborative') return 'supervised'
  if (value === 'autopilot' || value === 'supervised' || value === 'review_only') return value
  return 'supervised'
}

function gateDisplayLabel(task: TaskRecord | null | undefined): string {
  const fromContext = String(task?.gate_context?.display || '').trim()
  if (fromContext) return fromContext
  const gate = String(task?.pending_gate || '').trim()
  if (!gate) return 'approval gate'
  return GATE_DISPLAY_MAP[gate] || humanizeLabel(gate)
}

function gateApprovalButtonLabel(gate: string | null | undefined): string {
  const normalized = String(gate || '').trim()
  if (normalized === 'before_implement') return 'Approve plan'
  if (normalized === 'before_generate_tasks') return 'Generate tasks'
  if (normalized === 'before_done') return 'Move to Done'
  if (normalized === 'before_commit') return 'Approve'
  if (normalized === 'human_intervention') return 'Acknowledge'
  if (normalized === 'before_post_review') return 'Post review'
  return 'Approve gate'
}

type WaitingGateKind = 'approval_wait' | 'intervention_wait'

function taskWaitingGateKind(task: TaskRecord | null | undefined): WaitingGateKind | null {
  const waitKind = String(task?.wait_state?.kind || '').trim()
  if (waitKind === 'intervention_wait') return 'intervention_wait'
  if (waitKind === 'approval_wait') return 'approval_wait'
  if (waitKind === 'auto_recovery_wait') return null
  const kind = String(task?.gate_context?.status_kind || '').trim()
  if (kind === 'approval_wait' || kind === 'intervention_wait') return kind
  const gate = String(task?.pending_gate || '').trim()
  if (!gate) return null
  return gate === 'human_intervention' ? 'intervention_wait' : 'approval_wait'
}

function taskWaitingGateLabel(task: TaskRecord | null | undefined): string | null {
  const kind = taskWaitingGateKind(task)
  if (kind === 'intervention_wait') return 'Needs Intervention'
  if (kind === 'approval_wait') {
    const display = String(task?.gate_context?.display || '').trim()
    return display || 'Awaiting Approval'
  }
  return null
}

function taskStatusDisplay(task: TaskRecord | null | undefined): { label: string; classStatus: string } {
  if (!task) return { label: 'Unknown', classStatus: 'unknown' }
  if (task.status !== 'cancelled' && task.status === 'in_progress' && String(task.pending_gate || '').trim()) {
    const kind = taskWaitingGateKind(task)
    return {
      label: taskWaitingGateLabel(task) || 'Awaiting Approval',
      classStatus: kind === 'intervention_wait' ? 'blocked' : 'in_review',
    }
  }
  return {
    label: humanizeLabel(task.status || 'unknown'),
    classStatus: task.status || 'unknown',
  }
}

function dispatchBlockedReasonLabel(raw: string | null | undefined): string {
  const normalized = String(raw || '').trim().toLowerCase()
  if (!normalized) return ''
  if (normalized === 'paused') return 'Queue paused'
  if (normalized === 'blocked_by_dependencies') return 'Queued tasks blocked by dependencies'
  if (normalized === 'waiting_gate') return 'Queued tasks waiting for approval gates'
  if (normalized === 'concurrency_limit') return 'At concurrency limit'
  if (normalized === 'no_runnable_queued_tasks') return 'No runnable queued tasks'
  if (normalized === 'scheduler_stale') return 'Scheduler stale'
  if (normalized === 'integration_degraded') return 'Paused — post-merge tests failing'
  return humanizeLabel(normalized)
}

function orderLogStepsByPipeline(steps: string[], pipelineTemplate?: string[]): string[] {
  const uniqueSteps: string[] = []
  const seen = new Set<string>()
  for (const raw of steps || []) {
    const step = String(raw || '').trim()
    if (!step || seen.has(step)) continue
    seen.add(step)
    uniqueSteps.push(step)
  }
  if (!Array.isArray(pipelineTemplate) || pipelineTemplate.length === 0) return uniqueSteps

  const templateOrder = new Map<string, number>()
  for (const [index, raw] of pipelineTemplate.entries()) {
    const step = String(raw || '').trim()
    if (!step || templateOrder.has(step)) continue
    templateOrder.set(step, index)
  }
  const originalIndex = new Map<string, number>()
  uniqueSteps.forEach((step, index) => originalIndex.set(step, index))

  return [...uniqueSteps].sort((a, b) => {
    const aOrder = templateOrder.has(a) ? templateOrder.get(a)! : Number.MAX_SAFE_INTEGER
    const bOrder = templateOrder.has(b) ? templateOrder.get(b)! : Number.MAX_SAFE_INTEGER
    if (aOrder !== bOrder) return aOrder - bOrder
    return (originalIndex.get(a) ?? 0) - (originalIndex.get(b) ?? 0)
  })
}

function orderLogAttempts(
  attempts: Array<{ step: string; run_id: string; attempt?: number; started_at?: string | null; finished_at?: string | null }>,
): Array<{ step: string; run_id: string; attempt?: number; started_at?: string | null; finished_at?: string | null }> {
  return [...attempts].sort((a, b) => {
    const aAttempt = Number(a.attempt || 0)
    const bAttempt = Number(b.attempt || 0)
    if (aAttempt > 0 && bAttempt > 0 && aAttempt !== bAttempt) return aAttempt - bAttempt
    const aTs = String(a.started_at || '')
    const bTs = String(b.started_at || '')
    if (aTs && bTs && aTs !== bTs) return aTs.localeCompare(bTs)
    return String(a.run_id || '').localeCompare(String(b.run_id || ''))
  })
}

function routeFromHash(hash: string): RouteKey {
  const cleaned = hash.replace(/^#\/?/, '').trim().toLowerCase()
  const found = ROUTES.find((route) => route.key === cleaned)
  return found?.key ?? 'board'
}

function toHash(route: RouteKey): string {
  return `#/${route}`
}

type ApiRequestError = Error & {
  status: number
  statusText: string
  url: string
  code?: string
  data?: unknown
  payload?: unknown
}

function _extractApiErrorFields(payload: unknown): { detail: string; code?: string; data?: unknown } {
  if (!payload || typeof payload !== 'object') return { detail: '' }
  const top = payload as Record<string, unknown>
  let detail = ''
  let code: string | undefined
  let data: unknown

  if (typeof top.code === 'string' && top.code.trim()) code = top.code.trim()
  if (top.data !== undefined) data = top.data

  const rawDetail = top.detail
  if (typeof rawDetail === 'string') {
    detail = rawDetail
    return { detail, code, data }
  }
  if (rawDetail && typeof rawDetail === 'object') {
    const nested = rawDetail as Record<string, unknown>
    if (typeof nested.detail === 'string' && nested.detail.trim()) {
      detail = nested.detail
    } else if (typeof nested.message === 'string' && nested.message.trim()) {
      detail = nested.message
    } else if (typeof nested.error === 'string' && nested.error.trim()) {
      detail = nested.error
    }
    if (!code && typeof nested.code === 'string' && nested.code.trim()) code = nested.code.trim()
    if (data === undefined && nested.data !== undefined) data = nested.data
    return { detail, code, data }
  }
  if (typeof top.message === 'string' && top.message.trim()) detail = top.message
  return { detail, code, data }
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: buildAuthHeaders(init?.headers || {}),
  })
  if (!response.ok) {
    let payload: unknown = null
    try {
      payload = await response.json()
    } catch {
      // ignore parse failures for non-json bodies
    }
    const parsed = _extractApiErrorFields(payload)
    const displayMessage = parsed.detail
      ? parsed.detail
      : `${response.status} ${response.statusText}`
    const error = new Error(displayMessage) as ApiRequestError
    error.status = response.status
    error.statusText = response.statusText
    error.url = url
    if (parsed.code) error.code = parsed.code
    if (parsed.data !== undefined) error.data = parsed.data
    if (payload !== null) error.payload = payload
    throw error
  }
  return response.json() as Promise<T>
}

function parseStringMap(input: string, label: string): Record<string, string> {
  if (!input.trim()) return {}
  let parsed: unknown
  try {
    parsed = JSON.parse(input)
  } catch {
    throw new Error(`${label} must be valid JSON`)
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object`)
  }
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
    const normalizedKey = String(key || '').trim()
    const normalizedValue = String(value || '').trim()
    if (normalizedKey && normalizedValue) {
      out[normalizedKey] = normalizedValue
    }
  }
  return out
}

function parseProjectCommandsYaml(input: string): Record<string, LanguageCommandSettings> {
  const out: Record<string, LanguageCommandSettings> = {}
  let currentLang = ''
  for (const rawLine of input.split('\n')) {
    const line = rawLine.trimEnd()
    if (!line || line.trim().startsWith('#')) continue
    if (!/^\s/.test(line) && line.trimEnd().endsWith(':')) {
      // Top-level language key (no leading whitespace, ends with colon)
      currentLang = line.slice(0, -1).trim().toLowerCase()
      if (!currentLang) throw new Error('Empty language key')
      if (!out[currentLang]) out[currentLang] = {}
    } else if (/^\s/.test(line) && currentLang) {
      // Indented command entry: split on first colon
      const trimmed = line.trim()
      const colonIdx = trimmed.indexOf(':')
      if (colonIdx <= 0) throw new Error(`Invalid command line: "${trimmed}"`)
      const key = trimmed.slice(0, colonIdx).trim()
      const value = trimmed.slice(colonIdx + 1).trim()
      if (!key) throw new Error(`Empty command key under "${currentLang}"`)
      if (!value) throw new Error(`Empty command value for "${currentLang}.${key}"`)
      out[currentLang][key] = value
    } else {
      throw new Error(`Unexpected line: "${line.trim()}"`)
    }
  }
  // Remove empty language groups
  for (const lang of Object.keys(out)) {
    if (Object.keys(out[lang]).length === 0) delete out[lang]
  }
  return out
}

function parseProjectCommands(input: string): Record<string, LanguageCommandSettings> {
  const trimmed = input.trim()
  if (!trimmed) return {}

  // If it looks like JSON, parse as JSON (backward compatible)
  if (trimmed.startsWith('{')) {
    let parsed: unknown
    try {
      parsed = JSON.parse(trimmed)
    } catch {
      throw new Error('Project commands must be valid JSON')
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('Project commands must be a JSON object')
    }
    const out: Record<string, LanguageCommandSettings> = {}
    for (const [rawLanguage, rawCommands] of Object.entries(parsed as Record<string, unknown>)) {
      const language = String(rawLanguage || '').trim().toLowerCase()
      if (!language) continue
      if (!rawCommands || typeof rawCommands !== 'object' || Array.isArray(rawCommands)) {
        throw new Error(`Project commands for "${language}" must be a JSON object`)
      }
      const commands: LanguageCommandSettings = {}
      for (const [rawField, rawValue] of Object.entries(rawCommands as Record<string, unknown>)) {
        const field = String(rawField || '').trim()
        if (!field) continue
        if (typeof rawValue !== 'string') {
          throw new Error(`Project command "${language}.${field}" must be a string`)
        }
        commands[field] = rawValue
      }
      if (Object.keys(commands).length > 0) {
        out[language] = commands
      }
    }
    return out
  }

  // Otherwise parse as YAML-like format
  return parseProjectCommandsYaml(trimmed)
}

function serializeProjectCommandsYaml(commands: Record<string, LanguageCommandSettings>): string {
  const lines: string[] = []
  for (const [lang, cmds] of Object.entries(commands)) {
    lines.push(`${lang}:`)
    for (const [key, value] of Object.entries(cmds)) {
      lines.push(`  ${key}: ${value}`)
    }
  }
  return lines.join('\n')
}

function formatProjectCommandsField(input: string): string {
  const trimmed = input.trim()
  if (!trimmed) return ''
  const parsed = parseProjectCommands(trimmed)
  if (Object.keys(parsed).length === 0) return ''
  return serializeProjectCommandsYaml(parsed)
}

function normalizePromptMap(value: unknown): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return {}
  }
  const out: Record<string, string> = {}
  for (const [rawStep, rawPrompt] of Object.entries(value as Record<string, unknown>)) {
    const step = String(rawStep || '').trim().toLowerCase()
    if (!step || typeof rawPrompt !== 'string' || !rawPrompt.trim()) continue
    out[step] = rawPrompt
  }
  return out
}

function formatJsonObjectInput(input: string, label: string): string {
  const trimmed = input.trim()
  if (!trimmed) return ''
  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch {
    throw new Error(`${label} must be valid JSON`)
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object`)
  }
  return JSON.stringify(parsed, null, 2)
}

function parseWorkerProviders(input: string): Record<string, WorkerProviderSettings> {
  if (!input.trim()) {
    return {}
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(input)
  } catch {
    throw new Error('Worker providers must be valid JSON')
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Worker providers must be a JSON object')
  }
  const out: Record<string, WorkerProviderSettings> = {}
  for (const [rawName, rawValue] of Object.entries(parsed as Record<string, unknown>)) {
    const name = String(rawName || '').trim()
    if (!name) continue
    if (!rawValue || typeof rawValue !== 'object' || Array.isArray(rawValue)) {
      throw new Error(`Worker provider "${name}" must be a JSON object`)
    }
    const record = rawValue as Record<string, unknown>
    let type = String(record.type || (name === 'codex' ? 'codex' : name === 'claude' ? 'claude' : '')).trim().toLowerCase()
    if (type === 'local') type = 'ollama'
    if (type !== 'codex' && type !== 'ollama' && type !== 'claude') {
      throw new Error(`Worker provider "${name}" has invalid type "${type}" (allowed: codex, ollama, claude)`)
    }
    if (type === 'codex' || type === 'claude') {
      const defaultCommand = type === 'codex' ? 'codex exec' : 'claude -p'
      const command = String(record.command || defaultCommand).trim() || defaultCommand
      const provider: WorkerProviderSettings = { type, command }
      const executionMode = String(record.execution_mode || '').trim().toLowerCase()
      if (executionMode === 'sandboxed' || executionMode === 'host_access') {
        provider.execution_mode = executionMode
      } else {
        provider.execution_mode = type === 'claude' ? 'host_access' : 'sandboxed'
      }
      const model = String(record.model || '').trim()
      if (model) provider.model = model
      const reasoningEffort = String(record.reasoning_effort || '').trim().toLowerCase()
      if (reasoningEffort === 'low' || reasoningEffort === 'medium' || reasoningEffort === 'high') {
        provider.reasoning_effort = reasoningEffort
      }
      out[name] = provider
      continue
    }
    const provider: WorkerProviderSettings = { type: 'ollama' }
    const endpoint = String(record.endpoint || '').trim()
    const model = String(record.model || '').trim()
    if (endpoint) provider.endpoint = endpoint
    if (model) provider.model = model
    const maybeTemperature = Number(record.temperature)
    if (Number.isFinite(maybeTemperature)) {
      provider.temperature = maybeTemperature
    }
    const maybeNumCtx = Number(record.num_ctx)
    if (Number.isFinite(maybeNumCtx) && maybeNumCtx > 0) {
      provider.num_ctx = Math.floor(maybeNumCtx)
    }
    out[name] = provider
  }
  return out
}

function parseNonNegativeInt(input: string, fallback: number): number {
  const parsed = Number(input)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(0, Math.floor(parsed))
}

function parseOptionalPositiveInt(input: string): number | null {
  const trimmed = input.trim()
  if (!trimmed) return null
  const parsed = Number(trimmed)
  if (!Number.isFinite(parsed)) return null
  const normalized = Math.floor(parsed)
  return normalized > 0 ? normalized : null
}

function normalizeWorkers(payload: Partial<SystemSettings['workers']> | null | undefined): SystemSettings['workers'] {
  const defaultWorker = String(payload?.default || DEFAULT_SETTINGS.workers.default).trim() || 'codex'
  const defaultModel = String(payload?.default_model || '').trim()
  const routingRaw = payload?.routing && typeof payload.routing === 'object' ? payload.routing : {}
  const providersRaw = payload?.providers && typeof payload.providers === 'object' ? payload.providers : {}
  const routing: Record<string, string> = {}
  for (const [rawKey, rawValue] of Object.entries(routingRaw)) {
    const key = String(rawKey || '').trim()
    const value = String(rawValue || '').trim()
    if (key && value) {
      routing[key] = value
    }
  }

  // Backend guarantees normalized provider output — pass through as-is
  // with only structural validation.
  const providers: Record<string, WorkerProviderSettings> = {}
  for (const [rawName, rawValue] of Object.entries(providersRaw)) {
    const name = String(rawName || '').trim()
    if (!name || !rawValue || typeof rawValue !== 'object') continue
    providers[name] = rawValue as WorkerProviderSettings
  }
  if (!providers.codex || providers.codex.type !== 'codex') {
    providers.codex = { type: 'codex', command: 'codex exec', execution_mode: 'sandboxed' }
  }
  const effectiveDefault = providers[defaultWorker] ? defaultWorker : 'codex'
  return {
    default: effectiveDefault,
    default_model: defaultModel,
    routing,
    providers,
  }
}

function normalizeSettings(payload: Partial<SystemSettings> | null | undefined): SystemSettings {
  const orchestrator: Partial<SystemSettings['orchestrator']> = payload?.orchestrator || {}
  const routing: Partial<SystemSettings['agent_routing']> = payload?.agent_routing || {}
  const defaults: Partial<SystemSettings['defaults']> = payload?.defaults || {}
  const qualityGate: Partial<SystemSettings['defaults']['quality_gate']> = defaults.quality_gate || {}
  const taskGenerationDefaults: Partial<SystemSettings['defaults']['task_generation']> = defaults.task_generation || {}
  const workers = normalizeWorkers(payload?.workers)
  const projectCommandsRaw = payload?.project?.commands
  const projectPromptOverrides = normalizePromptMap(payload?.project?.prompt_overrides)
  const projectPromptInjections = normalizePromptMap(payload?.project?.prompt_injections)
  const projectPromptDefaults = normalizePromptMap(payload?.project?.prompt_defaults)
  const projectCommands: Record<string, LanguageCommandSettings> = {}
  if (projectCommandsRaw && typeof projectCommandsRaw === 'object') {
    for (const [rawLanguage, rawCommands] of Object.entries(projectCommandsRaw)) {
      const language = String(rawLanguage || '').trim().toLowerCase()
      if (!language || !rawCommands || typeof rawCommands !== 'object' || Array.isArray(rawCommands)) continue
      const commands: LanguageCommandSettings = {}
      for (const [rawField, rawValue] of Object.entries(rawCommands as Record<string, unknown>)) {
        const field = String(rawField || '').trim()
        if (!field || typeof rawValue !== 'string') continue
        commands[field] = rawValue
      }
      if (Object.keys(commands).length > 0) {
        projectCommands[language] = commands
      }
    }
  }

  const maybeConcurrency = Number(orchestrator.concurrency)
  const maybeMaxReviewAttempts = Number(orchestrator.max_review_attempts)
  const maybeMaxMergeConflictAttempts = Number(orchestrator.max_merge_conflict_attempts)
  const maybeStepTimeoutSeconds = Number(orchestrator.step_timeout_seconds)
  const maybeCritical = Number(qualityGate.critical)
  const maybeHigh = Number(qualityGate.high)
  const maybeMedium = Number(qualityGate.medium)
  const maybeLow = Number(qualityGate.low)

  return {
    orchestrator: {
      concurrency: Number.isFinite(maybeConcurrency) ? Math.max(1, Math.floor(maybeConcurrency)) : DEFAULT_SETTINGS.orchestrator.concurrency,
      auto_deps: typeof orchestrator.auto_deps === 'boolean' ? orchestrator.auto_deps : DEFAULT_SETTINGS.orchestrator.auto_deps,
      auto_fix_push: typeof orchestrator.auto_fix_push === 'boolean' ? orchestrator.auto_fix_push : DEFAULT_SETTINGS.orchestrator.auto_fix_push,
      max_review_attempts: Number.isFinite(maybeMaxReviewAttempts) ? Math.max(1, Math.floor(maybeMaxReviewAttempts)) : DEFAULT_SETTINGS.orchestrator.max_review_attempts,
      max_merge_conflict_attempts: Number.isFinite(maybeMaxMergeConflictAttempts)
        ? Math.min(10, Math.max(1, Math.floor(maybeMaxMergeConflictAttempts)))
        : DEFAULT_SETTINGS.orchestrator.max_merge_conflict_attempts,
      step_timeout_seconds: Number.isFinite(maybeStepTimeoutSeconds)
        ? Math.min(7200, Math.max(0, Math.floor(maybeStepTimeoutSeconds)))
        : DEFAULT_SETTINGS.orchestrator.step_timeout_seconds,
    },
    agent_routing: {
      default_role: String(routing.default_role || DEFAULT_SETTINGS.agent_routing.default_role),
      task_type_roles: routing.task_type_roles && typeof routing.task_type_roles === 'object' ? routing.task_type_roles : {},
      role_provider_overrides: routing.role_provider_overrides && typeof routing.role_provider_overrides === 'object' ? routing.role_provider_overrides : {},
    },
    defaults: {
      quality_gate: {
        critical: Number.isFinite(maybeCritical) ? Math.max(0, Math.floor(maybeCritical)) : DEFAULT_SETTINGS.defaults.quality_gate.critical,
        high: Number.isFinite(maybeHigh) ? Math.max(0, Math.floor(maybeHigh)) : DEFAULT_SETTINGS.defaults.quality_gate.high,
        medium: Number.isFinite(maybeMedium) ? Math.max(0, Math.floor(maybeMedium)) : DEFAULT_SETTINGS.defaults.quality_gate.medium,
        low: Number.isFinite(maybeLow) ? Math.max(0, Math.floor(maybeLow)) : DEFAULT_SETTINGS.defaults.quality_gate.low,
      },
      dependency_policy: ['permissive', 'prudent', 'strict'].includes(String(defaults.dependency_policy || '')) ? String(defaults.dependency_policy) : DEFAULT_SETTINGS.defaults.dependency_policy,
      hitl_mode: normalizeHitlMode(String(defaults.hitl_mode || DEFAULT_SETTINGS.defaults.hitl_mode)),
      task_generation: {
        child_status: normalizeGeneratedTaskStatus(taskGenerationDefaults.child_status || DEFAULT_SETTINGS.defaults.task_generation.child_status),
        child_hitl_mode: normalizeGeneratedTaskHitlSelection(taskGenerationDefaults.child_hitl_mode || DEFAULT_SETTINGS.defaults.task_generation.child_hitl_mode),
        infer_deps: typeof taskGenerationDefaults.infer_deps === 'boolean'
          ? taskGenerationDefaults.infer_deps
          : DEFAULT_SETTINGS.defaults.task_generation.infer_deps,
      },
    },
    workers,
    project: {
      commands: projectCommands,
      prompt_overrides: projectPromptOverrides,
      prompt_injections: projectPromptInjections,
      prompt_defaults: projectPromptDefaults,
    },
  }
}

function describeTask(taskId: string, taskIndex: Map<string, TaskRecord>): { label: string; status: string } {
  const task = taskIndex.get(taskId)
  if (!task) {
    return { label: taskId, status: 'unknown' }
  }
  const title = task.title?.trim() || taskId
  return {
    label: `${title} (${task.id})`,
    status: task.status || 'unknown',
  }
}

function normalizeTaskRecord(task: TaskRecord): TaskRecord {
  return {
    ...task,
    hitl_mode: normalizeHitlMode(task.hitl_mode),
  }
}

function normalizeBoard(board: BoardResponse): BoardResponse {
  const columns: Record<string, TaskRecord[]> = {}
  for (const [key, tasks] of Object.entries(board?.columns || {})) {
    columns[key] = (tasks || []).map((task) => normalizeTaskRecord(task))
  }
  return { columns }
}

function normalizeHumanBlockingIssues(value: unknown): HumanBlockingIssue[] {
  if (!Array.isArray(value)) return []
  return value
    .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item))
    .map((item) => {
      const summary = String(item.summary || item.issue || '').trim()
      const details = String(item.details || item.rationale || '').trim()
      const category = String(item.category || '').trim()
      const action = String(item.action || '').trim()
      const blockingOn = String(item.blocking_on || '').trim()
      const severity = String(item.severity || '').trim()
      return {
        summary: summary || (details ? details.split('\n')[0] : ''),
        details: details || undefined,
        category: category || undefined,
        action: action || undefined,
        blocking_on: blockingOn || undefined,
        severity: severity || undefined,
      }
    })
    .filter((item) => !!item.summary)
}

function normalizeMetrics(payload: unknown): MetricsSnapshot | null {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return null
  }
  const raw = payload as Record<string, unknown>
  const toNumber = (value: unknown, fallback = 0): number => {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : fallback
  }
  return {
    tokens_used: toNumber(raw.tokens_used),
    estimated_cost_usd: toNumber(raw.estimated_cost_usd),
    cost_available: raw.cost_available === true,
    worker_time_seconds: toNumber(raw.worker_time_seconds),
    tasks_completed: toNumber(raw.tasks_completed),
    queue_depth: toNumber(raw.queue_depth),
    in_progress: toNumber(raw.in_progress),
  }
}

function normalizeWorkerHealth(payload: unknown): WorkerHealthRecord[] {
  const itemsRaw = payload && typeof payload === 'object' && !Array.isArray(payload) && Array.isArray((payload as { providers?: unknown[] }).providers)
    ? (payload as { providers: unknown[] }).providers
    : []
  return itemsRaw
    .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item))
    .map((item) => {
      const rawStatus = String(item.status || '').trim().toLowerCase()
      const status: WorkerHealthRecord['status'] =
        rawStatus === 'connected' || rawStatus === 'unavailable' || rawStatus === 'not_configured'
          ? rawStatus
          : 'unavailable'
      return {
        name: String(item.name || '').trim(),
        type: String(item.type || '').trim(),
        configured: Boolean(item.configured),
        healthy: Boolean(item.healthy),
        status,
        detail: String(item.detail || '').trim(),
        checked_at: String(item.checked_at || '').trim(),
        command: item.command ? String(item.command) : null,
        endpoint: item.endpoint ? String(item.endpoint) : null,
        model: item.model ? String(item.model) : null,
      }
    })
    .filter((item) => !!item.name)
}

function normalizeWorkerRouting(payload: unknown): { defaultProvider: string; rows: WorkerRoutingRow[] } {
  const root = payload && typeof payload === 'object' && !Array.isArray(payload)
    ? payload as { default?: unknown; rows?: unknown[] }
    : {}
  const rowsRaw = Array.isArray(root.rows) ? root.rows : []
  const rows = rowsRaw
    .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item))
    .map((item) => {
      const source: WorkerRoutingRow['source'] =
        String(item.source || '').trim().toLowerCase() === 'explicit' ? 'explicit' : 'default'
      return {
        step: String(item.step || '').trim(),
        provider: String(item.provider || '').trim(),
        provider_type: item.provider_type ? String(item.provider_type) : null,
        source,
        configured: Boolean(item.configured),
      }
    })
    .filter((item) => !!item.step && !!item.provider)
  return {
    defaultProvider: String(root.default || 'codex').trim() || 'codex',
    rows,
  }
}

function normalizeTimelineEvents(payload: unknown): CollaborationTimelineEvent[] {
  const eventsRaw = payload && typeof payload === 'object' && !Array.isArray(payload) && Array.isArray((payload as { events?: unknown[] }).events)
    ? (payload as { events: unknown[] }).events
    : []
  return eventsRaw
    .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item))
    .map((item) => ({
      id: String(item.id || '').trim(),
      type: String(item.type || 'event').trim(),
      timestamp: String(item.timestamp || '').trim(),
      actor: String(item.actor || 'system').trim(),
      actor_type: String(item.actor_type || 'system').trim(),
      summary: String(item.summary || '').trim(),
      details: String(item.details || '').trim(),
      human_blocking_issues: normalizeHumanBlockingIssues(item.human_blocking_issues),
    }))
    .filter((item) => !!item.id)
}

function normalizePlanRevision(item: unknown): PlanRevisionRecord | null {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return null
  const raw = item as Record<string, unknown>
  const id = String(raw.id || '').trim()
  const taskId = String(raw.task_id || '').trim()
  const content = String(raw.content || '')
  if (!id || !taskId) return null
  const sourceRaw = String(raw.source || 'human_edit').trim()
  const source: PlanRevisionRecord['source'] = (
    sourceRaw === 'worker_plan' || sourceRaw === 'worker_refine' || sourceRaw === 'import'
  ) ? sourceRaw : 'human_edit'
  const statusRaw = String(raw.status || 'draft').trim()
  const status: PlanRevisionRecord['status'] = statusRaw === 'committed' ? 'committed' : 'draft'
  return {
    id,
    task_id: taskId,
    created_at: String(raw.created_at || ''),
    source,
    parent_revision_id: raw.parent_revision_id ? String(raw.parent_revision_id) : null,
    step: raw.step ? String(raw.step) : null,
    feedback_note: raw.feedback_note ? String(raw.feedback_note) : null,
    provider: raw.provider ? String(raw.provider) : null,
    model: raw.model ? String(raw.model) : null,
    content,
    content_hash: String(raw.content_hash || ''),
    status,
  }
}

function normalizePlanRefineJob(item: unknown): PlanRefineJobRecord | null {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return null
  const raw = item as Record<string, unknown>
  const id = String(raw.id || '').trim()
  const taskId = String(raw.task_id || '').trim()
  const baseRevisionId = String(raw.base_revision_id || '').trim()
  if (!id || !taskId || !baseRevisionId) return null
  const statusRaw = String(raw.status || 'queued').trim()
  const status: PlanRefineJobRecord['status'] = (
    statusRaw === 'running' || statusRaw === 'completed' || statusRaw === 'failed' || statusRaw === 'cancelled'
  ) ? statusRaw : 'queued'
  const priorityRaw = String(raw.priority || 'normal').trim()
  const priority: PlanRefineJobRecord['priority'] = priorityRaw === 'high' ? 'high' : 'normal'
  return {
    id,
    task_id: taskId,
    base_revision_id: baseRevisionId,
    status,
    created_at: String(raw.created_at || ''),
    started_at: raw.started_at ? String(raw.started_at) : null,
    finished_at: raw.finished_at ? String(raw.finished_at) : null,
    feedback: String(raw.feedback || ''),
    instructions: raw.instructions ? String(raw.instructions) : null,
    priority,
    result_revision_id: raw.result_revision_id ? String(raw.result_revision_id) : null,
    error: raw.error ? String(raw.error) : null,
  }
}

function normalizeTaskPlan(payload: unknown): TaskPlanDocument {
  const root = payload && typeof payload === 'object' && !Array.isArray(payload)
    ? payload as Record<string, unknown>
    : {}
  const revisionsRaw = Array.isArray(root.revisions) ? root.revisions : []
  const revisions = revisionsRaw.map((item) => normalizePlanRevision(item)).filter((item): item is PlanRevisionRecord => item !== null)
  return {
    task_id: String(root.task_id || ''),
    latest_revision_id: root.latest_revision_id ? String(root.latest_revision_id) : null,
    committed_revision_id: root.committed_revision_id ? String(root.committed_revision_id) : null,
    revisions,
    active_refine_job: normalizePlanRefineJob(root.active_refine_job || null),
    plan_mutable: typeof root.plan_mutable === 'boolean' ? root.plan_mutable : undefined,
  }
}

function normalizePlanRefineJobs(payload: unknown): PlanRefineJobRecord[] {
  const root = payload && typeof payload === 'object' && !Array.isArray(payload)
    ? payload as Record<string, unknown>
    : {}
  const jobsRaw = Array.isArray(root.jobs) ? root.jobs : []
  return jobsRaw
    .map((item) => normalizePlanRefineJob(item))
    .filter((item): item is PlanRefineJobRecord => item !== null)
}

function toLocaleTimestamp(value?: string | null): string {
  if (!value) return ''
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString()
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return ''
  if (seconds < 60) return `${Math.round(seconds)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`
  const h = Math.floor(m / 60)
  const rm = m % 60
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`
}

function taskTotalDurationSeconds(summary: TaskTimingSummary | null | undefined, nowMs: number): number | null {
  if (!summary) return null
  const totalCompleted = typeof summary.total_completed_seconds === 'number' && Number.isFinite(summary.total_completed_seconds)
    ? Math.max(summary.total_completed_seconds, 0)
    : 0
  if (!summary.is_running || !summary.active_run_started_at) {
    return totalCompleted
  }
  const started = new Date(summary.active_run_started_at).getTime()
  if (Number.isNaN(started)) {
    return totalCompleted
  }
  const activeSeconds = Math.max((nowMs - started) / 1000, 0)
  return totalCompleted + activeSeconds
}

function inferProjectId(projectDir: string): string {
  const normalized = projectDir.trim().replace(/[\\/]+$/, '')
  if (!normalized) return ''
  const parts = normalized.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || ''
}

function repoNameFromPath(projectPath: string): string {
  const normalized = projectPath.trim().replace(/[\\/]+$/, '')
  if (!normalized) return ''
  const parts = normalized.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || normalized
}

type StructuredStdoutChunk = {
  text: string
  hasContent: boolean
  structured: boolean
  parsedLines: number
  streamEvents: number
  hasTextDelta: boolean
}

function coerceMessageText(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === 'string') return item
        if (!item || typeof item !== 'object' || Array.isArray(item)) return ''
        const rec = item as Record<string, unknown>
        if (typeof rec.text === 'string') return rec.text
        if (typeof rec.content === 'string') return rec.content
        return ''
      })
      .filter((item) => item.length > 0)
      .join('\n')
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const rec = value as Record<string, unknown>
    if (typeof rec.text === 'string') return rec.text
    if (typeof rec.content === 'string') return rec.content
  }
  return ''
}

function renderStructuredStdoutChunk(raw: string, prevHasTextDelta = false): StructuredStdoutChunk {
  const shouldPreservePlainLine = (trimmed: string): boolean => {
    if (!trimmed) return false
    // Drop likely orphaned JSON fragments introduced by chunk boundaries.
    const looksUuidFragment = /[0-9a-f]{4,}(?:-[0-9a-f]{2,}){2,}/i.test(trimmed) && !/\s/.test(trimmed)
    if (looksUuidFragment) return false
    if (!/[A-Za-z]/.test(trimmed)) return false
    return true
  }
  const input = String(raw || '')
  if (!input.trim()) {
    return { text: '', hasContent: false, structured: false, parsedLines: 0, streamEvents: 0, hasTextDelta: prevHasTextDelta }
  }
  const lines = input.split('\n')
  let parsedLines = 0
  let streamEvents = 0
  let hasTextDelta = prevHasTextDelta
  let lastAssistantText = ''
  const parts: string[] = []
  const pushText = (text: string): void => {
    if (!text) return
    parts.push(text)
  }
  const pushLine = (line: string): void => {
    if (!line.trim()) return
    if (parts.length > 0 && !parts[parts.length - 1].endsWith('\n')) {
      parts.push('\n')
    }
    parts.push(line.trimEnd())
    parts.push('\n')
  }

  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed) continue
    let obj: unknown
    try {
      obj = JSON.parse(trimmed)
    } catch {
      if (shouldPreservePlainLine(trimmed)) {
        // Preserve plain text lines when logs mix prose with JSON events.
        pushLine(line)
      }
      continue
    }
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
      if (shouldPreservePlainLine(trimmed)) {
        pushLine(line)
      }
      continue
    }
    parsedLines += 1
    const record = obj as Record<string, unknown>
    const hasType = typeof record.type === 'string' && String(record.type || '').trim().length > 0
    const type = hasType ? String(record.type || '') : ''
    const emittedToolErrors = new Set<string>()
    const emitToolError = (rawError: string): void => {
      const toolError = rawError.trim()
      if (!toolError || emittedToolErrors.has(toolError)) return
      emittedToolErrors.add(toolError)
      pushLine(`Tool error: ${toolError}`)
    }

    const topLevelToolError = coerceMessageText(record.tool_use_result).trim()
    if (topLevelToolError) {
      emitToolError(topLevelToolError)
    }

    if (type === 'tool_use_result') {
      const toolResultError = coerceMessageText(record.result ?? record.content ?? record.message).trim()
      if (toolResultError) {
        emitToolError(toolResultError)
      }
      continue
    }

    if (type === 'assistant') {
      // Save but don't emit yet — assistant records duplicate text already
      // captured by stream_event text_deltas.  With --include-partial-messages
      // there can be many partial assistant snapshots that each repeat a
      // growing prefix.  We keep only the last one as a fallback for logs
      // that lack stream_events entirely.
      const message = record.message
      if (message && typeof message === 'object' && !Array.isArray(message)) {
        const content = (message as Record<string, unknown>).content
        if (Array.isArray(content)) {
          const texts: string[] = []
          for (const item of content) {
            if (!item || typeof item !== 'object' || Array.isArray(item)) continue
            const ci = item as Record<string, unknown>
            if (String(ci.type || '') !== 'text') continue
            const text = coerceMessageText(ci.text)
            if (text) texts.push(text)
          }
          if (texts.length > 0) lastAssistantText = texts.join('')
        }
      }
      continue
    }
    if (type === 'result') {
      const resultText = coerceMessageText(record.result).trim()
      if (resultText) {
        pushLine(`Result: ${resultText}`)
      }
      continue
    }
    if (type === 'stream_event') {
      streamEvents += 1
      const event = record.event
      if (!event || typeof event !== 'object' || Array.isArray(event)) continue
      const e = event as Record<string, unknown>
      const eventType = String(e.type || '')
      if (eventType === 'content_block_start') {
        const block = e.content_block
        if (block && typeof block === 'object' && !Array.isArray(block)) {
          const b = block as Record<string, unknown>
          const blockType = String(b.type || '')
          if (blockType === 'tool_use') {
            const toolName = String(b.name || '').trim()
            if (toolName) {
              pushLine(`Tools used: ${toolName}`)
            }
          }
          const startText = coerceMessageText(b.text)
          if (startText) pushText(startText)
        }
        continue
      }
      if (eventType === 'content_block_delta') {
        const delta = e.delta
        if (!delta || typeof delta !== 'object' || Array.isArray(delta)) continue
        const d = delta as Record<string, unknown>
        const deltaType = String(d.type || '')
        if (deltaType === 'text_delta') {
          const text = coerceMessageText(d.text)
          if (text) {
            pushText(text)
            hasTextDelta = true
          }
        }
      }
      // Other stream events are intentionally ignored.
      continue
    }
    if (type === 'user') {
      const message = record.message
      if (message && typeof message === 'object' && !Array.isArray(message)) {
        const content = (message as Record<string, unknown>).content
        if (Array.isArray(content)) {
          for (const item of content) {
            if (!item || typeof item !== 'object' || Array.isArray(item)) continue
            const ci = item as Record<string, unknown>
            if (String(ci.type || '') === 'tool_result' && ci.is_error) {
              const errText = coerceMessageText(ci.content).trim()
              if (errText) {
                emitToolError(errText)
              }
            }
          }
        }
      }
      continue
    }
    // Preserve opaque JSON records (for example final verification/review JSON)
    // only when they don't declare a stream event type.
    if (!hasType) {
      pushLine(trimmed)
    }
  }

  if (parsedLines === 0) {
    return { text: input, hasContent: true, structured: false, parsedLines: 0, streamEvents: 0, hasTextDelta }
  }

  // Fallback: if no stream_event text_deltas were seen, use the last
  // assistant message text (covers logs without streaming output).
  if (!hasTextDelta && lastAssistantText) {
    pushText(lastAssistantText)
  }

  const text = parts.join('')
  return {
    text,
    hasContent: text.length > 0,
    structured: true,
    parsedLines,
    streamEvents,
    hasTextDelta,
  }
}

function mergeTranscriptChunk(previous: string, incoming: string, mode: 'reset' | 'prepend' | 'append'): string {
  if (mode === 'reset') return incoming
  if (!incoming) return previous
  if (mode === 'prepend') return `${incoming}${previous}`
  return `${previous}${incoming}`
}

function formatProgressEntries(progress?: Record<string, unknown>): Array<{ key: string; value: string }> {
  if (!progress || typeof progress !== 'object') return []
  const root = progress as Record<string, unknown>
  const orderedKeys = ['status', 'phase', 'step', 'progress', 'percent', 'message', 'summary', 'last_heartbeat', 'run_id']
  const seen = new Set<string>()
  const out: Array<{ key: string; value: string }> = []
  const toValue = (value: unknown): string => {
    if (value == null) return ''
    if (typeof value === 'string') return value
    if (typeof value === 'number' || typeof value === 'boolean') return String(value)
    if (Array.isArray(value)) return value.map((item) => (typeof item === 'string' ? item : JSON.stringify(item))).join(', ')
    try {
      return JSON.stringify(value)
    } catch {
      return String(value)
    }
  }
  for (const key of orderedKeys) {
    if (!(key in root)) continue
    const value = toValue(root[key])
    if (!value.trim()) continue
    out.push({ key, value })
    seen.add(key)
  }
  for (const [key, rawValue] of Object.entries(root)) {
    if (seen.has(key)) continue
    const value = toValue(rawValue)
    if (!value.trim()) continue
    out.push({ key, value })
    if (out.length >= 12) break
  }
  return out
}

export default function App() {
  const [route, setRoute] = useState<RouteKey>(() => routeFromHash(window.location.hash || localStorage.getItem(STORAGE_ROUTE) || '#/board'))
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('providers')
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => (localStorage.getItem('ao-theme') as ThemeMode) || 'system')
  const [projectDir, setProjectDir] = useState<string>(() => localStorage.getItem(STORAGE_PROJECT) || '')
  const [board, setBoard] = useState<BoardResponse>({ columns: {} })
  const [orchestrator, setOrchestrator] = useState<OrchestratorStatus | null>(null)
  const [_agents, setAgents] = useState<AgentRecord[]>([])
  const [workerHealth, setWorkerHealth] = useState<WorkerHealthRecord[]>([])
  const [workerRoutingRows, setWorkerRoutingRows] = useState<WorkerRoutingRow[]>([])
  const [workerDefaultProvider, setWorkerDefaultProvider] = useState('codex')
  const [workerHealthRefreshing, setWorkerHealthRefreshing] = useState(false)
  const [projects, setProjects] = useState<ProjectRef[]>([])
  const [pinnedProjects, setPinnedProjects] = useState<PinnedProjectRef[]>([])
  const [executionBatches, setExecutionBatches] = useState<string[][]>([])
  const [executionCompleted, setExecutionCompleted] = useState<string[]>([])
  const [metrics, setMetrics] = useState<MetricsSnapshot | null>(null)
  const [activeProjectId, setActiveProjectId] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string>('')

  // Git push feature state
  const [gitStatus, setGitStatus] = useState<{
    branch: string
    remote_branch: string | null
    ahead_count: number
    behind_count: number
    commits: { sha: string; message: string }[]
    has_remote: boolean
  } | null>(null)
  const [pushPopoverOpen, setPushPopoverOpen] = useState(false)
  const [pushTargetBranch, setPushTargetBranch] = useState('')
  const [pushInProgress, setPushInProgress] = useState(false)
  const [pushAutoFixRunning, setPushAutoFixRunning] = useState(false)
  const [pushMessage, setPushMessage] = useState('')
  const [pushError, setPushError] = useState('')
  const [pushOutputLines, setPushOutputLines] = useState<string[]>([])
  const [autoNameInProgress, setAutoNameInProgress] = useState(false)

  const [workOpen, setWorkOpen] = useState(false)
  const [createTab, setCreateTab] = useState<CreateTab>('task')
  const [prList, setPrList] = useState<PullRequestItem[]>([])
  const [prListLoading, setPrListLoading] = useState(false)
  const [prListError, setPrListError] = useState('')
  const [prErrorCode, setPrErrorCode] = useState<string | null>(null)
  const [prPlatform, setPrPlatform] = useState<string | null>(null)
  const [postCommentsError, setPostCommentsError] = useState<string | null>(null)
  const [postCommentsErrorCode, setPostCommentsErrorCode] = useState<string | null>(null)
  const [selectedCommentIndices, setSelectedCommentIndices] = useState<Set<number>>(new Set())
  const [editedCommentBodies, setEditedCommentBodies] = useState<Map<number, string>>(new Map())
  const [editingCommentIndex, setEditingCommentIndex] = useState<number | null>(null)
  const [platformStatus, setPlatformStatus] = useState<{ platform: string | null; cli_installed: boolean; cli_authenticated: boolean; cli_name: string | null; setup_steps: { step: number; label: string; done: boolean }[] } | null>(null)
  const [selectedPrNumber, setSelectedPrNumber] = useState<number | null>(null)
  const [prReviewGuidance, setPrReviewGuidance] = useState('')
  const [prReviewMode, setPrReviewMode] = useState<ReviewMode>(ReviewMode.FixOnly)
  const [isSubmittingReview, setIsSubmittingReview] = useState(false)
  const [terminalOpen, setTerminalOpen] = useState(false)
  const [selectedTaskId, setSelectedTaskId] = useState<string>('')
  const modalDismissedRef = useRef(false)
  const modalExplicitRef = useRef(false)
  const taskSelectTabRef = useRef<TaskDetailTab | undefined>(undefined)
  const [selectedTaskDetail, setSelectedTaskDetail] = useState<TaskRecord | null>(null)
  const [selectedTaskDetailLoading, setSelectedTaskDetailLoading] = useState(false)
  const [taskDetailNowMs, setTaskDetailNowMs] = useState<number>(() => Date.now())
  const [selectedTaskPlan, setSelectedTaskPlan] = useState<TaskPlanDocument | null>(null)
  const [selectedTaskPlanJobs, setSelectedTaskPlanJobs] = useState<PlanRefineJobRecord[]>([])
  const [selectedTaskWorkdoc, setSelectedTaskWorkdoc] = useState<TaskWorkdocDocument | null>(null)
  const [selectedTaskWorkdocLoading, setSelectedTaskWorkdocLoading] = useState(false)
  const [selectedTaskWorkdocError, setSelectedTaskWorkdocError] = useState('')
  const [selectedPlanRevisionId, setSelectedPlanRevisionId] = useState('')
  const [planManualContent, setPlanManualContent] = useState('')
  const [planManualFeedbackNote, setPlanManualFeedbackNote] = useState('')
  const [planRefineFeedback, setPlanRefineFeedback] = useState('')
  const [planJobLoading, setPlanJobLoading] = useState(false)
  const [planRefineStdout, setPlanRefineStdout] = useState('')
  const [planRefineUiState, setPlanRefineUiState] = useState<'idle' | 'running' | 'done'>('idle')
  const [planRefineTrackedJobId, setPlanRefineTrackedJobId] = useState('')
  const [planTabMode, setPlanTabMode] = useState<'view' | 'edit' | 'refine'>('view')
  const [planSavingManual, setPlanSavingManual] = useState(false)
  const [planCommitting, setPlanCommitting] = useState(false)
  const [planGenerateChildStatus, setPlanGenerateChildStatus] = useState<GeneratedTaskStatus>('backlog')
  const [planGenerateChildHitlMode, setPlanGenerateChildHitlMode] = useState<GeneratedTaskHitlSelection>('inherit_parent')
  const [planGenerateInferDeps, setPlanGenerateInferDeps] = useState(true)
  const [planGenerateSaveAsDefault, setPlanGenerateSaveAsDefault] = useState(false)
  const [taskGenerationSystemDefaults, setTaskGenerationSystemDefaults] = useState<SystemSettings['defaults']['task_generation']>(
    DEFAULT_SETTINGS.defaults.task_generation,
  )
  const [planActionMessage, setPlanActionMessage] = useState('')
  const [planActionError, setPlanActionError] = useState('')
  const [taskDetailTab, setTaskDetailTab] = useState<TaskDetailTab>('overview')
  const [taskDiff, setTaskDiff] = useState<{
    mode?: 'working_tree' | 'committed' | 'preserved_branch' | 'none'
    reason?: 'task_context_missing' | 'invalid_worktree_context' | 'preserved_branch_missing'
    branch?: string | null
    base_branch?: string | null
    base_ref?: string | null
    base_sha?: string | null
    head_ref?: string | null
    head_sha?: string | null
    base_source?: 'metadata_sha' | 'metadata_branch' | 'review_context_sha' | 'review_context_branch' | 'heuristic' | 'none'
    confidence?: 'high' | 'medium' | 'low'
    warnings?: string[]
    commit: string | null
    files: { path: string; changes: string }[]
    diff: string
    stat: string
  } | null>(null)
  const [taskDiffLoading, setTaskDiffLoading] = useState(false)
  const [boardCompact, setBoardCompact] = useState(false)
  const [expandedSummarySteps, setExpandedSummarySteps] = useState<Set<string>>(new Set())
  const [pipelineHighlightStatus, setPipelineHighlightStatus] = useState('')
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', themeMode)
    localStorage.setItem('ao-theme', themeMode)
  }, [themeMode])

  useEffect(() => {
    if (!pipelineHighlightStatus) return
    const timer = window.setTimeout(() => setPipelineHighlightStatus(''), 2500)
    return () => window.clearTimeout(timer)
  }, [pipelineHighlightStatus])

  useEffect(() => {
    if (!planActionMessage) return
    const timer = window.setTimeout(() => setPlanActionMessage(''), 4000)
    return () => window.clearTimeout(timer)
  }, [planActionMessage])

  useEffect(() => {
    if (!selectedTaskId) return
    const allTasks = [
      ...(board.columns.backlog || []),
      ...(board.columns.queued || []),
      ...(board.columns.in_progress || []),
      ...(board.columns.in_review || []),
      ...(board.columns.blocked || []),
      ...(board.columns.done || []),
      ...(board.columns.cancelled || []),
    ]
    const selected = (selectedTaskDetail && selectedTaskDetail.id === selectedTaskId)
      ? selectedTaskDetail
      : (allTasks.find((task) => task.id === selectedTaskId) || null)
    if (!selected) return
    const defaults = resolveTaskGenerationDefaults(selected, taskGenerationSystemDefaults)
    setPlanGenerateChildStatus(defaults.child_status)
    setPlanGenerateChildHitlMode(defaults.child_hitl_mode)
    setPlanGenerateInferDeps(defaults.infer_deps)
    setPlanGenerateSaveAsDefault(false)
  }, [selectedTaskId, taskGenerationSystemDefaults])

  // Reset comment selection/edit state when task changes or post_status values change
  const commentPostStatuses = useMemo(() => {
    const genComments = selectedTaskDetail?.metadata?.generated_review_comments as Array<{ post_status?: string }> | undefined
    if (!genComments || genComments.length === 0) return ''
    return genComments.map((c) => c.post_status ?? 'staged').join(',')
  }, [selectedTaskDetail])
  useEffect(() => {
    setEditedCommentBodies(new Map())
    setEditingCommentIndex(null)
    if (!selectedTaskId) {
      setSelectedCommentIndices(new Set())
      return
    }
    const allTasks = [
      ...(board.columns.backlog || []),
      ...(board.columns.queued || []),
      ...(board.columns.in_progress || []),
      ...(board.columns.in_review || []),
      ...(board.columns.blocked || []),
      ...(board.columns.done || []),
      ...(board.columns.cancelled || []),
    ]
    const task = (selectedTaskDetail && selectedTaskDetail.id === selectedTaskId)
      ? selectedTaskDetail
      : (allTasks.find((t) => t.id === selectedTaskId) || null)
    const genComments = task?.metadata?.generated_review_comments as Array<{ post_status?: string }> | undefined
    if (!genComments || genComments.length === 0) {
      setSelectedCommentIndices(new Set())
      return
    }
    const staged = new Set<number>()
    genComments.forEach((c, i) => {
      if ((c.post_status ?? 'staged') !== 'posted') staged.add(i)
    })
    setSelectedCommentIndices(staged)
  }, [selectedTaskId, commentPostStatuses])

  // taskEditMode removed — configLocked (status-based) controls editability
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; taskId: string } | null>(null)
  useEffect(() => {
    if (!contextMenu) return
    const dismiss = () => setContextMenu(null)
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') dismiss() }
    document.addEventListener('click', dismiss)
    document.addEventListener('keydown', onKey)
    window.addEventListener('resize', dismiss)
    return () => { document.removeEventListener('click', dismiss); document.removeEventListener('keydown', onKey); window.removeEventListener('resize', dismiss) }
  }, [contextMenu])
  const [taskActionPending, setTaskActionPending] = useState<TaskActionKey | null>(null)
  const [taskActionDetail, setTaskActionDetail] = useState('')
  const [taskActionMessage, setTaskActionMessage] = useState('')
  const [taskActionError, setTaskActionError] = useState('')
  useEffect(() => {
    if (!taskActionMessage) return
    const timer = window.setTimeout(() => setTaskActionMessage(''), 4000)
    return () => window.clearTimeout(timer)
  }, [taskActionMessage])
  const [gateRequestOpen, setGateRequestOpen] = useState(false)
  const [gateRequestGuidance, setGateRequestGuidance] = useState('')
  const [editTaskTitle, setEditTaskTitle] = useState('')
  const [editTaskDescription, setEditTaskDescription] = useState('')
  const [editTaskType, setEditTaskType] = useState('feature')
  const [editTaskPriority, setEditTaskPriority] = useState('P2')
  const [editTaskLabels, setEditTaskLabels] = useState('')
  const [editTaskHitlMode, setEditTaskHitlMode] = useState('supervised')
  const [editTaskDependencyPolicy, setEditTaskDependencyPolicy] = useState<'permissive' | 'prudent' | 'strict'>('prudent')
  const [editTaskStepTimeout, setEditTaskStepTimeout] = useState('')
  const [editTaskProjectCommands, setEditTaskProjectCommands] = useState('')

  const [isSubmittingTask, setIsSubmittingTask] = useState(false)
  const [submittingTaskTarget, setSubmittingTaskTarget] = useState<'queued' | 'backlog' | null>(null)
  const [newTaskTitle, setNewTaskTitle] = useState('')
  const [newTaskDescription, setNewTaskDescription] = useState('')
  const [newTaskType, setNewTaskType] = useState('auto')
  const [gatePipelineSelection, setGatePipelineSelection] = useState('')
  const [gateReviewDecision, setGateReviewDecision] = useState<ReviewDecisionType>(ReviewDecisionType.Comment)
  const [gatePostComments, setGatePostComments] = useState(false)
  const [newTaskPriority, setNewTaskPriority] = useState('P2')
  const [newTaskLabels, setNewTaskLabels] = useState('')
  const [newTaskBlockedBy, setNewTaskBlockedBy] = useState('')
  const [newTaskHitlMode, setNewTaskHitlMode] = useState('supervised')
  const [newTaskDependencyPolicy, setNewTaskDependencyPolicy] = useState<'permissive' | 'prudent' | 'strict'>('prudent')
  const [newTaskParentId, setNewTaskParentId] = useState('')
  const [newTaskPipelineTemplate, setNewTaskPipelineTemplate] = useState('')
  const [newTaskStepTimeout, setNewTaskStepTimeout] = useState('')
  const [newTaskMetadata, setNewTaskMetadata] = useState('')
  const [newTaskProjectCommands, setNewTaskProjectCommands] = useState('')
  const [newTaskWorkerModel, setNewTaskWorkerModel] = useState('')
  const [newTaskWorkerProvider, setNewTaskWorkerProvider] = useState('')
  const [newDependencyId, setNewDependencyId] = useState('')
  const [dependencyActionLoading, setDependencyActionLoading] = useState(false)
  const [dependencyActionMessage, setDependencyActionMessage] = useState('')
  useEffect(() => {
    if (!dependencyActionMessage) return
    const timer = window.setTimeout(() => setDependencyActionMessage(''), 4000)
    return () => window.clearTimeout(timer)
  }, [dependencyActionMessage])
  const [taskExplorerQuery, _setTaskExplorerQuery] = useState('')
  const [taskExplorerStatus, _setTaskExplorerStatus] = useState('')
  const [taskExplorerType, _setTaskExplorerType] = useState('')
  const [taskExplorerPriority, _setTaskExplorerPriority] = useState('')
  const [taskExplorerOnlyBlocked, _setTaskExplorerOnlyBlocked] = useState(false)
  const [_taskExplorerLoading, setTaskExplorerLoading] = useState(false)
  const [_taskExplorerError, setTaskExplorerError] = useState('')
  const [taskExplorerPage, setTaskExplorerPage] = useState(1)
  const [taskExplorerPageSize, _setTaskExplorerPageSize] = useState(6)
  const [taskExplorerItems, setTaskExplorerItems] = useState<TaskRecord[]>([])
  const [collaborationTimeline, setCollaborationTimeline] = useState<CollaborationTimelineEvent[]>([])
  const [collaborationLoading, setCollaborationLoading] = useState(false)
  const [collaborationError, setCollaborationError] = useState('')
  const [selectedTaskLogs, setSelectedTaskLogs] = useState<TaskLogsSnapshot | null>(null)
  const [selectedTaskLogsError, setSelectedTaskLogsError] = useState('')
  const [selectedTaskLogsLoading, setSelectedTaskLogsLoading] = useState(false)
  const planManualSeedRef = useRef<{ taskId: string; workerText: string }>({ taskId: '', workerText: '' })
  const planRefineOutputRef = useRef<{ taskId: string; logId: string; jobKey: string; text: string }>({ taskId: '', logId: '', jobKey: '', text: '' })
  const logAccumRef = useRef<LogAccumState>(createEmptyLogAccum())
  const [stdoutHistory, setStdoutHistory] = useState('')
  const [stderrHistory, setStderrHistory] = useState('')
  const [stdoutRawHistory, setStdoutRawHistory] = useState('')
  const [stderrRawHistory, setStderrRawHistory] = useState('')
  const [stdoutRenderStats, setStdoutRenderStats] = useState<{ parsedLines: number; streamEvents: number; structured: boolean }>({
    parsedLines: 0,
    streamEvents: 0,
    structured: false,
  })
  const stdoutPreRef = useRef<HTMLPreElement>(null)
  const stderrPreRef = useRef<HTMLPreElement>(null)
  const taskLogsRequestSeqRef = useRef(0)
  const logAutoPinRef = useRef<Record<LogPaneKey, boolean>>({ stdout: true, stderr: true })
  const logScrollSnapshotRef = useRef<Record<LogPaneKey, { top: number; height: number; op: 'none' | 'prepend' | 'append' | 'reset' }>>({
    stdout: { top: 0, height: 0, op: 'none' },
    stderr: { top: 0, height: 0, op: 'none' },
  })

  const [importText, setImportText] = useState('')
  const [importJobId, setImportJobId] = useState('')
  const [importPreview, setImportPreview] = useState<PrdPreview | null>(null)
  const [recentImportJobIds, setRecentImportJobIds] = useState<string[]>([])
  const [recentImportCommitMap, setRecentImportCommitMap] = useState<Record<string, string[]>>({})
  const [selectedImportJobId, setSelectedImportJobId] = useState('')
  const [selectedImportJob, setSelectedImportJob] = useState<ImportJobRecord | null>(null)
  const [selectedImportJobLoading, setSelectedImportJobLoading] = useState(false)
  const [selectedImportJobError, setSelectedImportJobError] = useState('')
  const [selectedImportJobErrorAt, setSelectedImportJobErrorAt] = useState('')

  const [reviewGuidance, setReviewGuidance] = useState('')
  const [retryFromStep, setRetryFromStep] = useState('')
  const [retryProvider, setRetryProvider] = useState('')
  const [showAllActivity, setShowAllActivity] = useState(false)
  const [logViewStep, setLogViewStep] = useState('')
  const logViewStepRef = useRef('')
  const [logViewRunId, setLogViewRunId] = useState('')
  const logViewRunIdRef = useRef('')

  const [manualPinPath, setManualPinPath] = useState('')
  const [allowNonGit, setAllowNonGit] = useState(false)
  const [browseOpen, setBrowseOpen] = useState(false)
  const [browsePath, setBrowsePath] = useState('')
  const [browseParentPath, setBrowseParentPath] = useState<string | null>(null)
  const [browseDirectories, setBrowseDirectories] = useState<BrowseDirectoryEntry[]>([])
  const [browseCurrentIsGit, setBrowseCurrentIsGit] = useState(false)
  const [browseLoading, setBrowseLoading] = useState(false)
  const [browseError, setBrowseError] = useState('')
  const [browseAllowNonGit, setBrowseAllowNonGit] = useState(false)
  const [topbarProjectPickerFocused, setTopbarProjectPickerFocused] = useState(false)

  const [settingsLoading, setSettingsLoading] = useState(false)
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [settingsError, setSettingsError] = useState('')
  const [settingsSuccess, setSettingsSuccess] = useState('')
  const [providerConfigDirty, setProviderConfigDirty] = useState(false)
  const [qualityGateDirty, setQualityGateDirty] = useState(false)
  const [executionFormDirty, setExecutionFormDirty] = useState(false)
  const [advancedRoutingDirty, setAdvancedRoutingDirty] = useState(false)
  const [projectCommandsDirty, setProjectCommandsDirty] = useState(false)
  const [advancedFormDirty, setAdvancedFormDirty] = useState(false)
  useEffect(() => {
    if (!settingsSuccess) return
    const timer = window.setTimeout(() => setSettingsSuccess(''), 4000)
    return () => window.clearTimeout(timer)
  }, [settingsSuccess])
  const [settingsConcurrency, setSettingsConcurrency] = useState(String(DEFAULT_SETTINGS.orchestrator.concurrency))
  const [settingsAutoDeps, setSettingsAutoDeps] = useState(DEFAULT_SETTINGS.orchestrator.auto_deps)
  const [settingsAutoFixPush, setSettingsAutoFixPush] = useState(DEFAULT_SETTINGS.orchestrator.auto_fix_push)
  const [settingsMaxReviewAttempts, setSettingsMaxReviewAttempts] = useState(String(DEFAULT_SETTINGS.orchestrator.max_review_attempts))
  const [settingsMaxMergeConflictAttempts, setSettingsMaxMergeConflictAttempts] = useState(
    String(DEFAULT_SETTINGS.orchestrator.max_merge_conflict_attempts)
  )
  const [settingsStepTimeoutSeconds, setSettingsStepTimeoutSeconds] = useState(String(DEFAULT_SETTINGS.orchestrator.step_timeout_seconds))
  const [settingsDefaultRole, setSettingsDefaultRole] = useState(DEFAULT_SETTINGS.agent_routing.default_role)
  const [settingsTaskTypeRoles, setSettingsTaskTypeRoles] = useState('')
  const [settingsRoleProviderOverrides, setSettingsRoleProviderOverrides] = useState('')
  const [settingsWorkerDefault, setSettingsWorkerDefault] = useState(DEFAULT_SETTINGS.workers.default)
  const [settingsProviderView, setSettingsProviderView] = useState<'codex' | 'ollama' | 'claude'>('codex')
  const [settingsWorkerRouting, setSettingsWorkerRouting] = useState('')
  const [settingsWorkerProviders, setSettingsWorkerProviders] = useState('')
  const [settingsCodexCommand, setSettingsCodexCommand] = useState('codex exec')
  const [settingsCodexModel, setSettingsCodexModel] = useState('')
  const [settingsCodexEffort, setSettingsCodexEffort] = useState('')
  const [settingsCodexExecutionMode, setSettingsCodexExecutionMode] = useState<'sandboxed' | 'host_access'>('sandboxed')
  const [settingsClaudeCommand, setSettingsClaudeCommand] = useState('claude -p')
  const [settingsClaudeModel, setSettingsClaudeModel] = useState('')
  const [settingsClaudeEffort, setSettingsClaudeEffort] = useState('')
  const [settingsClaudeExecutionMode, setSettingsClaudeExecutionMode] = useState<'sandboxed' | 'host_access'>('host_access')
  const [settingsOllamaEndpoint, setSettingsOllamaEndpoint] = useState('http://localhost:11434')
  const [settingsOllamaModel, setSettingsOllamaModel] = useState('')
  const [settingsOllamaTemperature, setSettingsOllamaTemperature] = useState('')
  const [settingsOllamaNumCtx, setSettingsOllamaNumCtx] = useState('')
  const [settingsProjectCommands, setSettingsProjectCommands] = useState('')
  const [settingsPromptOverrides, setSettingsPromptOverrides] = useState<Record<string, string>>({})
  const [settingsPromptOverridesBaseline, setSettingsPromptOverridesBaseline] = useState<Record<string, string>>({})
  const [settingsPromptOverrideClears, setSettingsPromptOverrideClears] = useState<Record<string, true>>({})
  const [settingsLegacyPromptInjections, setSettingsLegacyPromptInjections] = useState<Record<string, string>>({})
  const [settingsPromptDefaults, setSettingsPromptDefaults] = useState<Record<string, string>>({})
  const [settingsPromptStep, setSettingsPromptStep] = useState('')
  const [settingsGateCritical, setSettingsGateCritical] = useState(String(DEFAULT_SETTINGS.defaults.quality_gate.critical))
  const [settingsGateHigh, setSettingsGateHigh] = useState(String(DEFAULT_SETTINGS.defaults.quality_gate.high))
  const [settingsGateMedium, setSettingsGateMedium] = useState(String(DEFAULT_SETTINGS.defaults.quality_gate.medium))
  const [settingsGateLow, setSettingsGateLow] = useState(String(DEFAULT_SETTINGS.defaults.quality_gate.low))
  const [settingsDependencyPolicy, setSettingsDependencyPolicy] = useState(DEFAULT_SETTINGS.defaults.dependency_policy)
  const [settingsDefaultHitlMode, setSettingsDefaultHitlMode] = useState<SystemSettings['defaults']['hitl_mode']>(DEFAULT_SETTINGS.defaults.hitl_mode)
  const [autoDetectedDefaults, setAutoDetectedDefaults] = useState<{
    languages: string[]
    commands: Record<string, Record<string, string>>
    venv: { path: string; source: string } | null
    binary_warnings: Record<string, string[]>
  } | null>(null)
  const [disabledCommands, setDisabledCommands] = useState<Set<string>>(new Set())
  const [resolvedEnvVars, setResolvedEnvVars] = useState<{ key: string; source: string; has_value: boolean }[]>([])
  const settingsLoadedRef = useRef(false)
  const shouldPrefillTaskProjectCommandsRef = useRef(false)

  const selectedTaskIdRef = useRef(selectedTaskId)
  const selectedImportJobIdRef = useRef(selectedImportJobId)
  const activeProjectIdRef = useRef(activeProjectId)
  const projectDirRef = useRef(projectDir)
  const taskDetailRequestSeqRef = useRef(0)
  const collaborationRequestSeqRef = useRef(0)
  const taskExplorerRequestSeqRef = useRef(0)
  const reloadAllSeqRef = useRef(0)
  const reloadTimerRef = useRef<number | null>(null)
  const realtimeRefreshInFlightRef = useRef(false)
  const realtimeRefreshPendingRef = useRef(false)
  const realtimeChannelsRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    selectedTaskIdRef.current = selectedTaskId
  }, [selectedTaskId])

  // Sync gate review controls from selected task's metadata when task changes
  // or when the pending gate becomes before_post_review.
  const selectedBoardTask = Object.values(board.columns).flat().find((t) => t.id === selectedTaskId)
  const selectedTaskGate = selectedBoardTask?.pending_gate
  useEffect(() => {
    if (selectedTaskGate === 'before_post_review' && selectedBoardTask) {
      const meta = (selectedBoardTask.metadata || {}) as Record<string, unknown>
      const proposed = String(meta.proposed_review_decision || 'comment')
      setGateReviewDecision(proposed as ReviewDecisionType)
      setGatePostComments(false)
    }
  }, [selectedTaskId, selectedTaskGate]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const explicitTab = taskSelectTabRef.current
    if (!explicitTab && selectedTaskId) {
      // Auto-switch to plan tab when task is paused at planning/decomposition gates.
      const matchedTask = Object.values(board.columns).flat().find((t) => t.id === selectedTaskId)
      if (
        matchedTask?.pending_gate === 'before_implement' || matchedTask?.pending_gate === 'before_generate_tasks'
      ) {
        setTaskDetailTab('plan')
      } else {
        setTaskDetailTab('overview')
      }
    } else {
      setTaskDetailTab(explicitTab || 'overview')
    }
    taskSelectTabRef.current = undefined
    setTaskActionPending(null)
    setTaskActionDetail('')
    setTaskActionError('')
    setTaskActionMessage('')
    setGateRequestOpen(false)
    setGateRequestGuidance('')
    setNewDependencyId('')
    setDependencyActionMessage('')
    setPlanRefineFeedback('')
    setTaskDiff(null)
    setTaskDiffLoading(false)
    setReviewGuidance('')
    setRetryFromStep('')
    setRetryProvider('')
    setShowAllActivity(false)
    setPlanTabMode('view')
    setSelectedTaskWorkdoc(null)
    setSelectedTaskWorkdocLoading(false)
    setSelectedTaskWorkdocError('')
  }, [selectedTaskId])

  useEffect(() => {
    selectedImportJobIdRef.current = selectedImportJobId
  }, [selectedImportJobId])

  useEffect(() => {
    activeProjectIdRef.current = activeProjectId
  }, [activeProjectId])

  useEffect(() => {
    projectDirRef.current = projectDir
  }, [projectDir])

  useEffect(() => {
    return () => {
      if (reloadTimerRef.current !== null) {
        window.clearTimeout(reloadTimerRef.current)
        reloadTimerRef.current = null
      }
      realtimeChannelsRef.current.clear()
      realtimeRefreshPendingRef.current = false
      realtimeRefreshInFlightRef.current = false
    }
  }, [])

  useEffect(() => {
    const syncFromHash = () => {
      const next = routeFromHash(window.location.hash)
      setRoute(next)
      localStorage.setItem(STORAGE_ROUTE, toHash(next))
    }
    window.addEventListener('hashchange', syncFromHash)
    if (!window.location.hash) {
      window.location.hash = toHash(route)
    }
    return () => window.removeEventListener('hashchange', syncFromHash)
  }, [route])

  async function loadProjectIdentity(): Promise<void> {
    const fallback = inferProjectId(projectDir)
    setActiveProjectId(fallback)
    try {
      const root = await requestJson<RootSnapshot>(buildApiUrl('/readyz', projectDir))
      const resolved = String(root.project_id || '').trim()
      if (resolved) {
        setActiveProjectId(resolved)
      }
    } catch {
      // Keep fallback project identity when root metadata is unavailable.
    }
  }

  useEffect(() => {
    if (projectDir) {
      localStorage.setItem(STORAGE_PROJECT, projectDir)
    } else {
      localStorage.removeItem(STORAGE_PROJECT)
    }
    settingsLoadedRef.current = false
    setSettingsProjectCommands('')
    setSettingsPromptOverrides({})
    setSettingsPromptOverridesBaseline({})
    setSettingsPromptOverrideClears({})
    setSettingsLegacyPromptInjections({})
    setSettingsPromptDefaults({})
    setSettingsPromptStep('')
    void loadProjectIdentity()
  }, [projectDir])

  useEffect(() => {
    if (settingsLoadedRef.current || settingsLoading) return
    void loadSettings()
  }, [projectDir])

  useEffect(() => {
    const hasModalOpen = workOpen || browseOpen || (!!selectedTaskId && modalExplicitRef.current && !modalDismissedRef.current)
    document.documentElement.classList.toggle('modal-open', hasModalOpen)
    document.body.classList.toggle('modal-open', hasModalOpen)
    return () => {
      document.documentElement.classList.remove('modal-open')
      document.body.classList.remove('modal-open')
    }
  }, [workOpen, browseOpen, selectedTaskId, route])

  useEffect(() => {
    const columns = ['backlog', 'queued', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled'] as const
    const allTasks = columns.flatMap((column) => board.columns[column] || [])
    if (!selectedTaskId && !modalDismissedRef.current && allTasks.length > 0) {
      setSelectedTaskId(allTasks[0].id)
    }
    if (selectedTaskId && allTasks.every((task) => task.id !== selectedTaskId)) {
      setSelectedTaskId(allTasks[0]?.id || '')
      setSelectedTaskDetail(null)
    }
  }, [board, selectedTaskId])

  useEffect(() => {
    if (route !== 'execution') return
    const prioritized = [
      ...(board.columns.in_progress || []),
      ...(board.columns.queued || []),
      ...(board.columns.backlog || []),
      ...(board.columns.in_review || []),
      ...(board.columns.blocked || []),
      ...(board.columns.done || []),
      ...(board.columns.cancelled || []),
    ]
    if (prioritized.length === 0) return
    if (selectedTaskId && prioritized.every((task) => task.id !== selectedTaskId)) {
      setSelectedTaskId(prioritized[0].id)
    }
  }, [route, board, selectedTaskId])

  function applySettings(payload: SystemSettings): void {
    setSettingsConcurrency(String(payload.orchestrator.concurrency))
    setSettingsAutoDeps(payload.orchestrator.auto_deps)
    setSettingsAutoFixPush(payload.orchestrator.auto_fix_push)
    setSettingsMaxReviewAttempts(String(payload.orchestrator.max_review_attempts))
    setSettingsMaxMergeConflictAttempts(String(payload.orchestrator.max_merge_conflict_attempts))
    setSettingsStepTimeoutSeconds(String(payload.orchestrator.step_timeout_seconds))
    setSettingsDefaultRole(payload.agent_routing.default_role || 'general')
    const taskTypeRoles = payload.agent_routing.task_type_roles || {}
    setSettingsTaskTypeRoles(Object.keys(taskTypeRoles).length > 0 ? JSON.stringify(taskTypeRoles, null, 2) : '')
    const roleProviderOverrides = payload.agent_routing.role_provider_overrides || {}
    setSettingsRoleProviderOverrides(
      Object.keys(roleProviderOverrides).length > 0 ? JSON.stringify(roleProviderOverrides, null, 2) : ''
    )
    const workerDefault = payload.workers.default || 'codex'
    setSettingsWorkerDefault(workerDefault)
    const providerView: 'codex' | 'ollama' | 'claude' =
      workerDefault === 'ollama' || workerDefault === 'claude' ? workerDefault : 'codex'
    setSettingsProviderView(providerView)
    const workerRouting = payload.workers.routing || {}
    setSettingsWorkerRouting(Object.keys(workerRouting).length > 0 ? JSON.stringify(workerRouting, null, 2) : '')
    const providers = payload.workers.providers || {}
    const advancedProviders = Object.fromEntries(
      Object.entries(providers).filter(([name]) => name !== 'codex' && name !== 'claude' && name !== 'ollama')
    )
    setSettingsWorkerProviders(
      Object.keys(advancedProviders).length > 0 ? JSON.stringify(advancedProviders, null, 2) : ''
    )
    const entries = Object.entries(providers)

    const codexEntry = entries.find(([name, provider]) => name === 'codex' && provider?.type === 'codex')
      || entries.find(([, provider]) => provider?.type === 'codex')
    if (codexEntry) {
      const [, provider] = codexEntry
      setSettingsCodexCommand(String(provider.command || 'codex exec'))
      setSettingsCodexModel(String(provider.model || ''))
      setSettingsCodexEffort(String(provider.reasoning_effort || ''))
      setSettingsCodexExecutionMode(provider.execution_mode === 'host_access' ? 'host_access' : 'sandboxed')
    } else {
      setSettingsCodexCommand('codex exec')
      setSettingsCodexModel('')
      setSettingsCodexEffort('')
      setSettingsCodexExecutionMode('sandboxed')
    }

    const claudeEntry = entries.find(([name, provider]) => name === 'claude' && provider?.type === 'claude')
      || entries.find(([, provider]) => provider?.type === 'claude')
    if (claudeEntry) {
      const [, provider] = claudeEntry
      setSettingsClaudeCommand(String(provider.command || 'claude -p'))
      setSettingsClaudeModel(String(provider.model || ''))
      setSettingsClaudeEffort(String(provider.reasoning_effort || ''))
      setSettingsClaudeExecutionMode(provider.execution_mode === 'sandboxed' ? 'sandboxed' : 'host_access')
    } else {
      setSettingsClaudeCommand('claude -p')
      setSettingsClaudeModel('')
      setSettingsClaudeEffort('')
      setSettingsClaudeExecutionMode('host_access')
    }

    const ollamaEntry = entries.find(([name, provider]) => name === 'ollama' && provider?.type === 'ollama')
      || entries.find(([, provider]) => provider?.type === 'ollama')
    if (ollamaEntry) {
      const [, provider] = ollamaEntry
      setSettingsOllamaEndpoint(String(provider.endpoint || 'http://localhost:11434'))
      setSettingsOllamaModel(String(provider.model || ''))
      setSettingsOllamaTemperature(
        provider.temperature === undefined || provider.temperature === null ? '' : String(provider.temperature)
      )
      setSettingsOllamaNumCtx(provider.num_ctx === undefined || provider.num_ctx === null ? '' : String(provider.num_ctx))
    } else {
      setSettingsOllamaEndpoint('http://localhost:11434')
      setSettingsOllamaModel('')
      setSettingsOllamaTemperature('')
      setSettingsOllamaNumCtx('')
    }
    const projectCommands = payload.project.commands || {}
    setSettingsProjectCommands(
      Object.keys(projectCommands).length > 0 ? serializeProjectCommandsYaml(projectCommands) : ''
    )
    const rawDisabled = (payload.project as Record<string, unknown>).disabled_commands
    if (Array.isArray(rawDisabled)) {
      setDisabledCommands(new Set(rawDisabled.filter((v): v is string => typeof v === 'string')))
    } else {
      setDisabledCommands(new Set())
    }
    const promptOverrides = payload.project.prompt_overrides || {}
    const promptInjections = payload.project.prompt_injections || {}
    const promptDefaults = payload.project.prompt_defaults || {}
    setSettingsPromptOverrides(promptOverrides)
    setSettingsPromptOverridesBaseline(promptOverrides)
    setSettingsPromptOverrideClears({})
    setSettingsLegacyPromptInjections(promptInjections)
    setSettingsPromptDefaults(promptDefaults)
    const promptSteps = Array.from(
      new Set([...Object.keys(promptDefaults), ...Object.keys(promptOverrides), ...Object.keys(promptInjections)])
    ).sort()
    setSettingsPromptStep((previous) => {
      if (previous && promptSteps.includes(previous)) return previous
      return promptSteps[0] || ''
    })
    setSettingsGateCritical(String(payload.defaults.quality_gate.critical))
    setSettingsGateHigh(String(payload.defaults.quality_gate.high))
    setSettingsGateMedium(String(payload.defaults.quality_gate.medium))
    setSettingsGateLow(String(payload.defaults.quality_gate.low))
    setSettingsDependencyPolicy(payload.defaults.dependency_policy || 'prudent')
    setSettingsDefaultHitlMode(normalizeHitlMode(payload.defaults.hitl_mode))
    setTaskGenerationSystemDefaults(payload.defaults.task_generation || DEFAULT_SETTINGS.defaults.task_generation)
  }

  async function loadSettings(): Promise<void> {
    setSettingsLoading(true)
    setSettingsError('')
    setSettingsSuccess('')
    try {
      const payload = await requestJson<Partial<SystemSettings> & { auto_detected_defaults?: unknown; resolved_env_vars?: unknown }>(buildApiUrl('/api/settings', projectDir))
      applySettings(normalizeSettings(payload))
      // Capture auto-detected defaults and resolved env vars
      const rawDefaults = payload.auto_detected_defaults
      if (rawDefaults && typeof rawDefaults === 'object' && !Array.isArray(rawDefaults)) {
        const d = rawDefaults as Record<string, unknown>
        setAutoDetectedDefaults({
          languages: Array.isArray(d.languages) ? d.languages.map(String) : [],
          commands: (d.commands && typeof d.commands === 'object' && !Array.isArray(d.commands))
            ? d.commands as Record<string, Record<string, string>> : {},
          venv: (d.venv && typeof d.venv === 'object') ? d.venv as { path: string; source: string } : null,
          binary_warnings: (d.binary_warnings && typeof d.binary_warnings === 'object' && !Array.isArray(d.binary_warnings))
            ? d.binary_warnings as Record<string, string[]> : {},
        })
      } else {
        setAutoDetectedDefaults(null)
      }
      const rawEnvVars = payload.resolved_env_vars
      if (Array.isArray(rawEnvVars)) {
        setResolvedEnvVars(rawEnvVars.filter(
          (v): v is { key: string; source: string; has_value: boolean } =>
            !!v && typeof v === 'object' && typeof (v as Record<string, unknown>).key === 'string'
        ))
      } else {
        setResolvedEnvVars([])
      }
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'unknown error'
      setSettingsError(`Failed to load settings (${detail})`)
      applySettings(DEFAULT_SETTINGS)
    } finally {
      settingsLoadedRef.current = true
      setSettingsLoading(false)
    }
  }

  useEffect(() => {
    if (route !== 'settings') return
    void loadSettings()
  }, [route, projectDir])

  useEffect(() => {
    if (!workOpen || createTab !== 'task') return
    if (!shouldPrefillTaskProjectCommandsRef.current) return
    if (newTaskProjectCommands.trim()) {
      shouldPrefillTaskProjectCommandsRef.current = false
      return
    }
    if (!settingsLoadedRef.current) return
    shouldPrefillTaskProjectCommandsRef.current = false
    if (!settingsProjectCommands.trim()) return
    setNewTaskProjectCommands(settingsProjectCommands)
  }, [workOpen, createTab, newTaskProjectCommands, settingsProjectCommands])

  async function loadTaskDetail(taskId: string, quiet = false): Promise<void> {
    if (!taskId) {
      setSelectedTaskDetail(null)
      return
    }
    const requestSeq = taskDetailRequestSeqRef.current + 1
    taskDetailRequestSeqRef.current = requestSeq
    if (!quiet) {
      setSelectedTaskDetailLoading(true)
    }
    try {
      const detail = await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/tasks/${taskId}`, projectDir))
      if (requestSeq !== taskDetailRequestSeqRef.current || selectedTaskIdRef.current !== taskId) {
        return
      }
      const task = {
        ...detail.task,
        hitl_mode: normalizeHitlMode(detail.task.hitl_mode),
      }
      setSelectedTaskDetail(task)
      // Always reload plan + workdoc so the plan tab updates when a step completes
      // (including quiet WebSocket-triggered refreshes).
      void loadTaskPlan(taskId)
      void loadTaskWorkdoc(taskId)
      if (!quiet) {
        setEditTaskTitle(task.title || '')
        setEditTaskDescription(task.description || '')
        setEditTaskType(task.task_type || 'feature')
        setEditTaskPriority(task.priority || 'P2')
        setEditTaskLabels((task.labels || []).join(', '))
        setEditTaskHitlMode(normalizeHitlMode(task.hitl_mode))
        setEditTaskDependencyPolicy(task.dependency_policy || 'prudent')
        const perTaskTimeout = Number(task.step_timeout_seconds)
        setEditTaskStepTimeout(Number.isFinite(perTaskTimeout) && perTaskTimeout > 0 ? String(Math.floor(perTaskTimeout)) : '')
        setEditTaskProjectCommands(task.project_commands && Object.keys(task.project_commands).length > 0 ? serializeProjectCommandsYaml(task.project_commands) : '')
        if (task.status === 'blocked' && task.current_step) {
          setRetryFromStep(task.current_step)
        }
      }
    } catch {
      if (requestSeq !== taskDetailRequestSeqRef.current || selectedTaskIdRef.current !== taskId) {
        return
      }
      setSelectedTaskDetail(null)
      setSelectedTaskPlan(null)
      setSelectedTaskWorkdoc(null)
    } finally {
      if (requestSeq === taskDetailRequestSeqRef.current) {
        setSelectedTaskDetailLoading(false)
      }
    }
  }

  async function loadTaskWorkdoc(taskId: string): Promise<void> {
    if (!taskId) {
      setSelectedTaskWorkdoc(null)
      setSelectedTaskWorkdocError('')
      return
    }
    setSelectedTaskWorkdocLoading(true)
    setSelectedTaskWorkdocError('')
    try {
      const payload = await requestJson<unknown>(buildApiUrl(`/api/tasks/${taskId}/workdoc`, projectDir))
      if (selectedTaskIdRef.current !== taskId) {
        return
      }
      const root = (payload && typeof payload === 'object') ? payload as Record<string, unknown> : {}
      const rawContent = typeof root.content === 'string' ? root.content : ''
      const content = rawContent.replace(/<!--[\s\S]*?-->/g, '').replace(/\n{3,}/g, '\n\n')
      setSelectedTaskWorkdoc({
        task_id: String(root.task_id || taskId),
        content,
        exists: !!root.exists,
      })
    } catch (err) {
      if (selectedTaskIdRef.current !== taskId) {
        return
      }
      setSelectedTaskWorkdoc(null)
      const apiErr = err as ApiRequestError
      if (apiErr.status === 409) {
        setSelectedTaskWorkdocError('Workdoc is missing for this task.')
      } else {
        setSelectedTaskWorkdocError(toErrorMessage('Failed to load workdoc', err))
      }
    } finally {
      if (selectedTaskIdRef.current === taskId) {
        setSelectedTaskWorkdocLoading(false)
      }
    }
  }

  async function loadTaskPlan(taskId: string): Promise<void> {
    if (!taskId) {
      setSelectedTaskPlan(null)
      setSelectedTaskPlanJobs([])
      setSelectedPlanRevisionId('')
      return
    }
    try {
      const payload = await requestJson<unknown>(buildApiUrl(`/api/tasks/${taskId}/plan`, projectDir))
      if (selectedTaskIdRef.current !== taskId) {
        return
      }
      const planDoc = normalizeTaskPlan(payload)
      setSelectedTaskPlan(planDoc)
      void loadTaskPlanJobs(taskId)
      const committedOrLatest = planDoc.committed_revision_id || planDoc.latest_revision_id || ''
      setSelectedPlanRevisionId((prev) => {
        if (prev === '') {
          return ''
        }
        if (prev && planDoc.revisions.some((item) => item.id === prev)) {
          return prev
        }
        return committedOrLatest
      })
      setPlanActionError('')
    } catch (err) {
      if (selectedTaskIdRef.current !== taskId) {
        return
      }
      setSelectedTaskPlan(null)
      setSelectedTaskPlanJobs([])
      setPlanActionError(toErrorMessage('Failed to load planning data', err))
    }
  }

  async function loadTaskPlanJobs(taskId: string): Promise<void> {
    try {
      const payload = await requestJson<unknown>(buildApiUrl(`/api/tasks/${taskId}/plan/jobs`, projectDir))
      if (selectedTaskIdRef.current !== taskId) return
      setSelectedTaskPlanJobs(normalizePlanRefineJobs(payload))
    } catch {
      // Keep the last known history on transient failures to avoid UI dropouts.
      if (selectedTaskIdRef.current !== taskId) return
    }
  }

  useEffect(() => {
    const refineOut = planRefineOutputRef.current
    if (refineOut.taskId !== selectedTaskId) {
      planRefineOutputRef.current = { taskId: selectedTaskId || '', logId: '', jobKey: '', text: '' }
      planManualSeedRef.current = { taskId: selectedTaskId || '', workerText: '' }
      setPlanRefineUiState('idle')
      setPlanRefineTrackedJobId('')
      setPlanRefineStdout('')
      setPlanManualContent('')
      setPlanManualFeedbackNote('')
    }
    if (!selectedTaskId) {
      setSelectedTaskPlan(null)
      setSelectedTaskPlanJobs([])
      setSelectedTaskWorkdoc(null)
      setSelectedTaskWorkdocLoading(false)
      setSelectedTaskWorkdocError('')
      setSelectedPlanRevisionId('')
      setPlanActionMessage('')
      setPlanActionError('')
      return
    }
    setPlanActionMessage('')
    setPlanActionError('')
    void loadTaskDetail(selectedTaskId)
  }, [selectedTaskId, projectDir])

  function appendPlanRefineStdout(
    taskId: string,
    logId: string,
    jobKey: string,
    incoming: string,
    mode: 'reset' | 'prepend' | 'append',
  ): void {
    if (!taskId || !jobKey) return
    const nextText = String(incoming || '')
    const current = planRefineOutputRef.current
    const logChanged = !!(logId && current.logId && logId !== current.logId)
    if (current.taskId !== taskId || current.jobKey !== jobKey || logChanged || mode === 'reset') {
      planRefineOutputRef.current = { taskId, logId, jobKey, text: nextText }
      setPlanRefineStdout(nextText)
      return
    }
    const merged = mergeTranscriptChunk(current.text, nextText, mode)
    if (merged !== current.text) {
      planRefineOutputRef.current = { taskId, logId: logId || current.logId, jobKey, text: merged }
      setPlanRefineStdout(merged)
    }
  }

  async function loadCollaboration(taskId: string): Promise<void> {
    if (!taskId) {
      setCollaborationTimeline([])
      setCollaborationError('')
      return
    }
    const requestSeq = collaborationRequestSeqRef.current + 1
    collaborationRequestSeqRef.current = requestSeq
    setCollaborationLoading(true)
    setCollaborationError('')
    try {
      const timelinePayload = await requestJson<unknown>(buildApiUrl(`/api/collaboration/timeline/${taskId}`, projectDir))
      if (requestSeq !== collaborationRequestSeqRef.current || selectedTaskIdRef.current !== taskId) {
        return
      }
      setCollaborationTimeline(normalizeTimelineEvents(timelinePayload))
    } catch (err) {
      if (requestSeq !== collaborationRequestSeqRef.current || selectedTaskIdRef.current !== taskId) {
        return
      }
      setCollaborationTimeline([])
      const detail = err instanceof Error ? err.message : 'unknown error'
      setCollaborationError(`Failed to load activity (${detail})`)
    } finally {
      if (requestSeq === collaborationRequestSeqRef.current) {
        setCollaborationLoading(false)
      }
    }
  }

  function getLogPaneElement(pane: LogPaneKey): HTMLPreElement | null {
    return pane === 'stdout' ? stdoutPreRef.current : stderrPreRef.current
  }

  function updateLogAutoPin(pane: LogPaneKey): void {
    const el = getLogPaneElement(pane)
    if (!el) return
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    logAutoPinRef.current[pane] = distanceFromBottom < LOG_NEAR_BOTTOM_PX
  }

  function handleLogPaneScroll(pane: LogPaneKey): void {
    updateLogAutoPin(pane)
  }

  function snapshotLogScrollOps(stdoutOp: 'none' | 'prepend' | 'append' | 'reset', stderrOp: 'none' | 'prepend' | 'append' | 'reset'): void {
    const stdoutEl = stdoutPreRef.current
    const stderrEl = stderrPreRef.current
    logScrollSnapshotRef.current.stdout = {
      top: stdoutEl ? stdoutEl.scrollTop : 0,
      height: stdoutEl ? stdoutEl.scrollHeight : 0,
      op: stdoutOp,
    }
    logScrollSnapshotRef.current.stderr = {
      top: stderrEl ? stderrEl.scrollTop : 0,
      height: stderrEl ? stderrEl.scrollHeight : 0,
      op: stderrOp,
    }
  }

  async function loadTaskLogs(taskId: string, quiet = false, stepOverride?: string, runIdOverride?: string): Promise<void> {
    if (!taskId) {
      setSelectedTaskLogs(null)
      setSelectedTaskLogsError('')
      return
    }
    const requestSeq = taskLogsRequestSeqRef.current + 1
    taskLogsRequestSeqRef.current = requestSeq
    if (!quiet) setSelectedTaskLogsLoading(true)
    let continueBackfill = false
    try {
      if (logAccumRef.current.taskId !== taskId) {
        logAccumRef.current = createEmptyLogAccum(taskId)
      }
      const accum = logAccumRef.current
      const backfillStdoutBefore = accum.stdoutBackfillOffset
      const backfillStderrBefore = accum.stderrBackfillOffset
      const params = new URLSearchParams()
      params.set('max_chars', String(LOG_CHUNK_CHARS))
      const viewStep = stepOverride !== undefined ? stepOverride : logViewStep
      const viewRunId = runIdOverride !== undefined ? runIdOverride : logViewRunId
      if (viewStep) {
        params.set('step', viewStep)
        if (viewRunId) {
          params.set('run_id', viewRunId)
        }
      }
      if (accum.phase === 'backfill') {
        params.set('backfill', 'true')
        const stdoutNeedsBackfill = accum.stdoutTailStart > 0 && accum.stdoutBackfillOffset > 0
        const stderrNeedsBackfill = accum.stderrTailStart > 0 && accum.stderrBackfillOffset > 0

        const stdoutReadTo = stdoutNeedsBackfill ? accum.stdoutBackfillOffset : accum.stdoutOffset
        const stderrReadTo = stderrNeedsBackfill ? accum.stderrBackfillOffset : accum.stderrOffset
        const stdoutOffset = stdoutNeedsBackfill
          ? Math.max(0, accum.stdoutBackfillOffset - LOG_CHUNK_CHARS)
          : accum.stdoutOffset
        const stderrOffset = stderrNeedsBackfill
          ? Math.max(0, accum.stderrBackfillOffset - LOG_CHUNK_CHARS)
          : accum.stderrOffset

        params.set('stdout_offset', String(stdoutOffset))
        params.set('stderr_offset', String(stderrOffset))
        params.set('stdout_read_to', String(stdoutReadTo))
        params.set('stderr_read_to', String(stderrReadTo))
      } else if (accum.phase === 'forward') {
        // Incremental: read from where we left off.
        params.set('stdout_offset', String(Math.max(0, accum.stdoutOffset)))
        params.set('stderr_offset', String(Math.max(0, accum.stderrOffset)))
      }
      const qs = params.toString()
      const url = buildApiUrl(`/api/tasks/${taskId}/logs${qs ? `?${qs}` : ''}`, projectDir)
      const payload = await requestJson<TaskLogsSnapshot>(url)
      if (requestSeq !== taskLogsRequestSeqRef.current || selectedTaskIdRef.current !== taskId) return

      const payloadLogId = String(payload.log_id || payload.started_at || '').trim()
      if (accum.logId && payloadLogId && accum.logId !== payloadLogId) {
        const reset = createEmptyLogAccum(taskId)
        reset.logId = payloadLogId
        logAccumRef.current = reset
        snapshotLogScrollOps('reset', 'reset')
        planRefineOutputRef.current = { taskId, logId: payloadLogId, jobKey: '', text: '' }
        // Don't clear display state here — let the re-fetch replace it
        // to avoid a blank flash between step transitions.
        setPlanRefineStdout('')
        void loadTaskLogs(taskId, true, logViewStepRef.current || undefined, logViewRunIdRef.current || undefined)
        return
      }
      if (!accum.logId && payloadLogId) {
        accum.logId = payloadLogId
      }

      setSelectedTaskLogs(payload)
      setSelectedTaskLogsError('')

      let stdoutOp: 'none' | 'prepend' | 'append' | 'reset' = 'none'
      let stderrOp: 'none' | 'prepend' | 'append' | 'reset' = 'none'
      let stdoutChunkRendered = ''
      let stdoutChunkMode: 'none' | 'prepend' | 'append' | 'reset' = 'none'

      const stdoutBackfillRequested = accum.phase === 'backfill' && accum.stdoutTailStart > 0 && accum.stdoutBackfillOffset > 0
      const stderrBackfillRequested = accum.phase === 'backfill' && accum.stderrTailStart > 0 && accum.stderrBackfillOffset > 0
      const nextStdoutBackfillOffset = stdoutBackfillRequested
        ? Math.max(0, accum.stdoutBackfillOffset - LOG_CHUNK_CHARS)
        : accum.stdoutBackfillOffset
      const nextStderrBackfillOffset = stderrBackfillRequested
        ? Math.max(0, accum.stderrBackfillOffset - LOG_CHUNK_CHARS)
        : accum.stderrBackfillOffset

      if (accum.phase === 'init') {
        const stdoutChunkRaw = payload.stdout || ''
        const stderrChunkRaw = payload.stderr || ''
        const stdoutParsed = renderStructuredStdoutChunk(stdoutChunkRaw)
        const stderrParsed = renderStructuredStdoutChunk(stderrChunkRaw)
        accum.stdoutRaw = stdoutChunkRaw
        accum.stderrRaw = stderrChunkRaw
        accum.stdoutRendered = stdoutParsed.text
        accum.stderrRendered = stderrParsed.text
        accum.stdoutParsedLines = stdoutParsed.parsedLines
        accum.stdoutStreamEvents = stdoutParsed.streamEvents
        accum.stderrParsedLines = stderrParsed.parsedLines
        accum.stderrStreamEvents = stderrParsed.streamEvents
        accum.stdoutHasTextDelta = stdoutParsed.hasTextDelta
        accum.stderrHasTextDelta = stderrParsed.hasTextDelta
        stdoutOp = 'reset'
        stderrOp = 'reset'
        if (stdoutParsed.text) {
          stdoutChunkRendered = stdoutParsed.text
          stdoutChunkMode = 'reset'
        }
        if (payload.stdout_offset != null) accum.stdoutOffset = payload.stdout_offset
        if (payload.stderr_offset != null) accum.stderrOffset = payload.stderr_offset
        accum.stdoutTailStart = payload.stdout_tail_start || 0
        accum.stderrTailStart = payload.stderr_tail_start || 0
        accum.stdoutBackfillOffset = accum.stdoutTailStart
        accum.stderrBackfillOffset = accum.stderrTailStart
        accum.phase = (accum.stdoutTailStart > 0 || accum.stderrTailStart > 0) ? 'backfill' : 'forward'
      } else if (accum.phase === 'backfill') {
        const stdoutNeedsBackfill = stdoutBackfillRequested
        const stderrNeedsBackfill = stderrBackfillRequested

        if (stdoutNeedsBackfill) {
          const stdoutChunkRaw = payload.stdout || ''
          const stdoutParsed = renderStructuredStdoutChunk(stdoutChunkRaw)
          if (stdoutChunkRaw) {
            accum.stdoutRaw = stdoutChunkRaw + accum.stdoutRaw
          }
          if (stdoutParsed.text) {
            accum.stdoutRendered = stdoutParsed.text + accum.stdoutRendered
            stdoutOp = 'prepend'
            stdoutChunkRendered = stdoutParsed.text
            stdoutChunkMode = 'prepend'
          }
          accum.stdoutParsedLines += stdoutParsed.parsedLines
          accum.stdoutStreamEvents += stdoutParsed.streamEvents
          accum.stdoutHasTextDelta = accum.stdoutHasTextDelta || stdoutParsed.hasTextDelta
          const reportedStart = typeof payload.stdout_chunk_start === 'number'
            ? payload.stdout_chunk_start
            : nextStdoutBackfillOffset
          accum.stdoutBackfillOffset = Math.max(0, Math.min(reportedStart, accum.stdoutBackfillOffset))
        }
        if (stderrNeedsBackfill) {
          const stderrChunkRaw = payload.stderr || ''
          const stderrParsed = renderStructuredStdoutChunk(stderrChunkRaw)
          if (stderrChunkRaw) {
            accum.stderrRaw = stderrChunkRaw + accum.stderrRaw
          }
          if (stderrParsed.text) {
            accum.stderrRendered = stderrParsed.text + accum.stderrRendered
            stderrOp = 'prepend'
          }
          accum.stderrParsedLines += stderrParsed.parsedLines
          accum.stderrStreamEvents += stderrParsed.streamEvents
          accum.stderrHasTextDelta = accum.stderrHasTextDelta || stderrParsed.hasTextDelta
          const reportedStart = typeof payload.stderr_chunk_start === 'number'
            ? payload.stderr_chunk_start
            : nextStderrBackfillOffset
          accum.stderrBackfillOffset = Math.max(0, Math.min(reportedStart, accum.stderrBackfillOffset))
        }

        const stdoutBackfillDone = accum.stdoutTailStart <= 0 || accum.stdoutBackfillOffset <= 0
        const stderrBackfillDone = accum.stderrTailStart <= 0 || accum.stderrBackfillOffset <= 0
        if (stdoutBackfillDone && stderrBackfillDone) {
          accum.phase = 'forward'
        }
      } else {
        const stdoutChunkRaw = payload.stdout || ''
        const stderrChunkRaw = payload.stderr || ''
        const stdoutParsed = renderStructuredStdoutChunk(stdoutChunkRaw, accum.stdoutHasTextDelta)
        const stderrParsed = renderStructuredStdoutChunk(stderrChunkRaw, accum.stderrHasTextDelta)
        accum.stdoutHasTextDelta = stdoutParsed.hasTextDelta
        accum.stderrHasTextDelta = stderrParsed.hasTextDelta
        if (stdoutChunkRaw) {
          accum.stdoutRaw += stdoutChunkRaw
        }
        if (stdoutParsed.text) {
          accum.stdoutRendered += stdoutParsed.text
          stdoutOp = 'append'
          stdoutChunkRendered = stdoutParsed.text
          stdoutChunkMode = 'append'
        }
        accum.stdoutParsedLines += stdoutParsed.parsedLines
        accum.stdoutStreamEvents += stdoutParsed.streamEvents
        if (stderrChunkRaw) {
          accum.stderrRaw += stderrChunkRaw
        }
        if (stderrParsed.text) {
          accum.stderrRendered += stderrParsed.text
          stderrOp = 'append'
        }
        accum.stderrParsedLines += stderrParsed.parsedLines
        accum.stderrStreamEvents += stderrParsed.streamEvents
        if (payload.stdout_offset != null) accum.stdoutOffset = payload.stdout_offset
        if (payload.stderr_offset != null) accum.stderrOffset = payload.stderr_offset
      }

      if (accum.stdoutRaw.length > LOG_HISTORY_MAX_CHARS) {
        accum.stdoutRaw = accum.stdoutRaw.slice(accum.stdoutRaw.length - LOG_HISTORY_MAX_CHARS)
      }
      if (accum.stderrRaw.length > LOG_HISTORY_MAX_CHARS) {
        accum.stderrRaw = accum.stderrRaw.slice(accum.stderrRaw.length - LOG_HISTORY_MAX_CHARS)
      }
      if (accum.stdoutRendered.length > LOG_HISTORY_MAX_CHARS) {
        accum.stdoutRendered = accum.stdoutRendered.slice(accum.stdoutRendered.length - LOG_HISTORY_MAX_CHARS)
        stdoutOp = 'reset'
        stdoutChunkMode = 'none'
      }
      if (accum.stderrRendered.length > LOG_HISTORY_MAX_CHARS) {
        accum.stderrRendered = accum.stderrRendered.slice(accum.stderrRendered.length - LOG_HISTORY_MAX_CHARS)
        stderrOp = 'reset'
      }

      snapshotLogScrollOps(stdoutOp, stderrOp)
      setStdoutRawHistory(accum.stdoutRaw)
      setStderrRawHistory(accum.stderrRaw)
      setStdoutHistory(accum.stdoutRendered)
      setStderrHistory(accum.stderrRendered)
      setStdoutRenderStats({
        parsedLines: accum.stdoutParsedLines,
        streamEvents: accum.stdoutStreamEvents,
        structured: accum.stdoutParsedLines > 0,
      })

      if (stdoutChunkMode !== 'none') {
        const step = String(payload.step || '')
        if (step === 'plan_refine') {
          const refineJobKey = String(selectedTaskPlan?.active_refine_job?.id || payload.started_at || payload.log_id || 'plan_refine')
          appendPlanRefineStdout(taskId, payloadLogId, refineJobKey, stdoutChunkRendered, stdoutChunkMode)
        }
      }

      const stdoutProgressed = accum.stdoutBackfillOffset < backfillStdoutBefore
      const stderrProgressed = accum.stderrBackfillOffset < backfillStderrBefore
      continueBackfill = accum.phase === 'backfill' && (stdoutProgressed || stderrProgressed)
    } catch (err) {
      if (requestSeq !== taskLogsRequestSeqRef.current || selectedTaskIdRef.current !== taskId) return
      setSelectedTaskLogs(null)
      if (!quiet) {
        const detail = err instanceof Error ? err.message : 'unknown error'
        setSelectedTaskLogsError(`Failed to load logs (${detail})`)
      }
    } finally {
      if (requestSeq === taskLogsRequestSeqRef.current && !quiet) setSelectedTaskLogsLoading(false)
    }
    if (continueBackfill && requestSeq === taskLogsRequestSeqRef.current && selectedTaskIdRef.current === taskId) {
      // Drain historical backfill quickly so rendered text stabilizes sooner.
      void loadTaskLogs(taskId, true, logViewStepRef.current || undefined, logViewRunIdRef.current || undefined)
    }
  }

  useEffect(() => {
    setGateRequestOpen(false)
    setGateRequestGuidance('')
    if (!selectedTaskId) {
      setCollaborationTimeline([])
      setCollaborationError('')
      setSelectedTaskLogs(null)
      setSelectedTaskLogsError('')
      logAccumRef.current = createEmptyLogAccum()
      taskLogsRequestSeqRef.current += 1
      logAutoPinRef.current = { stdout: true, stderr: true }
      logScrollSnapshotRef.current = {
        stdout: { top: 0, height: 0, op: 'none' },
        stderr: { top: 0, height: 0, op: 'none' },
      }
      setStdoutHistory('')
      setStderrHistory('')
      setStdoutRawHistory('')
      setStderrRawHistory('')
      setStdoutRenderStats({ parsedLines: 0, streamEvents: 0, structured: false })
      return
    }
    logAutoPinRef.current = { stdout: true, stderr: true }
    logScrollSnapshotRef.current = {
      stdout: { top: 0, height: 0, op: 'reset' },
      stderr: { top: 0, height: 0, op: 'reset' },
    }
    setLogViewStep('')
    logViewStepRef.current = ''
    setLogViewRunId('')
    logViewRunIdRef.current = ''
    void loadCollaboration(selectedTaskId)
    void loadTaskLogs(selectedTaskId)
  }, [selectedTaskId, projectDir])

  useEffect(() => {
    if (!selectedTaskId) return
    const timer = window.setInterval(() => {
      void loadTaskLogs(selectedTaskId, true, logViewStepRef.current || undefined, logViewRunIdRef.current || undefined)
    }, 2_000)
    return () => window.clearInterval(timer)
  }, [selectedTaskId, projectDir])

  // Keep log panes pinned to bottom unless user scrolled up.
  useEffect(() => {
    const panes: LogPaneKey[] = ['stdout', 'stderr']
    for (const pane of panes) {
      const el = getLogPaneElement(pane)
      if (!el) continue
      const snap = logScrollSnapshotRef.current[pane]
      const shouldPin = logAutoPinRef.current[pane]
      if (shouldPin) {
        el.scrollTop = el.scrollHeight
      } else if (snap.op === 'prepend') {
        const delta = el.scrollHeight - snap.height
        if (delta !== 0) {
          el.scrollTop = Math.max(0, snap.top + delta)
        }
      }
      logScrollSnapshotRef.current[pane] = {
        top: el.scrollTop,
        height: el.scrollHeight,
        op: 'none',
      }
    }
  }, [stdoutHistory, stderrHistory])

  async function loadTaskExplorer(): Promise<void> {
    const requestSeq = taskExplorerRequestSeqRef.current + 1
    taskExplorerRequestSeqRef.current = requestSeq
    setTaskExplorerLoading(true)
    setTaskExplorerError('')
    try {
      const params: Record<string, string> = {}
      const effectiveStatus = taskExplorerOnlyBlocked ? 'blocked' : taskExplorerStatus
      if (effectiveStatus) params.status = effectiveStatus
      if (taskExplorerType) params.task_type = taskExplorerType
      if (taskExplorerPriority) params.priority = taskExplorerPriority
      const response = await requestJson<{ tasks: TaskRecord[] }>(buildApiUrl('/api/tasks', projectDir, params))
      const tasks = response.tasks || []
      const query = taskExplorerQuery.trim().toLowerCase()
      const filtered = query
        ? tasks.filter((task) => {
            const haystack = `${task.title} ${task.description || ''} ${task.id}`.toLowerCase()
            return haystack.includes(query)
          })
        : tasks
      if (requestSeq !== taskExplorerRequestSeqRef.current) {
        return
      }
      setTaskExplorerItems(filtered)
    } catch (err) {
      if (requestSeq !== taskExplorerRequestSeqRef.current) {
        return
      }
      setTaskExplorerItems([])
      const detail = err instanceof Error ? err.message : 'unknown error'
      setTaskExplorerError(`Failed to load task explorer (${detail})`)
    } finally {
      if (requestSeq === taskExplorerRequestSeqRef.current) {
        setTaskExplorerLoading(false)
      }
    }
  }

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadTaskExplorer()
    }, 250)
    return () => window.clearTimeout(timer)
  }, [projectDir, taskExplorerQuery, taskExplorerStatus, taskExplorerType, taskExplorerPriority, taskExplorerOnlyBlocked])

  useEffect(() => {
    setTaskExplorerPage(1)
  }, [taskExplorerQuery, taskExplorerStatus, taskExplorerType, taskExplorerPriority, taskExplorerOnlyBlocked, taskExplorerPageSize])

  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(taskExplorerItems.length / taskExplorerPageSize))
    if (taskExplorerPage > maxPage) {
      setTaskExplorerPage(maxPage)
    }
  }, [taskExplorerItems.length, taskExplorerPageSize, taskExplorerPage])

  async function loadImportJobDetail(jobId: string): Promise<void> {
    if (!jobId) {
      setSelectedImportJob(null)
      return
    }
    setSelectedImportJobLoading(true)
    setSelectedImportJobError('')
    setSelectedImportJobErrorAt('')
    try {
      const payload = await requestJson<{ job: ImportJobRecord }>(buildApiUrl(`/api/import/${jobId}`, projectDir))
      if (selectedImportJobIdRef.current !== jobId) return
      setSelectedImportJob(payload.job ?? null)
    } catch (err) {
      if (selectedImportJobIdRef.current !== jobId) return
      setSelectedImportJob(null)
      const detail = err instanceof Error ? err.message : 'unknown error'
      setSelectedImportJobError(`Failed to load import job detail (${detail})`)
      setSelectedImportJobErrorAt(new Date().toLocaleTimeString())
    } finally {
      setSelectedImportJobLoading(false)
    }
  }

  useEffect(() => {
    if (!selectedImportJobId) return
    void loadImportJobDetail(selectedImportJobId)
  }, [selectedImportJobId, projectDir])

  useEffect(() => {
    if (!workOpen || createTab !== 'import') return
    if (!selectedImportJobId) return
    const startedAt = Date.now()
    const timer = window.setInterval(() => {
      if (Date.now() - startedAt > 60_000) {
        window.clearInterval(timer)
        return
      }
      void loadImportJobDetail(selectedImportJobId)
    }, 2_000)
    return () => window.clearInterval(timer)
  }, [workOpen, createTab, selectedImportJobId, projectDir])

  useEffect(() => {
    const activeJob = selectedTaskPlan?.active_refine_job
    const isActive = activeJob && (activeJob.status === 'queued' || activeJob.status === 'running')
    if (!isActive && planJobLoading) {
      setPlanJobLoading(false)
    }
  }, [selectedTaskPlan?.active_refine_job?.status, planJobLoading])

  useEffect(() => {
    const activeJob = selectedTaskPlan?.active_refine_job || null
    const activeStatus = activeJob?.status || null
    const isActive = activeStatus === 'queued' || activeStatus === 'running'
    if (isActive) {
      if (planRefineUiState !== 'running') {
        setPlanRefineUiState('running')
      }
      if (!planRefineTrackedJobId && activeJob?.id) {
        setPlanRefineTrackedJobId(activeJob.id)
      }
      return
    }
    if (planRefineUiState !== 'running') {
      return
    }
    const trackedJob = planRefineTrackedJobId
      ? selectedTaskPlanJobs.find((job) => job.id === planRefineTrackedJobId) || null
      : null
    const terminalStatus = trackedJob?.status
      || (activeStatus === 'completed' || activeStatus === 'failed' || activeStatus === 'cancelled' ? activeStatus : null)
    if (terminalStatus === 'completed') {
      setPlanRefineUiState('done')
      setPlanRefineTrackedJobId('')
      setPlanTabMode('view')
      setSelectedPlanRevisionId('')
      return
    }
    if (terminalStatus === 'failed' || terminalStatus === 'cancelled') {
      setPlanRefineUiState('idle')
      setPlanRefineTrackedJobId('')
    }
  }, [selectedTaskPlan?.active_refine_job, selectedTaskPlanJobs, planRefineTrackedJobId, planRefineUiState])

  useEffect(() => {
    if (selectedTaskPlan?.plan_mutable === false && planTabMode !== 'view') {
      setPlanTabMode('view')
    }
  }, [selectedTaskPlan?.plan_mutable, planTabMode])

  useEffect(() => {
    if (!selectedTaskId) return
    const activeJob = selectedTaskPlan?.active_refine_job
    if (!activeJob) return
    if (!(activeJob.status === 'queued' || activeJob.status === 'running')) return
    async function pollRefine(): Promise<void> {
      void loadTaskPlan(selectedTaskId)
    }
    void pollRefine()
    const timer = window.setInterval(() => void pollRefine(), 2_000)
    return () => window.clearInterval(timer)
  }, [selectedTaskId, selectedTaskPlan?.active_refine_job?.status, projectDir])

  function toErrorMessage(prefix: string, err: unknown): string {
    const detail = err instanceof Error ? err.message : 'unknown error'
    return `${prefix} (${detail})`
  }

  async function refreshTasksSurface(): Promise<void> {
    const refreshProjectDir = projectDirRef.current
    try {
      const [boardData, orchestratorData, executionOrderData, metricsData] = await Promise.all([
        requestJson<BoardResponse>(buildApiUrl('/api/tasks/board', refreshProjectDir)),
        requestJson<OrchestratorStatus>(buildApiUrl('/api/orchestrator/status', refreshProjectDir)),
        requestJson<{ batches: string[][]; completed?: string[] }>(buildApiUrl('/api/tasks/execution-order', refreshProjectDir)),
        requestJson<unknown>(buildApiUrl('/api/metrics', refreshProjectDir)).catch(() => null),
      ])
      if (refreshProjectDir !== projectDirRef.current) {
        return
      }
      setBoard(normalizeBoard(boardData))
      setOrchestrator(orchestratorData)
      setExecutionBatches(executionOrderData.batches || [])
      setExecutionCompleted(executionOrderData.completed || [])
      setMetrics(normalizeMetrics(metricsData))

      const selectedTask = String(selectedTaskIdRef.current || '').trim()
      if (selectedTask) {
        void loadTaskDetail(selectedTask, true)
      }
    } catch (err) {
      if (refreshProjectDir !== projectDirRef.current) {
        return
      }
      setError(toErrorMessage('Failed to refresh tasks surface', err))
    }
  }

  async function refreshAgentsSurface(): Promise<void> {
    const refreshProjectDir = projectDirRef.current
    try {
      const [agentData, healthData, routingData] = await Promise.all([
        requestJson<{ agents: AgentRecord[] }>(buildApiUrl('/api/agents', refreshProjectDir)),
        requestJson<unknown>(buildApiUrl('/api/workers/health', refreshProjectDir)).catch(() => ({ providers: [] })),
        requestJson<unknown>(buildApiUrl('/api/workers/routing', refreshProjectDir)).catch(() => ({ default: 'codex', rows: [] })),
      ])
      if (refreshProjectDir !== projectDirRef.current) {
        return
      }
      setAgents(agentData.agents || [])
      setWorkerHealth(normalizeWorkerHealth(healthData))
      const normalizedRouting = normalizeWorkerRouting(routingData)
      setWorkerDefaultProvider(normalizedRouting.defaultProvider)
      setWorkerRoutingRows(normalizedRouting.rows)
    } catch (err) {
      if (refreshProjectDir !== projectDirRef.current) {
        return
      }
      setError(toErrorMessage('Failed to refresh workers surface', err))
    }
  }

  async function reloadAll(): Promise<void> {
    const requestSeq = reloadAllSeqRef.current + 1
    reloadAllSeqRef.current = requestSeq
    setLoading(true)
    setError('')
    try {
      const [
        boardData,
        orchestratorData,
        agentData,
        projectData,
        pinnedData,
        executionOrderData,
        metricsData,
        workerHealthData,
        workerRoutingData,
      ] = await Promise.all([
        requestJson<BoardResponse>(buildApiUrl('/api/tasks/board', projectDir)),
        requestJson<OrchestratorStatus>(buildApiUrl('/api/orchestrator/status', projectDir)),
        requestJson<{ agents: AgentRecord[] }>(buildApiUrl('/api/agents', projectDir)),
        requestJson<{ projects: ProjectRef[] }>(buildApiUrl('/api/projects', projectDir)),
        requestJson<{ items: PinnedProjectRef[] }>(buildApiUrl('/api/projects/pinned', projectDir)),
        requestJson<{ batches: string[][]; completed?: string[] }>(buildApiUrl('/api/tasks/execution-order', projectDir)),
        requestJson<unknown>(buildApiUrl('/api/metrics', projectDir)).catch(() => null),
        requestJson<unknown>(buildApiUrl('/api/workers/health', projectDir)).catch(() => ({ providers: [] })),
        requestJson<unknown>(buildApiUrl('/api/workers/routing', projectDir)).catch(() => ({ default: 'codex', rows: [] })),
      ])
      if (requestSeq !== reloadAllSeqRef.current) {
        return
      }
      setBoard(normalizeBoard(boardData))
      setOrchestrator(orchestratorData)
      setAgents(agentData.agents || [])
      setProjects(projectData.projects || [])
      setPinnedProjects(pinnedData.items || [])
      setExecutionBatches(executionOrderData.batches || [])
      setExecutionCompleted(executionOrderData.completed || [])
      setMetrics(normalizeMetrics(metricsData))
      setWorkerHealth(normalizeWorkerHealth(workerHealthData))
      const normalizedRouting = normalizeWorkerRouting(workerRoutingData)
      setWorkerDefaultProvider(normalizedRouting.defaultProvider)
      setWorkerRoutingRows(normalizedRouting.rows)
    } catch (err) {
      if (requestSeq !== reloadAllSeqRef.current) {
        return
      }
      setError(err instanceof Error ? err.message : 'Failed to load data')
    } finally {
      if (requestSeq === reloadAllSeqRef.current) {
        setLoading(false)
      }
    }
  }

  async function fetchGitStatus(): Promise<void> {
    try {
      const data = await requestJson<{
        branch: string
        remote_branch: string | null
        ahead_count: number
        behind_count: number
        commits: { sha: string; message: string }[]
        has_remote: boolean
      }>(buildApiUrl('/api/git/status', projectDirRef.current))
      setGitStatus(data)
    } catch {
      // Silently ignore — git status is optional UI
    }
  }

  async function handleGitPush(targetBranch?: string, autoName?: boolean): Promise<void> {
    setPushInProgress(true)
    setPushAutoFixRunning(false)
    setPushError('')
    setPushMessage('')
    setPushOutputLines([])
    let autoFixStarted = false
    try {
      const result = await requestJson<{
        success: boolean
        error: string | null
        remote_branch: string
        pushed_commits: number
        auto_fix?: boolean
      }>(buildApiUrl('/api/git/push', projectDirRef.current), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target_branch: targetBranch || null,
          auto_name: autoName || false,
        }),
      })
      if (result.auto_fix) {
        // Worker is running in background — keep pushInProgress true.
        // WebSocket git.pushed / git.push_output events will update the UI.
        autoFixStarted = true
        setPushAutoFixRunning(true)
        return
      }
      setPushMessage(
        result.pushed_commits === 0
          ? `Set upstream tracking to ${result.remote_branch}`
          : `Pushed ${result.pushed_commits} commit${result.pushed_commits !== 1 ? 's' : ''} to ${result.remote_branch}`,
      )
      setPushPopoverOpen(false)
      setPushTargetBranch('')
      void fetchGitStatus()
      window.setTimeout(() => setPushMessage(''), 6000)
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Push failed'
      if (msg !== 'Push cancelled') {
        setPushError(msg)
      }
    } finally {
      if (!autoFixStarted) {
        setPushInProgress(false)
      }
    }
  }

  async function handleCancelPush(): Promise<void> {
    try {
      await requestJson<{ cancelled: boolean }>(
        buildApiUrl('/api/git/push/cancel', projectDirRef.current),
        { method: 'POST' },
      )
    } catch {
      // best-effort
    }
  }

  async function handleAutoNamePush(): Promise<void> {
    setAutoNameInProgress(true)
    setPushError('')
    setPushMessage('')
    try {
      const suggestion = await requestJson<{
        branch_name: string | null
        error: string | null
      }>(buildApiUrl('/api/git/suggest-branch-name', projectDirRef.current), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      })
      if (suggestion.error || !suggestion.branch_name) {
        setPushError(suggestion.error || 'Failed to generate branch name')
        return
      }
      await handleGitPush(suggestion.branch_name)
    } catch (err) {
      setPushError(err instanceof Error ? err.message : 'Auto name generation failed')
    } finally {
      setAutoNameInProgress(false)
    }
  }

  function isSchedulerStale(status: OrchestratorStatus): boolean {
    if (typeof status.scheduler_stale === 'boolean') return status.scheduler_stale
    return status.status === 'running'
      && status.scheduler_attached === false
      && ((Number(status.queue_depth) || 0) > 0 || (Number(status.in_progress) || 0) > 0)
  }

  async function refreshWithSchedulerRepair(): Promise<void> {
    await reloadAll()
    void fetchGitStatus()
    const refreshProjectDir = projectDirRef.current
    try {
      const status = await requestJson<OrchestratorStatus>(buildApiUrl('/api/orchestrator/status', refreshProjectDir))
      if (!isSchedulerStale(status)) return
      await requestJson<OrchestratorStatus>(buildApiUrl('/api/orchestrator/control', refreshProjectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'reset' }),
      })
      await requestJson<OrchestratorStatus>(buildApiUrl('/api/orchestrator/control', refreshProjectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'reconcile' }),
      })
      await reloadAll()
      setTaskActionMessage('Scheduler reattached and reconciled.')
    } catch (err) {
      setError(toErrorMessage('Failed to repair scheduler', err))
    }
  }

  async function flushRealtimeRefreshQueue(): Promise<void> {
    if (realtimeRefreshInFlightRef.current) {
      realtimeRefreshPendingRef.current = true
      return
    }
    realtimeRefreshInFlightRef.current = true
    try {
      do {
        realtimeRefreshPendingRef.current = false
        const channels = new Set(realtimeChannelsRef.current)
        realtimeChannelsRef.current.clear()
        if (channels.size === 0) {
          continue
        }

        const refreshTasks = channels.has('tasks') || channels.has('queue') || channels.has('review') || channels.has('notifications')
        const refreshAgents = channels.has('agents') || channels.has('notifications')
        const ops: Array<Promise<void>> = []
        if (refreshTasks) ops.push(refreshTasksSurface())
        if (refreshAgents) ops.push(refreshAgentsSurface())
        if (ops.length > 0) {
          await Promise.all(ops)
        }
      } while (realtimeRefreshPendingRef.current || realtimeChannelsRef.current.size > 0)
    } finally {
      realtimeRefreshInFlightRef.current = false
    }
  }

  function scheduleRealtimeRefresh(channel: string, delayMs = 160): void {
    if (!WS_RELOAD_CHANNELS.has(channel)) {
      return
    }
    realtimeChannelsRef.current.add(channel)

    if (realtimeRefreshInFlightRef.current) {
      realtimeRefreshPendingRef.current = true
      return
    }

    if (reloadTimerRef.current !== null) {
      return
    }
    reloadTimerRef.current = window.setTimeout(() => {
      reloadTimerRef.current = null
      void flushRealtimeRefreshQueue()
    }, delayMs)
  }

  useEffect(() => {
    void reloadAll()
    void fetchGitStatus()
  }, [projectDir])

  useEffect(() => {
    if (!pushPopoverOpen) return
    const handler = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      if (target && !target.closest('.git-push-wrap')) {
        setPushPopoverOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [pushPopoverOpen])

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      if (target && !target.closest('.board-more-menu')) {
        const details = document.querySelector('.board-more-menu') as HTMLDetailsElement | null
        if (details?.open) details.open = false
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  useEffect(() => {
    let stopped = false
    let socket: WebSocket | null = null
    let reconnectTimer: number | null = null
    let reconnectAttempts = 0
    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`
    const subscribedChannels = ['tasks', 'queue', 'agents', 'review', 'terminal', 'notifications', 'system']

    const scheduleReconnect = (): void => {
      if (stopped || reconnectTimer !== null) return
      const attempt = Math.min(reconnectAttempts, 6)
      const baseDelay = Math.min(30_000, 1_000 * (2 ** attempt))
      const jitter = Math.floor(Math.random() * 300)
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null
        reconnectAttempts += 1
        connect()
      }, baseDelay + jitter)
    }

    const handleMessage = (event: MessageEvent): void => {
      let payload: unknown
      try {
        payload = JSON.parse(String(event.data || '{}'))
      } catch {
        return
      }
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        return
      }
      const data = payload as Record<string, unknown>
      const channel = String(data.channel || '').trim()
      if (!channel) {
        return
      }
      if (channel === 'system') {
        const eventType = String(data.event_type || '')
        if (eventType === 'git.pushed') {
          void fetchGitStatus()
          // Auto-fix worker completed — clear in-progress state
          const payload = data.payload as Record<string, unknown> | undefined
          if (payload?.auto_fix) {
            setPushInProgress(false)
            setPushAutoFixRunning(false)
            setPushMessage(String(payload.detail ?? 'Push succeeded (auto-fix)'))
            window.setTimeout(() => setPushMessage(''), 8000)
          }
        }
        if (eventType === 'git.push_output') {
          const payload = data.payload as Record<string, unknown> | undefined
          const line = String(payload?.line ?? '')
          setPushOutputLines((prev) => [...prev, line])
        }
        if (eventType === 'git.auto_fix_done') {
          const payload = data.payload as Record<string, unknown> | undefined
          setPushInProgress(false)
          setPushAutoFixRunning(false)
          setPushError(String(payload?.detail ?? 'Auto-fix failed'))
        }
        return
      }
      const eventProjectId = String(data.project_id || '').trim()
      const currentProjectId = String(activeProjectIdRef.current || '').trim()
      if (eventProjectId && (!currentProjectId || currentProjectId !== eventProjectId)) {
        return
      }

      if (WS_RELOAD_CHANNELS.has(channel)) {
        scheduleRealtimeRefresh(channel, 120)
      }
    }

    const connect = (): void => {
      if (stopped) return
      socket = new WebSocket(wsUrl)
      socket.addEventListener('open', () => {
        reconnectAttempts = 0
        socket?.send(JSON.stringify({
          action: 'subscribe',
          channels: subscribedChannels,
          project_id: activeProjectIdRef.current || undefined,
        }))
      })
      socket.addEventListener('message', handleMessage)
      socket.addEventListener('error', () => {
        socket?.close()
      })
      socket.addEventListener('close', () => {
        scheduleReconnect()
      })
    }

    connect()

    return () => {
      stopped = true
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
      socket?.close()
    }
  }, [projectDir, activeProjectId])

  async function runTaskMutation(
    kind: TaskActionKey,
    mutation: () => Promise<void>,
    options?: {
      startMessage?: string
      successMessage?: string
      errorPrefix?: string
      clearEditModeOnSuccess?: boolean
      detail?: string
    },
  ): Promise<void> {
    setTaskActionPending(kind)
    setTaskActionDetail(options?.detail || '')
    setTaskActionError('')
    if (options?.startMessage !== undefined) {
      setTaskActionMessage(options.startMessage)
    } else {
      setTaskActionMessage('')
    }
    try {
      await mutation()
      if (options?.successMessage) {
        setTaskActionMessage(options.successMessage)
      }
      // clearEditModeOnSuccess — no-op (config editability is status-driven)
    } catch (err) {
      setTaskActionError(toErrorMessage(options?.errorPrefix || `Failed to ${kind} task`, err))
      // Always clear on error so buttons become clickable again.
      setTaskActionPending(null)
      setTaskActionDetail('')
      return
    }
    // For transitions the board reload changes which buttons render, so
    // clearing the pending flag early causes a brief flash of the idle
    // button label before the new status takes effect.  Defer clearing
    // for transitions so React renders the updated status first, then
    // the new buttons become interactive.
    if (kind !== 'transition') {
      setTaskActionPending(null)
      setTaskActionDetail('')
    } else {
      // Use a microtask so the status-driven button swap renders first
      queueMicrotask(() => {
        setTaskActionPending(null)
        setTaskActionDetail('')
      })
    }
  }

  async function submitTask(event: FormEvent, statusOverride?: 'queued' | 'backlog'): Promise<void> {
    event.preventDefault()
    if (!newTaskTitle.trim()) return
    if (isSubmittingTask) return
    setIsSubmittingTask(true)
    setSubmittingTaskTarget(statusOverride ?? 'queued')
    try {
    let parsedMetadata: Record<string, unknown> | undefined
    if (newTaskMetadata.trim()) {
      try {
        const metadataJson = JSON.parse(newTaskMetadata)
        if (metadataJson && typeof metadataJson === 'object' && !Array.isArray(metadataJson)) {
          parsedMetadata = metadataJson as Record<string, unknown>
        } else {
          setError('Task metadata must be a JSON object')
          return
        }
      } catch {
        setError('Task metadata must be valid JSON')
        return
      }
    }
    const parsedStepTimeout = parseOptionalPositiveInt(newTaskStepTimeout)
    if (newTaskStepTimeout.trim() && parsedStepTimeout === null) {
      setError('Task timeout must be a positive number of seconds')
      return
    }
    let parsedProjectCommands: Record<string, Record<string, string>> | undefined
    if (newTaskProjectCommands.trim()) {
      try {
        parsedProjectCommands = parseProjectCommands(newTaskProjectCommands)
        if (Object.keys(parsedProjectCommands).length === 0) parsedProjectCommands = undefined
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Invalid project commands JSON')
        return
      }
    }
    const parsedPipelineTemplate = newTaskPipelineTemplate
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
    try {
      await requestJson<{ task: TaskRecord }>(buildApiUrl('/api/tasks', projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: newTaskTitle.trim(),
          description: newTaskDescription,
          task_type: newTaskType,
          priority: newTaskPriority,
          labels: newTaskLabels.split(',').map((item) => item.trim()).filter(Boolean),
          blocked_by: newTaskBlockedBy.split(',').map((item) => item.trim()).filter(Boolean),
          hitl_mode: newTaskHitlMode,
          dependency_policy: newTaskDependencyPolicy,
          worker_model: newTaskWorkerModel.trim() || undefined,
          worker_provider: newTaskWorkerProvider.trim() || undefined,
          step_timeout_seconds: parsedStepTimeout ?? undefined,
          parent_id: newTaskParentId.trim() || undefined,
          pipeline_template: parsedPipelineTemplate.length > 0 ? parsedPipelineTemplate : undefined,
          metadata: parsedMetadata,
          project_commands: parsedProjectCommands,
          status: statusOverride || 'queued',
        }),
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create task')
      return
    }
    setError('')
    setNewTaskTitle('')
    setNewTaskDescription('')
    setNewTaskType('auto')
    setNewTaskPriority('P2')
    setNewTaskLabels('')
    setNewTaskBlockedBy('')
    setNewTaskHitlMode(settingsDefaultHitlMode)
    setNewTaskDependencyPolicy('prudent')
    setNewTaskParentId('')
    setNewTaskPipelineTemplate('')
    setNewTaskStepTimeout('')
    setNewTaskMetadata('')
    setNewTaskProjectCommands('')
    setNewTaskWorkerModel('')
    setNewTaskWorkerProvider('')
    setWorkOpen(false)
    await reloadAll()
    } finally {
      setIsSubmittingTask(false)
      setSubmittingTaskTarget(null)
    }
  }

  async function fetchPullRequests(): Promise<void> {
    setPrListLoading(true)
    setPrListError('')
    setPrErrorCode(null)
    try {
      const data = await requestJson<{ platform: string | null; error: string | null; error_code: string | null; items: PullRequestItem[] }>(
        buildApiUrl('/api/pull-requests', projectDir),
      )
      setPrPlatform(data.platform)
      if (data.error) {
        setPrListError(data.error)
        setPrErrorCode(data.error_code ?? null)
        setPrList([])
      } else {
        setPrList(data.items)
      }
    } catch (err) {
      setPrListError(err instanceof Error ? err.message : 'Failed to load pull requests')
      setPrErrorCode('cli_error')
      setPrList([])
    } finally {
      setPrListLoading(false)
    }
  }

  async function fetchPlatformStatus(): Promise<void> {
    try {
      const data = await requestJson<{ platform: string | null; cli_installed: boolean; cli_authenticated: boolean; cli_name: string | null; setup_steps: { step: number; label: string; done: boolean }[] }>(
        buildApiUrl('/api/git-platform-status', projectDir),
      )
      setPlatformStatus(data)
    } catch {
      // Non-critical — silently ignore
    }
  }

  async function submitPrReview(): Promise<void> {
    if (selectedPrNumber === null) return
    setIsSubmittingReview(true)
    try {
      await requestJson(buildApiUrl(`/api/pull-requests/${selectedPrNumber}/review`, projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          guidance: prReviewGuidance,
          review_mode: prReviewMode,
        }),
      })
      setWorkOpen(false)
      setSelectedPrNumber(null)
      setPrReviewGuidance('')
      setPrReviewMode(ReviewMode.FixOnly)
      void reloadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create review')
    } finally {
      setIsSubmittingReview(false)
    }
  }

  async function previewImport(event: FormEvent): Promise<void> {
    event.preventDefault()
    if (!importText.trim()) return
    try {
      const preview = await requestJson<{ job_id: string; preview: PrdPreview }>(buildApiUrl('/api/import/prd/preview', projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: importText, default_priority: 'P2' }),
      })
      setImportJobId(preview.job_id)
      setImportPreview(preview.preview)
      setSelectedImportJobId(preview.job_id)
      setRecentImportJobIds((prev) => [preview.job_id, ...prev.filter((item) => item !== preview.job_id)].slice(0, 8))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to preview import')
    }
  }

  async function commitImport(): Promise<void> {
    if (!importJobId) return
    try {
      const commitResponse = await requestJson<{ created_task_ids: string[] }>(buildApiUrl('/api/import/prd/commit', projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: importJobId }),
      })
      setRecentImportCommitMap((prev) => ({ ...prev, [importJobId]: commitResponse.created_task_ids || [] }))
      if (importJobId) {
        setSelectedImportJobId(importJobId)
        setRecentImportJobIds((prev) => [importJobId, ...prev.filter((item) => item !== importJobId)].slice(0, 8))
        await loadImportJobDetail(importJobId)
      }
      setImportJobId('')
      setImportPreview(null)
      setImportText('')
      setWorkOpen(false)
      await reloadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to commit import')
    }
  }

  async function transitionTask(taskId: string, targetStatus: string): Promise<void> {
    const status = targetStatus
    await runTaskMutation(
      'transition',
      async () => {
        await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/tasks/${taskId}/transition`, projectDir), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status }),
        })
        await reloadAll()
        if (selectedTaskIdRef.current === taskId) {
          await loadTaskDetail(taskId)
        }
      },
      {
        successMessage: `Task moved to ${humanizeLabel(status)}.`,
        errorPrefix: 'Failed to transition task',
        detail: status,
      },
    )
  }

  async function deleteTask(taskId: string): Promise<void> {
    if (!window.confirm('Delete this task permanently?')) {
      return
    }
    await runTaskMutation(
      'delete',
      async () => {
        await requestJson<{ deleted: boolean; task_id: string }>(buildApiUrl(`/api/tasks/${taskId}`, projectDir), {
          method: 'DELETE',
        })
        await reloadAll()
        if (selectedTaskIdRef.current === taskId) {
          modalDismissedRef.current = true
          modalExplicitRef.current = false
          setSelectedTaskId('')
          setSelectedTaskDetail(null)
          setSelectedTaskPlan(null)
        }
      },
      {
        successMessage: 'Task deleted.',
        errorPrefix: 'Failed to delete task',
      },
    )
  }

  async function clearAllTasks(): Promise<void> {
    if (!window.confirm('Clear all tasks? Existing runtime state will be archived to disk.')) {
      return
    }
    await runTaskMutation(
      'clear',
      async () => {
        const applyClearResult = async (result: { cleared: boolean; archived_to?: string; message?: string }) => {
          await reloadAll()
          modalDismissedRef.current = true
          modalExplicitRef.current = false
          setSelectedTaskId('')
          setSelectedTaskDetail(null)
          setSelectedTaskPlan(null)
          setTaskActionMessage(String(result.message || 'Cleared all tasks.'))
        }

        try {
          const result = await requestJson<{ cleared: boolean; archived_to?: string; message?: string }>(
            buildApiUrl('/api/tasks/clear', projectDir),
            { method: 'POST' },
          )
          await applyClearResult(result)
          return
        } catch (err) {
          const apiErr = err as Partial<ApiRequestError>
          const isActiveExecutionConflict = apiErr.status === 409 && apiErr.code === 'active_execution'
          if (!isActiveExecutionConflict) throw err

          if (
            !window.confirm(
              'Tasks are still running. Force clear will cancel active tasks before archiving state. Continue?'
            )
          ) {
            setTaskActionMessage('Clear cancelled. Active tasks are still running.')
            return
          }
        }

        const forced = await requestJson<{ cleared: boolean; archived_to?: string; message?: string }>(
          buildApiUrl('/api/tasks/clear', projectDir, { force: true, timeout_seconds: 30 }),
          { method: 'POST' },
        )
        await applyClearResult(forced)
      },
      {
        startMessage: 'Clearing all tasks...',
        errorPrefix: 'Failed to clear tasks',
      },
    )
  }

  async function queueAllBacklog(): Promise<void> {
    const backlogTasks = board.columns.backlog || []
    if (backlogTasks.length === 0) {
      setTaskActionMessage('No backlog tasks to queue.')
      return
    }
    if (!window.confirm(`Queue all ${backlogTasks.length} backlog task(s)?`)) {
      return
    }
    await runTaskMutation(
      'queue-backlog',
      async () => {
        const result = await requestJson<{ queued_count: number; message?: string }>(
          buildApiUrl('/api/tasks/queue-backlog', projectDir),
          { method: 'POST' },
        )
        await reloadAll()
        setTaskActionMessage(String(result.message || `Queued ${result.queued_count} task(s).`))
      },
      {
        startMessage: 'Queuing backlog tasks...',
        errorPrefix: 'Failed to queue backlog tasks',
      },
    )
  }

  function switchLogStep(step: string): void {
    const effective = step || ''
    setLogViewStep(effective)
    logViewStepRef.current = effective
    const effectiveRunId = ''
    setLogViewRunId(effectiveRunId)
    logViewRunIdRef.current = effectiveRunId
    // Reset accumulator so we fetch the full log for the new step.
    // Don't clear display state here — let loadTaskLogs replace it when
    // the fetch completes, avoiding a blank flash between steps.
    logAccumRef.current = createEmptyLogAccum(selectedTaskId)
    snapshotLogScrollOps('reset', 'reset')
    if (selectedTaskId) {
      void loadTaskLogs(selectedTaskId, false, effective, effectiveRunId || undefined)
    }
  }

  function switchLogStepAttempt(step: string, runId: string): void {
    const normalizedStep = step || ''
    const normalizedRunId = runId || ''
    setLogViewStep(normalizedStep)
    logViewStepRef.current = normalizedStep
    setLogViewRunId(normalizedRunId)
    logViewRunIdRef.current = normalizedRunId
    logAccumRef.current = createEmptyLogAccum(selectedTaskId)
    snapshotLogScrollOps('reset', 'reset')
    if (selectedTaskId) {
      void loadTaskLogs(selectedTaskId, false, normalizedStep || undefined, normalizedRunId || undefined)
    }
  }

  async function fetchTaskDiff(taskId: string): Promise<void> {
    setTaskDiffLoading(true)
    try {
      try {
        const data = await requestJson<{
          mode?: 'working_tree' | 'committed' | 'preserved_branch' | 'none'
          reason?: 'task_context_missing' | 'invalid_worktree_context' | 'preserved_branch_missing'
          branch?: string | null
          base_branch?: string | null
          base_ref?: string | null
          base_sha?: string | null
          head_ref?: string | null
          head_sha?: string | null
          base_source?: 'metadata_sha' | 'metadata_branch' | 'review_context_sha' | 'review_context_branch' | 'heuristic' | 'none'
          confidence?: 'high' | 'medium' | 'low'
          warnings?: string[]
          commit: string | null
          files: { path: string; changes: string }[]
          diff: string
          stat: string
        }>(
          buildApiUrl(`/api/tasks/${taskId}/changes`, projectDir),
        )
        setTaskDiff({
          mode: data.mode,
          reason: data.reason,
          branch: data.branch ?? null,
          base_branch: data.base_branch ?? null,
          base_ref: data.base_ref ?? null,
          base_sha: data.base_sha ?? null,
          head_ref: data.head_ref ?? null,
          head_sha: data.head_sha ?? null,
          base_source: data.base_source ?? 'none',
          confidence: data.confidence ?? 'high',
          warnings: Array.isArray(data.warnings) ? data.warnings.map((item) => String(item)).filter(Boolean) : [],
          commit: data.commit ?? null,
          files: Array.isArray(data.files) ? data.files : [],
          diff: typeof data.diff === 'string' ? data.diff : '',
          stat: typeof data.stat === 'string' ? data.stat : '',
        })
      } catch {
        const legacy = await requestJson<{ commit: string | null; files: { path: string; changes: string }[]; diff: string; stat: string }>(
          buildApiUrl(`/api/tasks/${taskId}/diff`, projectDir),
        )
        setTaskDiff({
          mode: legacy.commit ? 'committed' : 'none',
          reason: undefined,
          branch: null,
          base_branch: null,
          base_ref: null,
          base_sha: null,
          head_ref: null,
          head_sha: null,
          base_source: 'none',
          confidence: 'high',
          warnings: [],
          commit: legacy.commit ?? null,
          files: Array.isArray(legacy.files) ? legacy.files : [],
          diff: typeof legacy.diff === 'string' ? legacy.diff : '',
          stat: typeof legacy.stat === 'string' ? legacy.stat : '',
        })
      }
    } catch {
      setTaskDiff(null)
    } finally {
      setTaskDiffLoading(false)
    }
  }

  async function addDependency(taskId: string): Promise<void> {
    if (!newDependencyId.trim()) return
    try {
      await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/tasks/${taskId}/dependencies`, projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ depends_on: newDependencyId.trim() }),
      })
      setNewDependencyId('')
      await reloadAll()
      if (selectedTaskIdRef.current === taskId) {
        await loadTaskDetail(taskId)
      }
    } catch (err) {
      setTaskActionError(err instanceof Error ? err.message : 'Failed to add dependency')
    }
  }

  async function removeDependency(taskId: string, depId: string): Promise<void> {
    try {
      await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/tasks/${taskId}/dependencies/${depId}`, projectDir), {
        method: 'DELETE',
      })
      await reloadAll()
      if (selectedTaskIdRef.current === taskId) {
        await loadTaskDetail(taskId)
      }
    } catch (err) {
      setTaskActionError(err instanceof Error ? err.message : 'Failed to remove dependency')
    }
  }

  async function analyzeDependencies(): Promise<void> {
    setDependencyActionLoading(true)
    setDependencyActionMessage('')
    try {
      const result = await requestJson<{ edges?: Array<{ from: string; to: string; reason?: string }> }>(
        buildApiUrl('/api/tasks/analyze-dependencies', projectDir),
        { method: 'POST' },
      )
      const edgeCount = result.edges?.length || 0
      setDependencyActionMessage(`Dependency analysis complete (${edgeCount} inferred edge${edgeCount === 1 ? '' : 's'}).`)
      await reloadAll()
      if (selectedTaskId) {
        await loadTaskDetail(selectedTaskId)
      }
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'unknown error'
      setDependencyActionMessage(`Dependency analysis failed (${detail})`)
    } finally {
      setDependencyActionLoading(false)
    }
  }

  async function resetDependencyAnalysis(taskId: string): Promise<void> {
    setDependencyActionLoading(true)
    setDependencyActionMessage('')
    try {
      await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/tasks/${taskId}/reset-dep-analysis`, projectDir), {
        method: 'POST',
      })
      setDependencyActionMessage('Reset inferred dependency analysis for selected task.')
      await reloadAll()
      await loadTaskDetail(taskId)
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'unknown error'
      setDependencyActionMessage(`Reset dependency analysis failed (${detail})`)
    } finally {
      setDependencyActionLoading(false)
    }
  }

  async function approveGate(taskId: string, gate?: string | null, reviewOpts?: { review_decision?: string; post_comments?: boolean }): Promise<void> {
    const previousBoardTask = Object.values(board.columns || {})
      .flat()
      .find((task) => task.id === taskId) || null
    const previousSelectedTask = selectedTaskDetail && selectedTaskDetail.id === taskId ? selectedTaskDetail : null

    setTaskActionPending('approve_gate')
    setTaskActionDetail(`approve:${String(gate || '')}`)
    setTaskActionError('')
    try {
      setBoard((prev) => {
        const nextColumns: Record<string, TaskRecord[]> = {}
        for (const [column, items] of Object.entries(prev.columns || {})) {
          nextColumns[column] = (items || []).map((item) => (
            item.id === taskId
              ? {
                  ...item,
                  pending_gate: null,
                  gate_context: item.gate_context
                    ? { ...item.gate_context, is_waiting: false, gate: null, display: null, status_kind: null }
                    : null,
                }
              : item
          ))
        }
        return { ...prev, columns: nextColumns }
      })
      setSelectedTaskDetail((prev) => (
        prev && prev.id === taskId
          ? {
              ...prev,
              pending_gate: null,
              gate_context: prev.gate_context
                ? { ...prev.gate_context, is_waiting: false, gate: null, display: null, status_kind: null }
                : null,
            }
          : prev
      ))

      const response = await requestJson<{ task: TaskRecord; message?: string }>(buildApiUrl(`/api/tasks/${taskId}/approve-gate`, projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          gate: gate || undefined,
          action: 'approve',
          generation_policy: gate === 'before_generate_tasks'
            ? {
                child_status: planGenerateChildStatus,
                child_hitl_mode: planGenerateChildHitlMode,
                infer_deps: planGenerateInferDeps,
              }
            : undefined,
          save_generation_policy_as_default: gate === 'before_generate_tasks'
            ? planGenerateSaveAsDefault
            : undefined,
          ...(gate === 'before_post_review' && reviewOpts ? {
            review_decision: reviewOpts.review_decision,
            post_comments: reviewOpts.post_comments,
          } : {}),
        }),
      })
      if (response.message) {
        setTaskActionMessage(response.message)
      }
      await reloadAll()
      if (selectedTaskIdRef.current === taskId) {
        await loadTaskDetail(taskId)
      }
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'Failed to approve gate'
      if (String(detail).toLowerCase().includes('no pending gate')) {
        // Treat stale double-approve as a no-op.
      } else {
        if (previousBoardTask) {
          setBoard((prev) => {
            const nextColumns: Record<string, TaskRecord[]> = {}
            for (const [column, items] of Object.entries(prev.columns || {})) {
              nextColumns[column] = (items || []).map((item) => (
                item.id === taskId
                  ? {
                      ...item,
                      pending_gate: previousBoardTask.pending_gate,
                      gate_context: previousBoardTask.gate_context || null,
                    }
                  : item
              ))
            }
            return { ...prev, columns: nextColumns }
          })
        }
        if (previousSelectedTask) {
          setSelectedTaskDetail((prev) => (
            prev && prev.id === taskId
              ? {
                  ...prev,
                  pending_gate: previousSelectedTask.pending_gate,
                  gate_context: previousSelectedTask.gate_context || null,
                }
              : prev
          ))
        }
        setTaskActionError(detail)
      }
    } finally {
      setTaskActionPending(null)
      setTaskActionDetail('')
    }
  }

  async function refineTaskPlan(taskId: string): Promise<void> {
    if (planJobLoading) return
    const activeJob = selectedTaskPlan?.active_refine_job
    if (activeJob && (activeJob.status === 'queued' || activeJob.status === 'running')) return
    if (!planRefineFeedback.trim()) {
      setPlanActionError('Refine feedback is required.')
      return
    }
    setPlanJobLoading(true)
    setPlanActionMessage('')
    setPlanActionError('')
    try {
      const baseRevisionId = selectedPlanRevisionId || selectedTaskPlan?.latest_revision_id || undefined
      const payload = await requestJson<{ job: PlanRefineJobRecord }>(buildApiUrl(`/api/tasks/${taskId}/plan/refine`, projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          base_revision_id: baseRevisionId,
          feedback: planRefineFeedback.trim(),
          priority: 'normal',
        }),
      })
      setPlanRefineUiState('running')
      setPlanRefineTrackedJobId(payload.job?.id || '')
      planRefineOutputRef.current = { taskId, logId: '', jobKey: '', text: '' }
      setPlanRefineStdout('')
      await loadTaskPlan(taskId)
    } catch (err) {
      setPlanActionError(toErrorMessage('Failed to queue plan refine job', err))
      setPlanRefineUiState('idle')
      setPlanRefineTrackedJobId('')
      setPlanJobLoading(false)
    }
  }

  async function saveManualPlanRevision(taskId: string): Promise<string | undefined> {
    const manualContent = planManualContent.trim()
    if (!manualContent) {
      setPlanActionError('Manual revision content is required.')
      return undefined
    }
    setPlanSavingManual(true)
    setPlanActionMessage('')
    setPlanActionError('')
    try {
      const resp = await requestJson<{ revision: PlanRevisionRecord }>(buildApiUrl(`/api/tasks/${taskId}/plan/revisions`, projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          content: manualContent,
          parent_revision_id: selectedPlanRevisionId || selectedTaskPlan?.latest_revision_id || undefined,
          feedback_note: planManualFeedbackNote.trim() || undefined,
        }),
      })
      setPlanManualContent(resp.revision.content || manualContent)
      planManualSeedRef.current = { taskId, workerText: manualContent }
      setSelectedPlanRevisionId(resp.revision.id)
      setPlanActionMessage('Manual plan revision saved.')
      await loadTaskPlan(taskId)
      return resp.revision.id
    } catch (err) {
      setPlanActionError(toErrorMessage('Failed to save manual plan revision', err))
      return undefined
    } finally {
      setPlanSavingManual(false)
    }
  }

  async function commitPlanRevision(taskId: string, revisionId: string): Promise<void> {
    if (!revisionId) {
      setPlanActionError('Select a plan revision to commit.')
      return
    }
    setPlanCommitting(true)
    setPlanActionMessage('')
    setPlanActionError('')
    try {
      await requestJson<{ committed_revision_id: string }>(buildApiUrl(`/api/tasks/${taskId}/plan/commit`, projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision_id: revisionId }),
      })
      setPlanActionMessage('Committed selected plan revision.')
      await loadTaskPlan(taskId)
    } catch (err) {
      setPlanActionError(toErrorMessage('Failed to commit plan revision', err))
    } finally {
      setPlanCommitting(false)
    }
  }

  async function saveTaskEdits(taskId: string): Promise<void> {
    if (!editTaskTitle.trim()) {
      setError('Task title cannot be empty')
      return
    }
    const parsedStepTimeout = parseOptionalPositiveInt(editTaskStepTimeout)
    if (editTaskStepTimeout.trim() && parsedStepTimeout === null) {
      setError('Task timeout must be a positive number of seconds')
      return
    }
    let pcPayload: Record<string, Record<string, string>> | undefined
    if (editTaskProjectCommands.trim()) {
      try {
        const parsed = parseProjectCommands(editTaskProjectCommands)
        pcPayload = Object.keys(parsed).length > 0 ? parsed : {}
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Invalid project commands JSON')
        return
      }
    } else if (selectedTaskDetail?.project_commands) {
      // Field was cleared — send empty object to signal removal
      pcPayload = {}
    }
    await runTaskMutation(
      'save',
      async () => {
        await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/tasks/${taskId}`, projectDir), {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title: editTaskTitle.trim(),
            description: editTaskDescription,
            task_type: editTaskType,
            priority: editTaskPriority,
            labels: editTaskLabels.split(',').map((item) => item.trim()).filter(Boolean),
            hitl_mode: editTaskHitlMode,
            dependency_policy: editTaskDependencyPolicy,
            step_timeout_seconds: parsedStepTimeout,
            project_commands: pcPayload,
          }),
        })
        await reloadAll()
        await loadTaskDetail(taskId)
      },
      {
        successMessage: 'Task configuration saved.',
        errorPrefix: 'Failed to save task configuration',
        clearEditModeOnSuccess: true,
      },
    )
  }

  function buildWorkerProvidersPayload(extraProviders: Record<string, WorkerProviderSettings>): Record<string, WorkerProviderSettings> {
    const providers: Record<string, WorkerProviderSettings> = {}

    const codexProvider: WorkerProviderSettings = {
      type: 'codex',
      command: settingsCodexCommand.trim() || 'codex exec',
      execution_mode: settingsCodexExecutionMode,
    }
    const codexModel = settingsCodexModel.trim()
    if (codexModel) codexProvider.model = codexModel
    const codexEffort = settingsCodexEffort.trim().toLowerCase()
    if (codexEffort === 'low' || codexEffort === 'medium' || codexEffort === 'high') {
      codexProvider.reasoning_effort = codexEffort
    }
    providers.codex = codexProvider

    const claudeProvider: WorkerProviderSettings = {
      type: 'claude',
      command: settingsClaudeCommand.trim() || 'claude -p',
      execution_mode: settingsClaudeExecutionMode,
    }
    const claudeModel = settingsClaudeModel.trim()
    if (claudeModel) claudeProvider.model = claudeModel
    const claudeEffort = settingsClaudeEffort.trim().toLowerCase()
    if (claudeEffort === 'low' || claudeEffort === 'medium' || claudeEffort === 'high') {
      claudeProvider.reasoning_effort = claudeEffort
    }
    providers.claude = claudeProvider

    const ollamaEndpoint = settingsOllamaEndpoint.trim()
    const ollamaModel = settingsOllamaModel.trim()
    const shouldConfigureOllama = Boolean(ollamaModel) || settingsWorkerDefault === 'ollama'
    if (shouldConfigureOllama) {
      if (!ollamaEndpoint || !ollamaModel) {
        throw new Error('Ollama provider requires endpoint and model')
      }
      const ollamaProvider: WorkerProviderSettings = {
        type: 'ollama',
        endpoint: ollamaEndpoint,
        model: ollamaModel,
      }
      const temperature = Number(settingsOllamaTemperature)
      if (settingsOllamaTemperature.trim() && Number.isFinite(temperature)) {
        ollamaProvider.temperature = temperature
      }
      const numCtx = Number(settingsOllamaNumCtx)
      if (settingsOllamaNumCtx.trim() && Number.isFinite(numCtx) && numCtx > 0) {
        ollamaProvider.num_ctx = Math.floor(numCtx)
      }
      providers.ollama = ollamaProvider
    }

    for (const [name, provider] of Object.entries(extraProviders)) {
      providers[name] = provider
    }
    return providers
  }

  async function saveSettings(event: { preventDefault(): void }): Promise<void> {
    event.preventDefault()
    setSettingsSaving(true)
    setSettingsError('')
    setSettingsSuccess('')
    try {
      const taskTypeRoles = parseStringMap(settingsTaskTypeRoles, 'Task type role map')
      const roleProviderOverrides = parseStringMap(settingsRoleProviderOverrides, 'Role provider overrides')
      const workerRouting = Object.fromEntries(
        workerRoutingRows
          .filter((row) => row.source === 'explicit' && row.step.trim() && row.provider.trim())
          .map((row) => [row.step.trim(), row.provider.trim()])
      )
      let advancedWorkerProviders: Record<string, WorkerProviderSettings> = {}
      try {
        advancedWorkerProviders = parseWorkerProviders(settingsWorkerProviders)
      } catch {
        advancedWorkerProviders = {}
      }
      const workerProviders = buildWorkerProvidersPayload(advancedWorkerProviders)
      const projectCommands = parseProjectCommands(settingsProjectCommands)
      const promptOverridesPayload: Record<string, string> = {}
      for (const [rawStep, rawPrompt] of Object.entries(settingsPromptOverrides)) {
        const step = String(rawStep || '').trim().toLowerCase()
        if (!step || typeof rawPrompt !== 'string' || !rawPrompt.trim()) continue
        promptOverridesPayload[step] = rawPrompt
      }
      const removedOverrideSteps = new Set(
        Object.keys(settingsPromptOverridesBaseline)
          .map((step) => step.trim().toLowerCase())
          .filter((step) => step && !(step in promptOverridesPayload))
      )
      for (const rawStep of Object.keys(settingsPromptOverrideClears)) {
        const step = rawStep.trim().toLowerCase()
        if (!step) continue
        removedOverrideSteps.add(step)
      }
      for (const step of removedOverrideSteps) {
        if (!step || (step in promptOverridesPayload)) continue
        promptOverridesPayload[step] = ''
      }
      const promptInjectionsPayload: Record<string, string> = {}
      for (const [rawStep, rawPrompt] of Object.entries(settingsLegacyPromptInjections)) {
        const step = String(rawStep || '').trim().toLowerCase()
        if (!step) continue
        if (typeof rawPrompt !== 'string') continue
        const trimmedInjection = rawPrompt.trim()
        if (!trimmedInjection) continue
        const existingOverride = promptOverridesPayload[step]
        if (!removedOverrideSteps.has(step)) {
          if (!existingOverride || !existingOverride.trim()) {
            promptOverridesPayload[step] = rawPrompt
          } else if (!existingOverride.includes(trimmedInjection)) {
            promptOverridesPayload[step] = `${existingOverride.trimEnd()}\n\n${rawPrompt}`
          }
        }
        promptInjectionsPayload[step] = ''
      }
      const payload = {
        orchestrator: {
          concurrency: Math.max(1, parseNonNegativeInt(settingsConcurrency, DEFAULT_SETTINGS.orchestrator.concurrency)),
          auto_deps: settingsAutoDeps,
          auto_fix_push: settingsAutoFixPush,
          max_review_attempts: Math.max(1, parseNonNegativeInt(settingsMaxReviewAttempts, DEFAULT_SETTINGS.orchestrator.max_review_attempts)),
          max_merge_conflict_attempts: Math.min(
            10,
            Math.max(
              1,
              parseNonNegativeInt(
                settingsMaxMergeConflictAttempts,
                DEFAULT_SETTINGS.orchestrator.max_merge_conflict_attempts,
              ),
            ),
          ),
          step_timeout_seconds: Math.min(
            7200,
            Math.max(0, parseNonNegativeInt(settingsStepTimeoutSeconds, DEFAULT_SETTINGS.orchestrator.step_timeout_seconds)),
          ),
        },
        agent_routing: {
          default_role: settingsDefaultRole.trim() || 'general',
          task_type_roles: taskTypeRoles,
          role_provider_overrides: roleProviderOverrides,
        },
        defaults: {
          quality_gate: {
            critical: parseNonNegativeInt(settingsGateCritical, 0),
            high: parseNonNegativeInt(settingsGateHigh, 0),
            medium: parseNonNegativeInt(settingsGateMedium, 0),
            low: parseNonNegativeInt(settingsGateLow, 0),
          },
          dependency_policy: settingsDependencyPolicy,
          hitl_mode: settingsDefaultHitlMode,
        },
        workers: {
          default: settingsWorkerDefault || 'codex',
          default_model: '',
          routing: workerRouting,
          providers: workerProviders,
        },
        project: {
          commands: projectCommands,
          prompt_overrides: promptOverridesPayload,
          prompt_injections: promptInjectionsPayload,
        },
      }
      const updated = await requestJson<Partial<SystemSettings>>(buildApiUrl('/api/settings', projectDir), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      applySettings(normalizeSettings(updated))
      setSettingsSuccess('Settings saved.')
      setProviderConfigDirty(false)
      setQualityGateDirty(false)
      setExecutionFormDirty(false)
      setAdvancedRoutingDirty(false)
      setProjectCommandsDirty(false)
      setAdvancedFormDirty(false)
      await reloadAll()
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'unknown error'
      setSettingsError(`Failed to save settings (${detail})`)
    } finally {
      setSettingsSaving(false)
    }
  }

  async function toggleDisabledCommand(key: string): Promise<void> {
    const next = new Set(disabledCommands)
    if (next.has(key)) {
      next.delete(key)
    } else {
      next.add(key)
    }
    setDisabledCommands(next)
    try {
      await requestJson(buildApiUrl('/api/settings', projectDir), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: { disabled_commands: [...next] } }),
      })
    } catch {
      // revert on failure
      setDisabledCommands(disabledCommands)
    }
  }

  async function saveWorkerMaps(event: FormEvent): Promise<void> {
    event.preventDefault()
    setSettingsSaving(true)
    setSettingsError('')
    setSettingsSuccess('')
    try {
      const workerRouting = parseStringMap(settingsWorkerRouting, 'Worker routing map')
      const advancedWorkerProviders = parseWorkerProviders(settingsWorkerProviders)
      const workerProviders = buildWorkerProvidersPayload(advancedWorkerProviders)
      const updated = await requestJson<Partial<SystemSettings>>(buildApiUrl('/api/settings', projectDir), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          workers: {
            default: settingsWorkerDefault || 'codex',
            default_model: '',
            routing: workerRouting,
            providers: workerProviders,
          },
        }),
      })
      applySettings(normalizeSettings(updated))
      setSettingsSuccess('Worker routing saved.')
      await reloadAll()
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'unknown error'
      setSettingsError(`Failed to save worker routing (${detail})`)
    } finally {
      setSettingsSaving(false)
    }
  }

  async function patchSettings(partial: Record<string, unknown>): Promise<void> {
    try {
      await requestJson<Partial<SystemSettings>>(buildApiUrl('/api/settings', projectDir), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(partial),
      })
    } catch {
      void loadSettings()
    }
  }

  async function setDefaultProvider(name: string): Promise<void> {
    setSettingsWorkerDefault(name)
    void patchSettings({ workers: { default: name } })
  }

  async function handleRecheckProviders(): Promise<void> {
    setWorkerHealthRefreshing(true)
    try {
      await refreshAgentsSurface()
    } finally {
      setWorkerHealthRefreshing(false)
    }
  }

  function updateWorkerRoute(step: string, provider: string): void {
    try {
      const current = parseStringMap(settingsWorkerRouting, 'Worker routing map')
      const next = { ...current }
      const normalizedStep = step.trim()
      if (!normalizedStep) return
      if (!provider.trim() || provider.trim() === workerDefaultProvider) {
        delete next[normalizedStep]
      } else {
        next[normalizedStep] = provider.trim()
      }
      const nextJson = Object.keys(next).length > 0 ? JSON.stringify(next, null, 2) : ''
      setSettingsWorkerRouting(nextJson)
      setSettingsError('')
      // Auto-save routing change
      void requestJson<Partial<SystemSettings>>(buildApiUrl('/api/settings', projectDir), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ workers: { routing: next } }),
      }).catch(() => void loadSettings())
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'invalid routing JSON'
      setSettingsError(detail)
    }
  }

  function handleFormatJsonField(
    label: string,
    value: string,
    setter: (next: string) => void,
  ): void {
    try {
      setter(formatJsonObjectInput(value, label))
      setSettingsError('')
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'invalid JSON'
      setSettingsError(detail)
    }
  }

  async function controlOrchestrator(action: 'pause' | 'resume' | 'drain' | 'stop'): Promise<void> {
    try {
      await requestJson<OrchestratorStatus>(buildApiUrl('/api/orchestrator/control', projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      })
      await reloadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} orchestrator`)
    }
  }

  async function retryTask(taskId: string, startFromStep?: string): Promise<void> {
    await runTaskMutation(
      'retry',
      async () => {
        await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/tasks/${taskId}/retry`, projectDir), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            guidance: reviewGuidance.trim() || undefined,
            start_from_step: startFromStep || undefined,
            worker_provider: retryProvider.trim() || undefined,
          }),
        })
        setReviewGuidance('')
        setRetryFromStep('')
        setRetryProvider('')
        await reloadAll()
        if (selectedTaskIdRef.current === taskId) {
          await loadTaskDetail(taskId)
        }
      },
      {
        successMessage: startFromStep ? `Task re-queued from ${startFromStep}.` : 'Task re-queued.',
        errorPrefix: 'Failed to retry task',
      },
    )
  }

  async function reviewCommit(taskId: string): Promise<void> {
    await runTaskMutation(
      'review_commit',
      async () => {
        const result = await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/tasks/${taskId}/review-commit`, projectDir), {
          method: 'POST',
        })
        await reloadAll()
        if (result.task?.id) {
          modalDismissedRef.current = false
          modalExplicitRef.current = true
          setSelectedTaskId(result.task.id)
          await loadTaskDetail(result.task.id)
        }
      },
      {
        successMessage: 'Commit review task created.',
        errorPrefix: 'Failed to create commit review',
      },
    )
  }

  async function generateFollowUpTasks(taskId: string): Promise<void> {
    await runTaskMutation(
      'generate_follow_ups',
      async () => {
        await requestJson<{ task: TaskRecord; created_task_ids: string[]; children: TaskRecord[] }>(
          buildApiUrl(`/api/tasks/${taskId}/generate-tasks`, projectDir),
          { method: 'POST', body: JSON.stringify({ source: 'latest' }) },
        )
        await reloadAll()
        if (selectedTaskId === taskId) {
          await loadTaskDetail(taskId)
        }
      },
      {
        successMessage: 'Follow-up tasks generated.',
        errorPrefix: 'Failed to generate follow-up tasks',
      },
    )
  }

  async function postReviewComments(taskId: string): Promise<void> {
    setPostCommentsError(null)
    setPostCommentsErrorCode(null)
    setTaskActionPending('post_review_comments')
    setTaskActionDetail('')
    setTaskActionError('')
    setTaskActionMessage('')
    try {
      // Build selective payload from current selection state
      const genComments = selectedTaskDetail?.metadata?.generated_review_comments as Array<{ body?: string; post_status?: string }> | undefined
      const comments: Array<{ index: number; body?: string }> = []
      for (const idx of selectedCommentIndices) {
        const c = genComments?.[idx]
        if (!c || (c.post_status ?? 'staged') === 'posted') continue
        const editedBody = editedCommentBodies.get(idx)
        comments.push(editedBody !== undefined ? { index: idx, body: editedBody } : { index: idx })
      }
      if (comments.length === 0) return
      await requestJson<{ posted_count: number; failed_count: number }>(
        buildApiUrl(`/api/tasks/${taskId}/post-review-comments`, projectDir),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ comments }),
        },
      )
      await loadTaskDetail(taskId)
      // Clear edited bodies for posted comments
      setEditedCommentBodies((prev) => {
        const next = new Map(prev)
        for (const c of comments) next.delete(c.index)
        return next
      })
      setEditingCommentIndex(null)
      setTaskActionMessage('Review comments posted.')
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to post review comments'
      setTaskActionError(toErrorMessage('Failed to post review comments', err))
      // Use structured error_code from backend when available.
      const apiErr = err as Partial<ApiRequestError>
      const code = apiErr.code ?? 'cli_error'
      setPostCommentsError(errorMsg)
      setPostCommentsErrorCode(code)
    } finally {
      setTaskActionPending(null)
      setTaskActionDetail('')
    }
  }

  async function skipToPrecommit(taskId: string): Promise<void> {
    await runTaskMutation(
      'transition',
      async () => {
        await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/tasks/${taskId}/skip-to-precommit`, projectDir), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ guidance: reviewGuidance.trim() || undefined }),
        })
        setReviewGuidance('')
        await reloadAll()
        if (selectedTaskIdRef.current === taskId) {
          await loadTaskDetail(taskId)
        }
      },
      {
        successMessage: 'Task moved to pre-commit review.',
        errorPrefix: 'Failed to skip to pre-commit',
        detail: 'skip_to_precommit',
      },
    )
  }

  async function finalizeMergeConflict(taskId: string): Promise<void> {
    await runTaskMutation(
      'transition',
      async () => {
        await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/tasks/${taskId}/finalize-merge-conflict`, projectDir), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ guidance: reviewGuidance.trim() || undefined }),
        })
        setReviewGuidance('')
        await reloadAll()
        if (selectedTaskIdRef.current === taskId) {
          await loadTaskDetail(taskId)
        }
      },
      {
        successMessage: 'Task finalized after manual merge verification.',
        errorPrefix: 'Failed to finalize manual merge',
        detail: 'finalize_merge_conflict',
      },
    )
  }

  async function approveTask(taskId: string): Promise<void> {
    await runTaskMutation(
      'transition',
      async () => {
        await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/review/${taskId}/approve`, projectDir), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ guidance: reviewGuidance.trim() || undefined }),
        })
        setReviewGuidance('')
        await reloadAll()
        if (selectedTaskIdRef.current === taskId) {
          await loadTaskDetail(taskId)
        }
      },
      {
        successMessage: 'Task approved.',
        errorPrefix: 'Failed to approve task',
        detail: 'done',
      },
    )
  }

  async function requestChanges(taskId: string): Promise<void> {
    await runTaskMutation(
      'transition',
      async () => {
        await requestJson<{ task: TaskRecord }>(buildApiUrl(`/api/review/${taskId}/request-changes`, projectDir), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ guidance: reviewGuidance.trim() || undefined }),
        })
        setReviewGuidance('')
        await reloadAll()
        if (selectedTaskIdRef.current === taskId) {
          await loadTaskDetail(taskId)
        }
      },
      {
        successMessage: 'Changes requested. Task re-queued.',
        errorPrefix: 'Failed to request changes',
        detail: 'request_changes',
      },
    )
  }

  async function requestGateChanges(taskId: string, gate?: string | null): Promise<void> {
    const guidance = gateRequestGuidance.trim()
    setTaskActionPending('approve_gate')
    setTaskActionDetail(`request:${String(gate || '')}`)
    setTaskActionError('')
    try {
      const response = await requestJson<{ task: TaskRecord; message?: string }>(buildApiUrl(`/api/tasks/${taskId}/approve-gate`, projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          gate: gate || undefined,
          action: 'request_changes',
          guidance,
        }),
      })
      setGateRequestGuidance('')
      setGateRequestOpen(false)
      if (response.message) {
        setTaskActionMessage(response.message)
      }
      await reloadAll()
      if (selectedTaskIdRef.current === taskId) {
        await loadTaskDetail(taskId)
      }
    } catch (err) {
      setTaskActionError(toErrorMessage('Failed to request changes', err))
    } finally {
      setTaskActionPending(null)
      setTaskActionDetail('')
    }
  }

  async function pinProjectPath(path: string, allowNonGitValue: boolean): Promise<void> {
    try {
      const pinned = await requestJson<{ project: ProjectRef }>(buildApiUrl('/api/projects/pinned', projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, allow_non_git: allowNonGitValue }),
      })
      setProjectDir(pinned.project?.path || path)
      await reloadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to pin project')
    }
  }

  async function unpinProject(projectId: string): Promise<void> {
    try {
      await requestJson<{ removed: boolean }>(buildApiUrl(`/api/projects/pinned/${projectId}`, projectDir), {
        method: 'DELETE',
      })
      await reloadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to unpin project')
    }
  }

  async function pinManualProject(event: FormEvent): Promise<void> {
    event.preventDefault()
    if (!manualPinPath.trim()) return
    await pinProjectPath(manualPinPath.trim(), allowNonGit)
    setManualPinPath('')
  }

  async function handleTopbarProjectChange(nextValue: string): Promise<void> {
    if (nextValue === ADD_REPO_VALUE) {
      setBrowseOpen(true)
      void loadBrowseDirectories()
      return
    }
    setProjectDir(nextValue)
  }

  async function loadBrowseDirectories(nextPath?: string): Promise<void> {
    setBrowseLoading(true)
    setBrowseError('')
    try {
      const data = await requestJson<BrowseProjectsResponse>(
        buildApiUrl('/api/projects/browse', projectDir, nextPath ? { path: nextPath } : {})
      )
      setBrowsePath(data.path)
      setBrowseParentPath(data.parent)
      setBrowseCurrentIsGit(data.current_is_git)
      setBrowseDirectories(data.directories)
    } catch (err) {
      setBrowseError(err instanceof Error ? err.message : 'Failed to browse directories')
    } finally {
      setBrowseLoading(false)
    }
  }

  async function pinFromBrowse(): Promise<void> {
    if (!browsePath) return
    await pinProjectPath(browsePath, browseAllowNonGit)
    setBrowseOpen(false)
  }

  function handleRouteChange(nextRoute: RouteKey): void {
    modalDismissedRef.current = false
    window.location.hash = toHash(nextRoute)
    setRoute(nextRoute)
  }

  function handleTaskSelect(taskId: string, defaultTab?: TaskDetailTab): void {
    modalDismissedRef.current = false
    modalExplicitRef.current = true
    taskSelectTabRef.current = defaultTab
    setSelectedTaskId(taskId)
  }

  const boardColumns = ['backlog', 'queued', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled']
  const allBoardTasks = boardColumns.flatMap((column) => board.columns[column] || [])
  const taskIndex = new Map<string, TaskRecord>()
  for (const task of allBoardTasks) {
    taskIndex.set(task.id, task)
  }
  if (selectedTaskDetail) {
    taskIndex.set(selectedTaskDetail.id, selectedTaskDetail)
  }
  const selectedTask = allBoardTasks.find((task) => task.id === selectedTaskId) ?? (selectedTaskId ? undefined : allBoardTasks[0])
  const selectedTaskView = selectedTaskDetail && selectedTaskDetail.id === selectedTaskId ? selectedTaskDetail : selectedTask
  const blockerIds = selectedTaskView?.blocked_by || []
  const blockedIds = selectedTaskView?.blocks || []
  const selectedTaskSupportsGeneration = taskSupportsGenerateTasks(selectedTaskView)
  const isTaskActionBusy = taskActionPending !== null
  const configLocked = !new Set(['backlog', 'queued', 'blocked', 'cancelled']).has(selectedTaskView?.status || '')
  const taskStatus = selectedTaskView?.status || ''
  const selectedTaskStatusDisplay = taskStatusDisplay(selectedTaskView)
  const unresolvedBlockers = blockerIds.filter((depId) => {
    const dep = taskIndex.get(depId)
    return !dep || (dep.status !== 'done' && dep.status !== 'cancelled')
  })
  const hasUnresolvedBlockers = unresolvedBlockers.length > 0
  const showPlanGate = !!(
    selectedTaskView
    && selectedTaskView.status !== 'cancelled'
    && (selectedTaskView.pending_gate === 'before_implement' || selectedTaskView.pending_gate === 'before_generate_tasks')
  )
  const showTopLevelGateBanner = !!(
    selectedTaskView
    && selectedTaskView.status !== 'cancelled'
    && selectedTaskView.pending_gate
  )
  const showChangesTab = !!(
    selectedTaskView
    && !isPlanningTaskType(selectedTaskView.task_type)
    && (
      selectedTaskView.status === 'in_review'
      || selectedTaskView.status === 'done'
      || selectedTaskView.status === 'blocked'
      || selectedTaskView.pending_gate === 'before_implement'
      || selectedTaskView.pending_gate === 'before_commit'
      || selectedTaskView.pending_gate === 'before_done'
    )
  )
  const showPlanTab = !!selectedTaskView && (
    (selectedTaskPlan?.revisions?.length ?? 0) > 0 ||
    showPlanGate
  )
  // Guard: if the Plan tab is selected but no longer visible, fall back to overview.
  const effectiveTaskDetailTab = (taskDetailTab === 'plan' && !showPlanTab) ? 'overview' : taskDetailTab
  const selectedTaskTotalSeconds = taskTotalDurationSeconds(selectedTaskView?.timing_summary, taskDetailNowMs)

  useEffect(() => {
    const timingSummary = selectedTaskView?.timing_summary
    if (!selectedTaskId || !timingSummary?.is_running || !timingSummary.active_run_started_at) return
    setTaskDetailNowMs(Date.now())
    const timer = window.setInterval(() => setTaskDetailNowMs(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [selectedTaskId, selectedTaskView?.id, selectedTaskView?.timing_summary?.is_running, selectedTaskView?.timing_summary?.active_run_started_at])

  async function selectPipelineForTask(taskId: string, pipelineId: string): Promise<void> {
    try {
      await requestJson(buildApiUrl(`/api/tasks/${taskId}/select-pipeline`, projectDir), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pipeline_id: pipelineId }),
      })
      setGatePipelineSelection('')
      await reloadAll()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to select pipeline')
    }
  }

  const renderPendingGateBanner = (task: TaskRecord): JSX.Element => {
    if (task.pending_gate === 'pipeline_classify') {
      return (
        <div className="preview-box plan-gate-banner plan-gate-banner-compact">
          <div className="plan-gate-header-row">
            <p className="field-label">Classifying pipeline&hellip; The task will become eligible for execution once classification completes.</p>
          </div>
        </div>
      )
    }
    if (task.pending_gate === 'select_pipeline') {
      const allowedPipelines: string[] = (task.metadata as Record<string, unknown>)?.classification_allowed_pipelines as string[] || []
      const reason = String((task.metadata as Record<string, unknown>)?.classifier_reason || '').trim()
      return (
        <div className="preview-box plan-gate-banner plan-gate-banner-compact">
          <div className="plan-gate-header-row">
            <p className="field-label">
              <strong>Select pipeline</strong>
              {reason ? <> &mdash; {reason}</> : null}
            </p>
          </div>
          <div className="inline-actions plan-gate-actions">
            <select
              value={gatePipelineSelection}
              onChange={(event) => setGatePipelineSelection(event.target.value)}
            >
              <option value="">Choose pipeline&hellip;</option>
              {allowedPipelines.map((pid) => (
                <option key={pid} value={pid}>{humanizeLabel(pid)}</option>
              ))}
            </select>
            <button
              className="button button-primary"
              onClick={() => void selectPipelineForTask(task.id, gatePipelineSelection)}
              disabled={!gatePipelineSelection}
            >
              Confirm
            </button>
          </div>
        </div>
      )
    }
    if (task.pending_gate === 'before_post_review') {
      const meta = (task.metadata || {}) as Record<string, unknown>
      const rawProposedDecision = meta.proposed_review_decision as string | undefined
      const proposedDecision = rawProposedDecision ? String(rawProposedDecision) : null
      const reviewSummary = String(meta.review_summary || '').trim()
      const commentsPreview = meta.review_comments_preview
      const commentsList = Array.isArray(commentsPreview) ? commentsPreview as Array<Record<string, unknown>> : []
      const addressedList = commentsPreview && typeof commentsPreview === 'object' && !Array.isArray(commentsPreview)
        ? (commentsPreview as Record<string, unknown>).addressed as Array<Record<string, unknown>> | undefined
        : undefined
      return (
        <div className="preview-box plan-gate-banner">
          <div className="plan-gate-header-row">
            <p className="field-label plan-gate-title-inline">
              <strong>Review complete</strong> <span aria-hidden="true">&middot;</span> Review before posting
            </p>
          </div>
          {reviewSummary ? (
            <div className="gate-review-summary" style={{ margin: '0.5rem 0', padding: '0.5rem', background: 'var(--bg-secondary, #f5f5f5)', borderRadius: '4px', fontSize: '0.9em' }}>
              <strong>Summary:</strong> {reviewSummary}
            </div>
          ) : null}
          {commentsList.length > 0 ? (
            <details style={{ margin: '0.5rem 0' }}>
              <summary style={{ cursor: 'pointer', fontWeight: 500 }}>
                {commentsList.length} review comment{commentsList.length !== 1 ? 's' : ''} generated
              </summary>
              <div style={{ maxHeight: '300px', overflow: 'auto', marginTop: '0.25rem' }}>
                {commentsList.map((c, i) => (
                  <div key={i} style={{ padding: '0.25rem 0', borderBottom: '1px solid var(--border-color, #e0e0e0)', fontSize: '0.85em' }}>
                    <code>{String(c.path || '')}:{String(c.line || '')}</code>
                    {c.severity ? <span style={{ marginLeft: '0.5rem', fontWeight: 600, textTransform: 'uppercase', fontSize: '0.75em' }}>[{String(c.severity)}]</span> : null}
                    <p style={{ margin: '0.25rem 0 0 0' }}>{String(c.body || '')}</p>
                  </div>
                ))}
              </div>
            </details>
          ) : addressedList && addressedList.length > 0 ? (
            <details style={{ margin: '0.5rem 0' }}>
              <summary style={{ cursor: 'pointer', fontWeight: 500 }}>
                {addressedList.length} comment response{addressedList.length !== 1 ? 's' : ''} drafted
              </summary>
              <div style={{ maxHeight: '300px', overflow: 'auto', marginTop: '0.25rem' }}>
                {addressedList.map((c, i) => (
                  <div key={i} style={{ padding: '0.25rem 0', borderBottom: '1px solid var(--border-color, #e0e0e0)', fontSize: '0.85em' }}>
                    <strong>#{String(c.comment_id || '')}</strong>
                    <p style={{ margin: '0.25rem 0 0 0' }}>{String(c.response_body || c.fix_description || '')}</p>
                  </div>
                ))}
              </div>
            </details>
          ) : (
            <p className="text-muted" style={{ margin: '0.5rem 0', fontSize: '0.9em' }}>No review comments generated.</p>
          )}
          <div style={{ display: 'flex', gap: '1rem', alignItems: 'center', margin: '0.5rem 0' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '0.35rem' }}>
              <span className="field-label" style={{ margin: 0 }}>Decision:</span>
              <select
                value={gateReviewDecision}
                onChange={(e) => setGateReviewDecision(e.target.value as ReviewDecisionType)}
                disabled={taskActionPending !== null}
              >
                {REVIEW_DECISION_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </label>
            <label className="checkbox-row" style={{ margin: 0 }}>
              <input
                type="checkbox"
                checked={gatePostComments}
                onChange={(e) => setGatePostComments(e.target.checked)}
                disabled={taskActionPending !== null}
              />
              Post to PR/MR
            </label>
          </div>
          {proposedDecision ? (
            <p className="text-muted" style={{ fontSize: '0.8em', margin: '0 0 0.5rem 0' }}>
              AI proposed: <strong>{proposedDecision}</strong>
            </p>
          ) : null}
          <div className="inline-actions plan-gate-actions">
            <button
              className="button button-primary"
              onClick={() => void approveGate(task.id, task.pending_gate, { review_decision: gateReviewDecision, post_comments: gatePostComments })}
              disabled={taskActionPending === 'approve_gate'}
            >
              {taskActionPending === 'approve_gate' && taskActionDetail.startsWith('approve:')
                ? 'Posting...'
                : gatePostComments ? 'Post review' : 'Stage review'}
            </button>
            {!gateRequestOpen ? (
              <button
                className="button"
                onClick={() => setGateRequestOpen(true)}
                disabled={taskActionPending !== null}
              >
                Refine review
              </button>
            ) : (
              <>
                <input
                  className="review-guidance-input"
                  value={gateRequestGuidance}
                  onChange={(event) => setGateRequestGuidance(event.target.value)}
                  placeholder="What should be refined?"
                  disabled={taskActionPending !== null}
                />
                <button
                  className="button"
                  onClick={() => void requestGateChanges(task.id, task.pending_gate)}
                  disabled={taskActionPending !== null}
                >
                  Re-review
                </button>
                <button className="button" onClick={() => { setGateRequestOpen(false); setGateRequestGuidance('') }}>Cancel</button>
              </>
            )}
          </div>
        </div>
      )
    }
    return (
    <div className="preview-box plan-gate-banner plan-gate-banner-compact">
      <div className="plan-gate-header-row">
        {task.pending_gate === 'before_generate_tasks' ? (
          <p className="field-label plan-gate-title-inline"><strong>Plan ready to generate tasks</strong> <span aria-hidden="true">·</span> Pending gate</p>
        ) : (
          <p className="field-label">Pending gate: <strong>{gateDisplayLabel(task)}</strong></p>
        )}
      </div>
      {task.pending_gate === 'before_generate_tasks' ? (
        selectedTaskSupportsGeneration ? (
          <div className="plan-gate-policy-line" role="group" aria-label="Generation policy">
            <label className="plan-gate-policy-item" htmlFor="gate-quick-status">
              <span className="plan-gate-policy-label">Start:</span>
              <select
                id="gate-quick-status"
                aria-label="Start"
                className="plan-gate-inline-select"
                value={planGenerateChildStatus}
                onChange={(event) => setPlanGenerateChildStatus(normalizeGeneratedTaskStatus(event.target.value))}
                disabled={taskActionPending !== null}
              >
                <option value="backlog">Backlog</option>
                <option value="queued">Queue</option>
              </select>
            </label>
            <span className="plan-gate-policy-item">
              <span className="plan-gate-policy-label">Mode:</span>
            </span>
            <div className="plan-gate-policy-item">
              <HITLModeSelector
                currentMode={planGenerateChildHitlMode}
                onModeChange={(mode) => setPlanGenerateChildHitlMode(normalizeGeneratedTaskHitlSelection(mode))}
                projectDir={projectDir}
                allowInheritParent
                inheritParentLabel="Inherit parent"
                inheritParentDescription="Use the same HITL mode as the parent task."
                compact
                disabled={taskActionPending !== null}
              />
            </div>
          </div>
        ) : (
          <p className="field-label">This pipeline does not support task generation.</p>
        )
      ) : null}
      <div className="inline-actions plan-gate-actions">
        <button
          className="button button-primary"
          onClick={() => void approveGate(task.id, task.pending_gate)}
          disabled={taskActionPending === 'approve_gate'}
        >
          {taskActionPending === 'approve_gate' && taskActionDetail.startsWith('approve:')
            ? 'Approving...'
            : gateApprovalButtonLabel(task.pending_gate)}
        </button>
        {!gateRequestOpen ? (
          <button
            className="button"
            onClick={() => setGateRequestOpen(true)}
            disabled={taskActionPending !== null}
          >
            {String(task.pending_gate || '') === 'before_done' ? 'Refine' : 'Request changes'}
          </button>
        ) : (
          <>
            <input
              className="review-guidance-input"
              value={gateRequestGuidance}
              onChange={(event) => setGateRequestGuidance(event.target.value)}
              placeholder={String(task.pending_gate || '') === 'before_done' ? 'What should be refined?' : 'Guidance for changes...'}
              disabled={taskActionPending !== null}
            />
            <button
              className="button"
              onClick={() => void requestGateChanges(task.id, task.pending_gate)}
              disabled={taskActionPending !== null}
            >
              {taskActionPending === 'approve_gate' && taskActionDetail === `request:${String(task.pending_gate || '')}`
                ? 'Requesting...'
                : 'Submit'}
            </button>
            <button
              className="button button-ghost"
              onClick={() => {
                setGateRequestOpen(false)
                setGateRequestGuidance('')
              }}
              disabled={taskActionPending !== null}
            >
              Cancel
            </button>
          </>
        )}
      </div>
    </div>
  )}

  const taskDetailContent = selectedTaskView ? (
      <div className="detail-card">
        {selectedTaskDetailLoading ? <p className="field-label">Loading full task detail...</p> : null}
        <p className="task-meta"><span className="task-id-chip" title={selectedTaskView.id} onClick={() => { void navigator.clipboard.writeText(selectedTaskView.id) }} role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); void navigator.clipboard.writeText(selectedTaskView.id) } }}>{selectedTaskView.id.replace(/^task-/, '')}</span> · {humanizeLabel(normalizeHitlMode(selectedTaskView.hitl_mode || 'supervised'))} · {selectedTaskView.priority} · {humanizeLabel(selectedTaskView.task_type || 'feature')}{(() => { const ch = selectedTaskView.execution_summary?.steps?.map(s => s.commit).filter(Boolean).pop(); return ch ? <>{' · '}<span className="execution-step-commit" title={`Click to copy: ${ch}`} onClick={(e) => { e.stopPropagation(); void navigator.clipboard.writeText(ch) }} role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); void navigator.clipboard.writeText(ch) } }}>{ch.slice(0, 8)}</span></> : null })()}</p>
        {selectedTaskTotalSeconds != null ? (
          <p className="task-meta">
            Total time taken: {formatDuration(selectedTaskTotalSeconds) || '0s'}
            {selectedTaskView.timing_summary?.is_running ? ' · running' : ''}
          </p>
        ) : null}
        {showTopLevelGateBanner ? renderPendingGateBanner(selectedTaskView) : null}
        <div className="detail-tabs" role="tablist" aria-label="Task detail sections">
          <button
            className={`detail-tab ${effectiveTaskDetailTab === 'overview' ? 'is-active' : ''}`}
            aria-pressed={effectiveTaskDetailTab === 'overview'}
            onClick={() => setTaskDetailTab('overview')}
          >
            Overview
          </button>
          {showPlanTab ? (
            <button
              className={`detail-tab ${effectiveTaskDetailTab === 'plan' ? 'is-active' : ''}`}
              aria-pressed={effectiveTaskDetailTab === 'plan'}
              onClick={() => setTaskDetailTab('plan')}
            >
              Plan
            </button>
          ) : null}
          <button
            className={`detail-tab ${taskDetailTab === 'workdoc' ? 'is-active' : ''}`}
            aria-pressed={taskDetailTab === 'workdoc'}
            onClick={() => setTaskDetailTab('workdoc')}
          >
            Workdoc
          </button>
          <button
            className={`detail-tab ${taskDetailTab === 'logs' ? 'is-active' : ''}`}
            aria-pressed={taskDetailTab === 'logs'}
            onClick={() => setTaskDetailTab('logs')}
          >
            Logs
          </button>
          <button
            className={`detail-tab ${taskDetailTab === 'activity' ? 'is-active' : ''}`}
            aria-pressed={taskDetailTab === 'activity'}
            onClick={() => setTaskDetailTab('activity')}
          >
            Activity
          </button>
          <button
            className={`detail-tab ${taskDetailTab === 'dependencies' ? 'is-active' : ''}`}
            aria-pressed={taskDetailTab === 'dependencies'}
            onClick={() => setTaskDetailTab('dependencies')}
          >
            Dependencies
          </button>
          <button
            className={`detail-tab ${taskDetailTab === 'configuration' ? 'is-active' : ''}`}
            aria-pressed={taskDetailTab === 'configuration'}
            onClick={() => setTaskDetailTab('configuration')}
          >
            Configuration
          </button>
          {showChangesTab ? (
            <button
              className={`detail-tab ${taskDetailTab === 'changes' ? 'is-active' : ''}`}
              aria-pressed={taskDetailTab === 'changes'}
              onClick={() => { setTaskDetailTab('changes'); if (!taskDiff && !taskDiffLoading) { void fetchTaskDiff(selectedTaskView.id) } }}
            >
              Changes
            </button>
          ) : null}
        </div>
        {effectiveTaskDetailTab === 'overview' ? (
          <div className="task-detail-section-body">
            {selectedTaskView.description ? <RenderedMarkdown content={selectedTaskView.description} className="task-desc" /> : <p className="task-desc">No description.</p>}
            <p className="field-label">
              {'Depends on: '}
              {blockerIds.length > 0 ? (
                blockerIds.map((depId, idx) => {
                  const dep = describeTask(depId, taskIndex)
                  const resolved = dep.status === 'done' || dep.status === 'cancelled'
                  return (
                    <span key={`dep-inline-${depId}`}>
                      {idx > 0 ? ', ' : ''}
                      <button className="link-button" onClick={() => handleTaskSelect(depId)}>{dep.label}</button>
                      <span className={`status-pill status-pill-inline ${resolved ? 'status-running' : 'status-blocked'}`}>{humanizeLabel(dep.status)}</span>
                    </span>
                  )
                })
              ) : 'None'}
            </p>
            {selectedTaskView.parent_id ? (
              <p className="field-label">
                {'Generated from: '}
                <button className="link-button" onClick={() => handleTaskSelect(selectedTaskView.parent_id!)}>{describeTask(selectedTaskView.parent_id, taskIndex).label}</button>
              </p>
            ) : null}
            {(selectedTaskView.children_ids?.length ?? 0) > 0 ? (
              <div className="generated-tasks-section">
                <p className="field-label">Generated tasks ({selectedTaskView.children_ids!.length}):</p>
                <div className="generated-tasks-list">
                  {selectedTaskView.children_ids!.map((childId) => {
                    const child = describeTask(childId, taskIndex)
                    return (
                      <button key={childId} className="generated-task-chip" onClick={() => handleTaskSelect(childId)}>
                        <span className={`status-pill status-pill-inline ${child.status === 'done' ? 'status-done' : child.status === 'in_progress' ? 'status-running' : child.status === 'blocked' || child.status === 'cancelled' ? 'status-failed' : 'status-paused'}`}>{humanizeLabel(child.status)}</span>
                        {child.label}
                      </button>
                    )
                  })}
                </div>
              </div>
            ) : null}
            {selectedTaskView.execution_summary && selectedTaskView.execution_summary.steps.length > 0 && (['in_review', 'blocked', 'done'].includes(selectedTaskView.status) || (selectedTaskView.status === 'in_progress' && !!selectedTaskView.pending_gate)) ? (() => {
              const sumStep = selectedTaskView.execution_summary!.steps.slice().reverse().find((s) => s.step === 'summary' || s.step === 'summarize')
              const otherSteps = selectedTaskView.execution_summary!.steps.filter((s) => s.step !== 'summary' && s.step !== 'summarize')
              return (
                <div className="execution-summary-box">
                  <p className="field-label">Execution summary</p>
                  {sumStep?.summary ? (
                    <RenderedMarkdown content={sumStep.summary} className="execution-summary-prose" />
                  ) : (
                    <p className="execution-summary-meta">
                      {selectedTaskView.execution_summary!.run_summary}
                    </p>
                  )}
                  {(otherSteps.length > 0 || selectedTaskView.execution_summary!.started_at) ? (
                    <details className="execution-details-collapse">
                      <summary className="execution-details-toggle">Step details</summary>
                      <p className="execution-summary-meta">
                        {selectedTaskView.execution_summary!.run_summary}
                        {formatDuration(selectedTaskView.execution_summary!.duration_seconds) ? ` · ${formatDuration(selectedTaskView.execution_summary!.duration_seconds)}` : ''}
                        {selectedTaskView.execution_summary!.started_at ? ` · started ${new Date(selectedTaskView.execution_summary!.started_at).toLocaleString()}` : ''}
                        {selectedTaskView.execution_summary!.finished_at ? ` · finished ${new Date(selectedTaskView.execution_summary!.finished_at).toLocaleString()}` : ''}
                      </p>
                      {otherSteps.map((step) => {
                        const isOk = step.status === 'ok' || step.status === 'completed' || step.status === 'done'
                        const stepKey = `${selectedTaskView.execution_summary!.run_id}-${step.step}`
                        const isExpanded = expandedSummarySteps.has(stepKey)
                        const summaryText = step.summary || ''
                        const needsTruncation = summaryText.length > 500
                        const displayText = !isExpanded && needsTruncation ? summaryText.slice(0, 500) + '...' : summaryText
                        return (
                          <div key={step.step} className="execution-step-row">
                            <div className="execution-step-header">
                              <span className={`execution-step-icon ${isOk ? 'step-ok' : 'step-error'}`}>{isOk ? '\u2713' : '\u2717'}</span>
                              <span className="execution-step-name">{humanizeLabel(step.step)}</span>
                              <span className={`status-pill status-pill-inline ${isOk ? 'status-done' : 'status-failed'}`}>{step.status}</span>
                              {formatDuration(step.duration_seconds) ? <span className="execution-step-duration">{formatDuration(step.duration_seconds)}</span> : null}
                              {step.open_counts && Object.keys(step.open_counts).length > 0 ? (
                                <span className="execution-step-counts">
                                  {Object.entries(step.open_counts).map(([sev, count]) => `${count} ${sev}`).join(', ')}
                                </span>
                              ) : null}
                              {step.commit ? <span className="execution-step-commit" title={step.commit}>{step.commit.slice(0, 8)}</span> : null}
                            </div>
                            {displayText ? (
                              <pre className="execution-step-summary">{displayText}</pre>
                            ) : null}
                            {needsTruncation ? (
                              <button
                                className="link-button"
                                onClick={() => {
                                  setExpandedSummarySteps((prev) => {
                                    const next = new Set(prev)
                                    if (next.has(stepKey)) next.delete(stepKey)
                                    else next.add(stepKey)
                                    return next
                                  })
                                }}
                              >
                                {isExpanded ? 'Show less' : 'Read more'}
                              </button>
                            ) : null}
                          </div>
                        )
                      })}
                    </details>
                  ) : null}
                </div>
              )
            })() : null}
            {(() => {
              const genComments = selectedTaskView.metadata?.generated_review_comments as Array<{path?: string; line?: number; severity?: string; body?: string; post_status?: string}> | undefined
              if (!genComments || genComments.length === 0) return null
              const stagedCount = genComments.filter((c) => (c.post_status ?? 'staged') !== 'posted').length
              const selectedStagedCount = Array.from(selectedCommentIndices).filter((idx) => {
                const c = genComments[idx]
                return c && (c.post_status ?? 'staged') !== 'posted'
              }).length
              return (
                <details className="execution-details-collapse" open>
                  <summary className="execution-details-toggle">
                    Generated comments ({selectedStagedCount} of {genComments.length} selected)
                  </summary>
                  {stagedCount > 0 ? (
                    <div className="generated-comments-toolbar">
                      <button
                        className="link-button"
                        onClick={() => {
                          const allStaged = new Set<number>()
                          genComments.forEach((c, i) => {
                            if ((c.post_status ?? 'staged') !== 'posted') allStaged.add(i)
                          })
                          const allSelected = Array.from(allStaged).every((i) => selectedCommentIndices.has(i))
                          setSelectedCommentIndices(allSelected ? new Set() : allStaged)
                        }}
                      >
                        {genComments.every((c, idx) => (c.post_status ?? 'staged') === 'posted' || selectedCommentIndices.has(idx)) ? 'Deselect All' : 'Select All'}
                      </button>
                    </div>
                  ) : null}
                  <div className="generated-comments-list">
                    {genComments.map((c, i) => {
                      const status = c.post_status ?? 'staged'
                      const isPosted = status === 'posted'
                      const isEditing = editingCommentIndex === i
                      const displayBody = editedCommentBodies.get(i) ?? c.body ?? ''
                      const isEdited = editedCommentBodies.has(i) && editedCommentBodies.get(i) !== c.body
                      return (
                        <div key={i} className={`generated-comment-item${isPosted ? ' comment-posted' : ''}`}>
                          <div className="generated-comment-item-row">
                            <input
                              type="checkbox"
                              className="generated-comment-checkbox"
                              checked={selectedCommentIndices.has(i)}
                              disabled={isPosted}
                              onChange={() => {
                                setSelectedCommentIndices((prev) => {
                                  const next = new Set(prev)
                                  if (next.has(i)) next.delete(i)
                                  else next.add(i)
                                  return next
                                })
                              }}
                            />
                            <div className="generated-comment-content">
                              <p className="generated-comment-location">
                                <code>{c.path || '(general)'}</code>
                                {c.line ? <span>:{c.line}</span> : null}
                                {c.severity ? <span className={`status-pill status-pill-inline severity-${c.severity}`}>{c.severity}</span> : null}
                                <span className={`status-pill status-pill-inline post-status-${status}`}>{status}</span>
                                {isEdited ? <span className="generated-comment-edited-badge">(edited)</span> : null}
                              </p>
                              {isEditing ? (
                                <div>
                                  <textarea
                                    className="generated-comment-edit-area"
                                    value={displayBody}
                                    onChange={(e) => {
                                      const val = e.target.value
                                      setEditedCommentBodies((prev) => {
                                        const next = new Map(prev)
                                        next.set(i, val)
                                        return next
                                      })
                                    }}
                                  />
                                  <div className="generated-comment-edit-actions">
                                    <button className="link-button" onClick={() => setEditingCommentIndex(null)}>Done</button>
                                    <button className="link-button" onClick={() => {
                                      setEditedCommentBodies((prev) => {
                                        const next = new Map(prev)
                                        next.delete(i)
                                        return next
                                      })
                                      setEditingCommentIndex(null)
                                    }}>Reset</button>
                                  </div>
                                </div>
                              ) : (
                                <>
                                  <RenderedMarkdown content={displayBody} className="generated-comment-body" />
                                  {!isPosted ? (
                                    <div className="generated-comment-edit-actions">
                                      <button className="link-button" onClick={() => setEditingCommentIndex(i)}>Edit</button>
                                    </div>
                                  ) : null}
                                </>
                              )}
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </details>
              )
            })()}
            {Array.isArray(selectedTaskView.human_review_actions) && selectedTaskView.human_review_actions.length > 0 ? (
              <div className="review-history-box">
                <p className="review-history-header">Review history</p>
                {selectedTaskView.human_review_actions.map((entry, idx) => {
                  const actionLabel = entry.action === 'approve'
                    ? 'Approved'
                    : entry.action === 'request_changes'
                      ? 'Changes requested'
                      : entry.action === 'retry'
                        ? 'Retry with guidance'
                        : entry.action === 'finalize_merge_conflict'
                          ? 'Finalized manual merge'
                          : entry.action
                  const badgeClass = entry.action === 'approve' || entry.action === 'finalize_merge_conflict'
                    ? 'status-done'
                    : entry.action === 'request_changes'
                      ? 'status-review'
                      : 'status-blocked'
                  return (
                    <div className="review-history-entry" key={`review-action-${idx}`}>
                      <div className="review-history-entry-head">
                        <span className={`status-pill status-pill-inline ${badgeClass}`}>{actionLabel}</span>
                        <span className="review-history-ts">{toLocaleTimestamp(entry.ts) || '-'}</span>
                      </div>
                      {entry.guidance ? <RenderedMarkdown content={entry.guidance} className="review-history-guidance" /> : null}
                      {entry.previous_error ? <pre className="review-history-error">{entry.previous_error}</pre> : null}
                    </div>
                  )
                })}
              </div>
            ) : null}
            {selectedTaskView.error?.trim() ? (() => {
              const errorText = selectedTaskView.error!.trim()
              const contextLines: string[] = []
              // Pull summaries from failed/error execution steps for context
              const execSteps = selectedTaskView.execution_summary?.steps || []
              const failedSteps = execSteps.filter((s) => s.status === 'error' || s.status === 'blocked')
              for (const fs of failedSteps) {
                if (fs.summary && fs.summary !== errorText) {
                  contextLines.push(`[${fs.step}] ${fs.summary}`)
                }
              }
              // Append stderr tail only if it adds new information not already
              // present in the error or step summaries (the backend now embeds
              // sanitized stderr into step summaries, so repeating it is noisy).
              const stderrTail = (stderrHistory || '').trim()
              if (stderrTail) {
                const last = stderrTail.slice(-400)
                const fromNewline = last.indexOf('\n')
                const stderrSnippet = (fromNewline > 0 ? last.slice(fromNewline + 1) : last).trim()
                const alreadyShown = errorText + '\n' + contextLines.join('\n')
                if (stderrSnippet && !alreadyShown.includes(stderrSnippet.slice(-120))) {
                  contextLines.push('stderr: ' + stderrSnippet)
                }
              }
              const context = contextLines.join('\n\n').trim()
              return (
                <div className="error-detail-box">
                  <p className="error-detail-label">Error details</p>
                  <pre>{context ? `${errorText}\n\n${context}` : errorText}</pre>
                </div>
              )
            })() : null}
            {selectedTaskView.recommended_action?.trim() ? (
              <div className="recommended-action-box">
                <p className="recommended-action-label">Recommended Action</p>
                <p className="recommended-action-text">{selectedTaskView.recommended_action}</p>
              </div>
            ) : null}
            {Array.isArray(selectedTaskView.human_blocking_issues) && selectedTaskView.human_blocking_issues.length > 0 ? (
              <div className="preview-box">
                <p className="field-label">Human blocking issues</p>
                {selectedTaskView.human_blocking_issues.map((issue, index) => (
                  <div className="row-card" key={`task-human-issue-${index}`}>
                    <p className="task-title">{issue.summary}</p>
                    {issue.details ? <p className="task-desc">{issue.details}</p> : null}
                    {(issue.action || issue.blocking_on || issue.category || issue.severity) ? (
                      <p className="task-meta">
                        {issue.action ? `action: ${issue.action}` : null}
                        {issue.action && issue.blocking_on ? ' · ' : null}
                        {issue.blocking_on ? `blocking on: ${issue.blocking_on}` : null}
                        {(issue.action || issue.blocking_on) && issue.category ? ' · ' : null}
                        {issue.category ? `category: ${issue.category}` : null}
                        {(issue.action || issue.blocking_on || issue.category) && issue.severity ? ' · ' : null}
                        {issue.severity ? `severity: ${issue.severity}` : null}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
        {effectiveTaskDetailTab === 'plan' ? (() => {
          const planRevisions = selectedTaskPlan?.revisions || []
          const selectedPlanRevision = selectedPlanRevisionId
            ? (planRevisions.find((r) => r.id === selectedPlanRevisionId) || null)
            : null
          const latestPlanRevision = selectedTaskPlan?.latest_revision_id
            ? (planRevisions.find((r) => r.id === selectedTaskPlan.latest_revision_id) || null)
            : null
          const effectiveRevision = selectedPlanRevision || latestPlanRevision
          const planContent = (effectiveRevision?.content || '').trim()
          const isRefining = !!(selectedTaskPlan?.active_refine_job
            && (selectedTaskPlan.active_refine_job.status === 'queued' || selectedTaskPlan.active_refine_job.status === 'running'))
          const showRefineRunningBanner = planRefineUiState === 'running' || isRefining
          const showRefineDoneBanner = planRefineUiState === 'done'
          const isPlanLocked = selectedTaskPlan?.plan_mutable === false
          const renderPlanModeToolbar = (): JSX.Element | null => {
            if (isPlanLocked) return null
            return (
              <div className="plan-tab-toolbar">
                <div className="plan-tab-mode-switcher">
                  <button className={`button ${planTabMode === 'view' ? 'is-active' : ''}`} onClick={() => setPlanTabMode('view')}>View</button>
                  <button className={`button ${planTabMode === 'edit' ? 'is-active' : ''}`} onClick={() => {
                    if (planTabMode !== 'edit') {
                      const text = planContent
                      const seeded = planManualSeedRef.current
                      const wasSeeded = seeded.taskId === selectedTaskView.id && seeded.workerText.trim().length > 0
                      const sameAsSeeded = wasSeeded && planManualContent.trim() === seeded.workerText.trim()
                      if (!planManualContent.trim() || sameAsSeeded) {
                        setPlanManualContent(text)
                        planManualSeedRef.current = { taskId: selectedTaskView.id, workerText: text }
                      }
                    }
                    setPlanTabMode('edit')
                  }}>Edit</button>
                  <button className={`button ${planTabMode === 'refine' ? 'is-active' : ''}`} onClick={() => setPlanTabMode('refine')}>Refine</button>
                </div>
              </div>
            )
          }
          return (
            <div className="task-detail-section-body">
              {showPlanGate && !showTopLevelGateBanner ? renderPendingGateBanner(selectedTaskView) : null}
              {planActionMessage ? <p className="field-label" style={{ color: 'var(--color-success, #5cb85c)' }}>{planActionMessage}</p> : null}
              {planActionError ? <p className="field-label" style={{ color: 'var(--color-danger, #d9534f)' }}>{planActionError}</p> : null}
              {showRefineRunningBanner ? (
                <div className="refine-banner">
                  <span className="refine-banner-dot" />
                  <span>Refining plan...</span>
                </div>
              ) : null}
              {showRefineDoneBanner ? (
                <div className="refine-banner refine-banner-done">
                  <span>Refine complete. Showing latest plan.</span>
                </div>
              ) : null}
              {planTabMode === 'view' ? (
                <div className="form-stack">
                  {planRevisions.length > 1 ? (
                    <>
                      <label className="field-label" htmlFor="plan-tab-revision-selector">Revision</label>
                      <select
                        id="plan-tab-revision-selector"
                        value={selectedPlanRevisionId}
                        onChange={(e) => setSelectedPlanRevisionId(e.target.value)}
                      >
                        <option value="">(latest)</option>
                        {planRevisions.map((rev) => (
                          <option key={rev.id} value={rev.id}>
                            {rev.id} · {humanizeLabel(rev.source)} · {rev.status}
                          </option>
                        ))}
                      </select>
                    </>
                  ) : null}
                  {planContent ? (
                    <div className="preview-box">
                      {renderPlanModeToolbar()}
                      {effectiveRevision ? (
                        <p className="task-meta">
                          {humanizeLabel(effectiveRevision.source)}
                          {effectiveRevision.step ? ` · ${humanizeLabel(effectiveRevision.step)}` : ''}
                          {' · '}{humanizeLabel(effectiveRevision.status)}
                        </p>
                      ) : null}
                      <RenderedMarkdown
                        content={planContent}
                        className="plan-content-field"
                      />
                    </div>
                  ) : (
                    <>
                      {renderPlanModeToolbar()}
                      <p className="empty">No plan content yet.</p>
                    </>
                  )}
                </div>
              ) : null}
              {planTabMode === 'edit' ? (
                <div className="form-stack">
                  {renderPlanModeToolbar()}
                  <textarea
                    className="plan-content-field"
                    rows={20}
                    value={planManualContent}
                    onChange={(e) => setPlanManualContent(e.target.value)}
                    placeholder="Edit plan text."
                  />
                  <div className="plan-tab-commit-bar">
                    <button
                      className="button button-primary"
                      disabled={planSavingManual || planCommitting || !planManualContent.trim()}
                      onClick={async () => {
                        const newRevId = await saveManualPlanRevision(selectedTaskView.id)
                        if (newRevId) {
                          await commitPlanRevision(selectedTaskView.id, newRevId)
                        }
                      }}
                    >
                      {planSavingManual || planCommitting ? 'Saving...' : 'Save & commit'}
                    </button>
                  </div>
                </div>
              ) : null}
              {planTabMode === 'refine' ? (
                <div className="form-stack">
                  {renderPlanModeToolbar()}
                  <label className="field-label" htmlFor="plan-tab-refine-feedback">Request changes from worker</label>
                  <div className="planning-refine-inline">
                    <input
                      id="plan-tab-refine-feedback"
                      value={planRefineFeedback}
                      onChange={(e) => setPlanRefineFeedback(e.target.value)}
                      placeholder="Describe what should change in the plan."
                    />
                    <button
                      className="button"
                      onClick={() => void refineTaskPlan(selectedTaskView.id)}
                      disabled={planJobLoading || isRefining || !planRefineFeedback.trim()}
                    >
                      {planJobLoading || isRefining ? 'Refining...' : 'Refine'}
                    </button>
                  </div>
                  <div className="preview-box">
                    <p className="field-label">Worker output{isRefining ? ' (live)' : ''}</p>
                    <pre className="task-log-output plan-content-field planning-worker-output">{planRefineStdout || ' '}</pre>
                  </div>
                  {!isRefining && planRevisions.length > 0 ? (
                    <div className="plan-tab-commit-bar">
                      <button
                        className="button button-primary"
                        disabled={planCommitting || (!selectedPlanRevisionId && !selectedTaskPlan?.latest_revision_id)}
                        onClick={() => void commitPlanRevision(selectedTaskView.id, selectedPlanRevisionId || selectedTaskPlan?.latest_revision_id || '')}
                      >
                        {planCommitting ? 'Committing...' : 'Commit this revision'}
                      </button>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          )
        })() : null}
        {taskDetailTab === 'workdoc' ? (
          <div className="task-detail-section-body">
            {selectedTaskWorkdocLoading ? <p className="field-label">Loading workdoc...</p> : null}
            {selectedTaskWorkdocError ? <p className="field-label" style={{ color: 'var(--color-danger, #d9534f)' }}>{selectedTaskWorkdocError}</p> : null}
            {!selectedTaskWorkdocLoading && !selectedTaskWorkdocError && selectedTaskWorkdoc?.content ? (
              <div className="preview-box">
                <RenderedMarkdown content={selectedTaskWorkdoc.content} className="plan-content-field" />
              </div>
            ) : null}
            {!selectedTaskWorkdocLoading && !selectedTaskWorkdocError && !selectedTaskWorkdoc?.content ? (
              <p className="empty">No workdoc content available.</p>
            ) : null}
          </div>
        ) : null}
        {taskDetailTab === 'configuration' ? (
          <div className="task-detail-section-body">
              <div className="form-stack">
                <label className="field-label" htmlFor="edit-task-title">Title</label>
                <input
                  id="edit-task-title"
                  value={configLocked ? (selectedTaskView.title || '') : editTaskTitle}
                  onChange={(event) => setEditTaskTitle(event.target.value)}
                  disabled={configLocked || taskActionPending === 'save'}
                />
                <label className="field-label" htmlFor="edit-task-description">Description</label>
                <textarea
                  id="edit-task-description"
                  className="json-editor-textarea"
                  rows={4}
                  value={configLocked ? (selectedTaskView.description || '') : editTaskDescription}
                  onChange={(event) => setEditTaskDescription(event.target.value)}
                  disabled={configLocked || taskActionPending === 'save'}
                />
                <label className="field-label" htmlFor="edit-task-labels">Labels (comma-separated)</label>
                <input
                  id="edit-task-labels"
                  value={configLocked ? ((selectedTaskView.labels || []).join(', ')) : editTaskLabels}
                  onChange={(event) => setEditTaskLabels(event.target.value)}
                  disabled={configLocked || taskActionPending === 'save'}
                />
                <label className="field-label">HITL mode</label>
                {configLocked ? (
                  <p className="task-meta">{humanizeLabel(normalizeHitlMode(selectedTaskView.hitl_mode || 'supervised'))}</p>
                ) : (
                  <HITLModeSelector
                    currentMode={editTaskHitlMode}
                    onModeChange={setEditTaskHitlMode}
                    projectDir={projectDir}
                  />
                )}
                <label className="field-label">Dependency policy</label>
                {configLocked ? (
                  <p className="task-meta">{humanizeLabel(selectedTaskView.dependency_policy || 'prudent')}</p>
                ) : (
                  <div className="toggle-group" role="group" aria-label="Dependency policy">
                    {(['permissive', 'prudent', 'strict'] as const).map((level) => (
                      <button
                        key={level}
                        type="button"
                        className={`toggle-button ${editTaskDependencyPolicy === level ? 'is-active' : ''}`}
                        aria-pressed={editTaskDependencyPolicy === level}
                        onClick={() => setEditTaskDependencyPolicy(level)}
                        disabled={taskActionPending === 'save'}
                      >
                        {humanizeLabel(level)}
                      </button>
                    ))}
                  </div>
                )}
                <p className="field-label">
                  {(configLocked ? (selectedTaskView.dependency_policy || 'prudent') : editTaskDependencyPolicy) === 'permissive' && 'Prefer libraries over manual implementation. Install what you need.'}
                  {(configLocked ? (selectedTaskView.dependency_policy || 'prudent') : editTaskDependencyPolicy) === 'prudent' && 'Prefer existing deps. Only add new ones when manual implementation would be unreliable or complex.'}
                  {(configLocked ? (selectedTaskView.dependency_policy || 'prudent') : editTaskDependencyPolicy) === 'strict' && 'No new dependencies allowed. Work only with what is already installed.'}
                </p>
                <label className="field-label" htmlFor="edit-task-step-timeout">Task timeout for implement step (seconds)</label>
                <input
                  id="edit-task-step-timeout"
                  value={configLocked
                    ? ((selectedTaskView.step_timeout_seconds && selectedTaskView.step_timeout_seconds > 0)
                      ? String(selectedTaskView.step_timeout_seconds)
                      : '')
                    : editTaskStepTimeout}
                  onChange={(event) => setEditTaskStepTimeout(event.target.value)}
                  inputMode="numeric"
                  placeholder="Use global default"
                  disabled={configLocked || taskActionPending === 'save'}
                />
                <label className="field-label">Project commands override</label>
                {configLocked ? (
                  selectedTaskView.project_commands && Object.keys(selectedTaskView.project_commands).length > 0 ? (
                    <pre className="task-meta" style={{ whiteSpace: 'pre-wrap', fontSize: '0.85em' }}>{serializeProjectCommandsYaml(selectedTaskView.project_commands as Record<string, LanguageCommandSettings>)}</pre>
                  ) : (
                    <p className="task-meta">Not set (using global defaults)</p>
                  )
                ) : (
                  <div className="json-editor-group">
                    <textarea
                      className="json-editor-textarea"
                      rows={6}
                      value={editTaskProjectCommands}
                      onChange={(event) => setEditTaskProjectCommands(event.target.value)}
                      placeholder={PROJECT_COMMANDS_EXAMPLE}
                      disabled={taskActionPending === 'save'}
                    />
                    <div className="inline-actions">
                      <button type="button" className="button button-xs"
                        onClick={() => {
                          try {
                            setEditTaskProjectCommands(formatProjectCommandsField(editTaskProjectCommands))
                            setSettingsError('')
                          } catch (err) {
                            setSettingsError(err instanceof Error ? err.message : 'Invalid project commands')
                          }
                        }}
                      >Format</button>
                      <button type="button" className="button button-xs"
                        onClick={() => setEditTaskProjectCommands('')}
                      >Clear</button>
                    </div>
                  </div>
                )}
                {!configLocked ? (
                  <div className="inline-actions">
                    <button
                      className="button button-primary"
                      onClick={() => void saveTaskEdits(selectedTaskView.id)}
                      disabled={taskActionPending === 'save'}
                    >
                      {taskActionPending === 'save' ? 'Saving...' : 'Save'}
                    </button>
                  </div>
                ) : null}
              </div>
          </div>
        ) : null}
        {taskDetailTab === 'dependencies' ? (
          <div className="task-detail-section-body">
            <div className="dependency-graph-panel">
              <p className="field-label">Dependency graph</p>
              <div className="dependency-graph-grid">
                <div className="dependency-graph-column">
                  <p className="field-label">Depends on</p>
                  {blockerIds.length > 0 ? (
                    blockerIds.map((depId) => {
                      const dep = describeTask(depId, taskIndex)
                      return (
                        <div className="dependency-node dependency-node-blocker" key={`blocker-${depId}`} onClick={() => handleTaskSelect(depId)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleTaskSelect(depId) } }} role="button" tabIndex={0} style={{ cursor: 'pointer' }}>
                          <p className="dependency-node-title">{dep.label}</p>
                          <p className="dependency-node-meta">{humanizeLabel(dep.status)} {'->'} depends on</p>
                        </div>
                      )
                    })
                  ) : (
                    <p className="empty">No dependencies</p>
                  )}
                </div>
                <div className="dependency-graph-column dependency-graph-center">
                  <p className="field-label">Selected task</p>
                  <div className="dependency-node dependency-node-current">
                    <p className="dependency-node-title">{selectedTaskView.title} ({selectedTaskView.id})</p>
                    <p className="dependency-node-meta">{humanizeLabel(selectedTaskView.status)}</p>
                  </div>
                </div>
                <div className="dependency-graph-column">
                  <p className="field-label">Blocks</p>
                  {blockedIds.length > 0 ? (
                    blockedIds.map((depId) => {
                      const dep = describeTask(depId, taskIndex)
                      return (
                        <div className="dependency-node dependency-node-dependent" key={`dependent-${depId}`} onClick={() => handleTaskSelect(depId)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleTaskSelect(depId) } }} role="button" tabIndex={0} style={{ cursor: 'pointer' }}>
                          <p className="dependency-node-title">{dep.label}</p>
                          <p className="dependency-node-meta">depends on current task · {humanizeLabel(dep.status)}</p>
                        </div>
                      )
                    })
                  ) : (
                    <p className="empty">No dependents</p>
                  )}
                </div>
              </div>
              {blockerIds.length > 0 || blockedIds.length > 0 ? (
                <div className="dependency-edge-list">
                  {blockerIds.map((depId) => (
                    <p key={`edge-in-${depId}`} className="dependency-edge">
                      {describeTask(depId, taskIndex).label} {'->'} {selectedTaskView.id}
                    </p>
                  ))}
                  {blockedIds.map((depId) => (
                    <p key={`edge-out-${depId}`} className="dependency-edge">
                      {selectedTaskView.id} {'->'} {describeTask(depId, taskIndex).label}
                    </p>
                  ))}
                </div>
              ) : null}
            </div>
            <div className="form-stack">
              <label className="field-label" htmlFor="task-blocker-input">Add dependency task ID</label>
              <div className="inline-actions">
                <input
                  id="task-blocker-input"
                  value={newDependencyId}
                  onChange={(event) => setNewDependencyId(event.target.value)}
                  placeholder="task-xxxxxxxxxx"
                />
                <button className="button" onClick={() => void addDependency(selectedTaskView.id)}>Add dependency</button>
              </div>
              {(selectedTaskView.blocked_by || []).map((depId) => (
                <div className="row-card" key={depId}>
                  <p className="task-meta">{depId}</p>
                  <button className="button button-danger" onClick={() => void removeDependency(selectedTaskView.id, depId)}>
                    Remove
                  </button>
                </div>
              ))}
              <div className="inline-actions">
                <button
                  className="button"
                  onClick={() => void analyzeDependencies()}
                  disabled={dependencyActionLoading}
                >
                  Analyze dependencies
                </button>
                <button
                  className="button"
                  onClick={() => void resetDependencyAnalysis(selectedTaskView.id)}
                  disabled={dependencyActionLoading}
                >
                  Reset inferred deps
                </button>
              </div>
              {dependencyActionMessage ? <p className="field-label">{dependencyActionMessage}</p> : null}
            </div>
          </div>
        ) : null}
        {taskDetailTab === 'activity' ? (
          <div className="task-detail-section-body">
            <div className="list-stack">
              {collaborationLoading ? <p className="field-label">Loading activity...</p> : null}
              {(showAllActivity ? collaborationTimeline : collaborationTimeline.slice(0, 8)).map((event) => (
                <div className="row-card timeline-event-card" key={event.id}>
                  <div>
                    <p className="task-title">{event.summary || humanizeLabel(event.type)}</p>
                    <p className="task-meta">{humanizeLabel(event.type)} · {event.actor} · {toLocaleTimestamp(event.timestamp) || '-'}</p>
                  </div>
                  {event.details ? <RenderedMarkdown content={event.details} className="task-desc" /> : null}
                  {event.human_blocking_issues && event.human_blocking_issues.length > 0 ? (
                    <div className="list-stack">
                      <p className="field-label">Required human input</p>
                      {event.human_blocking_issues.map((issue, idx) => (
                        <p className="task-meta" key={`${event.id}-issue-${idx}`}>- {issue.summary}</p>
                      ))}
                    </div>
                  ) : null}
                </div>
              ))}
              {collaborationTimeline.length > 8 ? (
                <button className="link-button" onClick={() => setShowAllActivity((prev) => !prev)}>
                  {showAllActivity ? 'Show fewer' : `Show all (${collaborationTimeline.length}) events`}
                </button>
              ) : null}
              {collaborationTimeline.length === 0 && !collaborationLoading ? <p className="empty">No activity for this task yet.</p> : null}
              {collaborationError ? <p className="error-banner">{collaborationError}</p> : null}
            </div>
          </div>
        ) : null}
        {taskDetailTab === 'logs' ? (() => {
          const hasTaskLogs = !!(stdoutHistory || stderrHistory || stdoutRawHistory || stderrRawHistory) || (!!selectedTaskLogs && (selectedTaskLogs.mode !== 'none' || !!selectedTaskLogs.stdout || !!selectedTaskLogs.stderr))
          const progressEntries = formatProgressEntries(selectedTaskLogs?.progress)
          const availableSteps = selectedTaskLogs?.available_steps || []
          const orderedSteps = orderLogStepsByPipeline(availableSteps, selectedTaskView.pipeline_template)
          const stepCounts = selectedTaskLogs?.step_execution_counts || {}
          const stepLatestRun = selectedTaskLogs?.step_latest_run || {}
          const stepHistory = selectedTaskLogs?.step_history || []
          const activeLogStep = logViewStep || selectedTaskLogs?.step || ''
          const activeLogRun = logViewRunId || selectedTaskLogs?.run_id || ''
          const latestStep = selectedTaskLogs?.current_step || selectedTaskView.current_step || ''
          const taskIsRunning = selectedTaskView.status === 'in_progress' || selectedTaskView.status === 'queued'
          const totalExecutions = Object.values(stepCounts).reduce((a, b) => a + b, 0)
          const extraSteps = totalExecutions - Object.keys(stepCounts).length
          const stepSelectorSteps = orderedSteps.length > 0
            ? orderedSteps
            : (latestStep ? [latestStep] : [])
          const attemptsForActiveStep = activeLogStep
            ? stepHistory.filter((item) => item.step === activeLogStep)
            : []
          const orderedAttemptsForActiveStep = orderLogAttempts(attemptsForActiveStep)
          const stdoutSuppressedStructured = (
            !stdoutHistory
            && !!stdoutRawHistory
            && stdoutRenderStats.structured
            && stdoutRenderStats.streamEvents > 0
          )
          return (
            <div className="task-detail-section-body">
              {stepSelectorSteps.length > 0 ? (
                <div className="log-step-selector">
                  {stepSelectorSteps.map((s) => {
                    const isViewed = s === activeLogStep
                    const isLatest = s === latestStep
                    const count = stepCounts[s] || 0
                    const cls = `log-step-pill${isViewed ? ' is-active' : ''}${isLatest ? ' is-latest' : ''}`
                    return (
                      <button
                        key={s}
                        className={cls}
                        onClick={() => switchLogStepAttempt(s === activeLogStep ? '' : s, s === activeLogStep ? '' : (stepLatestRun[s] || ''))}
                      >
                        {humanizeLabel(s)}{count > 1 ? <span className="log-step-count">{'\u00d7'}{count}</span> : null}{isLatest && taskIsRunning ? <span className="log-step-live-dot" /> : null}
                      </button>
                    )
                  })}
                  {logViewStep ? (
                    <button className="log-step-pill" onClick={() => switchLogStep('')}>Latest</button>
                  ) : null}
                  {extraSteps > 0 ? (
                    <span className="log-step-summary">{totalExecutions} steps run · {extraSteps} {extraSteps === 1 ? 'retry' : 'retries'}</span>
                  ) : null}
                </div>
              ) : null}
              {orderedAttemptsForActiveStep.length > 1 ? (
                <>
                  <label className="field-label" htmlFor="log-attempt-selector">Attempt</label>
                  <select
                    id="log-attempt-selector"
                    value={activeLogRun}
                    onChange={(event) => switchLogStepAttempt(activeLogStep, event.target.value)}
                  >
                    {orderedAttemptsForActiveStep.map((item) => (
                      <option key={`${item.step}-${item.run_id}-${item.attempt || 0}`} value={item.run_id}>
                        {item.attempt ? `Attempt ${item.attempt}` : item.run_id} · {item.run_id.slice(0, 12)}
                        {item.started_at ? ` · ${toLocaleTimestamp(item.started_at) || item.started_at}` : ''}
                      </option>
                    ))}
                  </select>
                </>
              ) : null}
              <p className="task-meta">
                Source: {humanizeLabel(selectedTaskLogs?.mode || 'none')}
                {selectedTaskLogs?.step ? ` · step: ${selectedTaskLogs.step}` : ''}
                {selectedTaskLogs?.run_id ? ` · run: ${selectedTaskLogs.run_id.slice(0, 12)}` : ''}
                {selectedTaskLogs?.started_at ? ` · started: ${toLocaleTimestamp(selectedTaskLogs.started_at) || selectedTaskLogs.started_at}` : ''}
                {selectedTaskLogs?.finished_at ? ` · finished: ${toLocaleTimestamp(selectedTaskLogs.finished_at) || selectedTaskLogs.finished_at}` : ''}
              </p>
              {selectedTaskLogsLoading ? <p className="field-label">Loading logs...</p> : null}
              {selectedTaskLogsError ? <p className="error-banner">{selectedTaskLogsError}</p> : null}
              {progressEntries.length > 0 ? (
                <div className="preview-box">
                  <p className="field-label">Run snapshot</p>
                  {progressEntries.map((item) => (
                    <p className="task-meta" key={`detail-log-progress-${item.key}`}>
                      <strong>{humanizeLabel(item.key)}:</strong> {item.value}
                    </p>
                  ))}
                </div>
              ) : null}
              {hasTaskLogs ? (
                <div className="task-log-grid">
                  <div className="task-log-pane">
                    <p className="field-label">Stdout{stdoutRenderStats.structured ? ' (rendered)' : ''}</p>
                    {stdoutRenderStats.structured ? (
                      <p className="task-meta">Parsed {stdoutRenderStats.parsedLines} JSON lines · {stdoutRenderStats.streamEvents} stream events</p>
                    ) : null}
                    {stdoutSuppressedStructured ? (
                      <p className="task-meta">Streaming structured tool input (no text output yet).</p>
                    ) : null}
                    <pre className="task-log-output" ref={stdoutPreRef} onScroll={() => handleLogPaneScroll('stdout')}>
                      {stdoutHistory || (stdoutSuppressedStructured ? '(structured stream events only)' : '(empty)')}
                    </pre>
                  </div>
                  <div className="task-log-pane">
                    <p className="field-label">Stderr</p>
                    <pre className="task-log-output" ref={stderrPreRef} onScroll={() => handleLogPaneScroll('stderr')}>{stderrHistory || '(empty)'}</pre>
                  </div>
                </div>
              ) : (
                <p className="empty">No logs available.</p>
              )}
            </div>
          )
        })() : null}
        {taskDetailTab === 'changes' ? (
          <div className="task-detail-section-body">
            {taskDiffLoading ? <p className="field-label">Loading diff...</p> : null}
            {!taskDiffLoading && taskDiff && taskDiff.mode === 'none' && taskDiff.reason ? (
              <p className="field-label">No task-scoped changes available yet. Request changes to rerun implementation.</p>
            ) : null}
            {!taskDiffLoading && taskDiff && (taskDiff.mode === 'none' || (!taskDiff.commit && !taskDiff.diff && (taskDiff.files?.length || 0) === 0)) && !taskDiff.reason ? (
              <p className="empty">No changes found for this task.</p>
            ) : null}
            {!taskDiffLoading && taskDiff && (taskDiff.mode === 'working_tree' || taskDiff.mode === 'committed' || taskDiff.mode === 'preserved_branch' || taskDiff.commit || taskDiff.diff || (taskDiff.files?.length || 0) > 0) ? (() => {
              // Split unified diff into per-file chunks
              const fileChunks: { path: string; additions: number; deletions: number; lines: string[] }[] = []
              if (taskDiff.diff) {
                let current: { path: string; additions: number; deletions: number; lines: string[] } | null = null
                for (const line of taskDiff.diff.split('\n')) {
                  if (line.startsWith('diff --git ')) {
                    if (current) fileChunks.push(current)
                    const match = line.match(/diff --git a\/.+ b\/(.+)/)
                    current = { path: match ? match[1] : '?', additions: 0, deletions: 0, lines: [line] }
                  } else if (current) {
                    current.lines.push(line)
                    if (line.startsWith('+') && !line.startsWith('+++')) current.additions++
                    else if (line.startsWith('-') && !line.startsWith('---')) current.deletions++
                  }
                }
                if (current) fileChunks.push(current)
              }
              const totalAdditions = fileChunks.reduce((sum, chunk) => sum + chunk.additions, 0)
              const totalDeletions = fileChunks.reduce((sum, chunk) => sum + chunk.deletions, 0)
              const warningLabels: Record<string, string> = {
                heuristic_base_inferred: 'Base branch was inferred heuristically.',
                large_file_count: 'Diff spans an unusually large number of files.',
                large_line_churn: 'Diff contains unusually large line churn.',
                base_not_ancestor: 'Inferred base is not an ancestor of preserved work.',
              }
              const warningMessages = (taskDiff.warnings || []).map((code) => warningLabels[code] || code)
              const baseRef = taskDiff.base_ref || taskDiff.base_branch || null
              const headRef = taskDiff.head_ref || taskDiff.branch || null
              const baseSha = taskDiff.base_sha || null
              const headSha = taskDiff.head_sha || null
              const provenanceText = taskDiff.mode === 'preserved_branch'
                ? `${headRef || 'task branch'} vs ${baseRef || 'base'}`
                : (baseRef || headRef)
                  ? `${headRef || 'head'} vs ${baseRef || 'base'}`
                  : ''
              return (
                <>
                  <p className="task-meta">
                    {taskDiff.mode === 'working_tree'
                      ? 'Working tree changes'
                      : taskDiff.mode === 'preserved_branch'
                        ? `Branch changes${taskDiff.branch ? ` (${taskDiff.branch}` : ''}${taskDiff.base_branch ? ` vs ${taskDiff.base_branch}` : ''}${taskDiff.branch ? ')' : ''}`
                      : taskDiff.commit
                        ? <>Commit: <code>{taskDiff.commit.slice(0, 12)}</code></>
                        : 'Changes'}
                    {' · '}{taskDiff.files.length} file{taskDiff.files.length !== 1 ? 's' : ''} changed
                    {fileChunks.length > 0 ? (
                      <>
                        {' · '}
                        <span className="diff-file-stats">
                          <span className="diff-stat-add">+{totalAdditions}</span>
                          <span className="diff-stat-del">-{totalDeletions}</span>
                        </span>
                      </>
                    ) : null}
                  </p>
                  {provenanceText ? (
                    <p className="task-meta">
                      Provenance: {provenanceText}
                      {baseSha ? ` @${baseSha.slice(0, 12)}` : ''}
                      {headSha ? ` -> ${headSha.slice(0, 12)}` : ''}
                    </p>
                  ) : null}
                  {warningMessages.length > 0 ? (
                    <p className="info-banner">
                      {taskDiff.confidence === 'low' ? 'Low-confidence diff:' : 'Diff warning:'} {warningMessages.join(' ')}
                    </p>
                  ) : null}
                  {fileChunks.length > 0 ? (
                    <div className="diff-file-sections">
                      {fileChunks.map((chunk) => (
                        <details key={chunk.path} className="diff-file-section">
                          <summary className="diff-file-header">
                            <code className="diff-file-name">{chunk.path}</code>
                            <span className="diff-file-stats">
                              {chunk.additions > 0 ? <span className="diff-stat-add">+{chunk.additions}</span> : null}
                              {chunk.deletions > 0 ? <span className="diff-stat-del">-{chunk.deletions}</span> : null}
                            </span>
                          </summary>
                          <pre className="diff-content">{chunk.lines.map((line, i) => {
                            const cls = line.startsWith('+++') || line.startsWith('---') ? 'diff-line-meta'
                              : line.startsWith('@@') ? 'diff-line-hunk'
                              : line.startsWith('+') ? 'diff-line-add'
                              : line.startsWith('-') ? 'diff-line-del'
                              : line.startsWith('diff ') ? 'diff-line-meta'
                              : ''
                            return <span key={i} className={cls}>{line}{'\n'}</span>
                          })}</pre>
                        </details>
                      ))}
                    </div>
                  ) : (
                    <p className="empty">No diff content available.</p>
                  )}
                </>
              )
            })() : null}
          </div>
        ) : null}
      </div>
    ) : (
      <p className="empty">No tasks on board yet.</p>
    )

  function renderBoard(): JSX.Element {
    return (
      <section className="panel">
        {taskActionMessage ? <p className="field-label">{taskActionMessage}</p> : null}
        {taskActionError ? <div className="error-banner"><span>{taskActionError}</span><button type="button" className="error-banner-dismiss" onClick={() => setTaskActionError('')} aria-label="Dismiss">&times;</button></div> : null}
        <div className="board-toolbar">
          <label className="board-compact-toggle" title="Compact view">
            <input type="checkbox" checked={boardCompact} onChange={() => setBoardCompact((v) => !v)} />
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><line x1="2" y1="4" x2="14" y2="4" /><line x1="2" y1="8" x2="14" y2="8" /><line x1="2" y1="12" x2="14" y2="12" /></svg>
          </label>
          <div className="board-toolbar-spacer" />
          {gitStatus && (
            <div className="git-status-bar git-push-wrap">
              <button className="git-branch-label" title={gitStatus.branch} onClick={() => { const opening = !pushPopoverOpen; setPushPopoverOpen(opening); if (opening) void fetchGitStatus() }}>
                <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="5" cy="4" r="2" /><circle cx="5" cy="12" r="2" /><circle cx="12" cy="6" r="2" /><path d="M5 6v4M10.2 6.8C9 8 7 8 5 8" /></svg>
                {gitStatus.branch}
              </button>
              {gitStatus.ahead_count > 0 && (
                <span className="git-ahead-badge" title={`${gitStatus.ahead_count} commit${gitStatus.ahead_count !== 1 ? 's' : ''} ahead of remote`}>
                  {gitStatus.ahead_count} ahead
                </span>
              )}
              {gitStatus.behind_count > 0 && (
                <span className="git-behind-badge" title={`${gitStatus.behind_count} commit${gitStatus.behind_count !== 1 ? 's' : ''} behind remote`}>
                  {gitStatus.behind_count} behind
                </span>
              )}
              <button
                className="board-icon-btn git-push-btn"
                disabled={pushInProgress || autoNameInProgress || (gitStatus.ahead_count === 0 && !!gitStatus.remote_branch)}
                onClick={() => { const opening = !pushPopoverOpen; setPushPopoverOpen(opening); if (opening) void fetchGitStatus() }}
                title={gitStatus.ahead_count === 0 && gitStatus.remote_branch ? 'Nothing to push' : 'Push to remote'}
                aria-label="Push to remote"
              >
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M8 12V3" /><path d="M4 7l4-4 4 4" /><path d="M3 14h10" /></svg>
                {pushInProgress && <span className="git-push-spinner" />}
              </button>
              {pushPopoverOpen && (
                <div className="push-popover">
                  <div className="push-popover-header">{gitStatus.branch}</div>
                    {gitStatus.ahead_count > 0 && gitStatus.commits.length > 0 && (
                      <div className="push-commit-list">
                        {gitStatus.commits.slice(0, 5).map((c) => (
                          <div key={c.sha} className="push-commit-item" title={c.sha}>
                            <code>{c.sha.slice(0, 7)}</code> {c.message}
                          </div>
                        ))}
                        {gitStatus.commits.length > 5 && (
                          <div className="push-commit-item push-commit-more">...and {gitStatus.commits.length - 5} more</div>
                        )}
                      </div>
                    )}
                    {gitStatus.remote_branch ? (
                      <button
                        className="push-option-btn"
                        disabled={pushInProgress || autoNameInProgress || gitStatus.ahead_count === 0}
                        onClick={() => void handleGitPush()}
                      >
                        Push to {gitStatus.remote_branch}
                      </button>
                    ) : (
                      <button
                        className="push-option-btn"
                        disabled={pushInProgress || autoNameInProgress}
                        onClick={() => void handleGitPush()}
                      >
                        Push to origin/{gitStatus.branch}
                      </button>
                    )}
                    <div className="push-popover-divider" />
                    <div className="push-new-branch-section">
                      <label className="push-new-branch-label">Push to new branch</label>
                      <div className="push-new-branch-row">
                        <input
                          className="push-branch-input"
                          type="text"
                          placeholder="branch-name"
                          value={pushTargetBranch}
                          onChange={(e) => setPushTargetBranch(e.target.value)}
                          disabled={pushInProgress || autoNameInProgress}
                        />
                        <button
                          className="push-auto-btn"
                          disabled={pushInProgress || autoNameInProgress}
                          onClick={() => void handleAutoNamePush()}
                          title="Auto-generate branch name"
                        >
                          {autoNameInProgress ? <span className="spinner" /> : 'Auto'}
                        </button>
                      </div>
                      {pushTargetBranch.trim() && (
                        <button
                          className="push-option-btn"
                          disabled={pushInProgress || autoNameInProgress}
                          onClick={() => void handleGitPush(pushTargetBranch.trim())}
                        >
                          Push to origin/{pushTargetBranch.trim()}
                        </button>
                      )}
                    </div>
                    {pushInProgress && (
                      <>
                        {pushAutoFixRunning && (
                          <div className="push-autofix-status">
                            <span className="push-autofix-spinner" />
                            Auto-fix worker is analyzing and fixing the issue...
                          </div>
                        )}
                        {pushOutputLines.length > 0 && (
                          <div className="push-output-log" ref={(el) => { if (el) el.scrollTop = el.scrollHeight }}>
                            {pushOutputLines.map((line, i) => (
                              <div key={i} className="push-output-line">{line}</div>
                            ))}
                          </div>
                        )}
                        <button
                          className="push-cancel-btn"
                          onClick={() => void handleCancelPush()}
                        >
                          {pushAutoFixRunning ? 'Cancel auto-fix' : 'Cancel push'}
                        </button>
                      </>
                    )}
                    {pushMessage && !pushInProgress && (
                      <div className="push-success-msg">{pushMessage}</div>
                    )}
                    {pushError && <div className="push-error">{pushError}</div>}
                </div>
              )}
              {pushMessage && <span className="git-push-message">{pushMessage}</span>}
            </div>
          )}
          <details className="board-more-menu">
            <summary className="board-more-btn" title="More actions">···</summary>
            <div className="board-more-dropdown">
              <button
                className="board-more-item"
                onClick={() => void queueAllBacklog()}
                disabled={taskActionPending === 'queue-backlog'}
              >
                {taskActionPending === 'queue-backlog' ? 'Queuing...' : 'Queue All Backlog'}
              </button>
              <button
                className="board-more-item board-more-item-danger"
                onClick={() => void clearAllTasks()}
                disabled={taskActionPending === 'clear'}
              >
                {taskActionPending === 'clear' ? 'Clearing...' : 'Clear All Tasks'}
              </button>
            </div>
          </details>
          <button className="board-icon-btn board-icon-btn-primary" onClick={() => openCreateWorkModal()} title="Create Work" aria-label="Create Work">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><line x1="8" y1="2" x2="8" y2="14" /><line x1="2" y1="8" x2="14" y2="8" /></svg>
          </button>
        </div>
        <div className="board-grid">
          {boardColumns.map((column) => (
            <article className="board-col" key={column}>
              <h3>{humanizeLabel(column)}</h3>
              <div className="card-list">
                {(board.columns[column] || []).map((task) => (
                  (() => {
                    const waitingLabel = task.status !== 'cancelled' ? taskWaitingGateLabel(task) : null
                    const waitingKind = taskWaitingGateKind(task)
                    return (
                      <button
                        className={`task-card task-card-button${boardCompact ? ' task-card-compact' : ''}${waitingLabel ? ' task-card-awaiting-gate' : ''}`}
                        key={task.id}
                        onClick={() => handleTaskSelect(task.id, (task.pending_gate === 'before_implement' || task.pending_gate === 'before_generate_tasks') ? 'plan' : undefined)}
                        onContextMenu={(e) => {
                          e.preventDefault()
                          const menuW = 180
                          const menuH = 120
                          setContextMenu({ x: Math.max(0, Math.min(e.clientX, window.innerWidth - menuW)), y: Math.max(0, Math.min(e.clientY, window.innerHeight - menuH)), taskId: task.id })
                        }}
                      >
                        <p className="task-title">{task.title}</p>
                        {!boardCompact && <p className="task-meta">{task.priority} · {task.id.replace(/^task-/, '')}{task.parent_id ? ' · from plan' : ''}</p>}
                        {!boardCompact && waitingLabel ? (
                          <p className="task-meta">
                            <span className={`status-pill status-pill-inline status-pill-no-offset ${waitingKind === 'intervention_wait' ? 'status-blocked' : 'status-review'}`}>{waitingLabel}</span>
                          </p>
                        ) : null}
                        {!boardCompact && task.description ? <p className="task-desc">{task.description}</p> : null}
                      </button>
                    )
                  })()
                ))}
              </div>
            </article>
          ))}
        </div>
        {(dispatchBlockedReasonLabel(orchestrator?.dispatch_blocked_reason) || orchestrator?.integration_health?.status === 'degraded') ? (
        <div className="board-summary">
          {dispatchBlockedReasonLabel(orchestrator?.dispatch_blocked_reason) ? (
            <span className="field-label">Dispatch: {dispatchBlockedReasonLabel(orchestrator?.dispatch_blocked_reason)}</span>
          ) : null}
          {(() => {
            const ih = orchestrator?.integration_health
            if (ih?.status !== 'degraded') return null
            const fixId = ih.fix_task_id
            const fixTask = fixId ? Object.values(board.columns).flat().find((t) => t.id === fixId) : null
            const fixLabel = fixTask ? ` \u2014 fix ${humanizeLabel(fixTask.status)}` : ''
            return (
              <span
                className="field-label"
                style={{ color: 'var(--color-warning-700)' }}
                title={ih.failure_summary || undefined}
              >
                Post-merge tests: failing{fixLabel}
                {fixId ? (
                  <button
                    className="link-button"
                    style={{ marginLeft: '0.3rem', color: 'inherit', textDecoration: 'underline' }}
                    onClick={() => setSelectedTaskId(fixId)}
                  >
                    view fix
                  </button>
                ) : null}
              </span>
            )
          })()}
        </div>
        ) : null}
      </section>
    )
  }

  function renderExecution(): JSX.Element {
    const taskById = new Map<string, TaskRecord>()
    for (const tasks of Object.values(board.columns)) {
      for (const task of tasks) {
        taskById.set(task.id, task)
      }
    }
    const blockedCount = (board.columns.blocked || []).length
    const inProgressTasks = board.columns.in_progress || []
    const waitingApprovalCount = inProgressTasks.filter((task) => taskWaitingGateKind(task) === 'approval_wait').length
    const waitingInterventionCount = inProgressTasks.filter((task) => taskWaitingGateKind(task) === 'intervention_wait').length
    const runningNowCount = inProgressTasks.length - waitingApprovalCount - waitingInterventionCount
    return (
      <section className="panel">
        <header className="execution-header">
          <span className={`status-pill status-pill-prominent ${orchestrator?.status === 'running' ? (orchestrator?.draining ? 'status-blocked' : 'status-running') : orchestrator?.status === 'paused' ? 'status-paused' : 'status-failed'}`}>{humanizeLabel(orchestrator?.status === 'running' && orchestrator?.draining ? 'draining' : orchestrator?.status ?? 'unknown')}</span>
          <div className="execution-actions">
            {orchestrator?.status === 'running' ? <button className="button" onClick={() => void controlOrchestrator('pause')}>Pause</button> : null}
            {orchestrator?.status === 'paused' || orchestrator?.status === 'stopped' ? <button className="button" onClick={() => void controlOrchestrator('resume')}>Start</button> : null}
            {orchestrator?.status === 'running' && !orchestrator?.draining ? <button className="button" onClick={() => void controlOrchestrator('drain')}>Drain</button> : null}
            {orchestrator?.status !== 'stopped' ? <button className="button button-danger" onClick={() => void controlOrchestrator('stop')}>Stop</button> : null}
          </div>
        </header>
        <div className="status-grid">
          <button className={`status-card status-card-clickable${pipelineHighlightStatus === 'queued' ? ' status-card-active' : ''}`} onClick={() => setPipelineHighlightStatus((prev) => prev === 'queued' ? '' : 'queued')}>
            <span>Queue</span>
            <strong>{orchestrator?.queue_depth ?? 0}</strong>
          </button>
          <button className={`status-card status-card-clickable${pipelineHighlightStatus === 'in_progress' ? ' status-card-active' : ''}`} onClick={() => setPipelineHighlightStatus((prev) => prev === 'in_progress' ? '' : 'in_progress')}>
            <span>In Progress</span>
            <strong>{orchestrator?.in_progress ?? 0}</strong>
          </button>
          <button className="status-card">
            <span>Running now</span>
            <strong>{runningNowCount}</strong>
          </button>
          <button className="status-card">
            <span>Waiting approval</span>
            <strong>{waitingApprovalCount}</strong>
          </button>
          <button className="status-card">
            <span>Needs intervention</span>
            <strong>{waitingInterventionCount}</strong>
          </button>
          <button className={`status-card status-card-clickable${pipelineHighlightStatus === 'blocked' ? ' status-card-active' : ''}`} onClick={() => setPipelineHighlightStatus((prev) => prev === 'blocked' ? '' : 'blocked')}>
            <span>Blocked</span>
            <strong>{blockedCount}</strong>
          </button>
        </div>
        <div className="board-summary">
          <span className="field-label">Last tick: {toLocaleTimestamp(orchestrator?.last_tick_at) || '-'}</span>
          <span className="field-label">Tick lag: {Number.isFinite(Number(orchestrator?.tick_lag_seconds ?? NaN)) ? `${Number(orchestrator?.tick_lag_seconds)}s` : '-'}</span>
          <span className="field-label">Tick failures: {orchestrator?.consecutive_tick_failures ?? 0}</span>
          <span className="field-label">Last reconcile: {toLocaleTimestamp(orchestrator?.last_reconcile_at) || '-'}</span>
          <span className="field-label">Repairs: {orchestrator?.reconcile_repairs ?? 0}</span>
          {dispatchBlockedReasonLabel(orchestrator?.dispatch_blocked_reason) ? (
            <span className="field-label">Dispatch: {dispatchBlockedReasonLabel(orchestrator?.dispatch_blocked_reason)}</span>
          ) : null}
          {(() => {
            const ih = orchestrator?.integration_health
            if (ih?.status !== 'degraded') return null
            const fixId = ih.fix_task_id
            const fixTask = fixId ? Object.values(board.columns).flat().find((t) => t.id === fixId) : null
            const fixLabel = fixTask ? ` \u2014 fix ${humanizeLabel(fixTask.status)}` : ''
            return (
              <span
                className="field-label"
                style={{ color: 'var(--color-warning-700)' }}
                title={ih.failure_summary || undefined}
              >
                Post-merge tests: failing{fixLabel}
                {fixId ? (
                  <button
                    className="link-button"
                    style={{ marginLeft: '0.3rem', color: 'inherit', textDecoration: 'underline' }}
                    onClick={() => setSelectedTaskId(fixId)}
                  >
                    view fix
                  </button>
                ) : null}
              </span>
            )
          })()}
        </div>
        <div className="list-stack">
          <p className="field-label section-heading">Execution pipeline</p>
          {executionBatches.map((batch, index) => (
            <div className="wave-card" key={`batch-${index}`}>
              <div className="wave-label">
                <p className="wave-title">Wave {index + 1}</p>
                <p className="wave-count">{batch.length} {batch.length === 1 ? 'task' : 'tasks'}</p>
              </div>
              <div className="wave-tasks">
                {batch.map((taskId, i) => {
                  const task = taskById.get(taskId)
                  const label = task?.title || taskId
                  const status = task?.status || ''
                  return (
                    <span key={`batch-${index}-${taskId}`} className="wave-task-item">
                      {i > 0 ? <span className="wave-sep">|</span> : null}
                      <span className={`wave-task-content${pipelineHighlightStatus && status === pipelineHighlightStatus ? ' wave-task-highlight' : ''}`}>
                        <button className="link-button" onClick={() => handleTaskSelect(taskId, 'logs')}>{label}</button>
                        {status ? <span className={`status-pill status-pill-inline ${statusPillClass(status)}`}>{humanizeLabel(status)}</span> : null}
                      </span>
                    </span>
                  )
                })}
              </div>
            </div>
          ))}
          {executionBatches.length === 0 && executionCompleted.length === 0 ? <p className="empty">No tasks.</p> : null}
          {executionCompleted.length > 0 ? (
            <div style={{ marginTop: '0.5rem' }}>
              <p className="field-label section-heading">Completed ({executionCompleted.length})</p>
              <div className="wave-card wave-card-completed">
                <div className="wave-tasks">
                  {executionCompleted.map((taskId, i) => {
                    const task = taskById.get(taskId)
                    const label = task?.title || taskId
                    const status = task?.status || ''
                    return (
                      <span key={`completed-${taskId}`} className="wave-task-item">
                        {i > 0 ? <span className="wave-sep">|</span> : null}
                        <span className="wave-task-content">
                          <button className="link-button" onClick={() => handleTaskSelect(taskId, 'logs')}>{label}</button>
                          {status ? <span className={`status-pill status-pill-inline ${statusPillClass(status)}`}>{humanizeLabel(status)}</span> : null}
                        </span>
                      </span>
                    )
                  })}
                </div>
              </div>
            </div>
          ) : null}
        </div>
        <div className="list-stack">
          <p className="field-label section-heading">Runtime metrics</p>
          <div className="row-card">
            <p className="task-meta">
              Tasks completed: {metrics?.tasks_completed ?? 0} ·
              Worker time: {formatDuration(metrics?.worker_time_seconds ?? 0)} ·
              Tokens: {(metrics?.tokens_used ?? 0).toLocaleString()} ·
              Est. cost: {metrics?.cost_available ? `$${(metrics.estimated_cost_usd ?? 0).toFixed(2)}` : 'N/A'}
            </p>
          </div>
        </div>
        {(() => {
          const routingByStep = new Map(workerRoutingRows.map((row) => [row.step, row]))
          const resolvedProvider = (task: TaskRecord): string => {
            const step = String(task.current_step || '').trim() || (isPlanningTaskType(task.task_type) ? 'plan' : 'implement')
            return routingByStep.get(step)?.provider || workerDefaultProvider
          }
          return (
            <div className="list-stack">
              <p className="field-label section-heading">Worker Snapshot</p>
              {inProgressTasks.map((task) => (
                <div className="row-card" key={task.id}>
                  <div>
                    <p className="task-title">{task.title}</p>
                    <p className="task-meta">
                      {task.id} · step: {task.current_step || 'implement'}
                    </p>
                  </div>
                  <div className="inline-actions">
                    <span className="status-pill status-running">{resolvedProvider(task)}</span>
                  </div>
                </div>
              ))}
              {inProgressTasks.length === 0 ? <p className="empty">No tasks currently in progress.</p> : null}
            </div>
          )
        })()}
      </section>
    )
  }

  function renderSettings(): JSX.Element {
    const promptSteps = Array.from(
      new Set([
        ...Object.keys(settingsPromptDefaults),
        ...Object.keys(settingsPromptOverrides),
        ...Object.keys(settingsLegacyPromptInjections),
      ])
    ).sort()
    const activePromptStep = promptSteps.includes(settingsPromptStep)
      ? settingsPromptStep
      : (promptSteps[0] || '')
    const defaultPromptText = activePromptStep ? (settingsPromptDefaults[activePromptStep] || '') : ''
    const overridePromptText = activePromptStep ? (settingsPromptOverrides[activePromptStep] || '') : ''

    const healthOrder = ['codex', 'claude', 'ollama']
    const providerHealth = [...workerHealth].sort((a, b) => {
      const aIndex = healthOrder.indexOf(a.name)
      const bIndex = healthOrder.indexOf(b.name)
      const aRank = aIndex === -1 ? 999 : aIndex
      const bRank = bIndex === -1 ? 999 : bIndex
      if (aRank !== bRank) return aRank - bRank
      return a.name.localeCompare(b.name)
    })
    let editableRoutingMap: Record<string, string> = {}
    try {
      editableRoutingMap = parseStringMap(settingsWorkerRouting, 'Worker routing map')
    } catch {
      editableRoutingMap = {}
    }
    const dropdownProviders = Array.from(
      new Set(
        workerHealth
          .filter((item) => item.configured || item.healthy)
          .map((item) => item.name)
          .concat(workerDefaultProvider || 'codex')
      )
    ).sort((a, b) => a.localeCompare(b))
    const statusClass = (status: WorkerHealthRecord['status']): string => {
      if (status === 'connected') return 'status-pill status-running'
      if (status === 'not_configured') return 'status-pill status-paused'
      return 'status-pill status-failed'
    }
    const stepLabel = (step: string): string => {
      const normalized = String(step || '').trim().toLowerCase()
      if (normalized === 'plan') return 'Task Planning'
      return humanizeLabel(normalized)
    }

    return (
      <section className="panel">
        <div className="tab-row" role="tablist" aria-label="Settings tabs">
          {SETTINGS_TABS.map((tab) => (
            <button
              key={tab.key}
              role="tab"
              aria-selected={settingsTab === tab.key}
              className={`tab ${settingsTab === tab.key ? 'is-active' : ''}`}
              onClick={() => setSettingsTab(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {settingsTab === 'providers' ? (
          <div className="settings-tab-content">
            <article className="settings-card">
              <div className="settings-card-head">
                <h3>Default Provider</h3>
                <button
                  className={`topbar-icon-btn ${workerHealthRefreshing ? 'is-loading' : ''}`}
                  onClick={() => void handleRecheckProviders()}
                  disabled={workerHealthRefreshing}
                  aria-busy={workerHealthRefreshing}
                  aria-label="Recheck providers"
                  title="Recheck providers"
                >
                  <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M1.5 2v4.5H6" /><path d="M1.8 6.5A6.5 6.5 0 1 1 2.5 10" /></svg>
                </button>
              </div>
              <div className="list-stack">
                {providerHealth.map((provider) => (
                  <div
                    className={`row-card row-card-clickable${settingsWorkerDefault === provider.name ? ' row-card-default' : ''}`}
                    key={provider.name}
                    onClick={() => { if ((provider.configured || provider.healthy) && settingsWorkerDefault !== provider.name) void setDefaultProvider(provider.name) }}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(event) => { if ((event.key === 'Enter' || event.key === ' ') && (provider.configured || provider.healthy) && settingsWorkerDefault !== provider.name) void setDefaultProvider(provider.name) }}
                  >
                    <div>
                      <p className="task-title">
                        {humanizeLabel(provider.name)}
                        <span className="task-meta" style={{ fontWeight: 'normal', marginLeft: '0.5em' }}>
                          {[provider.model, provider.command, provider.endpoint].filter(Boolean).join(' · ')}
                        </span>
                      </p>
                      <p className="task-meta">{provider.detail || 'No diagnostics message.'}</p>
                    </div>
                    <div className="inline-actions">
                      {settingsWorkerDefault === provider.name ? (
                        <span className="status-pill status-pill-default">Default</span>
                      ) : null}
                      <span className={statusClass(provider.status)}>{humanizeLabel(provider.status)}</span>
                    </div>
                  </div>
                ))}
                {providerHealth.length === 0 ? <p className="empty">No providers detected.</p> : null}
              </div>
            </article>

            <form className="form-stack" onSubmit={(event) => void saveSettings(event)}>
              <article className="settings-card">
                <h3>Provider Configuration</h3>
                <div className="form-stack">
                  <select
                    id="settings-provider-view"
                    aria-label="Configure provider"
                    value={settingsProviderView}
                    onChange={(event) => setSettingsProviderView(event.target.value as 'codex' | 'ollama' | 'claude')}
                  >
                    <option value="codex">codex</option>
                    <option value="ollama">ollama</option>
                    <option value="claude">claude</option>
                  </select>
                  <div className="provider-grid">
                    {settingsProviderView === 'codex' ? (
                      <div className="provider-card">

                      <label className="field-label" htmlFor="settings-codex-command">Codex command</label>
                      <input
                        id="settings-codex-command"
                        value={settingsCodexCommand}
                        onChange={(event) => { setSettingsCodexCommand(event.target.value); setProviderConfigDirty(true) }}
                        placeholder="codex exec"
                      />
                      <label className="field-label" htmlFor="settings-codex-model">Codex model (optional)</label>
                      <input
                        id="settings-codex-model"
                        value={settingsCodexModel}
                        onChange={(event) => { setSettingsCodexModel(event.target.value); setProviderConfigDirty(true) }}
                        placeholder="gpt-5.3-codex"
                      />
                      <label className="field-label" htmlFor="settings-codex-execution-mode">Codex execution mode</label>
                      <select
                        id="settings-codex-execution-mode"
                        value={settingsCodexExecutionMode}
                        onChange={(event) => { setSettingsCodexExecutionMode(event.target.value as 'sandboxed' | 'host_access'); setProviderConfigDirty(true) }}
                      >
                        <option value="sandboxed">sandboxed</option>
                        <option value="host_access">host_access</option>
                      </select>
                      <label className="field-label" htmlFor="settings-codex-effort">Codex effort (optional)</label>
                      <select
                        id="settings-codex-effort"
                        value={settingsCodexEffort}
                        onChange={(event) => { setSettingsCodexEffort(event.target.value); setProviderConfigDirty(true) }}
                      >
                        <option value="">(none)</option>
                        <option value="low">low</option>
                        <option value="medium">medium</option>
                        <option value="high">high</option>
                      </select>
                      </div>
                    ) : null}
                    {settingsProviderView === 'ollama' ? (
                      <div className="provider-card">

                      <label className="field-label" htmlFor="settings-ollama-endpoint">Ollama endpoint</label>
                      <input
                        id="settings-ollama-endpoint"
                        value={settingsOllamaEndpoint}
                        onChange={(event) => { setSettingsOllamaEndpoint(event.target.value); setProviderConfigDirty(true) }}
                        placeholder="http://localhost:11434"
                      />
                      <label className="field-label" htmlFor="settings-ollama-model">Ollama model</label>
                      <input
                        id="settings-ollama-model"
                        value={settingsOllamaModel}
                        onChange={(event) => { setSettingsOllamaModel(event.target.value); setProviderConfigDirty(true) }}
                        placeholder="llama3.1:8b"
                      />
                      <div className="inline-actions">
                        <input
                          aria-label="Ollama temperature"
                          value={settingsOllamaTemperature}
                          onChange={(event) => { setSettingsOllamaTemperature(event.target.value); setProviderConfigDirty(true) }}
                          placeholder="temperature"
                        />
                        <input
                          aria-label="Ollama num ctx"
                          value={settingsOllamaNumCtx}
                          onChange={(event) => { setSettingsOllamaNumCtx(event.target.value); setProviderConfigDirty(true) }}
                          placeholder="num_ctx"
                        />
                      </div>
                      </div>
                    ) : null}
                    {settingsProviderView === 'claude' ? (
                      <div className="provider-card">

                      <label className="field-label" htmlFor="settings-claude-command">Claude command</label>
                      <input
                        id="settings-claude-command"
                        value={settingsClaudeCommand}
                        onChange={(event) => { setSettingsClaudeCommand(event.target.value); setProviderConfigDirty(true) }}
                        placeholder="claude -p"
                      />
                      <label className="field-label" htmlFor="settings-claude-model">Claude model (optional)</label>
                      <input
                        id="settings-claude-model"
                        value={settingsClaudeModel}
                        onChange={(event) => { setSettingsClaudeModel(event.target.value); setProviderConfigDirty(true) }}
                        placeholder="sonnet"
                      />
                      <label className="field-label" htmlFor="settings-claude-execution-mode">Claude execution mode</label>
                      <select
                        id="settings-claude-execution-mode"
                        value={settingsClaudeExecutionMode}
                        onChange={(event) => { setSettingsClaudeExecutionMode(event.target.value as 'sandboxed' | 'host_access'); setProviderConfigDirty(true) }}
                      >
                        <option value="host_access">host_access</option>
                        <option value="sandboxed">sandboxed</option>
                      </select>
                      <label className="field-label" htmlFor="settings-claude-effort">Claude effort (optional)</label>
                      <select
                        id="settings-claude-effort"
                        value={settingsClaudeEffort}
                        onChange={(event) => { setSettingsClaudeEffort(event.target.value); setProviderConfigDirty(true) }}
                      >
                        <option value="">(none)</option>
                        <option value="low">low</option>
                        <option value="medium">medium</option>
                        <option value="high">high</option>
                      </select>
                      </div>
                    ) : null}
                  </div>
                </div>
                <div className="inline-actions" style={{ marginTop: '0.5rem' }}>
                  <button className="button button-primary button-sm" type="submit" disabled={settingsSaving || !providerConfigDirty}>
                    {settingsSaving ? 'Saving...' : 'Save'}
                  </button>
                  {settingsError ? <span className="error-banner">{settingsError}</span> : null}
                  {settingsSuccess ? <span className="field-label">{settingsSuccess}</span> : null}
                </div>
              </article>
            </form>

            <article className="settings-card">
              <h3>Step Routing</h3>
              <div className="routing-table">
                {workerRoutingRows.map((row) => (
                  <div className="routing-row" key={row.step}>
                    <span className="routing-step">{stepLabel(row.step)}</span>
                    <select
                      aria-label={`Route ${row.step} provider`}
                      value={editableRoutingMap[row.step] ?? ''}
                      onChange={(event) => updateWorkerRoute(row.step, event.target.value)}
                    >
                      <option value="">Use default ({workerDefaultProvider})</option>
                      {dropdownProviders.map((providerName) => (
                        <option key={`${row.step}-${providerName}`} value={providerName}>
                          {providerName}
                        </option>
                      ))}
                    </select>
                  </div>
                ))}
                {workerRoutingRows.length === 0 ? <p className="empty">No routing rules configured; default applies to all steps.</p> : null}
              </div>
              <details className="advanced-fields workers-advanced">
                <summary>Advanced</summary>
                <form className="advanced-fields-body form-stack" onSubmit={(event) => void saveWorkerMaps(event)}>
                  <label className="field-label" htmlFor="settings-worker-routing">Worker routing map (JSON object: step {'->'} provider)</label>
                  <div className="json-editor-group">
                    <textarea
                      id="settings-worker-routing"
                      className="json-editor-textarea"
                      rows={4}
                      value={settingsWorkerRouting}
                      onChange={(event) => setSettingsWorkerRouting(event.target.value)}
                      placeholder={WORKER_ROUTING_EXAMPLE}
                    />
                    <div className="inline-actions json-editor-actions">
                      <button
                        className="button"
                        type="button"
                        onClick={() => handleFormatJsonField('Worker routing map', settingsWorkerRouting, setSettingsWorkerRouting)}
                      >
                        Format
                      </button>
                      <button className="button" type="button" onClick={() => setSettingsWorkerRouting('')}>
                        Clear
                      </button>
                    </div>
                  </div>
                  <label
                    className="field-label"
                    htmlFor="settings-worker-providers"
                    title="Configure reasoning effort in your CLI setup first (Codex/Claude profile or config). Overdrive only passes flags supported by your installed CLI version."
                  >
                    Worker providers (JSON object, optional advanced overrides)
                  </label>
                  <div className="json-editor-group">
                    <textarea
                      id="settings-worker-providers"
                      className="json-editor-textarea"
                      rows={8}
                      value={settingsWorkerProviders}
                      onChange={(event) => { setSettingsWorkerProviders(event.target.value); setAdvancedRoutingDirty(true) }}
                      placeholder={WORKER_PROVIDERS_EXAMPLE}
                    />
                    <div className="inline-actions json-editor-actions">
                      <button
                        className="button"
                        type="button"
                        onClick={() => handleFormatJsonField('Worker providers', settingsWorkerProviders, setSettingsWorkerProviders)}
                      >
                        Format
                      </button>
                      <button className="button" type="button" onClick={() => setSettingsWorkerProviders('')}>
                        Clear
                      </button>
                    </div>
                  </div>
                  <div className="inline-actions">
                    <button className="button button-primary" type="submit" disabled={settingsSaving || !advancedRoutingDirty}>
                      {settingsSaving ? 'Saving...' : 'Save'}
                    </button>
                    <button className="button" type="button" onClick={() => void loadSettings()} disabled={settingsLoading}>
                      {settingsLoading ? 'Loading...' : 'Reload'}
                    </button>
                  </div>
                  {settingsError ? <p className="error-banner">{settingsError}</p> : null}
                  {settingsSuccess ? <p className="field-label">{settingsSuccess}</p> : null}
                </form>
              </details>
            </article>
          </div>
        ) : null}

        {settingsTab === 'execution' ? (
          <div className="settings-tab-content">
              <article className="settings-card">
                <h3>Task Defaults</h3>
                <p className="field-label" style={{ marginBottom: '0.3rem' }}>
                  Applied to newly created tasks. You can still override these per task.
                </p>
                <div className="settings-field-group">
                  <p className="field-label">Default HITL mode</p>
                  <HITLModeSelector
                    currentMode={settingsDefaultHitlMode}
                    onModeChange={(mode) => { setSettingsDefaultHitlMode(mode as 'autopilot' | 'supervised' | 'review_only'); void patchSettings({ defaults: { hitl_mode: mode } }) }}
                    projectDir={projectDir}
                    compact
                  />
                </div>
                <div className="settings-field-group">
                  <p className="field-label">Dependency policy</p>
                  <div className="toggle-group" role="group" aria-label="Default dependency policy">
                    {(['permissive', 'prudent', 'strict'] as const).map((level) => (
                      <button
                        key={level}
                        type="button"
                        className={`toggle-button ${settingsDependencyPolicy === level ? 'is-active' : ''}`}
                        aria-pressed={settingsDependencyPolicy === level}
                        onClick={() => { setSettingsDependencyPolicy(level); void patchSettings({ defaults: { dependency_policy: level } }) }}
                      >
                        {humanizeLabel(level)}
                      </button>
                    ))}
                  </div>
                  <p className="field-label">
                    {settingsDependencyPolicy === 'permissive' && 'Workers will prefer well-maintained libraries over manual implementation and install what they need.'}
                    {settingsDependencyPolicy === 'prudent' && 'Workers will prefer existing dependencies but may install new ones when manual implementation would be unreliable or disproportionately complex.'}
                    {settingsDependencyPolicy === 'strict' && 'Workers must not install any new dependencies. All solutions must use only what is already in the project.'}
                  </p>
                </div>
              </article>

            <form className="form-stack" onSubmit={(event) => void saveSettings(event)}>
              <article className="settings-card">
                <h3>Project Commands</h3>
                <div className="form-stack">
                  <label className="field-label" htmlFor="settings-project-commands">Project commands by language (YAML format)</label>
                  <p className="field-label">
                    Used by workers during implement/review steps. Keys are language names (`python`, `typescript`, `go`) and each language supports `test`, `lint`, `typecheck`, `format`.
                  </p>
                  <div className="json-editor-group">
                    <textarea
                      id="settings-project-commands"
                      className="json-editor-textarea"
                      rows={8}
                      value={settingsProjectCommands}
                      onChange={(event) => { setSettingsProjectCommands(event.target.value); setProjectCommandsDirty(true) }}
                      placeholder={PROJECT_COMMANDS_EXAMPLE}
                    />
                    <div className="inline-actions json-editor-actions">
                      <button
                        className="button"
                        type="button"
                        onClick={() => {
                          try {
                            setSettingsProjectCommands(formatProjectCommandsField(settingsProjectCommands))
                            setSettingsError('')
                          } catch (err) {
                            setSettingsError(err instanceof Error ? err.message : 'Invalid project commands')
                          }
                        }}
                      >
                        Format
                      </button>
                      <button className="button" type="button" onClick={() => setSettingsProjectCommands('')}>
                        Clear
                      </button>
                    </div>
                  </div>
                </div>
                <div className="inline-actions" style={{ marginTop: '0.5rem' }}>
                  <button className="button button-primary button-sm" type="submit" disabled={settingsSaving || !projectCommandsDirty}>
                    {settingsSaving ? 'Saving...' : 'Save'}
                  </button>
                  {settingsError ? <span className="error-banner">{settingsError}</span> : null}
                  {settingsSuccess ? <span className="field-label">{settingsSuccess}</span> : null}
                </div>
              </article>
            </form>

            {autoDetectedDefaults && (
              <article className="settings-card">
                <h3>Auto-detected Defaults</h3>
                <p className="field-label" style={{ marginBottom: '0.75rem' }}>
                  What workers will use when no overrides are configured. Toggle individual commands or override by editing Project Commands above.
                </p>

                {autoDetectedDefaults.languages.length > 0 && (
                  <div style={{ marginBottom: '0.75rem' }}>
                    <span className="field-label" style={{ fontWeight: 600 }}>Languages: </span>
                    {autoDetectedDefaults.languages.map((lang) => (
                      <span key={lang} className="pr-review-badge" style={{ marginRight: '0.25rem' }}>{lang}</span>
                    ))}
                  </div>
                )}

                {autoDetectedDefaults.venv && (
                  <div style={{ marginBottom: '0.75rem' }}>
                    <span className="field-label" style={{ fontWeight: 600 }}>Python venv: </span>
                    <code style={{ fontSize: '0.85em' }}>{autoDetectedDefaults.venv.path}</code>
                    <span className="field-label"> ({autoDetectedDefaults.venv.source})</span>
                  </div>
                )}

                {Object.keys(autoDetectedDefaults.commands).length > 0 && (
                  <div style={{ marginBottom: '0.75rem' }}>
                    <span className="field-label" style={{ fontWeight: 600, display: 'block', marginBottom: '0.25rem' }}>Project commands:</span>
                    <div className="auto-detected-commands">
                      {Object.entries(autoDetectedDefaults.commands).map(([lang, cmds]) => (
                        <div key={lang} style={{ marginBottom: '0.5rem' }}>
                          <span style={{ fontSize: '0.85em', fontWeight: 600 }}>{lang}</span>
                          <table className="auto-detected-table">
                            <tbody>
                              {Object.entries(cmds).map(([step, cmd]) => {
                                const key = `${lang}.${step}`
                                const isDisabled = disabledCommands.has(key)
                                const hasBinaryWarning = (autoDetectedDefaults.binary_warnings[lang] || []).includes(step)
                                return (
                                  <tr key={step} style={isDisabled ? { opacity: 0.5 } : undefined}>
                                    <td className="auto-detected-step" style={{ whiteSpace: 'nowrap' }}>
                                      <label className="toggle-switch toggle-switch-sm" style={{ marginRight: '0.4rem', verticalAlign: 'middle' }}>
                                        <input type="checkbox" checked={!isDisabled} onChange={() => void toggleDisabledCommand(key)} />
                                        <span className="toggle-slider" />
                                      </label>
                                      {step}
                                    </td>
                                    <td>
                                      <code style={{ fontSize: '0.85em' }}>{cmd}</code>
                                      {hasBinaryWarning && !isDisabled && (
                                        <span className="binary-warning" title="Binary not found on PATH">not found</span>
                                      )}
                                    </td>
                                  </tr>
                                )
                              })}
                            </tbody>
                          </table>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {resolvedEnvVars.length > 0 && (
                  <div>
                    <span className="field-label" style={{ fontWeight: 600, display: 'block', marginBottom: '0.25rem' }}>Environment variables:</span>
                    <table className="auto-detected-table">
                      <tbody>
                        {resolvedEnvVars.map((v) => (
                          <tr key={v.key}>
                            <td className="auto-detected-step"><code style={{ fontSize: '0.85em' }}>{v.key}</code></td>
                            <td className="field-label">{v.source}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {autoDetectedDefaults.languages.length === 0 && Object.keys(autoDetectedDefaults.commands).length === 0 && !autoDetectedDefaults.venv && resolvedEnvVars.length === 0 && (
                  <p className="field-label">No languages or commands detected for this project.</p>
                )}
              </article>
            )}

            <form className="form-stack" onSubmit={(event) => void saveSettings(event)}>
              <article className="settings-card">
                <h3>Quality Gate Thresholds</h3>
                <div className="form-stack">
                  <p className="field-label">
                    Unresolved findings allowed before a task can pass quality checks.
                  </p>
                  <div className="quality-gate-grid">
                    <div className="quality-gate-row">
                      <div>
                        <p className="quality-gate-label">
                          <span className="quality-severity-badge severity-critical">Critical</span>
                        </p>
                        <p className="field-label">Release-blocking or security-critical issues.</p>
                      </div>
                      <div className="quality-gate-input-wrap">
                        <label className="field-label" htmlFor="quality-gate-critical-input">Allowed unresolved</label>
                        <input
                          id="quality-gate-critical-input"
                          aria-label="Quality gate critical"
                          value={settingsGateCritical}
                          onChange={(event) => { setSettingsGateCritical(event.target.value); setQualityGateDirty(true) }}
                          inputMode="numeric"
                        />
                      </div>
                    </div>
                    <div className="quality-gate-row">
                      <div>
                        <p className="quality-gate-label">
                          <span className="quality-severity-badge severity-high">High</span>
                        </p>
                        <p className="field-label">Major correctness or reliability problems.</p>
                      </div>
                      <div className="quality-gate-input-wrap">
                        <label className="field-label" htmlFor="quality-gate-high-input">Allowed unresolved</label>
                        <input
                          id="quality-gate-high-input"
                          aria-label="Quality gate high"
                          value={settingsGateHigh}
                          onChange={(event) => { setSettingsGateHigh(event.target.value); setQualityGateDirty(true) }}
                          inputMode="numeric"
                        />
                      </div>
                    </div>
                    <div className="quality-gate-row">
                      <div>
                        <p className="quality-gate-label">
                          <span className="quality-severity-badge severity-medium">Medium</span>
                        </p>
                        <p className="field-label">Important issues that should be addressed soon.</p>
                      </div>
                      <div className="quality-gate-input-wrap">
                        <label className="field-label" htmlFor="quality-gate-medium-input">Allowed unresolved</label>
                        <input
                          id="quality-gate-medium-input"
                          aria-label="Quality gate medium"
                          value={settingsGateMedium}
                          onChange={(event) => { setSettingsGateMedium(event.target.value); setQualityGateDirty(true) }}
                          inputMode="numeric"
                        />
                      </div>
                    </div>
                    <div className="quality-gate-row">
                      <div>
                        <p className="quality-gate-label">
                          <span className="quality-severity-badge severity-low">Low</span>
                        </p>
                        <p className="field-label">Minor issues, cleanup, and polish improvements.</p>
                      </div>
                      <div className="quality-gate-input-wrap">
                        <label className="field-label" htmlFor="quality-gate-low-input">Allowed unresolved</label>
                        <input
                          id="quality-gate-low-input"
                          aria-label="Quality gate low"
                          value={settingsGateLow}
                          onChange={(event) => { setSettingsGateLow(event.target.value); setQualityGateDirty(true) }}
                          inputMode="numeric"
                        />
                      </div>
                    </div>
                  </div>
                </div>
                <div className="inline-actions" style={{ marginTop: '0.5rem' }}>
                  <button className="button button-primary button-sm" type="submit" disabled={settingsSaving || !qualityGateDirty}>
                    {settingsSaving ? 'Saving...' : 'Save'}
                  </button>
                </div>
              </article>
            </form>

            <form className="form-stack" onSubmit={(event) => void saveSettings(event)}>
              <article className="settings-card">
                <h3>Execution Controls</h3>
                <div className="form-stack">
                  <label className="field-label" htmlFor="settings-step-timeout-seconds">Default task timeout (seconds, 0 = no timeout)</label>
                  <input
                    id="settings-step-timeout-seconds"
                    value={settingsStepTimeoutSeconds}
                    onChange={(event) => { setSettingsStepTimeoutSeconds(event.target.value); setExecutionFormDirty(true) }}
                    inputMode="numeric"
                  />
                  <label className="field-label" htmlFor="settings-review-attempts">Max review attempts</label>
                  <input
                    id="settings-review-attempts"
                    value={settingsMaxReviewAttempts}
                    onChange={(event) => { setSettingsMaxReviewAttempts(event.target.value); setExecutionFormDirty(true) }}
                    inputMode="numeric"
                  />
                  <label className="field-label" htmlFor="settings-merge-conflict-attempts">Max merge conflict attempts</label>
                  <input
                    id="settings-merge-conflict-attempts"
                    value={settingsMaxMergeConflictAttempts}
                    onChange={(event) => { setSettingsMaxMergeConflictAttempts(event.target.value); setExecutionFormDirty(true) }}
                    inputMode="numeric"
                  />
                  <label className="field-label" htmlFor="settings-concurrency">Orchestrator concurrency</label>
                  <input
                    id="settings-concurrency"
                    value={settingsConcurrency}
                    onChange={(event) => { setSettingsConcurrency(event.target.value); setExecutionFormDirty(true) }}
                    inputMode="numeric"
                  />
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={settingsAutoDeps}
                      onChange={(event) => { setSettingsAutoDeps(event.target.checked); setExecutionFormDirty(true) }}
                    />
                    Auto dependency analysis
                  </label>
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={settingsAutoFixPush}
                      onChange={(event) => { setSettingsAutoFixPush(event.target.checked); setExecutionFormDirty(true) }}
                    />
                    Auto-fix push errors (use worker to fix pre-push hook failures and retry)
                  </label>
                </div>
                <div className="inline-actions" style={{ marginTop: '0.5rem' }}>
                  <button className="button button-primary button-sm" type="submit" disabled={settingsSaving || !executionFormDirty}>
                    {settingsSaving ? 'Saving...' : 'Save'}
                  </button>
                </div>
              </article>
            </form>
          </div>
        ) : null}

        {settingsTab === 'advanced' ? (
          <div className="settings-tab-content">
            <form className="form-stack" onSubmit={(event) => void saveSettings(event)}>
              <article className="settings-card">
                <h3>Advanced Prompting</h3>
                <div className="form-stack">
                  <p className="field-label">
                    Review per-step built-in instructions and optionally define an override.
                  </p>
                  <label className="field-label" htmlFor="settings-step-prompt-step">Pipeline step</label>
                  <select
                    id="settings-step-prompt-step"
                    value={activePromptStep}
                    onChange={(event) => { setSettingsPromptStep(event.target.value); setAdvancedFormDirty(true) }}
                    disabled={promptSteps.length === 0}
                  >
                    {promptSteps.length === 0 ? <option value="">No configurable steps</option> : null}
                    {promptSteps.map((step) => (
                      <option key={step} value={step}>{step}</option>
                    ))}
                  </select>
                  <label className="field-label" htmlFor="settings-step-prompt-default">Default prompt</label>
                  <textarea
                    id="settings-step-prompt-default"
                    className="settings-prompt-textarea"
                    rows={10}
                    value={defaultPromptText}
                    readOnly
                    placeholder="No default prompt available for this step."
                  />
                  <label className="field-label" htmlFor="settings-step-prompt-override">Override prompt (optional)</label>
                  <div className="json-editor-group">
                    <textarea
                      id="settings-step-prompt-override"
                      className="json-editor-textarea settings-prompt-textarea"
                      rows={10}
                      value={overridePromptText}
                      onChange={(event) => {
                        const nextValue = event.target.value
                        setSettingsPromptOverrides((prev) => {
                          if (!activePromptStep) return prev
                          const next = { ...prev }
                          if (nextValue.trim()) {
                            next[activePromptStep] = nextValue
                          } else {
                            delete next[activePromptStep]
                          }
                          return next
                        })
                        if (!activePromptStep) return
                        setSettingsPromptOverrideClears((prev) => {
                          const next = { ...prev }
                          if (nextValue.trim()) {
                            delete next[activePromptStep]
                          } else {
                            next[activePromptStep] = true
                          }
                          return next
                        })
                        setAdvancedFormDirty(true)
                      }}
                      placeholder="Leave blank to use default prompt."
                    />
                    <div className="inline-actions json-editor-actions">
                      <button
                        className="button"
                        type="button"
                        onClick={() => {
                          if (!activePromptStep) return
                          setSettingsPromptOverrides((prev) => {
                            const next = { ...prev }
                            delete next[activePromptStep]
                            return next
                          })
                          setSettingsPromptOverrideClears((prev) => ({ ...prev, [activePromptStep]: true }))
                        }}
                        disabled={!activePromptStep || !overridePromptText}
                      >
                        Clear override
                      </button>
                    </div>
                  </div>
                </div>
              </article>

              <article className="settings-card">
                <h3>Advanced Role Routing</h3>
                <div className="form-stack">
                  <label className="field-label" htmlFor="settings-default-role">Default role</label>
                  <input
                    id="settings-default-role"
                    value={settingsDefaultRole}
                    onChange={(event) => { setSettingsDefaultRole(event.target.value); setAdvancedFormDirty(true) }}
                    placeholder="general"
                  />
                  <label className="field-label" htmlFor="settings-task-type-roles">Task type role map (JSON object)</label>
                  <p className="field-label">Maps <code>task_type</code> {'->'} <code>role</code>. Unmapped task types use Default role.</p>
                  <div className="json-editor-group">
                    <textarea
                      id="settings-task-type-roles"
                      className="json-editor-textarea"
                      rows={4}
                      value={settingsTaskTypeRoles}
                      onChange={(event) => { setSettingsTaskTypeRoles(event.target.value); setAdvancedFormDirty(true) }}
                      placeholder={TASK_TYPE_ROLE_MAP_EXAMPLE}
                    />
                    <div className="inline-actions json-editor-actions">
                      <button
                        className="button"
                        type="button"
                        onClick={() => {
                          handleFormatJsonField('Task type role map', settingsTaskTypeRoles, setSettingsTaskTypeRoles)
                        }}
                      >
                        Format
                      </button>
                      <button className="button" type="button" onClick={() => setSettingsTaskTypeRoles('')}>
                        Clear
                      </button>
                    </div>
                  </div>
                  <label className="field-label" htmlFor="settings-role-overrides">Role provider overrides (JSON object)</label>
                  <p className="field-label">Maps <code>role</code> {'->'} <code>provider</code> when that role executes.</p>
                  <div className="json-editor-group">
                    <textarea
                      id="settings-role-overrides"
                      className="json-editor-textarea"
                      rows={4}
                      value={settingsRoleProviderOverrides}
                      onChange={(event) => { setSettingsRoleProviderOverrides(event.target.value); setAdvancedFormDirty(true) }}
                      placeholder={ROLE_PROVIDER_OVERRIDES_EXAMPLE}
                    />
                    <div className="inline-actions json-editor-actions">
                      <button
                        className="button"
                        type="button"
                        onClick={() => handleFormatJsonField('Role provider overrides', settingsRoleProviderOverrides, setSettingsRoleProviderOverrides)}
                      >
                        Format
                      </button>
                      <button className="button" type="button" onClick={() => setSettingsRoleProviderOverrides('')}>
                        Clear
                      </button>
                    </div>
                  </div>
                </div>
                <div className="inline-actions" style={{ marginTop: '0.5rem' }}>
                  <button className="button button-primary button-sm" type="submit" disabled={settingsSaving || !advancedFormDirty}>
                    {settingsSaving ? 'Saving...' : 'Save'}
                  </button>
                  {settingsError ? <span className="error-banner">{settingsError}</span> : null}
                  {settingsSuccess ? <span className="field-label">{settingsSuccess}</span> : null}
                </div>
              </article>
            </form>

            <article className="settings-card">
              <h3>Pinned Projects</h3>
              <div className="settings-field-group">
                <form className="pin-project-row" onSubmit={(event) => void pinManualProject(event)}>
                  <input
                    id="manual-project-path"
                    value={manualPinPath}
                    onChange={(event) => setManualPinPath(event.target.value)}
                    placeholder="/absolute/path/to/repo"
                    required
                    style={{ flex: 1 }}
                  />
                  <label className="checkbox-row" style={{ whiteSpace: 'nowrap', fontSize: '0.8rem' }}>
                    <input
                      type="checkbox"
                      checked={allowNonGit}
                      onChange={(event) => setAllowNonGit(event.target.checked)}
                    />
                    Non-git
                  </label>
                  <button className="button button-primary button-sm" type="submit">Pin</button>
                </form>
              </div>
              {pinnedProjects.length > 0 ? (
                <div className="settings-field-group">
                  {pinnedProjects.map((project) => (
                    <div className="pinned-project-row" key={project.id}>
                      <span className="pinned-project-path" title={project.path}>{project.path}</span>
                      <button className="button-icon button-icon-danger" type="button" onClick={() => void unpinProject(project.id)} aria-label="Unpin" title="Unpin">&times;</button>
                    </div>
                  ))}
                </div>
              ) : null}
            </article>
          </div>
        ) : null}
      </section>
    )
  }

  function openCreateWorkModal(tab: CreateTab = 'task', taskTypeOverride?: string): void {
    setCreateTab(tab)
    if (tab === 'task') {
      shouldPrefillTaskProjectCommandsRef.current = true
      setNewTaskHitlMode(settingsDefaultHitlMode)
      if (taskTypeOverride) {
        setNewTaskType(taskTypeOverride)
      }
      if (!newTaskProjectCommands.trim() && settingsProjectCommands.trim()) {
        setNewTaskProjectCommands(settingsProjectCommands)
        shouldPrefillTaskProjectCommandsRef.current = false
      }
      if (!settingsLoadedRef.current && !settingsLoading) {
        void loadSettings()
      }
    }
    if (tab === 'review') {
      setSelectedPrNumber(null)
      setPrReviewGuidance('')
      setPrReviewMode(ReviewMode.FixOnly)
      void fetchPullRequests()
      void fetchPlatformStatus()
    }
    setWorkOpen(true)
  }

  function renderPlatformSetupGuidance(errorCode: string | null, platform: string | null): JSX.Element {
    const platformLabel = platform === 'gitlab' ? 'GitLab' : 'GitHub'
    const cliName = platform === 'gitlab' ? 'glab' : 'gh'
    const prLabel = platform === 'gitlab' ? 'merge requests' : 'pull requests'
    if (errorCode === 'no_remote' || !platform) {
      return (
        <div className="pr-review-setup-help">
          <p className="pr-review-setup-help-title">Set up a git remote</p>
          <p>This project does not appear to have a GitHub or GitLab remote configured. To use review features, connect your repository:</p>
          <ol className="pr-review-setup-steps">
            <li><span className="pr-review-step-label">Check your current remotes:</span><pre>git remote -v</pre></li>
            <li><span className="pr-review-step-label">Add a remote if none exists:</span><pre>git remote add origin https://gitlab.com/your-group/your-repo.git{'\n'}# or: git remote add origin https://github.com/your-org/your-repo.git</pre></li>
            <li><span className="pr-review-step-label">Verify the remote is configured:</span><pre>git remote -v</pre></li>
          </ol>
        </div>
      )
    }

    if (errorCode === 'cli_not_installed') {
      return (
        <div className="pr-review-setup-help">
          <p className="pr-review-setup-help-title">Install the {platformLabel} CLI</p>
          <p>The <code>{cliName}</code> command-line tool is required to list and review {prLabel}.</p>
          <ol className="pr-review-setup-steps">
            <li>
              <span className="pr-review-step-label">Install <code>{cliName}</code>:</span>
              <div className="pr-review-setup-os-tabs">
                {platform === 'github' ? (
                  <>
                    <div><span className="pr-review-os-label">macOS</span><pre>brew install gh</pre></div>
                    <div><span className="pr-review-os-label">Linux</span><pre>sudo apt install gh{'\n'}# or: sudo dnf install gh</pre></div>
                    <div><span className="pr-review-os-label">Windows</span><pre>winget install GitHub.cli{'\n'}# or: scoop install gh</pre></div>
                  </>
                ) : (
                  <>
                    <div><span className="pr-review-os-label">macOS</span><pre>brew install glab</pre></div>
                    <div><span className="pr-review-os-label">Linux</span><pre>sudo apt install glab</pre></div>
                    <div><span className="pr-review-os-label">Windows</span><pre>winget install GLab.GLab{'\n'}# or: scoop install glab</pre></div>
                  </>
                )}
              </div>
            </li>
            <li><span className="pr-review-step-label">Authenticate:</span><pre>{cliName} auth login</pre></li>
            <li><span className="pr-review-step-label">Verify it works:</span><pre>{platform === 'github' ? 'gh pr list' : 'glab mr list'}</pre></li>
          </ol>
        </div>
      )
    }

    if (errorCode === 'cli_not_authenticated') {
      return (
        <div className="pr-review-setup-help">
          <p className="pr-review-setup-help-title">Authenticate the {platformLabel} CLI</p>
          <p>The <code>{cliName}</code> CLI is installed but does not appear to be authenticated.</p>
          <ol className="pr-review-setup-steps">
            <li><span className="pr-review-step-label">Log in:</span><pre>{cliName} auth login</pre></li>
            <li><span className="pr-review-step-label">Verify authentication:</span><pre>{cliName} auth status</pre></li>
            <li><span className="pr-review-step-label">Test access to this repo:</span><pre>{platform === 'github' ? 'gh pr list' : 'glab mr list'}</pre></li>
          </ol>
        </div>
      )
    }

    // cli_error or unknown
    return (
      <div className="pr-review-setup-help">
        <p>Check that the {platformLabel} CLI is installed, authenticated, and can access this repository:</p>
        <pre>{platform === 'github' ? 'gh pr list' : 'glab mr list'}</pre>
        <p>If the issue persists, try re-authenticating:</p>
        <pre>{cliName} auth login</pre>
      </div>
    )
  }

  function renderRoute(): JSX.Element {
    if (route === 'execution') return renderExecution()
    if (route === 'settings') return renderSettings()
    return renderBoard()
  }

  return (
    <div className="orchestrator-app">
      <div className="bg-layer" aria-hidden="true" />
      <header className="topbar">
        <div>
          <h1>Overdrive</h1>
        </div>
        <div className="topbar-actions">
          <select
            className="topbar-project-select"
            value={projectDir}
            onFocus={() => setTopbarProjectPickerFocused(true)}
            onBlur={() => setTopbarProjectPickerFocused(false)}
            onChange={(event) => {
              setTopbarProjectPickerFocused(false)
              void handleTopbarProjectChange(event.target.value)
            }}
            aria-label="Active repo"
          >
            <option value="">Current workspace</option>
            {projects.map((project) => (
              <option key={`${project.id}-${project.path}`} value={project.path}>
                {(!topbarProjectPickerFocused && project.path === projectDir) ? repoNameFromPath(project.path) : project.path}
              </option>
            ))}
            <option value={ADD_REPO_VALUE}>Add repo...</option>
          </select>
          <button className="topbar-icon-btn" onClick={() => void refreshWithSchedulerRepair()} disabled={loading} title="Refresh" aria-label="Refresh">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M1.5 2v4.5H6" /><path d="M1.8 6.5A6.5 6.5 0 1 1 2.5 10" /></svg>
          </button>
          <button
            className="topbar-icon-btn"
            onClick={() => setThemeMode((m) => m === 'light' ? 'dark' : m === 'dark' ? 'system' : 'light')}
            title={`Theme: ${themeMode}`}
            aria-label={`Theme: ${themeMode}`}
          >
            {themeMode === 'light' && <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><circle cx="8" cy="8" r="3" /><path d="M8 1.5v1M8 13.5v1M1.5 8h1M13.5 8h1M3.4 3.4l.7.7M11.9 11.9l.7.7M3.4 12.6l.7-.7M11.9 4.1l.7-.7" /></svg>}
            {themeMode === 'dark' && <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M13.5 8.5a5.5 5.5 0 1 1-6-6 4.5 4.5 0 0 0 6 6z" /></svg>}
            {themeMode === 'system' && <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="12" height="8" rx="1" /><path d="M5 14h6M8 11v3" /></svg>}
          </button>
        </div>
      </header>

      <div className="nav-mobile-select-wrap">
        <label className="field-label" htmlFor="mobile-route-select">View</label>
        <select
          id="mobile-route-select"
          className="nav-mobile-select"
          value={route}
          onChange={(event) => handleRouteChange(event.target.value as RouteKey)}
          aria-label="Main navigation"
        >
          {ROUTES.map((item) => (
            <option key={`mobile-route-${item.key}`} value={item.key}>{item.label}</option>
          ))}
        </select>
      </div>

      <nav className="nav-strip" aria-label="Main navigation">
        {ROUTES.map((item) => (
          <button
            key={item.key}
            className={`nav-pill ${route === item.key ? 'is-active' : ''}`}
            onClick={() => handleRouteChange(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <main className="main-content">{renderRoute()}</main>

      {selectedTaskId && selectedTaskView && modalExplicitRef.current && !modalDismissedRef.current ? (
        <div className="modal-scrim modal-scrim-task-detail" role="dialog" aria-modal="true" aria-label="Task detail" onClick={(event) => { if (event.target === event.currentTarget) { modalDismissedRef.current = true; modalExplicitRef.current = false; setSelectedTaskId('') } }} onKeyDown={(event) => { if (event.key === 'Escape') { modalDismissedRef.current = true; modalExplicitRef.current = false; setSelectedTaskId('') } }}>
          <div className="modal-card task-detail-modal">
            <header className="task-detail-modal-head">
              <div className="task-detail-modal-head-row">
                <h2>{selectedTaskView.title}</h2>
                <span className={`status-pill status-pill-prominent ${statusPillClass(selectedTaskStatusDisplay.classStatus)}`}>{selectedTaskStatusDisplay.label}</span>
              </div>
              {(selectedTaskView.pipeline_template || []).length > 0 && selectedTaskView.pending_gate !== 'pipeline_classify' && selectedTaskView.pending_gate !== 'select_pipeline' ? (() => {
                const pipelineSteps = selectedTaskView.pipeline_template!
                const currentStep = selectedTaskView.current_step || null
                const pipelinePhase = String(selectedTaskView.metadata?.pipeline_phase || '') || currentStep
                const isDone = selectedTaskView.status === 'done'
                const phaseIdx = pipelinePhase ? pipelineSteps.indexOf(pipelinePhase) : -1
                return (
                  <div className="pipeline-flow">
                    {pipelineSteps.map((step, idx) => {
                      const isCurrent = step === pipelinePhase && !isDone
                      const isCompleted = isDone || (phaseIdx >= 0 && idx < phaseIdx)
                      const cls = isCurrent ? 'pipeline-step is-current' : isCompleted ? 'pipeline-step is-completed' : 'pipeline-step'
                      return (
                        <span key={step} className="pipeline-step-wrap">
                          {idx > 0 ? <span className={`pipeline-arrow${isCompleted || isCurrent ? ' is-reached' : ''}`} /> : null}
                          <span className={cls}>{humanizeLabel(step)}</span>
                        </span>
                      )
                    })}
                  </div>
                )
              })() : null}
            </header>
            <div className="task-detail-modal-body">
              {taskDetailContent}
            </div>
            <footer className="task-detail-modal-foot">
              {taskActionMessage ? <p className="field-label">{taskActionMessage}</p> : null}
              {taskActionError ? <div className="error-banner"><span>{taskActionError}</span><button type="button" className="error-banner-dismiss" onClick={() => setTaskActionError('')} aria-label="Dismiss">&times;</button></div> : null}
              {taskStatus === 'blocked' ? (
                <>
                  <div className="inline-actions blocked-retry-row">
                    <input
                      className="review-guidance-input"
                      value={reviewGuidance}
                      onChange={(event) => setReviewGuidance(event.target.value)}
                      placeholder="Guidance for retry..."
                      disabled={isTaskActionBusy || hasUnresolvedBlockers}
                    />
                    <select
                      className="retry-step-select"
                      value={retryFromStep}
                      onChange={(event) => setRetryFromStep(event.target.value)}
                      disabled={isTaskActionBusy || hasUnresolvedBlockers}
                    >
                      <option value="">From beginning</option>
                      {selectedTaskView.pending_gate !== 'pipeline_classify' && selectedTaskView.pending_gate !== 'select_pipeline' && (selectedTaskView.pipeline_template || []).map((step) => (
                        <option key={step} value={step}>{humanizeLabel(step)}</option>
                      ))}
                      {selectedTaskView.pending_gate !== 'pipeline_classify' && selectedTaskView.pending_gate !== 'select_pipeline' && selectedTaskView.current_step && !(selectedTaskView.pipeline_template || []).includes(selectedTaskView.current_step) && (
                        <option value={selectedTaskView.current_step}>{humanizeLabel(selectedTaskView.current_step)}</option>
                      )}
                    </select>
                    <select
                      className="retry-step-select"
                      value={retryProvider}
                      onChange={(event) => setRetryProvider(event.target.value)}
                      disabled={isTaskActionBusy || hasUnresolvedBlockers}
                    >
                      <option value="">{humanizeLabel(workerDefaultProvider)} (default)</option>
                      {workerHealth.filter((p) => (p.configured || p.healthy) && p.name !== workerDefaultProvider).map((p) => (
                        <option key={p.name} value={p.name}>{humanizeLabel(p.name)}</option>
                      ))}
                    </select>
                    <button
                      className="button button-primary"
                      onClick={() => void retryTask(selectedTaskView.id, retryFromStep || undefined)}
                      disabled={isTaskActionBusy || hasUnresolvedBlockers}
                      title={hasUnresolvedBlockers ? `Blocked by: ${unresolvedBlockers.map((id) => describeTask(id, taskIndex).label).join(', ')}` : undefined}
                    >
                      {taskActionPending === 'retry' ? 'Retrying...' : 'Retry'}
                    </button>
                  </div>
                  {hasUnresolvedBlockers ? (
                    <p className="field-label">Unresolved {unresolvedBlockers.length === 1 ? 'dependency' : 'dependencies'}: {unresolvedBlockers.map((id) => describeTask(id, taskIndex).label).join(', ')}</p>
                  ) : null}
                </>
              ) : null}
              <div className="inline-actions">
                {taskStatus === 'backlog' ? (
                  <>
                    <button className="button button-primary" onClick={() => void transitionTask(selectedTaskView.id, 'queued')} disabled={isTaskActionBusy}>{taskActionPending === 'transition' && taskActionDetail === 'queued' ? 'Queuing...' : 'Queue'}</button>
                    <button className="button button-danger" onClick={() => void transitionTask(selectedTaskView.id, 'cancelled')} disabled={isTaskActionBusy}>{taskActionPending === 'transition' && taskActionDetail === 'cancelled' ? 'Cancelling...' : 'Cancel'}</button>
                  </>
                ) : null}
                {taskStatus === 'queued' ? (
                  <>
                    <button className="button" onClick={() => void transitionTask(selectedTaskView.id, 'backlog')} disabled={isTaskActionBusy}>{taskActionPending === 'transition' && taskActionDetail === 'backlog' ? 'Moving...' : 'Move to Backlog'}</button>
                    <button className="button button-danger" onClick={() => void transitionTask(selectedTaskView.id, 'cancelled')} disabled={isTaskActionBusy}>{taskActionPending === 'transition' && taskActionDetail === 'cancelled' ? 'Cancelling...' : 'Cancel'}</button>
                  </>
                ) : null}
                {taskStatus === 'in_progress' ? (
                  <button className="button button-danger" onClick={() => void transitionTask(selectedTaskView.id, 'cancelled')} disabled={isTaskActionBusy}>{taskActionPending === 'transition' && taskActionDetail === 'cancelled' ? 'Cancelling...' : 'Cancel'}</button>
                ) : null}
                {taskStatus === 'in_review' ? (
                  <>
                    <button className="button button-primary" onClick={() => void approveTask(selectedTaskView.id)} disabled={isTaskActionBusy}>{taskActionPending === 'transition' && taskActionDetail === 'done' ? 'Approving...' : 'Approve'}</button>
                    <input
                      className="review-guidance-input"
                      value={reviewGuidance}
                      onChange={(event) => setReviewGuidance(event.target.value)}
                      placeholder="Guidance for changes..."
                      disabled={isTaskActionBusy}
                    />
                    <button className="button" onClick={() => void requestChanges(selectedTaskView.id)} disabled={isTaskActionBusy}>{taskActionPending === 'transition' && taskActionDetail === 'request_changes' ? 'Requesting...' : 'Request Changes'}</button>
                    <button className="button button-danger" onClick={() => void transitionTask(selectedTaskView.id, 'cancelled')} disabled={isTaskActionBusy}>{taskActionPending === 'transition' && taskActionDetail === 'cancelled' ? 'Cancelling...' : 'Cancel'}</button>
                  </>
                ) : null}
                {taskStatus === 'blocked' ? (
                  <>
                    {selectedTaskView.can_skip_to_precommit ? (
                      <button className="button button-primary" onClick={() => void skipToPrecommit(selectedTaskView.id)} disabled={isTaskActionBusy}>{taskActionPending === 'transition' && taskActionDetail === 'skip_to_precommit' ? 'Moving...' : 'Skip to Pre-commit Review'}</button>
                    ) : null}
                    {selectedTaskView.metadata?.merge_conflict ? (
                      <button
                        className="button button-primary"
                        onClick={() => void finalizeMergeConflict(selectedTaskView.id)}
                        disabled={isTaskActionBusy || !selectedTaskView.can_finalize_merge_conflict}
                        title={!selectedTaskView.can_finalize_merge_conflict && selectedTaskView.finalize_merge_conflict_reason_code
                          ? `Unavailable: ${humanizeLabel(selectedTaskView.finalize_merge_conflict_reason_code)}`
                          : undefined}
                      >
                        {taskActionPending === 'transition' && taskActionDetail === 'finalize_merge_conflict'
                          ? 'Finalizing...'
                          : 'Finalize Manual Merge'}
                      </button>
                    ) : null}
                    <button className="button button-danger" onClick={() => void transitionTask(selectedTaskView.id, 'cancelled')} disabled={isTaskActionBusy}>{taskActionPending === 'transition' && taskActionDetail === 'cancelled' ? 'Cancelling...' : 'Cancel'}</button>
                  </>
                ) : null}
                {taskStatus === 'cancelled' ? (
                  <>
                    <button className="button" onClick={() => void transitionTask(selectedTaskView.id, 'backlog')} disabled={isTaskActionBusy}>{taskActionPending === 'transition' && taskActionDetail === 'backlog' ? 'Moving...' : 'Move to Backlog'}</button>
                    <button className="button button-danger" onClick={() => void deleteTask(selectedTaskView.id)} disabled={isTaskActionBusy}>{taskActionPending === 'delete' ? 'Deleting...' : 'Delete'}</button>
                  </>
                ) : null}
                {taskStatus === 'done' && selectedTaskView.execution_summary?.steps.some((s) => s.commit) ? (
                  <button className="button" onClick={() => void reviewCommit(selectedTaskView.id)} disabled={isTaskActionBusy}>{taskActionPending === 'review_commit' ? 'Creating...' : 'Review Commit'}</button>
                ) : null}
                {(() => {
                  const gc = selectedTaskView.metadata?.generated_review_comments as Array<{ post_status?: string }> | undefined
                  const hasComments = Array.isArray(gc) && gc.length > 0
                  const hasStagedOrFailed = hasComments && gc.some((c) => (c.post_status ?? 'staged') !== 'posted')
                  const showPostButton = hasComments && (taskStatus === 'done' || taskStatus === 'in_review') && (selectedTaskView.metadata?.comment_dry_run || hasStagedOrFailed)
                  if (!showPostButton) return null
                  const postableSelectedCount = Array.from(selectedCommentIndices).filter((idx) => {
                    const c = gc[idx]
                    return c && (c.post_status ?? 'staged') !== 'posted'
                  }).length
                  return (
                    <>
                      <button
                        className="button button-primary"
                        onClick={() => { setPostCommentsError(null); setPostCommentsErrorCode(null); void postReviewComments(selectedTaskView.id) }}
                        disabled={isTaskActionBusy || postableSelectedCount === 0}
                      >
                        {taskActionPending === 'post_review_comments' ? 'Posting...' : `Post Selected Comments (${postableSelectedCount})`}
                      </button>
                      {postCommentsError && (postCommentsErrorCode === 'cli_not_installed' || postCommentsErrorCode === 'cli_not_authenticated') ? (
                        <div className="pr-review-setup pr-review-setup-inline">
                          {renderPlatformSetupGuidance(postCommentsErrorCode, (selectedTaskView.metadata?.comment_platform as Record<string, string> | undefined)?.platform ?? platformStatus?.platform ?? prPlatform ?? null)}
                        </div>
                      ) : null}
                    </>
                  )
                })()}
                {taskSupportsPostCompletionGeneration(selectedTaskView) ? (
                  <button className="button button-primary" onClick={() => void generateFollowUpTasks(selectedTaskView.id)} disabled={isTaskActionBusy}>{taskActionPending === 'generate_follow_ups' ? 'Generating...' : 'Generate Follow-Up Tasks'}</button>
                ) : null}
                {taskStatus === 'done' ? (
                  <button className="button button-danger" onClick={() => void deleteTask(selectedTaskView.id)} disabled={isTaskActionBusy}>{taskActionPending === 'delete' ? 'Deleting...' : 'Delete'}</button>
                ) : null}
                <span className="foot-spacer" />
                <button className="button" onClick={() => { modalDismissedRef.current = true; modalExplicitRef.current = false; setSelectedTaskId('') }}>Close</button>
              </div>
            </footer>
          </div>
        </div>
      ) : null}

      {contextMenu ? (() => {
        const ctxTask = taskIndex.get(contextMenu.taskId)
        const ctxStatus = ctxTask?.status
        return (
          <div className="context-menu" role="menu" style={{ top: contextMenu.y, left: contextMenu.x }}>
            <button role="menuitem" onClick={() => { void navigator.clipboard.writeText(contextMenu.taskId); setContextMenu(null) }}>Copy Task ID</button>
            {ctxStatus === 'done' ? (
              <button role="menuitem" disabled={isTaskActionBusy} onClick={() => { setContextMenu(null); void reviewCommit(contextMenu.taskId) }}>Review Commit</button>
            ) : null}
            {ctxStatus === 'done' && taskSupportsPostCompletionGeneration(ctxTask) ? (
              <button role="menuitem" disabled={isTaskActionBusy} onClick={() => { setContextMenu(null); void generateFollowUpTasks(contextMenu.taskId) }}>Generate Follow-Up Tasks</button>
            ) : null}
            {ctxStatus === 'in_progress' || ctxStatus === 'blocked' ? (
              <button role="menuitem" className="context-menu-danger" disabled={isTaskActionBusy} onClick={() => { setContextMenu(null); void transitionTask(contextMenu.taskId, 'cancelled') }}>Cancel Task</button>
            ) : null}
            {ctxStatus === 'backlog' || ctxStatus === 'queued' || ctxStatus === 'done' || ctxStatus === 'cancelled' ? (
              <button role="menuitem" className="context-menu-danger" disabled={isTaskActionBusy} onClick={() => { setContextMenu(null); void deleteTask(contextMenu.taskId) }}>Delete Task</button>
            ) : null}
          </div>
        )
      })() : null}

      {error ? <div className="error-banner"><span>{error}</span><button type="button" className="error-banner-dismiss" onClick={() => setError('')} aria-label="Dismiss">&times;</button></div> : null}

      {workOpen ? (
        <div className="modal-scrim" role="dialog" aria-modal="true" aria-label="Create Work modal" onClick={(event) => { if (event.target === event.currentTarget) setWorkOpen(false) }} onKeyDown={(event) => { if (event.key === 'Escape') setWorkOpen(false) }}>
          <div className="modal-card create-work-modal">
            <div className="modal-sticky-top">
              <header className="panel-head">
                <h2>Create Work</h2>
                <button className="topbar-icon-btn" onClick={() => setWorkOpen(false)} title="Close" aria-label="Close">&times;</button>
              </header>

              <div className="tab-row">
                <button className={`tab ${createTab === 'task' ? 'is-active' : ''}`} onClick={() => setCreateTab('task')}>Create Task</button>
                <button className={`tab ${createTab === 'import' ? 'is-active' : ''}`} onClick={() => setCreateTab('import')}>Import PRD</button>
                <button className={`tab ${createTab === 'review' ? 'is-active' : ''}`} onClick={() => { setCreateTab('review'); void fetchPullRequests(); void fetchPlatformStatus() }}>Review PR/MR</button>
              </div>
            </div>

            <div className="modal-body">
              {createTab === 'task' ? (
                <form id="create-task-form" className="form-stack create-task-form" onSubmit={(event) => void submitTask(event, 'queued')}>
                  <label className="field-label" htmlFor="task-title">Title</label>
                  <input id="task-title" value={newTaskTitle} onChange={(event) => setNewTaskTitle(event.target.value)} required />
                  <label className="field-label" htmlFor="task-description">Description</label>
                  <textarea id="task-description" rows={4} value={newTaskDescription} onChange={(event) => setNewTaskDescription(event.target.value)} />
                  <label className="field-label" htmlFor="task-type">Task Type</label>
                  <select
                    id="task-type"
                    value={newTaskType}
                    onChange={(event) => setNewTaskType(event.target.value)}
                  >
                    {TASK_TYPE_OPTIONS.map((taskType) => (
                      <option key={taskType} value={taskType}>
                        {humanizeLabel(taskType)}
                      </option>
                    ))}
                  </select>
                  <label className="field-label">HITL mode</label>
                  <HITLModeSelector
                    currentMode={newTaskHitlMode}
                    onModeChange={setNewTaskHitlMode}
                    projectDir={projectDir}
                  />
                  <label className="field-label" htmlFor="task-project-commands">Project commands override (optional)</label>
                  <textarea
                    id="task-project-commands"
                    className="json-editor-textarea"
                    rows={4}
                    value={newTaskProjectCommands}
                    onChange={(event) => {
                      shouldPrefillTaskProjectCommandsRef.current = false
                      setNewTaskProjectCommands(event.target.value)
                    }}
                    placeholder={PROJECT_COMMANDS_EXAMPLE}
                  />
                  <details className="advanced-fields">
                    <summary>Advanced</summary>
                    <div className="form-stack advanced-fields-body">
                      <label className="field-label">Priority</label>
                      <div className="toggle-group" role="group" aria-label="Task priority">
                        {['P0', 'P1', 'P2', 'P3'].map((priority) => (
                          <button
                            key={priority}
                            type="button"
                            className={`toggle-button ${newTaskPriority === priority ? 'is-active' : ''}`}
                            aria-pressed={newTaskPriority === priority}
                            onClick={() => setNewTaskPriority(priority)}
                          >
                            {priority}
                          </button>
                        ))}
                      </div>
                      <label className="field-label">Dependency policy</label>
                      <div className="toggle-group" role="group" aria-label="Dependency policy">
                        {(['permissive', 'prudent', 'strict'] as const).map((level) => (
                          <button
                            key={level}
                            type="button"
                            className={`toggle-button ${newTaskDependencyPolicy === level ? 'is-active' : ''}`}
                            aria-pressed={newTaskDependencyPolicy === level}
                            onClick={() => setNewTaskDependencyPolicy(level)}
                          >
                            {humanizeLabel(level)}
                          </button>
                        ))}
                      </div>
                      <p className="field-label">
                        {newTaskDependencyPolicy === 'permissive' && 'Prefer libraries over manual implementation. Install what you need.'}
                        {newTaskDependencyPolicy === 'prudent' && 'Prefer existing deps. Only add new ones when manual implementation would be unreliable or complex.'}
                        {newTaskDependencyPolicy === 'strict' && 'No new dependencies allowed. Work only with what is already installed.'}
                      </p>
                      <label className="field-label" htmlFor="task-labels">Labels (comma-separated)</label>
                      <input
                        id="task-labels"
                        value={newTaskLabels}
                        onChange={(event) => setNewTaskLabels(event.target.value)}
                        placeholder="frontend, urgent"
                      />
                      <label className="field-label" htmlFor="task-blocked-by">Depends on task IDs (comma-separated)</label>
                      <input
                        id="task-blocked-by"
                        value={newTaskBlockedBy}
                        onChange={(event) => setNewTaskBlockedBy(event.target.value)}
                        placeholder="task-abc123, task-def456"
                      />
                      <label className="field-label" htmlFor="task-parent-id">Parent task ID (optional)</label>
                      <input
                        id="task-parent-id"
                        value={newTaskParentId}
                        onChange={(event) => setNewTaskParentId(event.target.value)}
                        placeholder="task-parent-id"
                      />
                      <label className="field-label" htmlFor="task-pipeline-template">Pipeline template steps (comma-separated, optional)</label>
                      <input
                        id="task-pipeline-template"
                        value={newTaskPipelineTemplate}
                        onChange={(event) => setNewTaskPipelineTemplate(event.target.value)}
                        placeholder="plan, implement, verify, review"
                      />
                      <label className="field-label" htmlFor="task-step-timeout">Task timeout for implement step (seconds, optional)</label>
                      <input
                        id="task-step-timeout"
                        value={newTaskStepTimeout}
                        onChange={(event) => setNewTaskStepTimeout(event.target.value)}
                        inputMode="numeric"
                        placeholder="600"
                      />
                      <label className="field-label" htmlFor="task-worker-model">Worker model override (optional)</label>
                      <input
                        id="task-worker-model"
                        value={newTaskWorkerModel}
                        onChange={(event) => setNewTaskWorkerModel(event.target.value)}
                        placeholder="gpt-5-codex"
                      />
                      <label className="field-label" htmlFor="task-worker-provider">Worker provider override (optional)</label>
                      <select
                        id="task-worker-provider"
                        value={newTaskWorkerProvider}
                        onChange={(event) => setNewTaskWorkerProvider(event.target.value)}
                      >
                        <option value="">{humanizeLabel(workerDefaultProvider)} (default)</option>
                        {workerHealth.filter((p) => (p.configured || p.healthy) && p.name !== workerDefaultProvider).map((p) => (
                          <option key={p.name} value={p.name}>{humanizeLabel(p.name)}</option>
                        ))}
                      </select>
                      <label className="field-label" htmlFor="task-metadata">Metadata JSON object (optional)</label>
                      <textarea
                        id="task-metadata"
                        rows={4}
                        value={newTaskMetadata}
                        onChange={(event) => setNewTaskMetadata(event.target.value)}
                        placeholder='{"epic":"checkout","owner":"web"}'
                      />
                    </div>
                  </details>
                </form>
              ) : null}

              {createTab === 'import' ? (
                <div className="form-stack">
                  <form className="form-stack" onSubmit={(event) => void previewImport(event)}>
                    <label className="field-label" htmlFor="prd-text">PRD text</label>
                    <textarea id="prd-text" rows={8} value={importText} onChange={(event) => setImportText(event.target.value)} placeholder="- Task 1\n- Task 2" required />
                    <button className="button" type="submit">Preview</button>
                  </form>
                  <ImportJobPanel
                    importJobId={importJobId}
                    importPreview={importPreview}
                    recentImportJobIds={recentImportJobIds}
                    selectedImportJobId={selectedImportJobId}
                    selectedImportJob={selectedImportJob}
                    selectedImportJobLoading={selectedImportJobLoading}
                    selectedImportJobError={`${selectedImportJobError}${selectedImportJobErrorAt ? ` at ${selectedImportJobErrorAt}` : ''}`}
                    selectedCreatedTaskIds={recentImportCommitMap[selectedImportJobId] || selectedImportJob?.created_task_ids || []}
                    onCommitImport={() => void commitImport()}
                    onSelectImportJob={setSelectedImportJobId}
                    onRefreshImportJob={() => void loadImportJobDetail(selectedImportJobId)}
                    onRetryLoadImportJob={() => void loadImportJobDetail(selectedImportJobId)}
                  />
                </div>
              ) : null}

              {createTab === 'review' ? (
                <div className="form-stack">
                  {platformStatus?.setup_steps && !platformStatus.setup_steps.every((s) => s.done) ? (
                    <div className="pr-review-status-checklist">
                      {platformStatus.setup_steps.map((s) => (
                        <span key={s.step} className={`pr-review-status-item ${s.done ? 'is-done' : 'is-pending'}`}>
                          <span className="pr-review-status-icon">{s.done ? '\u2713' : '\u2022'}</span>
                          {s.label}
                        </span>
                      ))}
                    </div>
                  ) : null}
                  {prListLoading ? <p className="text-muted">Loading open {prPlatform === 'gitlab' ? 'merge requests' : 'pull requests'}…</p> : null}
                  {prListError ? (
                    <div className="pr-review-setup">
                      <p className="pr-review-setup-error">{prListError}</p>
                      {renderPlatformSetupGuidance(prErrorCode, prPlatform)}
                    </div>
                  ) : null}
                  {!prListLoading && !prListError && prList.length === 0 ? (
                    <p className="text-muted">No open {prPlatform === 'gitlab' ? 'merge requests' : 'pull requests'} found.</p>
                  ) : null}
                  {!prListLoading && prList.length > 0 ? (
                    <div className="pr-review-list" role="radiogroup" aria-label={`Open ${prPlatform === 'gitlab' ? 'merge requests' : 'pull requests'}`}>
                      {prList.map((pr) => (
                        <button
                          key={pr.number}
                          type="button"
                          className={`pr-review-row ${selectedPrNumber === pr.number ? 'is-selected' : ''} ${pr.has_review_task ? 'is-reviewed' : ''}`}
                          onClick={() => !pr.has_review_task && setSelectedPrNumber(pr.number)}
                          disabled={pr.has_review_task}
                          role="radio"
                          aria-checked={selectedPrNumber === pr.number}
                        >
                          <span className="pr-review-number">{prPlatform === 'gitlab' ? '!' : '#'}{pr.number}</span>
                          <span className="pr-review-title">{pr.title}</span>
                          <span className="pr-review-meta">
                            <span className="pr-review-author">{pr.author}</span>
                            <span className="pr-review-branches">{pr.base_ref} ← {pr.head_ref}</span>
                          </span>
                          {pr.has_review_task ? <span className="pr-review-badge">{prPlatform === 'gitlab' ? 'MR review exists' : 'PR review exists'}</span> : null}
                        </button>
                      ))}
                    </div>
                  ) : null}
                  <div className="review-mode-group">
                    <label className="field-label" htmlFor="review-mode">Review mode</label>
                    <select
                      id="review-mode"
                      value={prReviewMode}
                      onChange={(e) => setPrReviewMode(e.target.value as ReviewMode)}
                    >
                      {REVIEW_MODE_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                    <p className="review-mode-hint text-muted">{REVIEW_MODE_OPTIONS.find((o) => o.value === prReviewMode)?.description}</p>
                  </div>
                  <label className="field-label" htmlFor="pr-review-guidance">Review guidance (optional)</label>
                  <textarea
                    id="pr-review-guidance"
                    rows={3}
                    value={prReviewGuidance}
                    onChange={(e) => setPrReviewGuidance(e.target.value)}
                    placeholder="e.g. Focus on error handling in the auth module…"
                  />
                </div>
              ) : null}

            </div>
            {createTab === 'task' ? (
              <div className="modal-footer">
                <button className="button button-primary" type="submit" form="create-task-form" disabled={isSubmittingTask}>{submittingTaskTarget === 'queued' ? 'Creating…' : 'Create & Queue'}</button>
                <button className="button" type="button" onClick={(event) => void submitTask(event, 'backlog')} disabled={isSubmittingTask}>{submittingTaskTarget === 'backlog' ? 'Creating…' : 'Add to Backlog'}</button>
              </div>
            ) : null}
            {createTab === 'review' ? (
              <div className="modal-footer">
                <button
                  className="button button-primary"
                  type="button"
                  disabled={selectedPrNumber === null || isSubmittingReview || prList.find((p) => p.number === selectedPrNumber)?.has_review_task === true}
                  onClick={() => void submitPrReview()}
                >
                  {isSubmittingReview ? 'Creating…' : 'Create Review'}
                </button>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      {browseOpen ? (
        <div className="modal-scrim" role="dialog" aria-modal="true" aria-label="Browse repositories" onClick={(event) => { if (event.target === event.currentTarget) setBrowseOpen(false) }} onKeyDown={(event) => { if (event.key === 'Escape') setBrowseOpen(false) }}>
          <div className="modal-card">
            <header className="panel-head">
              <h2>Browse Repositories</h2>
              <button className="topbar-icon-btn" onClick={() => setBrowseOpen(false)} title="Close" aria-label="Close">&times;</button>
            </header>
            <div className="browse-toolbar">
              <button className="topbar-icon-btn" onClick={() => void loadBrowseDirectories(browseParentPath || undefined)} disabled={!browseParentPath || browseLoading} title="Up" aria-label="Up">
                &#x2191;
              </button>
              <button className="topbar-icon-btn" onClick={() => void loadBrowseDirectories(browsePath || undefined)} disabled={browseLoading} title="Refresh" aria-label="Refresh">
                &#x21BB;
              </button>
              <div className="browse-path-wrap">
                <input
                  className={`browse-path-input ${browseCurrentIsGit ? 'is-git' : ''}`}
                  value={browsePath}
                  onChange={(event) => setBrowsePath(event.target.value)}
                  onKeyDown={(event) => { if (event.key === 'Enter' && browsePath) void loadBrowseDirectories(browsePath) }}
                  aria-label="Browse path"
                />
                {browseCurrentIsGit ? <span className="git-chip">Git repo</span> : null}
              </div>
              <button className="topbar-icon-btn" onClick={() => void loadBrowseDirectories(browsePath || undefined)} disabled={!browsePath || browseLoading} title="Go" aria-label="Go">
                &#x2192;
              </button>
            </div>

            {browseError ? <p className="error-banner">{browseError}</p> : null}
            <div className="browse-list">
              {browseDirectories.map((entry) => (
                <button
                  key={entry.path}
                  className={`browse-item ${entry.is_git ? 'is-git' : ''}`}
                  onClick={() => void loadBrowseDirectories(entry.path)}
                >
                  <span className="browse-item-name">{entry.name}</span>
                  <span className="browse-item-kind">{entry.is_git ? 'git' : 'dir'}</span>
                </button>
              ))}
              {browseDirectories.length === 0 && !browseLoading ? <p className="empty">No directories found.</p> : null}
            </div>
            <div className="browse-actions">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={browseAllowNonGit}
                  onChange={(event) => setBrowseAllowNonGit(event.target.checked)}
                />
                Allow non-git directory
              </label>
              <button className="button button-primary" onClick={() => void pinFromBrowse()} disabled={!browsePath || browseLoading}>
                Pin this folder
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <div className={`terminal-float ${terminalOpen ? 'is-open' : ''}`}>
        <TerminalPanel projectDir={projectDir} visible={terminalOpen} onMinimize={() => setTerminalOpen(false)} />
      </div>
      <button className="terminal-fab" onClick={() => setTerminalOpen(v => !v)} aria-label="Toggle terminal" title="Terminal">&gt;_</button>
    </div>
  )
}
