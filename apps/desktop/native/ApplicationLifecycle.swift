import AppKit
import CoreFoundation

/// 从AppKit事件循环发起退出，让terminateLater中的异步清理继续使用主队列
@MainActor
func requestApplicationTermination() {
    RunLoop.main.perform(inModes: [.common]) { MainActor.assumeIsolated { NSApp.terminate(nil) } }
    CFRunLoopWakeUp(CFRunLoopGetMain())
}
