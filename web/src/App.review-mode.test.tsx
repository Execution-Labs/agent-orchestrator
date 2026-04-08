import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'

class MockWebSocket {
  static instances: MockWebSocket[] = []
  listeners: Record<string, Array<(event?: unknown) => void>> = {}
  url: string

  constructor(url: string) {
    this.url = url
    MockWebSocket.instances.push(this)
    setTimeout(() => this.dispatch('open'), 0)
  }

  addEventListener(event: string, cb: (event?: unknown) => void) {
    this.listeners[event] = this.listeners[event] || []
    this.listeners[event].push(cb)
  }

  send() {}

  close() {}

  dispatch(event: string, payload: unknown = {}) {
    for (const cb of this.listeners[event] || []) {
      cb(payload)
    }
  }
}

function installFetchMock(options?: { platform?: 'github' | 'gitlab'; prList?: Array<Record<string, unknown>> }) {
  const jsonResponse = (payload: unknown) =>
    Promise.resolve({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => payload,
    })

  const platform = options?.platform ?? 'github'
  const prList = options?.prList ?? [
    { number: 42, title: 'Add login page', author: 'alice', head_ref: 'feat/login', base_ref: 'main', url: 'https://github.com/org/repo/pull/42', has_review_task: false, review_task_id: null },
    { number: 43, title: 'Fix footer', author: 'bob', head_ref: 'fix/footer', base_ref: 'main', url: 'https://github.com/org/repo/pull/43', has_review_task: false, review_task_id: null },
  ]

  const mockedFetch = vi.fn().mockImplementation((url: string) => {
    const u = String(url)
    if (u === '/' || u.startsWith('/?')) return jsonResponse({ project_id: 'repo-alpha' })
    if (u.includes('/api/collaboration/modes')) return jsonResponse({ modes: [] })
    if (u.includes('/api/pull-requests') && !u.includes('/review')) return jsonResponse({ items: prList, platform })
    if (u.includes('/api/pull-requests') && u.includes('/review')) return jsonResponse({ task_id: 'task-review-1' })
    if (u.includes('/api/tasks/board')) return jsonResponse({ columns: { backlog: [], queued: [], in_progress: [], in_review: [], blocked: [], done: [] } })
    if (u.includes('/api/tasks/execution-order')) return jsonResponse({ batches: [], completed: [] })
    if (u.includes('/api/tasks') && !u.includes('/api/tasks/')) return jsonResponse({ tasks: [] })
    if (u.includes('/api/orchestrator/status')) return jsonResponse({ status: 'running', queue_depth: 0, in_progress: 0, draining: false, run_branch: null })
    if (u.includes('/api/review-queue')) return jsonResponse({ tasks: [] })
    if (u.includes('/api/agents/types')) return jsonResponse({ types: [] })
    if (u.includes('/api/agents')) return jsonResponse({ agents: [] })
    if (u.includes('/api/projects/pinned')) return jsonResponse({ items: [] })
    if (u.includes('/api/projects')) return jsonResponse({ projects: [] })
    if (u.includes('/api/phases')) return jsonResponse([])
    if (u.includes('/api/collaboration/presence')) return jsonResponse({ users: [] })
    if (u.includes('/api/metrics')) return jsonResponse({})
    if (u.includes('/api/settings')) return jsonResponse({
      orchestrator: { concurrency: 2, auto_deps: true, max_review_attempts: 10, step_timeout_seconds: 600 },
      agent_routing: { default_role: 'general', task_type_roles: {}, role_provider_overrides: {} },
      defaults: { quality_gate: { critical: 0, high: 0, medium: 0, low: 0 } },
      workers: { default: 'codex', default_model: '', routing: {}, providers: {} },
      project: { commands: {}, prompt_overrides: {}, prompt_injections: {}, prompt_defaults: {} },
    })
    if (u.includes('/api/git-platform-status')) return jsonResponse({
      platform,
      remote_url: platform === 'gitlab' ? 'https://gitlab.com/org/repo.git' : 'https://github.com/org/repo.git',
      cli_installed: true,
      cli_authenticated: true,
      cli_name: platform === 'gitlab' ? 'glab' : 'gh',
      setup_steps: [
        { step: 1, label: 'Configure git remote', done: true },
        { step: 2, label: `Install ${platform === 'gitlab' ? 'glab' : 'gh'}`, done: true },
        { step: 3, label: `Authenticate ${platform === 'gitlab' ? 'glab' : 'gh'}`, done: true },
      ],
    })
    if (u.includes('/api/workers/health')) return jsonResponse({ providers: [] })
    if (u.includes('/api/workers/routing')) return jsonResponse({ default: 'codex', rows: [] })
    if (u.includes('/api/terminal/session')) return jsonResponse({ session: null })
    return jsonResponse({})
  })

  global.fetch = mockedFetch as unknown as typeof fetch
  return mockedFetch
}

