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

function installFetchMock(doneTask: {
  id: string
  title: string
  task_type: string
  children_ids?: string[]
}) {
  const task = {
    ...doneTask,
    description: 'Done task for testing',
    priority: 'P2',
    status: 'done',
    labels: [],
    blocked_by: [],
    blocks: [],
  }

  const settingsPayload = {
    orchestrator: { concurrency: 2, auto_deps: true, max_review_attempts: 10, step_timeout_seconds: 600 },
    agent_routing: {
      default_role: 'general',
      task_type_roles: {},
      role_provider_overrides: {},
    },
    defaults: { quality_gate: { critical: 0, high: 0, medium: 0, low: 0 } },
    workers: {
      default: 'codex',
      default_model: '',
      routing: {},
      providers: {
        codex: { type: 'codex', command: 'codex exec' },
      },
    },
    project: {
      commands: {},
      prompt_overrides: {},
      prompt_injections: {},
      prompt_defaults: {},
    },
  }

  const jsonResponse = (payload: unknown) =>
    Promise.resolve({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => payload,
    })

  const mockedFetch = vi.fn().mockImplementation((url: string) => {
    const u = String(url)

    if (u === '/' || u.startsWith('/?')) return jsonResponse({ project_id: 'repo-alpha' })
    if (u.includes('/api/collaboration/modes')) return jsonResponse({ modes: [] })
    if (u.includes('/api/tasks/board')) {
      return jsonResponse({
        columns: {
          backlog: [],
          queued: [],
          in_progress: [],
          in_review: [],
          blocked: [],
          done: [task],
          cancelled: [],
        },
      })
    }
    if (u.includes('/api/tasks/execution-order')) return jsonResponse({ batches: [] })
    if (u.includes(`/api/tasks/${task.id}`) && !u.includes('/generate-tasks')) return jsonResponse({ task })
    if (u.includes('/api/tasks') && !u.includes('/api/tasks/')) return jsonResponse({ tasks: [task] })
    if (u.includes('/api/orchestrator/status')) {
      return jsonResponse({ status: 'running', queue_depth: 0, in_progress: 0, draining: false, run_branch: null })
    }
    if (u.includes('/api/review-queue')) return jsonResponse({ tasks: [] })
    if (u.includes('/api/agents/types')) return jsonResponse({ types: [] })
    if (u.includes('/api/agents') && !u.includes('/types')) return jsonResponse({ agents: [] })
    if (u.includes('/api/phases')) return jsonResponse([])
    if (u.includes('/api/settings')) return jsonResponse(settingsPayload)
    if (u.includes('/api/metrics')) {
      return jsonResponse({ worker_time_seconds: 0, tasks_completed: 0, tokens_used: 0, estimated_cost_usd: 0 })
    }
    if (u.includes('/api/collaboration/timeline/')) return jsonResponse({ events: [] })
    if (u.includes('/api/collaboration/feedback/')) return jsonResponse({ feedback: [] })
    if (u.includes('/api/collaboration/comments/')) return jsonResponse({ comments: [] })
    if (u.includes('/api/collaboration/presence')) return jsonResponse({ users: [] })
    if (u.includes('/api/workers/health')) {
      return jsonResponse({ providers: [] })
    }
    if (u.includes('/api/workers/routing')) {
      return jsonResponse({ default: 'codex', rows: [] })
    }
    if (u.includes('/api/projects/pinned')) return jsonResponse({ items: [] })
    if (u.includes('/api/projects')) return jsonResponse({ projects: [] })
    if (u.includes('/api/terminal/session')) return jsonResponse({ session: null })
    if (u.includes(`/api/tasks/${task.id}/generate-tasks`)) {
      return jsonResponse({
        task: { ...task, children_ids: ['child-1'] },
        created_task_ids: ['child-1'],
        children: [{ id: 'child-1', title: 'Follow-up', status: 'backlog', task_type: 'feature' }],
      })
    }
    return jsonResponse({})
  })

  global.fetch = mockedFetch as unknown as typeof fetch
  return mockedFetch
}

describe('Generate Follow-Up Tasks', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    window.location.hash = ''
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  it('shows Generate Follow-Up Tasks button for done research task', async () => {
    installFetchMock({ id: 'task-r1', title: 'Research findings', task_type: 'research' })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('Research findings')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('Research findings'))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Generate Follow-Up Tasks/i })).toBeInTheDocument()
    })
  }, 30000)

  it('does not show Generate Follow-Up Tasks button for done feature task', async () => {
    installFetchMock({ id: 'task-f1', title: 'Feature task', task_type: 'feature' })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('Feature task')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('Feature task'))

    await waitFor(() => {
      expect(screen.getAllByText('Feature task').length).toBeGreaterThan(1)
    })

    expect(screen.queryByRole('button', { name: /Generate Follow-Up Tasks/i })).not.toBeInTheDocument()
  }, 30000)

  it('hides Generate Follow-Up Tasks button when task already has children', async () => {
    installFetchMock({ id: 'task-r2', title: 'Research with children', task_type: 'research', children_ids: ['child-1'] })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('Research with children')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('Research with children'))

    await waitFor(() => {
      expect(screen.getAllByText('Research with children').length).toBeGreaterThan(1)
    })

    expect(screen.queryByRole('button', { name: /Generate Follow-Up Tasks/i })).not.toBeInTheDocument()
  }, 30000)

  it('shows Generate Follow-Up Tasks button for done spike task', async () => {
    installFetchMock({ id: 'task-s1', title: 'Spike findings', task_type: 'spike' })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('Spike findings')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('Spike findings'))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Generate Follow-Up Tasks/i })).toBeInTheDocument()
    })
  }, 30000)
})
