import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { build } from 'esbuild'
import { source as sdkStub } from './sdk-stub.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const project = resolve(here, '../..')
const built = await build({
  absWorkingDir: project,
  entryPoints: ['tests/e2e/app.js'],
  bundle: true,
  format: 'esm',
  platform: 'browser',
  write: false,
  plugins: [{
    name: 'hermes-sdk-harness',
    setup(builder) {
      builder.onResolve({ filter: /^@hermes\/plugin-sdk$/ }, () => ({ path: 'sdk', namespace: 'harness' }))
      builder.onLoad({ filter: /.*/, namespace: 'harness' }, () => ({ contents: sdkStub, loader: 'js', resolveDir: project }))
    }
  }]
})
const bundle = built.outputFiles[0].text
const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Skill Layers browser harness</title><style>:root{--ui-text-primary:#222;--ui-text-secondary:#555;--ui-text-tertiary:#777;--ui-stroke-secondary:#ccc;--ui-accent:#155eef}*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif}button{cursor:pointer}#root{min-height:100vh}</style></head><body><div id="root"></div><script type="module" src="/bundle.js"></script></body></html>`
const server = createServer(async (req, res) => {
  if (req.url === '/bundle.js') { res.writeHead(200, { 'content-type': 'text/javascript; charset=utf-8' }); res.end(bundle); return }
  if (req.url === '/' || req.url === '/index.html') { res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }); res.end(html); return }
  res.writeHead(404); res.end('not found')
})
server.listen(5393, '127.0.0.1')
process.on('SIGTERM', () => server.close(() => process.exit(0)))
process.on('SIGINT', () => server.close(() => process.exit(0)))
