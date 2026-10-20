import { createServer } from 'node:net'
import { chmod, mkdir } from 'node:fs/promises'
import { isAbsolute, join } from 'node:path'
import { timingSafeEqual } from 'node:crypto'
import { pathToFileURL } from 'node:url'
import { createConnection } from '@playwright/mcp'
import { chromium } from 'playwright'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { ListRootsRequestSchema } from '@modelcontextprotocol/sdk/types.js'

const frameLimit = 1024 * 1024
const outputLimit = 8 * 1024 * 1024
const utf8 = new TextDecoder('utf-8', { fatal: true })
// 宿主拥有整个浏览器的关闭职责；页面关闭使用 browser_tabs
const allowedTools = new Set([
  'browser_resize', 'browser_console_messages',
  'browser_cookie_list', 'browser_cookie_get', 'browser_cookie_set', 'browser_cookie_delete', 'browser_cookie_clear',
  'browser_handle_dialog', 'browser_emulate_media', 'browser_evaluate',
  'browser_file_upload', 'browser_drop', 'browser_find', 'browser_fill_form',
  'browser_press_key', 'browser_type', 'browser_mouse_move_xy', 'browser_mouse_click_xy',
  'browser_mouse_drag_xy', 'browser_mouse_down', 'browser_mouse_up', 'browser_mouse_wheel',
  'browser_navigate', 'browser_navigate_back', 'browser_network_requests', 'browser_network_request',
  'browser_pdf_save', 'browser_take_screenshot', 'browser_snapshot', 'browser_click',
  'browser_drag', 'browser_hover', 'browser_select_option', 'browser_storage_state',
  'browser_set_storage_state', 'browser_tabs', 'browser_wait_for',
  'browser_localstorage_list', 'browser_localstorage_get', 'browser_localstorage_set',
  'browser_localstorage_delete', 'browser_localstorage_clear', 'browser_sessionstorage_list',
  'browser_sessionstorage_get', 'browser_sessionstorage_set', 'browser_sessionstorage_delete',
  'browser_sessionstorage_clear',
])

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

async function initialization() {
  // 启动配置仅从宿主持有的标准输入接收，命令参数不能改变浏览器归属
  let pending = Buffer.alloc(0)
  for await (const chunk of process.stdin) {
    pending = Buffer.concat([pending, chunk])
    if (pending.length > 16 * 1024) throw new Error('Invalid initialization')
    const end = pending.indexOf(10)
    if (end < 0) continue
    const config = JSON.parse(utf8.decode(pending.subarray(0, end)))
    if (!object(config) || Object.keys(config).sort().join(',') !== 'executable,headed,profile,socket,token,workspace'
      || !['executable', 'profile', 'socket', 'workspace'].every(key => typeof config[key] === 'string' && isAbsolute(config[key]))
      || typeof config.token !== 'string' || !/^[a-f0-9]{64}$/.test(config.token) || typeof config.headed !== 'boolean') {
      throw new Error('Invalid initialization')
    }
    return config
  }
  throw new Error('Missing initialization')
}

const sockets = new Set()
let contextPromise
let mcpServer
let client
let service
let closing
let queue = Promise.resolve()

async function close() {
  if (closing) return closing
  closing = (async () => {
    for (const socket of sockets) socket.destroy()
    // createConnection 借用 context，不负责关闭 Chrome；应用必须等待显式关闭
    const cleanup = []
    if (contextPromise) cleanup.push(contextPromise.then(context => context.close()))
    if (client) cleanup.push(client.close())
    if (mcpServer) cleanup.push(mcpServer.close())
    if (service?.listening) cleanup.push(new Promise(resolve => service.close(resolve)))
    const settled = await Promise.allSettled(cleanup)
    await queue
    if (settled.some(item => item.status === 'rejected')) process.exitCode = 1
  })()
  return closing
}

function stop() {
  void close().then(() => process.exit(process.exitCode || 0))
}

for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.once(signal, stop)

