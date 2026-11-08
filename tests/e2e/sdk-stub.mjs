// Deterministic browser harness adapter. This is a test fixture, not a Desktop
// SDK replacement or evidence of host compatibility.
export const source = `
import * as React from 'react'
const atom = value => {
  const listeners = new Set()
  return { get: () => value, listen(fn) { listeners.add(fn); return () => listeners.delete(fn) }, set(next) { value = next; listeners.forEach(fn => fn(value)) }, subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn) } }
}
export const host = { state: { connectionId: atom('local'), profile: atom('default') } }
let epoch = 0
const epochListeners = new Set()
export const gatewayActivationEpoch = () => epoch
export function useGatewayActivationEpoch() { return React.useSyncExternalStore(fn => { epochListeners.add(fn); return () => epochListeners.delete(fn) }, () => epoch) }
export function useValue(value) { return React.useSyncExternalStore(fn => value.subscribe(fn), () => value.get()) }
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar'
export function Button({ variant, size, ...props }) { return React.createElement('button', { ...props, 'data-variant': variant || 'default' }) }
export function Badge({ variant, ...props }) { return React.createElement('span', { ...props, 'data-badge-variant': variant || 'default' }) }
export function Input(props) { return React.createElement('input', props) }
export function Textarea(props) { return React.createElement('textarea', props) }
export function EmptyState({ title, description }) { return React.createElement('div', null, React.createElement('strong', null, title), description ? React.createElement('p', null, description) : null) }
export function ErrorState({ title, description }) { return React.createElement('div', { role: 'alert' }, title, description) }
export function CopyButton({ text, label, children }) { return React.createElement('button', { type: 'button', 'aria-label': label, onClick: () => { window.__copied = typeof text === 'function' ? text() : text } }, children || label) }
window.changeSkillContext = (connectionId, profile) => { window.__profile = profile; host.state.connectionId.set(connectionId); host.state.profile.set(profile); epoch += 1; epochListeners.forEach(fn => fn()) }
`
