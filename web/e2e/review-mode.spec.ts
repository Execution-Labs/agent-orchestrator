import { expect, test, type APIRequestContext } from '@playwright/test'

type TaskResponse = { task: { id: string; status: string; metadata?: Record<string, unknown>; pipeline_template?: string[] } }

const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 6)

async function createReviewTask(
  request: APIRequestContext,
  opts: {
    title: string
    description: string
    taskType: string
    pipelineTemplate: string[]
    reviewMode: string
    reviewDecision?: string
  },
): Promise<TaskResponse['task']> {
  const metadata: Record<string, unknown> = {
    source_pr_number: 42,
    review_mode: opts.reviewMode,
  }
  if (opts.reviewDecision) {
    metadata.review_decision = opts.reviewDecision
  }

  const response = await request.post('/api/tasks', {
    data: {
      title: opts.title,
      description: opts.description,
      task_type: opts.taskType,
      priority: 'P1',
      status: 'backlog',
      labels: [],
      blocked_by: [],
      pipeline_template: opts.pipelineTemplate,
      metadata,
    },
  })
  expect(response.ok()).toBeTruthy()
  const payload = (await response.json()) as TaskResponse
  return payload.task
}

async function pauseOrchestrator(request: APIRequestContext): Promise<void> {
  const response = await request.post('/api/orchestrator/control', {
    data: { action: 'pause' },
  })
  expect(response.ok()).toBeTruthy()
}

test.beforeEach(async ({ page, request }) => {
  await pauseOrchestrator(request)
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Overdrive' })).toBeVisible()
})

test('creates fix_only review task and shows the expected detail flow', async ({ page, request }) => {
  const tag = uid()
  const task = await createReviewTask(request, {
    title: `PR Review: #42 — Fix only ${tag}`,
    description: 'Review PR #42 using fix_only mode.',
    taskType: 'pr_review_fix_only',
    pipelineTemplate: ['pr_review', 'implement', 'verify', 'review', 'commit'],
    reviewMode: 'fix_only',
  })
  expect(task.id).toBeTruthy()

  await page.goto('/')
  const taskCard = page.locator('.task-card', { hasText: tag })
  await expect(taskCard).toBeVisible({ timeout: 15_000 })
  await expect(taskCard).toContainText(`PR Review: #42 — Fix only ${tag}`)

  await taskCard.click()
  const detailCard = page.locator('.detail-card')
  await expect(detailCard).toBeVisible()
  await expect(detailCard).toContainText(task.id.replace(/^task-/, ''))
  await expect(detailCard).toContainText(/pr review fix only/i)
})

test('creates review_comment task and shows the expected detail flow', async ({ page, request }) => {
  const tag = uid()
  const task = await createReviewTask(request, {
    title: `PR Review: #43 — Comment ${tag}`,
    description: 'Review PR #43 using review_comment mode.',
    taskType: 'pr_review_comment',
    pipelineTemplate: ['fetch_comments', 'pr_review_comment', 'post_comments'],
    reviewMode: 'review_comment',
    reviewDecision: 'approve',
  })
  expect(task.id).toBeTruthy()

  await page.goto('/')
  const taskCard = page.locator('.task-card', { hasText: tag })
  await expect(taskCard).toBeVisible({ timeout: 15_000 })
  await expect(taskCard).toContainText(`PR Review: #43 — Comment ${tag}`)

  await taskCard.click()
  const detailCard = page.locator('.detail-card')
  await expect(detailCard).toBeVisible()
  await expect(detailCard).toContainText(task.id.replace(/^task-/, ''))
  await expect(detailCard).toContainText(/pr review comment/i)
})

test('fix_only pipeline has correct steps', async ({ request }) => {
  const tag = uid()
  const task = await createReviewTask(request, {
    title: `PR Review: #50 — Pipeline fix ${tag}`,
    description: 'Verify fix_only pipeline steps.',
    taskType: 'pr_review_fix_only',
    pipelineTemplate: ['pr_review', 'implement', 'verify', 'review', 'commit'],
    reviewMode: 'fix_only',
  })

  expect(task.pipeline_template).toEqual(['pr_review', 'implement', 'verify', 'review', 'commit'])
})

test('review_comment pipeline has correct steps', async ({ request }) => {
  const tag = uid()
  const task = await createReviewTask(request, {
    title: `PR Review: #51 — Pipeline rc ${tag}`,
    description: 'Verify review_comment pipeline steps.',
    taskType: 'pr_review_comment',
    pipelineTemplate: ['fetch_comments', 'pr_review_comment', 'post_comments'],
    reviewMode: 'review_comment',
  })

  expect(task.pipeline_template).toEqual(['fetch_comments', 'pr_review_comment', 'post_comments'])
})

test('fix_only and review_comment tasks both appear on the board with distinct titles', async ({ page, request }) => {
  const tag = uid()
  await createReviewTask(request, {
    title: `PR Review: #60 — Bfix ${tag}`,
    description: 'Fix only task for badge test.',
    taskType: 'pr_review_fix_only',
    pipelineTemplate: ['pr_review', 'implement', 'verify', 'review', 'commit'],
    reviewMode: 'fix_only',
  })

  await createReviewTask(request, {
    title: `PR Review: #61 — Bcom ${tag}`,
    description: 'Review comment task for badge test.',
    taskType: 'pr_review_comment',
    pipelineTemplate: ['fetch_comments', 'pr_review_comment', 'post_comments'],
    reviewMode: 'review_comment',
  })

  await page.goto('/')

  const fixCard = page.locator('.task-card', { hasText: `Bfix ${tag}` })
  const commentCard = page.locator('.task-card', { hasText: `Bcom ${tag}` })

  await expect(fixCard).toBeVisible({ timeout: 15_000 })
  await expect(commentCard).toBeVisible({ timeout: 15_000 })
  await expect(fixCard).toContainText(`PR Review: #60 — Bfix ${tag}`)
  await expect(commentCard).toContainText(`PR Review: #61 — Bcom ${tag}`)
})
