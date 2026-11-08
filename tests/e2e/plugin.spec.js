import { test, expect } from '@playwright/test'

const ready = () => ({
  candidate_id: 'candidate-1', layer_id: 'layer-1', kind: 'derive', state: 'ready', revision: 2, unresolved: [],
  review_hash: 'review-hash-2', destination: '/skills/sl-research-abc123', captured: { base: 'base-hash', local: null, source: 'source-hash' },
  files: [{ path: 'SKILL.md', old_executable: false, new_executable: false, diff: '--- before/SKILL.md\n+++ proposed/SKILL.md\n+<img src=x onerror=alert(1)>\n', conflict: null }],
  publish_command: 'hermes skill-layers publish candidate-1 --revision 2 --review-hash review-hash-2',
  notice: 'Later edits remain with the previous export.', behavior_notice: 'A clean textual merge does not establish correct skill behavior.', publication_boundary: 'Direct publication does not enter native approvals.'
})

const conflict = () => ({
  ...ready(), state: 'needs_resolution', revision: 1, review_hash: 'review-hash-1', unresolved: ['SKILL.md'],
  files: [{ path: 'SKILL.md', old_executable: false, new_executable: null, old_execute_bits: 0, new_execute_bits: null, old_sha256: 'localhash', new_sha256: null, resolution_required: true, conflict: { kind: 'text', local_available: true, source_available: true, sides: {base: {present:true,size:4,sha256:'basehash',execute_bits:0,text:'Original base'},local:{present:true,size:5,sha256:'localhash',execute_bits:0,text:'Local improvement'},source:{present:true,size:6,sha256:'sourcehash',execute_bits:0,text:'Source improvement'},proposal:{present:true,size:7,sha256:'proposalhash',execute_bits:0,text:'<<<<<<< LOCAL\n<img src=x onerror=alert(1)>\n||||||| BASE\nOriginal base\n=======\nSource improvement\n>>>>>>> SOURCE'}} }, diff: '<<<<<<< local\n<unsafe>\n=======\nsource\n>>>>>>> source\n' }]
})

async function prepare(page, fixture) {
  await page.addInitScript(value => localStorage.setItem('skill-layers-fixture', JSON.stringify(value)), fixture)
  await page.goto('/')
}

test('empty state and source picker use only discovered selectors', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('No skill layers yet')).toBeVisible()
  await page.getByRole('button', { name: 'Derive a skill' }).click()
  await expect(page.getByLabel('Skill selector')).toBeVisible()
  await expect(page.getByRole('option', { name: /planning — Planning/ })).toBeAttached()
  await expect(page.getByLabel('Layer label')).toBeVisible()
})

test('derive review publishes only after explicit review and renders skill content as text', async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(crypto, 'randomUUID', { configurable: true, value: undefined }))
  await prepare(page, {})
  await page.getByRole('button', { name: 'Derive a skill' }).click()
  await page.getByLabel('Skill selector').selectOption('src-one')
  await page.getByLabel('Layer label').fill('Research edition')
  await page.getByRole('button', { name: 'Prepare derive review' }).click()
  await expect(page.getByRole('heading', { name: 'derive review' })).toBeVisible()
  await expect(page.locator('img')).toHaveCount(0)
  await expect(page.locator('.sl-diff pre')).toContainText('<img src=x onerror=alert(1)>')
  await expect(page.getByRole('button', { name: 'Copy invocation' })).toHaveCount(0)
  const publish = page.getByRole('button', { name: 'Publish reviewed skill' })
  await expect(publish).toBeEnabled()
  await publish.click()
  await expect(page.getByText('Published native selector: sl-research-abc123')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Copy invocation' })).toBeVisible()
  const action = await page.evaluate(() => window.__fixture.mutations.at(-1))
  expect(action.path).toBe('/publish')
  expect(action.body.expected_profile).toBe('scope-default')
  expect(action.body.review_hash).toBe('review-hash-2')
  expect(action.body.request_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
})