try {
  const config = await initialization()
  const proof = Buffer.from(config.token)
  const proxyAddress = process.env.HTTPS_PROXY || process.env.HTTP_PROXY
  let proxy
  if (proxyAddress) {
    const selected = new URL(proxyAddress)
    if (!['http:', 'https:'].includes(selected.protocol)) throw new Error('Unsupported workspace proxy')
    proxy = {
      server: selected.origin,
      username: decodeURIComponent(selected.username),
      password: decodeURIComponent(selected.password),
      bypass: '<-loopback>',
    }
  }
  const context = async () => {
    if (closing) throw new Error('Browser session closed')
    if (!contextPromise) {
      contextPromise = chromium.launchPersistentContext(config.profile, {
        executablePath: config.executable,
        headless: !config.headed,
        chromiumSandbox: false,
        handleSIGINT: false,
        handleSIGTERM: false,
        handleSIGHUP: false,
        proxy,
        timeout: 30_000,
      })
    }
    return contextPromise
  }
  const output = join(config.workspace, '.tinkerfin', 'browser-output')
  await mkdir(output, { recursive: true, mode: 0o700 })
  mcpServer = await createConnection({
    browser: { browserName: 'chromium' },
    capabilities: ['core', 'pdf', 'vision', 'storage'],
    webmcp: false,
    // 本地页面遵循同一 LocalWorkspace 文件权限；官方默认会额外禁止全部 file: 页面
    allowUnrestrictedFileAccess: true,
    codegen: 'none',
    imageResponses: 'omit',
    outputDir: output,
    filePaths: 'absolute',
    timeouts: { action: 10_000, navigation: 60_000, idle: 0, settle: 0 },
  }, context)
  client = new Client({ name: 'tinkerfin-browser', version: '0.1.1' }, { capabilities: { roots: { listChanged: false } } })
  client.setRequestHandler(ListRootsRequestSchema, () => ({
    roots: [{ uri: pathToFileURL(config.workspace).href, name: 'Workspace' }],
  }))
  const [local, remote] = InMemoryTransport.createLinkedPair()
  await mcpServer.connect(remote)
  await client.connect(local)
  const catalog = (await client.listTools()).tools.filter(tool => allowedTools.has(tool.name))
  const names = new Set(catalog.map(tool => tool.name))

  service = createServer(socket => {
    if (closing || sockets.size >= 8) { socket.destroy(); return }
    sockets.add(socket)
    let pending = Buffer.alloc(0)
    let accepted = false
    let active = false
    let completed = false
    socket.setTimeout(10_000, () => socket.destroy())
    socket.on('error', () => {})
    socket.on('close', () => {
      sockets.delete(socket)
      // 动作结果未知时关闭会话，禁止断开的命令继续操作网页
      if (active && !completed) stop()
    })
    const reply = value => {
      if (socket.destroyed) return
      let text = JSON.stringify(value)
      if (Buffer.byteLength(text) > outputLimit) text = '{"error":"Browser response exceeds its limit"}'
      completed = true
      socket.end(text + '\n')
    }
    socket.on('data', chunk => {
      if (accepted) { socket.destroy(); return }
      pending = Buffer.concat([pending, chunk])
      if (pending.length > frameLimit) { socket.destroy(); return }
      const end = pending.indexOf(10)
      if (end < 0) return
      accepted = true
      let request
      try {
        if (end !== pending.length - 1) throw new Error()
        request = JSON.parse(utf8.decode(pending.subarray(0, end)))
        if (!object(request) || typeof request.token !== 'string'
          || Buffer.byteLength(request.token) !== proof.length
          || !timingSafeEqual(Buffer.from(request.token), proof)) throw new Error()
        if (!['tools', 'describe', 'call', 'shutdown'].includes(request.method)
          || Object.keys(request).some(key => !['token', 'method', 'name', 'arguments'].includes(key))) throw new Error()
      } catch {
        reply({ error: 'Browser request denied' })
        return
      }
      socket.setTimeout(0)
      if (request.method === 'shutdown') {
        socket.once('finish', stop)
        reply({ result: 'closed' })
        return
      }
      if (request.method === 'tools') {
        reply({ result: catalog.map(({ name, description }) => ({ name, description })) })
        return
      }
      if (typeof request.name !== 'string' || !names.has(request.name)) {
        reply({ error: 'Browser tool is not available' })
        return
      }
      if (request.method === 'describe') {
        reply({ result: catalog.find(tool => tool.name === request.name) })
        return
      }
      if (!object(request.arguments)) { reply({ error: 'Browser arguments must be an object' }); return }
      // 每个会话按收到的顺序执行，连接数同时限制排队数量；动作不自动重试
      queue = queue.then(async () => {
        if (closing || socket.destroyed) return
        active = true
        try {
          const result = await client.callTool({ name: request.name, arguments: request.arguments }, undefined, { timeout: 120_000 })
          reply({ result })
        } catch {
          reply({ error: 'Browser action did not complete; verify its result before retrying' })
          stop()
        } finally { active = false }
      })
    })
  })
  await new Promise((resolve, reject) => {
    service.once('error', reject)
    service.listen(config.socket, resolve)
  })
  await chmod(config.socket, 0o600)
  process.stdout.write('{"event":"ready"}\n')
} catch {
  process.stderr.write('Browser service failed\n')
  process.exitCode = 1
  await close()
}
