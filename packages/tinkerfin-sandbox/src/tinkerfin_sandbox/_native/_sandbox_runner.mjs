/** Run restricted workloads with SRT networking and protected-tree exceptions. */

import { spawn } from 'node:child_process'
import { readFile, realpath, stat } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

function quote(arguments_) {
  return arguments_.map(value => /^[A-Za-z0-9_./:@+,-][A-Za-z0-9_./:=@+,-]*$/.test(value)
    ? value : "'" + value.replaceAll("'", "'\"'\"'") + "'").join(' ')
}

/** Accept only the canonical literal argv encoding emitted by SRT 0.0.79. */
export function literalArguments(command) {
  const result = []
  let index = 0
  while (index < command.length) {
    let argument = ''
    while (index < command.length && command[index] !== ' ') {
      if (command[index] === "'") {
        const end = command.indexOf("'", index + 1)
        if (end < 0) throw new Error('Unterminated sandbox argument')
        argument += command.slice(index + 1, end)
        index = end + 1
      } else if (command.slice(index, index + 3) === "\"'\"") {
        argument += "'"
        index += 3
      } else {
        const token = command.slice(index).match(/^[A-Za-z0-9_./:=@+,-]+/)
        if (!token) throw new Error('Sandbox command contains a shell operation')
        argument += token[0]
        index += token[0].length
      }
    }
    result.push(argument)
    if (index < command.length) index += 1
  }
  // Re-encoding must reproduce every byte. No substitutions, redirections,
  // operators or alternative shell quoting reach a host shell.
  if (quote(result) !== command) throw new Error('Sandbox command is not canonical argv')
  return result
}

function nativeString(value) {
  if (typeof value !== 'string' || !path.isAbsolute(value) || /[\x00-\x1f\x7f]/.test(value)) {
    throw new Error('Protected paths must be native absolute paths')
  }
  return JSON.stringify(value)
}

function outside(root, exceptions) {
  const subtree = `(subpath ${nativeString(root)})`
  if (!exceptions.length) return subtree
  const allowed = exceptions.map(directory => `(subpath ${nativeString(directory)})`).join(' ')
  return `(require-all ${subtree} (require-not ${exceptions.length > 1 ? `(require-any ${allowed})` : allowed}))`
}

/** Add a kernel-enforced exclusion without disabling SRT filesystem rules. */
export function restrictTrees(command, isolation, graphics, protectedPaths) {
  const argv = literalArguments(command)
  const executable = argv.indexOf('/usr/bin/sandbox-exec')
  if (argv[0] !== 'env' || executable < 1 || argv.lastIndexOf('/usr/bin/sandbox-exec') !== executable
    || argv[executable + 1] !== '-p' || !argv[executable + 2]?.startsWith('(version 1)')
    || argv.length !== executable + 6 || argv[executable + 3] !== '/bin/bash'
    || argv[executable + 4] !== '-c') {
    throw new Error('Unexpected native sandbox command structure')
  }
  const restrictions = []
  if (graphics) {
    // Chromium 156.0.8078.4 base/mac/mac_util.mm IsVirtualMachine reads this exact
    // value during GUI initialization; no sysctl writes are authorized.
    restrictions.push('(allow sysctl-read (sysctl-name "kern.hv_vmm_present"))')
    // Chromium 155.0.8059.82 LaunchProcess registers each child's ports;
    // MachPortRendezvousServerMac::PortsForClient consumes that entry using
    // the kernel audit PID. Clients look up their parent's PID. This is
    // protocol authentication, not a private bootstrap namespace or an
    // assumption that optional code-signing checks are enforced.
    restrictions.push('(allow mach-register mach-lookup (global-name-regex #"^(com[.]google[.](chrome[.]for[.]testing|Chrome)|org[.]chromium[.]Chromium)[.]MachPortRendezvousServer[.][0-9]+$"))')
  }
  const pinned = new Set()
  const traversable = new Set()
  for (const item of isolation) {
    if (!item || Object.keys(item).sort().join(',') !== 'read,root,write'
      || !Array.isArray(item.read) || !(item.write === null || item.read.includes(item.write))) {
      throw new Error('Invalid protected-tree declaration')
    }
    const { root, read, write } = item
    nativeString(root)
    if (root === '/' || path.normalize(root) !== root) throw new Error('Invalid isolated root')
    for (const allowed of read) {
      nativeString(allowed)
      if (path.normalize(allowed) !== allowed || !allowed.startsWith(root + '/')) {
        throw new Error('An accessible tree must be a strict descendant of its isolated root')
      }
    }
    // Seatbelt's explicit operation rules outrank wildcard rules. SRT 0.0.79
    // generateReadRules explicitly allows create/unlink within write roots;
    // both operations must be denied explicitly here (native protection test).
    restrictions.push(`(deny file-read* file-read-metadata ${outside(root, read)})`)
    const writable = write === null ? [] : [write]
    restrictions.push(`(deny file-write* file-write-create file-write-unlink ${outside(root, writable)})`)
    restrictions.push(`(deny network-outbound (remote unix-socket ${outside(root, writable)}))`)
    // SRT allows metadata on every directory for realpath traversal. Only the
    // paths to explicit read exceptions need it inside an isolated tree.
    for (const allowed of read) {
      for (let ancestor = path.dirname(allowed); ancestor === root || ancestor.startsWith(root + '/'); ancestor = path.dirname(ancestor)) {
        traversable.add(ancestor)
      }
    }
    // Renaming an ancestor would move protected contents outside the path rule.
    // Roots with no read exceptions still need their ancestors pinned.
    for (const directory of [root, ...read]) {
      for (let ancestor = directory; ancestor !== '/'; ancestor = path.dirname(ancestor)) pinned.add(ancestor)
    }
  }
  // SRT can let allowRead exceptions override denyRead. Protected paths have
  // no exceptions in this API, including explicit grants to their children.
  for (const directory of protectedPaths) {
    restrictions.push(`(deny file-read* file-read-metadata file-write* file-write-create file-write-unlink (subpath ${nativeString(directory)}))`)
    restrictions.push(`(deny network-outbound (remote unix-socket (subpath ${nativeString(directory)})))`)
    for (let ancestor = path.dirname(directory); ancestor !== '/'; ancestor = path.dirname(ancestor)) pinned.add(ancestor)
  }
  for (const directory of pinned) restrictions.push(`(deny file-write* file-write-create file-write-unlink (literal ${nativeString(directory)}))`)
  for (const directory of traversable) {
    if (!protectedPaths.some(protectedPath => directory === protectedPath || directory.startsWith(protectedPath + '/'))) {
      restrictions.push(`(allow file-read-metadata (literal ${nativeString(directory)}))`)
    }
  }
  argv[executable + 2] += '\n' + restrictions.join('\n') + '\n'
  return argv
}

