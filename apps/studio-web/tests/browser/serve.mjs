import { withBuiltPreview } from '../server/http-servers.mjs'

const port = Number(process.argv[2])
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error('浏览器测试预览需要有效端口')
}

let stop
const stopped = new Promise(resolve => { stop = resolve })
// Playwright 用 SIGINT 结束本轮；由这里等待清理，避免 Vite 的 SIGTERM 处理直接退出进程
// 启动期间也接收停止信号，构建结束后统一关闭预览并清理本轮目录
process.once('SIGINT', stop)
try {
  await withBuiltPreview({
    preview: { host: '127.0.0.1', port, strictPort: true },
  }, async () => stopped)
} finally {
  process.removeListener('SIGINT', stop)
}
