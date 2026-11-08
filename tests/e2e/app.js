import { jsx } from 'react/jsx-runtime'
import { createRoot } from 'react-dom/client'
import plugin from '../../desktop/plugin.js'

const stored = (() => { try { return JSON.parse(localStorage.getItem('skill-layers-fixture') || '{}') } catch { return {} } })()
const state = {
  layers: [], sources: [{ id: 'src-one', selector: 'planning', name: 'Planning' }],
  candidate: null, checkpoints: [], exports: [], delayLayers: new URLSearchParams(location.search).has('delay'), fail: null, mutations: [], publish: null,
  ...stored
}
const envelope = (data, profile = window.__profile || 'default') => ({ ok: true, profile_id: `scope-${profile}`, profile: { name: profile }, data })
const ok = data => Promise.resolve(envelope(data))
const fail = (code, message) => Promise.resolve({ ok: false, profile_id: `scope-${window.__profile || 'default'}`, error: { code, message, retryable: false, details: {} } })
window.__fixture = state
window.__requests = []
const ctx = {
  setTimeout(fn, ms) { const handle = window.setTimeout(fn, ms); return () => window.clearTimeout(handle) },
  registerMany(rows) {
    const route = rows.find(row => row.area === 'routes')
    window.__route = route
    const root = createRoot(document.getElementById('root'))
    root.render(jsx(route.render, {}))
  },
  async rest(path, options) {
    const requestProfile = window.__profile || 'default'
    window.__requests.push({ path, profile: requestProfile, method: options?.method || 'GET' })
    const method = options?.method || 'GET'
    if (method === 'POST') {
      const body = options.body || {}
      state.mutations.push({ path, body })
      if (!body.expected_profile) return fail('PROFILE_MISMATCH', 'expected profile was missing')
      if (state.fail) return fail(state.fail.code, state.fail.message)
      if (path === '/derive') { state.candidate = readyCandidate(); return ok({ candidate_id: 'candidate-1', layer_id: 'layer-1', revision: 1, state: 'ready', unresolved: [] }) }
      if (path === '/resolve') {
        state.candidate = readyCandidate()
        return ok({ candidate_id: 'candidate-1', revision: 2, state: 'ready', unresolved: [] })
      }
      if (path === '/publish') {
        if (body.review_hash !== state.candidate?.review_hash) return fail('STALE_INPUT', 'review hash is stale')
        state.publish = { selector: 'sl-research-abc123' }
        return ok({ export: { selector: state.publish.selector }, notice: 'Later edits stay with the previous export.' })
      }
      if (path === '/checkpoint') {
        const checkpoint = { id: `checkpoint-${state.checkpoints.length + 1}`, layer_id: body.layer_id, identity: 'sl-exact-selector', created_at: new Date().toISOString(), base_snapshot: 'base-hash', captured_snapshot: 'checkpoint-snapshot' }
        state.checkpoints.push(checkpoint)
        return ok({ checkpoint })
      }
      if (path === '/rollback' || path === '/refresh') {
        state.candidate = { ...readyCandidate(), kind: path.slice(1) }
        return ok({ candidate_id: 'candidate-1', layer_id: 'layer-1', revision: 1, state: 'ready', unresolved: [] })
      }
      if (path === '/recover') return ok({ operations: [] })
      return fail('INVALID_STATE', `unknown ${path}`)
    }
    if (state.fail) return fail(state.fail.code, state.fail.message)
    if (path === '/layers') {
      const capturedLayers = state.layers
      if (state.delayLayers) await new Promise(resolve => setTimeout(resolve, 350))
      return envelope({ layers: capturedLayers, truncated: false }, requestProfile)
    }
    if (path === '/sources') return envelope({ sources: state.sources }, requestProfile)
    if (path === '/layers/layer-1') return envelope({ layer: state.layers[0] || { id: 'layer-1', label: 'Research', source_id: 'src-one' }, source: state.status?.source, exports: state.exports, checkpoints: state.checkpoints, candidates: state.candidate ? [{ candidate_id: 'candidate-1', layer_id: 'layer-1', kind: state.candidate.kind || 'refresh', revision: state.candidate.revision || 1, state: state.candidate.state, unresolved: state.candidate.unresolved || [] }] : [], changes: state.status?.changes || { local: false, source: false }, notice: 'Captured point-in-time.' }, requestProfile)
    if (path === '/candidates/candidate-1') return envelope(state.candidate || readyCandidate(), requestProfile)
    return fail('NOT_FOUND', `unknown ${path}`)
  }
}
function readyCandidate() {
  return {
    candidate_id: 'candidate-1', layer_id: 'layer-1', kind: 'derive', state: 'ready', revision: 2, unresolved: [],
    review_hash: 'review-hash-2', destination: '/skills/sl-research-abc123', captured: { base: 'base-hash', local: null, source: 'source-hash' },
    files: [{ path: 'SKILL.md', old_executable: false, new_executable: false, diff: '--- before/SKILL.md\n+++ proposed/SKILL.md\n+<img src=x onerror=alert(1)>\n', conflict: null }],
    publish_command: 'hermes skill-layers publish candidate-1 --revision 2 --review-hash review-hash-2',
    notice: 'Later edits remain with the previous export.', behavior_notice: 'A clean textual merge does not establish correct skill behavior.', publication_boundary: 'Direct publication does not enter native approvals.'
  }
}
plugin.register(ctx)