async function main() {
  const [moduleFlag, moduleName, policyFlag, policyFile, separator, ...arguments_] = process.argv.slice(2)
  if (moduleFlag !== '--module' || policyFlag !== '--policy' || separator !== '--' || !arguments_.length) {
    throw new Error('Sandbox runner requires its module, policy and workload argv')
  }
  const modulePath = await realpath(moduleName)
  const manifest = JSON.parse(await readFile(path.resolve(modulePath, '../../package.json'), 'utf8'))
  if (manifest.name !== '@anthropic-ai/sandbox-runtime' || manifest.version !== '0.0.79') {
    throw new Error('The installed sandbox runtime does not match the application')
  }
  const policy = JSON.parse(await readFile(policyFile, 'utf8'))
  if (Object.keys(policy).sort().join(',') !== 'graphics,isolation,protected,sandbox' || !Array.isArray(policy.isolation)
    || !Array.isArray(policy.protected)
    || policy.isolation.length > 32 || typeof policy.graphics !== 'boolean') throw new Error('Invalid sandbox policy')
  const { SandboxManager, SandboxRuntimeConfigSchema } = await import(pathToFileURL(modulePath).href)
  const config = SandboxRuntimeConfigSchema.parse(policy.sandbox)
  if (!config.network.strictAllowlist) throw new Error('Restricted access requires an explicit network allowlist')
  for (const { root, read } of policy.isolation) {
    for (const directory of [root, ...read]) {
      if (await realpath(directory) !== directory || !(await stat(directory)).isDirectory()) {
        throw new Error('Isolated-tree identities must be canonical existing directories')
      }
    }
  }
  if (policy.isolation.length && process.platform !== 'darwin') {
    throw new Error('Protected-tree exceptions require the macOS native sandbox')
  }
  let child
  const interrupted = new AbortController()
  const stop = signal => {
    interrupted.abort()
    child?.kill(signal)
  }
  const term = () => stop('SIGTERM')
  const interrupt = () => stop('SIGINT')
  process.on('SIGTERM', term)
  process.on('SIGINT', interrupt)
  try {
    // No log monitor process is needed: process-group ownership is maintained by
    // the Python host and all proxy resources remain owned by this runner.
    await SandboxManager.initialize(config, undefined, false)
    interrupted.signal.throwIfAborted()
    const wrapped = await SandboxManager.wrapWithSandbox(quote(arguments_), '/bin/bash', undefined, interrupted.signal)
    const argv = process.platform === 'darwin'
      ? restrictTrees(wrapped, policy.isolation, policy.graphics, policy.protected) : ['/bin/bash', '-c', wrapped]
    // Every restricted destination, including localhost, goes through policy.
    const boundary = argv.indexOf('/usr/bin/sandbox-exec')
    for (let index = 1; index < boundary; index++) {
      if (/^(?:NO_PROXY|no_proxy)=/.test(argv[index])) {
        argv[index] = argv[index].split('=', 1)[0] + '='
      }
    }
    interrupted.signal.throwIfAborted()
    process.exitCode = await new Promise((resolve, reject) => {
      child = spawn(argv[0], argv.slice(1), { shell: false, stdio: 'inherit' })
      child.once('error', reject)
      child.once('close', (code, signal) => resolve(code ?? (signal === 'SIGINT' ? 130 : 143)))
    })
  } finally {
    process.off('SIGTERM', term)
    process.off('SIGINT', interrupt)
    try {
      SandboxManager.cleanupAfterCommand()
    } finally {
      await SandboxManager.reset()
    }
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main().catch(() => {
    process.stderr.write('Local sandbox preparation or execution failed\n')
    process.exitCode = 1
  })
}