test('recorded conflicts block publish until a whole-file resolution is reviewed again', async ({ page }) => {
  await prepare(page, { layers: [{ id: 'layer-1', label: 'Research', source_id: 'src-one' }], candidate: conflict() })
  await page.getByRole('button', { name: 'History and status' }).click()
  await page.getByRole('button', { name: 'Review candidate' }).click()
  const publish = page.getByRole('button', { name: 'Publish reviewed skill' })
  await expect(publish).toBeDisabled()
  await expect(page.getByText('Deleted file', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('navigation', { name: 'Changed files' })).not.toContainText('deleted file')
  await expect(page.getByText('Execute bits: 00 → —', { exact: true })).toHaveCount(0)
  await expect(page.getByText('Git proposal · 7 bytes · SHA-256 proposalhash', { exact: true })).toBeVisible()
  await expect(page.locator('details[open] pre')).toContainText('<<<<<<< LOCAL')
  await expect(page.locator('img')).toHaveCount(0)
  await page.locator('summary').filter({ hasText: /^source ·/ }).click()
  await expect(page.locator('details[open] pre').filter({hasText:'Source improvement'}).last()).toBeVisible()
  await expect(page.getByRole('button', { name: 'Keep local file' })).toBeEnabled()
  await page.getByRole('button', { name: 'Take source file' }).click()
  await expect(page.getByText('Unresolved: 0')).toBeVisible()
  await expect(publish).toBeEnabled()
  const resolution = await page.evaluate(() => window.__fixture.mutations.find(row => row.path === '/resolve'))
  expect(resolution.body).toMatchObject({ take: 'source', path: 'SKILL.md', revision: 1, expected_profile: 'scope-default' })
})

test('local, delete, and edited-text decisions use whole-file resolve payloads', async ({ page }) => {
  await page.goto('/')
  for (const choice of ['local', 'delete', 'edited']) {
    await page.evaluate(fixture => localStorage.setItem('skill-layers-fixture', JSON.stringify(fixture)), {
      layers: [{ id: 'layer-1', label: 'Research', source_id: 'src-one' }], candidate: conflict()
    })
    await page.reload()
    await page.getByRole('button', { name: 'History and status' }).click()
    await page.getByRole('button', { name: 'Review candidate' }).click()
    if (choice === 'edited') {
      await page.getByLabel('Edited whole-file text').fill('reviewed text only')
      await page.getByRole('button', { name: 'Use edited text' }).click()
    } else {
      await page.getByRole('button', { name: choice === 'local' ? 'Keep local file' : 'Delete file' }).click()
    }
    await expect(page.getByText('Unresolved: 0')).toBeVisible()
    const resolution = await page.evaluate(() => window.__fixture.mutations.find(row => row.path === '/resolve'))
    expect(resolution.body).toMatchObject({ path: 'SKILL.md', revision: 1, expected_profile: 'scope-default' })
    expect(resolution.body[choice === 'edited' ? 'edited' : 'take']).toBe(choice === 'edited' ? 'reviewed text only' : choice)
  }
})

test('checkpoint and rollback retain history and open a separate rollback review', async ({ page }) => {
  await prepare(page, {
    layers: [{ id: 'layer-1', label: 'Research', source_id: 'src-one', preferred_export_id: 'export-1' }],
    exports: [{ id: 'export-1', layer_id: 'layer-1', selector: 'sl-exact-selector', created_at: '2026-11-07T00:00:00Z', base_snapshot: 'base-hash' }]
  })
  await page.getByRole('button', { name: 'History and status' }).click()
  await expect(page.getByText('sl-exact-selector', { exact: true }).last()).toBeVisible()
  await page.getByRole('button', { name: 'Create checkpoint' }).click()
  await expect(page.getByText(/Checkpoint/).first()).toBeVisible()
  await page.getByRole('button', { name: 'Review rollback' }).click()
  await expect(page.getByRole('heading', { name: 'rollback review' })).toBeVisible()
  const mutations = await page.evaluate(() => window.__fixture.mutations.map(row => row.path))
  expect(mutations).toContain('/checkpoint')
  expect(mutations).toContain('/rollback')
})

test('refresh prepares a new review candidate without publishing', async ({ page }) => {
  const candidate = ready()
  candidate.captured.inputs = { preferred_export_id: 'export-1' }
  await prepare(page, { layers: [{ id: 'layer-1', label: 'Research', source_id: 'src-one', preferred_export_id: 'export-1' }], candidate })
  await page.getByRole('button', { name: 'History and status' }).click()
  await page.getByRole('button', { name: 'Review candidate' }).click()
  await page.getByRole('button', { name: 'Prepare refresh' }).click()
  await expect(page.getByRole('heading', { name: 'refresh review' })).toBeVisible()
  expect(await page.evaluate(() => window.__fixture.mutations.at(-1).path)).toBe('/refresh')
  await expect(page.getByRole('button', { name: 'Publish reviewed skill' })).toBeEnabled()
})

test('review exposes execute-bit masks, binary hashes, and add/delete kinds', async ({ page }) => {
  const candidate = ready()
  candidate.files = [
    { path: 'SKILL.md', old_executable: true, new_executable: true, old_execute_bits: 1, new_execute_bits: 7, diff: 'mode change', old_sha256: 'same-a', new_sha256: 'same-b' },
    { path: 'unchanged-mode.txt', old_sha256: 'old-text', new_sha256: 'new-text', old_execute_bits: 0, new_execute_bits: 0, diff: '+changed text' },
    { path: 'media.bin', binary: true, old_sha256: 'binary-old', new_sha256: 'binary-new' },
    { path: 'added.txt', old_sha256: null, new_sha256: 'added-hash', diff: '+new' },
    { path: 'removed.txt', old_sha256: 'removed-hash', new_sha256: null, diff: '-old' }
  ]
  await prepare(page, { layers: [{ id: 'layer-1', label: 'Research', source_id: 'src-one' }], candidate })
  await page.getByRole('button', { name: 'History and status' }).click()
  await page.getByRole('button', { name: 'Review candidate' }).click()
  await expect(page.getByText('Execute bits: 01 → 07')).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Changed files' }).getByRole('button', { name: 'unchanged-mode.txt', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'unchanged-mode.txt', exact: true }).click()
  await expect(page.getByText('Execute bits: 00 → 00', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: /media.bin/ }).click()
  await expect(page.getByText('Old SHA-256: binary-old')).toBeVisible()
  await expect(page.getByText('New SHA-256: binary-new')).toBeVisible()
  await page.getByRole('button', { name: /added.txt/ }).click()
  await expect(page.getByText('Added file', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: /removed.txt/ }).click()
  await expect(page.getByText('Deleted file', { exact: true })).toBeVisible()
})

test('missing source and unknown candidate states stay explicit and non-publishable', async ({ page }) => {
  await prepare(page, { fail: { code: 'SOURCE_MOVED', message: 'The discovered source moved.' } })
  await expect(page.getByRole('alert')).toContainText('The discovered source moved.')
  await expect(page.getByText(/selected source is no longer available/)).toBeVisible()

  await page.evaluate(() => {
    window.__fixture.fail = null
    window.__fixture.layers = [{ id: 'layer-1', label: 'Research', source_id: 'src-one' }]
    window.__fixture.candidate = {
      candidate_id: 'candidate-1', layer_id: 'layer-1', kind: 'refresh', state: 'future_state', revision: 1,
      unresolved: [], review_hash: 'review-hash', files: [], destination: '/skills/generated', captured: { base: 'base-hash', local: null, source: 'source-hash' }
    }
    window.changeSkillContext('local', 'review-profile')
  })
  await page.getByRole('button', { name: 'History and status' }).click()
  await page.getByRole('button', { name: 'Review candidate' }).click()
  await expect(page.getByText('State: future_state')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Publish reviewed skill' })).toBeDisabled()
})

test('layer status uses stable source metadata and distinguishes missing preference from errors', async ({ page }) => {
  await prepare(page, {
    layers: [{ id: 'layer-1', label: 'Research', source_id: 'src-one', preferred_export_id: null }],
    status: {
      source: { selector: 'original-selector', display_name: 'Stable source', observed_name: 'Renamed frontmatter' },
      changes: { local: null, local_error: { message: 'Local export is unreadable.' }, source: null, source_error: { message: 'Source cannot be inspected.' } }
    }
  })
  await expect(page.getByText(/Source: Renamed frontmatter/)).toBeVisible()
  await expect(page.getByText('No export published yet.')).toBeVisible()
  await page.getByRole('button', { name: 'History and status' }).click()
  await expect(page.getByText('No preferred export (preferred export ID is empty).')).toBeVisible()
  await expect(page.getByText('Source: Renamed frontmatter')).toBeVisible()
  await expect(page.getByText(/Local status unavailable: Local export is unreadable./)).toBeVisible()
  await expect(page.getByText(/Source status unavailable: Source cannot be inspected./)).toBeVisible()
})

test('profile switch clears old state and ignores delayed response from prior context', async ({ page }) => {
  await prepare(page, { layers: [{ id: 'old-layer', label: 'Old profile secret', source_id: 'src-one' }], delayLayers: true })
  await page.waitForFunction(() => window.__requests?.some(row => row.path === '/layers' && row.profile === 'default'))
  await page.evaluate(() => { window.__fixture.layers = []; window.changeSkillContext('remote-one', 'research') })
  await expect(page.getByText('Profile: research')).toBeVisible()
  await expect(page.getByText('Old profile secret')).toHaveCount(0)
  await page.waitForTimeout(450)
  await expect(page.getByText('Old profile secret')).toHaveCount(0)
})

test('failure states are announced and stale backend responses do not expose actions', async ({ page }) => {
  await prepare(page, { fail: { code: 'DEPENDENCY_MISSING', message: 'Git is unavailable.' } })
  await expect(page.getByRole('alert')).toContainText('Git is unavailable.')
  await expect(page.getByText(/Install the documented Git/)).toBeVisible()
  await page.evaluate(() => { window.__fixture.fail = { code: 'STALE_INPUT', message: 'The local copy changed.' } })
  await page.getByRole('button', { name: 'Reload' }).click()
  await expect(page.getByRole('alert')).toContainText('The local copy changed.')
})

test('stale publish rejection invalidates the review and disables publish', async ({ page }) => {
  await prepare(page, { layers: [{ id: 'layer-1', label: 'Research', source_id: 'src-one' }], candidate: ready() })
  await page.getByRole('button', { name: 'History and status' }).click()
  await page.getByRole('button', { name: 'Review candidate' }).click()
  await page.evaluate(() => { window.__fixture.fail = { code: 'STALE_INPUT', message: 'The source changed after review.' } })
  await page.getByRole('button', { name: 'Publish reviewed skill' }).click()
  await expect(page.locator('.sl-alert').first()).toContainText('The source changed after review.')
  await expect(page.getByRole('button', { name: 'Publish reviewed skill' })).toBeDisabled()
})

test('recovery state exposes the explicit, profile-scoped recovery action', async ({ page }) => {
  await prepare(page, { fail: { code: 'RECOVERY_REQUIRED', message: 'Publication needs inspection.' } })
  await expect(page.getByRole('button', { name: 'Recover pending operation' })).toBeVisible()
  await page.evaluate(() => { window.__fixture.fail = null })
  await page.getByRole('button', { name: 'Recover pending operation' }).click()
  await expect(page.getByText('Recovery finished. Reloaded profile state.')).toBeVisible()
  const recovery = await page.evaluate(() => window.__fixture.mutations.at(-1))
  expect(recovery).toMatchObject({ path: '/recover', body: { expected_profile: 'scope-default' } })
})

test('review layout fits narrow and desktop viewports with keyboard accessible file selection', async ({ page }) => {
  await prepare(page, { layers: [{ id: 'layer-1', label: 'Research', source_id: 'src-one' }], candidate: ready() })
  await page.getByRole('button', { name: 'History and status' }).click()
  await page.getByRole('button', { name: 'Review candidate' }).click()
  await page.setViewportSize({ width: 1280, height: 900 })
  await expect(page.getByRole('navigation', { name: 'Changed files' })).toBeVisible()
  await page.screenshot({ path: 'test-results/skill-layers-desktop.png', fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: 'test-results/skill-layers-mobile.png', fullPage: true })
  await page.keyboard.press('Tab')
  await expect(page.locator(':focus')).toBeVisible()
})