async function openReviewTab(options?: Parameters<typeof installFetchMock>[0]) {
  if (options) {
    installFetchMock(options)
  }
  render(<App />)
  await waitFor(() => expect(screen.getAllByRole('button', { name: /^Create Work$/i }).length).toBeGreaterThan(0))
  fireEvent.click(screen.getAllByRole('button', { name: /^Create Work$/i })[0])
  await waitFor(() => expect(screen.getByText('Review PR/MR')).toBeInTheDocument())
  fireEvent.click(screen.getByText('Review PR/MR'))
  await waitFor(() => expect(screen.getByText('Review mode')).toBeInTheDocument())
}

describe('Review mode selector (simplified creation form)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    window.location.hash = ''
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
    installFetchMock()
  })

  it('renders review mode dropdown with Fix Code selected by default', async () => {
    await openReviewTab()
    const modeSelect = screen.getByLabelText('Review mode') as HTMLSelectElement
    expect(modeSelect.value).toBe('fix_only')
  })

  it('does not show review decision or post comments controls at creation time', async () => {
    await openReviewTab()
    // No review decision dropdown in any mode
    expect(screen.queryByLabelText('Review decision')).toBeNull()
    expect(screen.queryByText('Post comments to PR/MR')).toBeNull()

    // Switch to review_comment — still no decision/post controls
    fireEvent.change(screen.getByLabelText('Review mode'), { target: { value: 'review_comment' } })
    expect(screen.queryByLabelText('Review decision')).toBeNull()
    expect(screen.queryByText('Post comments to PR/MR')).toBeNull()

    // Switch to fix_respond — still no decision/post controls
    fireEvent.change(screen.getByLabelText('Review mode'), { target: { value: 'fix_respond' } })
    expect(screen.queryByLabelText('Review decision')).toBeNull()
    expect(screen.queryByText('Post comments to PR/MR')).toBeNull()
  })

  it('sends only review_mode and guidance in the API call (no review_decision or post_comments)', async () => {
    const mockedFetch = installFetchMock()
    await openReviewTab()

    // Select PR
    await waitFor(() => expect(screen.getByText('#42')).toBeInTheDocument())
    fireEvent.click(screen.getByText('#42').closest('button')!)

    // Select Review & Comment mode
    fireEvent.change(screen.getByLabelText('Review mode'), { target: { value: 'review_comment' } })

    // Submit
    fireEvent.click(screen.getByText('Create Review'))
    await waitFor(() => {
      const reviewCall = mockedFetch.mock.calls.find(
        ([u, init]: [string, RequestInit]) => String(u).includes('/api/pull-requests/42/review') && init?.method === 'POST',
      )
      expect(reviewCall).toBeDefined()
      const body = JSON.parse(String(reviewCall![1].body))
      expect(body.review_mode).toBe('review_comment')
      expect(body.review_decision).toBeUndefined()
      expect(body.post_comments).toBeUndefined()
    })
  })

  it('sends fix_only review_mode when using default mode', async () => {
    const mockedFetch = installFetchMock()
    await openReviewTab()

    await waitFor(() => expect(screen.getByText('#42')).toBeInTheDocument())
    fireEvent.click(screen.getByText('#42').closest('button')!)

    // Default mode is fix_only — submit directly
    fireEvent.click(screen.getByText('Create Review'))
    await waitFor(() => {
      const reviewCall = mockedFetch.mock.calls.find(
        ([u, init]: [string, RequestInit]) => String(u).includes('/api/pull-requests/42/review') && init?.method === 'POST',
      )
      expect(reviewCall).toBeDefined()
      const body = JSON.parse(String(reviewCall![1].body))
      expect(body.review_mode).toBe('fix_only')
      expect(body.review_decision).toBeUndefined()
      expect(body.post_comments).toBeUndefined()
    })
  })

  it('renders GitLab-specific review copy and MR selection state', async () => {
    await openReviewTab({
      platform: 'gitlab',
      prList: [
        { number: 15, title: 'Harden auth flow', author: 'carol', head_ref: 'mr/auth-hardening', base_ref: 'main', url: 'https://gitlab.com/org/repo/-/merge_requests/15', has_review_task: true, review_task_id: 'task-mr-15' },
        { number: 16, title: 'Improve retries', author: 'dave', head_ref: 'mr/retries', base_ref: 'main', url: 'https://gitlab.com/org/repo/-/merge_requests/16', has_review_task: false, review_task_id: null },
      ],
    })

    await waitFor(() => expect(screen.getByRole('radiogroup', { name: 'Open merge requests' })).toBeInTheDocument())
    expect(screen.getByText('!15')).toBeInTheDocument()
    expect(screen.getByText('!16')).toBeInTheDocument()
    expect(screen.getByText('MR review exists')).toBeInTheDocument()
  })
})
