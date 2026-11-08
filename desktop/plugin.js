import {
  Badge, Button, CopyButton, EmptyState, Input, Textarea,
  ROUTES_AREA, SIDEBAR_NAV_AREA, gatewayActivationEpoch, host, useGatewayActivationEpoch, useValue
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'skill-layers'
const ROUTE = '/skill-layers'
const NOTICE = 'Publication creates a new ordinary skill. You choose its selector explicitly; existing chats keep their current selection. Later edits remain with the previous export.'
const CSS = `
.sl-page{box-sizing:border-box;max-width:1240px;margin:0 auto;padding:24px;color:var(--ui-text-primary);font-size:14px;line-height:1.5}
.sl-page *{box-sizing:border-box}
.sl-page h1{font-size:24px;line-height:1.25;margin:0;font-weight:650}.sl-page h2{font-size:17px;margin:0 0 12px;font-weight:600}.sl-page h3{font-size:14px;margin:0 0 8px;font-weight:600}
.sl-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:20px}.sl-head-copy{min-width:0}.sl-context{display:flex;gap:8px;flex-wrap:wrap;color:var(--ui-text-secondary);margin-top:6px;font-size:12px}.sl-actions{display:flex;gap:8px;flex-wrap:wrap}
.sl-section{margin:24px 0}.sl-row{display:flex;gap:14px;align-items:flex-start;justify-content:space-between;padding:14px 0;border-bottom:1px solid var(--ui-stroke-secondary)}.sl-row-main{min-width:0;flex:1}.sl-muted{color:var(--ui-text-secondary)}.sl-small{font-size:12px;color:var(--ui-text-tertiary)}.sl-wrap{overflow-wrap:anywhere}.sl-status{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:8px}
.sl-banner{padding:12px 0;border-block:1px solid var(--ui-stroke-secondary);color:var(--ui-text-secondary);margin:16px 0}.sl-alert{color:var(--ui-text-primary);padding:12px 0;border-block:1px solid var(--ui-stroke-secondary);margin:16px 0}.sl-inline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.sl-form{display:grid;gap:14px;max-width:560px}.sl-field{display:grid;gap:5px}.sl-field select{width:100%}.sl-page textarea{min-height:130px;resize:vertical}
.sl-review{display:grid;grid-template-columns:minmax(220px, 0.36fr) minmax(0,1fr);gap:24px;margin-top:20px}.sl-files{min-width:0}.sl-file-list{list-style:none;padding:0;margin:0;border-top:1px solid var(--ui-stroke-secondary)}.sl-file-list li{border-bottom:1px solid var(--ui-stroke-secondary)}.sl-file-list button{display:block;width:100%;text-align:left;padding:9px 4px;border:0;background:none;cursor:pointer;overflow-wrap:anywhere}.sl-file-list button[aria-current=true]{color:var(--ui-accent)}.sl-diff{min-width:0}.sl-diff pre{white-space:pre;overflow:auto;max-width:100%;max-height:52vh;padding:14px 0;border-block:1px solid var(--ui-stroke-secondary);font:12px/1.55 var(--font-mono,ui-monospace,monospace);color:var(--ui-text-secondary)}.sl-diff pre.sl-empty{white-space:normal;font-family:inherit}.sl-conflict{margin:18px 0;padding:12px 0;border-block:1px solid var(--ui-stroke-secondary)}.sl-conflict-actions{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.sl-history{list-style:none;padding:0;margin:0}.sl-history li{padding:12px 0;border-bottom:1px solid var(--ui-stroke-secondary)}.sl-footer{margin-top:24px}.sl-actions button{white-space:normal}
@media(max-width:700px){.sl-page{padding:16px}.sl-head{display:grid}.sl-review{grid-template-columns:minmax(0,1fr)}.sl-review .sl-files{order:0}.sl-review .sl-diff{order:1}.sl-diff pre{max-height:42vh}.sl-row{display:grid}}
`

const randomId = () => {
  const secureCrypto = globalThis.crypto
  if (typeof secureCrypto?.randomUUID === 'function') return secureCrypto.randomUUID()
  if (typeof secureCrypto?.getRandomValues !== 'function') {
    throw new Error('Secure random IDs are unavailable. Mutations are disabled in this environment.')
  }
  const bytes = secureCrypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
const readAtom = atom => atom?.get?.() ?? null
const contextNow = () => ({
  connectionId: readAtom(host.state.connectionId) || 'local',
  profile: readAtom(host.state.profile) || 'default',
  epoch: gatewayActivationEpoch()
})
const sameContext = a => {
  const b = contextNow()
  return a.connectionId === b.connectionId && a.profile === b.profile && a.epoch === b.epoch
}
const profileKey = context => `${context.connectionId}\n${context.profile}\n${context.epoch}`
const messageOf = error => error?.error?.message || error?.message || 'The request could not be completed.'
const changeLabel = (value, error) => error ? 'unavailable' : value === null || value === undefined ? 'not available' : value ? 'yes' : 'no'
const sourceLabel = source => source?.observed_name || source?.selector || source?.display_name || source?.name || ''
const modeChange = file => {
  const oldBits = file?.old_execute_bits
  const newBits = file?.new_execute_bits
  if (oldBits !== null && oldBits !== undefined || newBits !== null && newBits !== undefined) {
    const octal = value => value === null || value === undefined ? '—' : `0${Number(value).toString(8)}`
    return `Execute bits: ${octal(oldBits)} → ${octal(newBits)}`
  }
  if (file && file.old_executable !== file.new_executable) {
    return `Executable: ${file.old_executable ? 'on' : 'off'} → ${file.new_executable ? 'on' : 'off'}`
  }
  return ''
}
const fileKind = file => file?.old_sha256 == null && file?.new_sha256 != null
  ? 'Added file'
  : file?.old_sha256 != null && file?.new_sha256 == null ? 'Deleted file' : ''

function LayerPage({ pluginContext }) {
  const connectionId = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  const epoch = useGatewayActivationEpoch()
  const liveContext = useMemo(() => ({ connectionId: connectionId || 'local', profile: profile || 'default', epoch }), [connectionId, profile, epoch])
  const liveKey = profileKey(liveContext)
  const scopeBinding = useRef({ key: '', id: '' })
  const [layers, setLayers] = useState([])
  const [sources, setSources] = useState([])
  const [selectedLayer, setSelectedLayer] = useState(null)
  const [status, setStatus] = useState(null)
  const [candidate, setCandidate] = useState(null)
  const [activeFile, setActiveFile] = useState('')
  const [page, setPage] = useState('layers')
  const [loading, setLoading] = useState(false)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState('')
  const [publishedSelector, setPublishedSelector] = useState('')
  const [deriveSource, setDeriveSource] = useState('')
  const [label, setLabel] = useState('')
  const [edited, setEdited] = useState('')
  const reviewHeading = useRef(null)

  const request = async (captured, path, options) => {
    if (!sameContext(captured)) return null
    const result = await pluginContext.rest(path, options)
    if (!sameContext(captured)) return null
    if (result?.profile_id && !scopeBinding.current.id) {
      scopeBinding.current = { key: profileKey(captured), id: result.profile_id }
    }
    if (!result || result.ok !== true) throw Object.assign(new Error(messageOf(result)), { envelope: result })
    const binding = scopeBinding.current
    if (binding.key === profileKey(captured) && binding.id && result.profile_id !== binding.id) throw new Error('The backend profile changed. Reload this page before continuing.')
    return result
  }
  const read = async (path, captured = contextNow()) => request(captured, path)
  const mutate = async (action, parameters, captured = contextNow()) => {
    if (!sameContext(captured)) return null
    const expectedProfile = scopeBinding.current.key === profileKey(captured) ? scopeBinding.current.id : ''
    if (!expectedProfile) throw new Error('The backend profile is not ready yet. Reload this page.')
    return request(captured, `/${action}`, {
      method: 'POST',
      body: { ...parameters, expected_profile: expectedProfile, request_id: randomId() }
    })
  }
  const ensureScope = (envelope, captured = contextNow()) => {
    if (envelope?.profile_id && sameContext(captured)) {
      scopeBinding.current = { key: profileKey(captured), id: envelope.profile_id }
    }
  }
  const loadLayers = async (captured = contextNow()) => {
    setLoading(true)
    setError(null)
    try {
      const [layerResponse, sourceResponse] = await Promise.all([read('/layers', captured), read('/sources', captured)])
      if (!layerResponse || !sourceResponse || !sameContext(captured)) return
      if (layerResponse.profile_id !== sourceResponse.profile_id) throw new Error('The backend returned different profile scopes. Reload before continuing.')
      ensureScope(layerResponse, captured)
      const rows = layerResponse.data?.layers || []
      const checkedRows = await Promise.all(rows.map(async layer => {
        try {
          const detail = await read(`/layers/${encodeURIComponent(layer.id)}`, captured)
          return detail ? { ...layer, _status: detail.data } : layer
        } catch { return layer }
      }))
      if (!sameContext(captured)) return
      setLayers(checkedRows)
      setSources(sourceResponse.data?.sources || [])
    } catch (caught) {
      if (sameContext(captured)) setError(caught)
    } finally {
      if (sameContext(captured)) setLoading(false)
    }
  }
  const loadStatus = async (layerId, captured = contextNow()) => {
    setLoading(true)
    setError(null)
    try {
      const result = await read(`/layers/${encodeURIComponent(layerId)}`, captured)
      if (!result || !sameContext(captured)) return
      ensureScope(result, captured)
      setStatus(result.data)
      setSelectedLayer(layerId)
      setCandidate(null)
      setPage('history')
    } catch (caught) {
      if (sameContext(captured)) {
        if (caught.envelope?.error?.code === 'STALE_INPUT') setCandidate(current => current ? { ...current, state: 'stale', review_hash: '' } : current)
        setError(caught)
      }
    }
    finally { if (sameContext(captured)) setLoading(false) }
  }
  const loadCandidate = async (candidateId, captured = contextNow()) => {
    setLoading(true)
    setError(null)
    try {
      const result = await read(`/candidates/${encodeURIComponent(candidateId)}`, captured)
      if (!result || !sameContext(captured)) return
      ensureScope(result, captured)
      setCandidate(result.data)
      setPublishedSelector(result.data.published_selector || '')
      setSelectedLayer(result.data.layer_id || selectedLayer)
      setActiveFile(result.data.files?.[0]?.path || '')
      setPage('review')
      setNotice('')
      pluginContext.setTimeout(() => { if (sameContext(captured)) reviewHeading.current?.focus() }, 0)
    } catch (caught) {
      if (sameContext(captured)) {
        if (caught.envelope?.error?.code === 'STALE_INPUT') setCandidate(current => current ? { ...current, state: 'stale', review_hash: '' } : current)
        setError(caught)
      }
    }
    finally { if (sameContext(captured)) setLoading(false) }
  }
  useEffect(() => {
    scopeBinding.current = { key: '', id: '' }
    setLayers([]); setSources([]); setSelectedLayer(null); setStatus(null); setCandidate(null); setPublishedSelector('')
    setPage('layers'); setError(null); setNotice(''); setDeriveSource(''); setLabel('')
    loadLayers(liveContext)
    // Key is a canonical route + host activation identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveKey])

  const action = async (name, params, after) => {
    const captured = contextNow()
    setWorking(true); setError(null); setNotice('')
    try {
      const result = await mutate(name, params, captured)
      if (!result || !sameContext(captured)) return
      ensureScope(result, captured)
      const data = result.data || {}
      if (after) await after(data, captured)
      else setNotice(`${name[0].toUpperCase()}${name.slice(1)} complete.`)
    } catch (caught) {
      if (sameContext(captured)) {
        if (caught.envelope?.error?.code === 'STALE_INPUT') setCandidate(current => current ? { ...current, state: 'stale', review_hash: '' } : current)
        setError(caught)
      }
    }
    finally { if (sameContext(captured)) setWorking(false) }
  }
  const startDerive = async event => {
    event.preventDefault()
    await action('derive', { source_id: deriveSource, label: label.trim() }, async (data, captured) => {
      if (data.candidate_id) await loadCandidate(data.candidate_id, captured)
      else setNotice('The derive request completed. Open the new candidate from its layer history.')
    })
  }
  const openDerive = async () => {
    const captured = contextNow()
    setError(null); setLoading(true)
    try {
      const response = await read('/sources', captured)
      if (!response || !sameContext(captured)) return
      ensureScope(response, captured); setSources(response.data?.sources || []); setPage('derive')
    } catch (caught) { if (sameContext(captured)) setError(caught) }
    finally { if (sameContext(captured)) setLoading(false) }
  }
  const openCandidate = id => loadCandidate(id)
  const openLayer = id => loadStatus(id)
  const selected = candidate?.files?.find(file => file.path === activeFile) || candidate?.files?.[0]
  const unresolved = (candidate?.unresolved || []).length
  const preferredExportId = status?.layer?.preferred_export_id || null
  const preferredExport = preferredExportId ? status?.exports?.find(row => row.id === preferredExportId) : null
  const localChangeText = changeLabel(status?.changes?.local, status?.changes?.local_error)
  const sourceChangeText = changeLabel(status?.changes?.source, status?.changes?.source_error)
  const canRefreshCandidate = candidate?.kind !== 'derive' || Boolean(candidate?.captured?.inputs?.preferred_export_id)
  const publishDisabled = working || loading || candidate?.state !== 'ready' || unresolved > 0 || !candidate?.review_hash || candidate?.truncated
  const back = () => { setError(null); setNotice(''); setCandidate(null); setStatus(null); setPage('layers') }
  const resolveConflict = async (choice, value) => {
    if (!candidate || !selected) return
    const params = { candidate_id: candidate.candidate_id, revision: candidate.revision, path: selected.path }
    if (choice === 'edited') params.edited = value
    else params.take = choice
    await action('resolve', params, async (data, captured) => {
      if (data.candidate_id) await loadCandidate(data.candidate_id, captured)
      else await loadCandidate(candidate.candidate_id, captured)
    })
  }
  const createCandidate = (type, layerId) => action(type, { layer_id: layerId }, async (data, captured) => {
    if (data.candidate_id) await loadCandidate(data.candidate_id, captured)
    else setNotice(`${type[0].toUpperCase()}${type.slice(1)} prepared. Open its candidate from history.`)
  })
  const historyRows = status ? [
    ...(status.exports || []).map(row => ({ ...row, type: 'Export' })),
    ...(status.checkpoints || []).map(row => ({ ...row, type: 'Checkpoint' }))
  ].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))) : []
  const layerNodes = layers.map(layer => jsx('li', { className: 'sl-row', children: [
    jsxs('div', { className: 'sl-row-main', children: [
      jsx('strong', { children: layer.label }),
      jsxs('div', { className: 'sl-small sl-wrap', children: ['Source: ', sourceLabel(layer._status?.source) || sources.find(source => source.id === layer.source_id)?.selector || sources.find(source => source.id === layer.source_id)?.name || 'Unavailable source'] }),
      layer.preferred_export_id ? jsx('div', { className: 'sl-small sl-wrap', children: [
        `Preferred selector: ${layer._status?.exports?.find(row => row.id === layer.preferred_export_id)?.selector || layer.preferred_export_id}`,
        ` · Local changes: ${changeLabel(layer._status?.changes?.local, layer._status?.changes?.local_error)}`,
        ` · Source changes: ${changeLabel(layer._status?.changes?.source, layer._status?.changes?.source_error)}`,
        layer._status?.changes?.local_error?.message ? ` · Local status: ${layer._status.changes.local_error.message}` : '',
        layer._status?.changes?.source_error?.message ? ` · Source status: ${layer._status.changes.source_error.message}` : ''
      ] }) : jsx('div', { className: 'sl-small', children: 'No export published yet.' })
    ] }),
    jsx(Button, { variant: 'secondary', onClick: () => openLayer(layer.id), children: 'History and status' })
  ] }, layer.id))

  return jsxs('main', { className: 'sl-page', children: [
    jsx('style', { children: CSS }),
    jsxs('header', { className: 'sl-head', children: [
      jsxs('div', { className: 'sl-head-copy', children: [
        jsx('h1', { children: 'Skill Layers' }),
        jsxs('div', { className: 'sl-context', 'aria-label': 'Active context', children: [jsx('span', { children: `Connection: ${liveContext.connectionId}` }), jsx('span', { children: `Profile: ${liveContext.profile}` })] })
      ] }),
      jsx('div', { className: 'sl-actions', children: page === 'layers' ? jsx(Button, { onClick: openDerive, children: 'Derive a skill' }) : jsx(Button, { variant: 'secondary', onClick: back, children: 'Back to layers' }) })
    ] }),
    error ? jsxs('div', { role: 'alert', className: 'sl-alert', children: [jsx('strong', { children: error.envelope?.error?.code || 'Request failed' }), jsx('div', { children: error.message }), error.envelope?.error?.code === 'DEPENDENCY_MISSING' ? jsx('p', { children: 'Install the documented Git and YAML dependencies, then reload Skill Layers.' }) : null, error.envelope?.error?.code === 'RECOVERY_REQUIRED' ? jsxs('div', { children: [jsx('p', { children: 'Inspect and reconcile the pending operation before retrying publication. Recovery never adopts unrelated files.' }), jsx(Button, { variant: 'secondary', disabled: working, onClick: () => action('recover', { operation_id: error.envelope?.operation_id }, async (_data, captured) => { setNotice('Recovery finished. Reloaded profile state.'); await loadLayers(captured) }), children: working ? 'Recovering…' : 'Recover pending operation' })] }) : null, error.envelope?.error?.code === 'SOURCE_MOVED' || error.envelope?.error?.code === 'INVALID_SOURCE' ? jsx('p', { children: 'The selected source is no longer available. Choose a currently discovered source or re-derive.' }) : null, jsx(Button, { variant: 'secondary', onClick: () => loadLayers(), children: 'Reload' })] }) : null,
    notice ? jsx('div', { role: 'status', className: 'sl-banner', children: notice }) : null,
    loading ? jsx('p', { role: 'status', 'aria-live': 'polite', children: 'Loading Skill Layers…' }) : null,
    page === 'layers' ? jsxs('section', { className: 'sl-section', children: [
      jsx('h2', { children: 'Your layers' }),
      !loading && layers.length === 0 ? jsx(EmptyState, { title: 'No skill layers yet', description: 'Derive a copy from a discovered skill. Your source stays unchanged.' }) : null,
      jsx('ul', { className: 'sl-history', children: layerNodes }),
      jsx('div', { className: 'sl-banner', children: NOTICE })
    ] }) : null,
    page === 'derive' ? jsxs('section', { className: 'sl-section', children: [
      jsx('h2', { children: 'Choose a discovered source' }),
      sources.length ? jsx('form', { className: 'sl-form', onSubmit: startDerive, children: [
        jsx('div', { className: 'sl-field', children: [jsx('label', { htmlFor: 'sl-source', children: 'Skill selector' }), jsx('select', { id: 'sl-source', required: true, value: deriveSource, onChange: e => setDeriveSource(e.target.value), children: [jsx('option', { value: '', children: 'Select a discovered skill' }), sources.map(source => jsx('option', { value: source.id, children: `${source.selector || source.name} — ${source.name || source.selector}` }, source.id))] })] }),
        jsx('div', { className: 'sl-field', children: [jsx('label', { htmlFor: 'sl-label', children: 'Layer label' }), jsx(Input, { id: 'sl-label', value: label, onChange: e => setLabel(e.target.value), maxLength: 100, required: true, placeholder: 'For example: Research edition' })] }),
        jsx('p', { className: 'sl-muted', children: 'Derive prepares a review candidate. It does not create the installed export.' }),
        jsx(Button, { type: 'submit', disabled: working || !deriveSource || !label.trim(), children: working ? 'Preparing candidate…' : 'Prepare derive review' })
      ] }) : jsx(EmptyState, { title: 'No discovered sources', description: 'There are no selectable installed skill sources in this profile.' }),
      jsx('div', { className: 'sl-banner', children: 'Source selectors are discovered by Hermes. Skill file contents are treated as untrusted text.' })
    ] }) : null,
    page === 'history' && status ? jsxs('section', { className: 'sl-section', children: [
      jsxs('div', { className: 'sl-head', children: [jsxs('div', { children: [jsx('h2', { children: status.layer?.label || 'Layer history' }), jsx('p', { className: 'sl-small', children: `Source: ${sourceLabel(status.source) || status.layer?.source_id || 'unavailable'}` }), preferredExportId ? jsx('p', { className: 'sl-small sl-wrap', children: `Preferred export: ${preferredExport?.selector || preferredExportId}` }) : jsx('p', { className: 'sl-small', children: 'No preferred export (preferred export ID is empty).' })] }), jsx('div', { className: 'sl-actions', children: [status.layer?.preferred_export_id ? jsx(Button, { variant: 'secondary', disabled: working, onClick: () => createCandidate('refresh', selectedLayer), children: 'Prepare refresh' }) : null, status.layer?.preferred_export_id ? jsx(Button, { variant: 'secondary', disabled: working, onClick: () => action('checkpoint', { layer_id: selectedLayer }, (_data, captured) => { setNotice('Checkpoint created.'); loadStatus(selectedLayer, captured) }), children: 'Create checkpoint' }) : null] })] }),
      jsx('div', { className: 'sl-status', children: [jsx(Badge, { variant: status.changes?.local_error ? 'warn' : status.changes?.local ? 'warn' : 'muted', children: `Local changes: ${preferredExportId ? localChangeText : 'not available'}` }), jsx(Badge, { variant: status.changes?.source_error ? 'warn' : status.changes?.source ? 'warn' : 'muted', children: `Source changes: ${sourceChangeText}` })] }),
      status.changes?.local_error?.message ? jsx('p', { className: 'sl-small', children: `Local status unavailable: ${status.changes.local_error.message}` }) : null,
      status.changes?.source_error?.message ? jsx('p', { className: 'sl-small', children: `Source status unavailable: ${status.changes.source_error.message}` }) : null,
      jsxs('div', { className: 'sl-section', children: [jsx('h3', { children: 'Candidates' }), status.candidates?.length ? jsx('ul', { className: 'sl-history', children: [...status.candidates].reverse().map(row => jsx('li', { className: 'sl-row', children: [jsxs('div', { className: 'sl-row-main', children: [jsx('strong', { children: `${row.kind || 'Candidate'} · ${row.state}` }), jsx('div', { className: 'sl-small', children: `Revision ${row.revision}; unresolved conflicts: ${row.unresolved?.length || 0}` })] }), jsx(Button, { variant: 'secondary', onClick: () => openCandidate(row.candidate_id), children: 'Review candidate' })] }, row.candidate_id)) }) : jsx('p', { className: 'sl-muted', children: 'No review candidates.' })] }),
      jsxs('div', { className: 'sl-section', children: [jsx('h3', { children: 'History' }), historyRows.length ? jsx('ul', { className: 'sl-history', children: historyRows.map(row => jsxs('li', { children: [jsxs('div', { className: 'sl-inline', children: [jsx(Badge, { variant: row.type === 'Export' ? 'default' : 'outline', children: row.type }), jsx('strong', { children: row.selector || row.identity || row.id })] }), jsx('div', { className: 'sl-small sl-wrap', children: `${row.created_at || 'date unavailable'} · associated base ${row.base_snapshot || 'unknown'}${row.id === status.layer?.preferred_export_id ? ' · preferred export' : ''}` }), row.type === 'Checkpoint' ? jsx(Button, { variant: 'secondary', onClick: () => action('rollback', { layer_id: selectedLayer, checkpoint_id: row.id }, async (data, captured) => data.candidate_id ? loadCandidate(data.candidate_id, captured) : setNotice('Rollback review prepared.')), children: 'Review rollback' }) : null] }, row.id)) }) : jsx('p', { className: 'sl-muted', children: 'No exports or checkpoints yet.' })] }),
      status.publication_boundary ? jsx('p', { className: 'sl-banner', children: status.publication_boundary }) : null
    ] }) : null,
    page === 'review' && candidate ? jsxs('section', { className: 'sl-section', children: [
      jsxs('header', { className: 'sl-head', children: [jsxs('div', { className: 'sl-head-copy', children: [jsx('h2', { tabIndex: -1, ref: reviewHeading, children: `${candidate.kind || 'Candidate'} review` }), jsx('div', { className: 'sl-context', children: [jsx('span', { children: `State: ${candidate.state}` }), jsx('span', { children: `Revision: ${candidate.revision}` }), jsx('span', { children: `Unresolved: ${unresolved}` })] })] }), jsx('div', { className: 'sl-actions', children: [canRefreshCandidate ? jsx(Button, { variant: 'secondary', disabled: working, onClick: () => createCandidate('refresh', candidate.layer_id), children: 'Prepare refresh' }) : null, jsx(Button, { disabled: publishDisabled, onClick: () => action('publish', { candidate_id: candidate.candidate_id, revision: candidate.revision, review_hash: candidate.review_hash }, async (data, captured) => { const selector = data.export?.selector || ''; setPublishedSelector(selector); setCandidate(current => current ? { ...current, state: 'published' } : current); setNotice(`Published ${selector || 'new skill'}. Choose its selector explicitly in Hermes. ${data.notice || ''}`); await loadLayers(captured) }), children: working ? 'Working…' : 'Publish reviewed skill' })] })] }),
      jsx('p', { className: 'sl-banner', children: `Destination: ${candidate.destination || 'not available'} · B ${candidate.captured?.base || '—'} · L ${candidate.captured?.local || '—'} · U ${candidate.captured?.source || '—'}` }),
      candidate.truncated ? jsx('div', { role: 'alert', className: 'sl-alert', children: 'This diff is truncated. Fetch the complete candidate diff before publishing.' }) : null,
      candidate.state === 'stale' ? jsx('div', { role: 'alert', className: 'sl-alert', children: 'This candidate is stale. Reopen the layer and prepare a new review.' }) : null,
      jsx('div', { className: 'sl-review', children: [
        jsxs('nav', { className: 'sl-files', 'aria-label': 'Changed files', children: [jsx('h3', { children: `Files (${candidate.files?.length || 0})` }), candidate.files?.length ? jsx('ul', { className: 'sl-file-list', children: candidate.files.map(file => jsx('li', { children: jsx('button', { type: 'button', 'aria-current': file.path === (activeFile || candidate.files?.[0]?.path), onClick: () => { setActiveFile(file.path); setEdited('') }, children: [file.path, fileKind(file) ? ` · ${fileKind(file).toLowerCase()}` : '', file.conflict ? ' · conflict' : '', file.binary ? ' · binary' : '', modeChange(file) ? ' · mode changed' : ''] }) }, file.path)) }) : jsx('p', { className: 'sl-muted', children: 'No changed files.' })] }),
        jsxs('article', { className: 'sl-diff', children: [jsx('h3', { children: selected?.path || 'Complete diff' }), fileKind(selected) ? jsx('p', { className: 'sl-small', children: fileKind(selected) }) : null, selected?.conflict ? jsx('p', { role: 'status', children: `Conflict: ${selected.conflict.kind || 'whole-file conflict'}` }) : null, modeChange(selected) ? jsx('p', { className: 'sl-small', children: modeChange(selected) }) : null, selected?.binary ? jsxs('div', { className: 'sl-small sl-wrap', children: [jsx('p', { children: 'Binary file changed. Content is not rendered.' }), jsx('p', { children: `Old SHA-256: ${selected.old_sha256 || 'not present'}` }), jsx('p', { children: `New SHA-256: ${selected.new_sha256 || 'not present'}` })] }) : jsx('pre', { children: selected?.diff || 'No textual changes for this file.' }), selected?.conflict ? jsxs('div', { className: 'sl-conflict', children: [jsx('h3', { children: 'Resolve this whole file' }), jsx('p', { className: 'sl-small', children: 'Conflict sides use the neutral skill identity. These captured versions are review inputs; the conflict is not a proposed deletion.' }), ...Object.entries(selected.conflict.sides || {}).map(([side, version]) => jsx('details', { open: side === 'proposal', children: [jsx('summary', { children: `${side === 'proposal' ? 'Git proposal' : side} · ${version.present ? `${version.size} bytes · SHA-256 ${version.sha256}` : 'File absent'}` }), version.present ? jsx('p', { className: 'sl-small', children: `Execute mask: ${Number(version.execute_bits || 0).toString(8)}${version.truncated ? ' · Preview truncated; fetch complete diff before resolving.' : ''}` }) : null, version.binary ? jsx('p', { children: 'Binary content is identified by its hash and is not rendered.' }) : version.text !== undefined ? jsx('pre', { children: version.text }) : null] }, side)), jsxs('div', { className: 'sl-conflict-actions', children: [jsx(Button, { variant: 'secondary', disabled: working || !selected.conflict.local_available, onClick: () => resolveConflict('local'), children: 'Keep local file' }), jsx(Button, { variant: 'secondary', disabled: working || !selected.conflict.source_available, onClick: () => resolveConflict('source'), children: 'Take source file' }), jsx(Button, { variant: 'secondary', disabled: working, onClick: () => resolveConflict('delete'), children: 'Delete file' })] }), jsx('label', { className: 'sl-field', children: [jsx('span', { children: 'Edited whole-file text' }), jsx(Textarea, { value: edited, onChange: e => setEdited(e.target.value), spellCheck: false, 'aria-label': 'Edited whole-file text' })] }), jsx(Button, { variant: 'secondary', disabled: working, onClick: () => resolveConflict('edited', edited), children: 'Use edited text' })] }) : null] })
      ] }),
      jsx('div', { className: 'sl-section', children: [jsx('h3', { children: 'Review inputs' }), jsx('p', { className: 'sl-small sl-wrap', children: `Captured base ${candidate.captured?.base || '—'}; local ${candidate.captured?.local || '—'}; source ${candidate.captured?.source || '—'}. Candidate revision ${candidate.revision}; review hash ${candidate.review_hash || 'unavailable'}.` }), publishedSelector ? jsxs('div', { className: 'sl-banner sl-inline', children: [jsx('strong', { children: `Published native selector: ${publishedSelector}` }), jsx(CopyButton, { text: `/${publishedSelector}`, label: 'Copy invocation', children: 'Copy invocation' }), jsx('span', { className: 'sl-small', children: 'Choose this exact skill in Hermes; existing chats do not switch.' })] }) : jsx('p', { className: 'sl-small sl-wrap', children: `Proposed selector: ${candidate.destination?.split(/[\\/]/).filter(Boolean).pop() || 'unavailable'} · Publish the reviewed candidate before copying an invocation.` }), jsx('p', { className: 'sl-muted', children: candidate.notice || NOTICE }), jsx('p', { className: 'sl-muted', children: candidate.behavior_notice || 'A clean textual merge does not establish correct skill behavior.' }), jsx('p', { className: 'sl-muted', children: candidate.publication_boundary || 'Direct plugin publication is separate from native skill_manage approvals. Never use it as a fallback for a denied native edit.' }), candidate.publish_command ? jsx('p', { className: 'sl-small sl-wrap', children: `CLI publication command: ${candidate.publish_command}` }) : null] })
    ] }) : null,
    working ? jsx('p', { role: 'status', 'aria-live': 'polite', children: 'Saving your reviewed action…' }) : null
  ] })
}

export default {
  id: ID,
  name: 'Skill Layers',
  defaultEnabled: false,
  register(pluginContext) {
    pluginContext.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(LayerPage, { pluginContext }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: ROUTE, label: 'Skill Layers', codicon: 'layers' } }
    ])
  }
}
