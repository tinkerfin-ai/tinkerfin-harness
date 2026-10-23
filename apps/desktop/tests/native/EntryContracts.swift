import AppKit

private func record(_ value: String) {
    try! FileHandle.standardOutput.write(contentsOf: Data((value + "\n").utf8))
}

// 只替换进程入口外的依赖；正式main与AppKit事件循环保持原样
enum ClientInstallation {
    static func accepts(_ location: URL) -> Bool { true }
    @MainActor static func showReplacementRequired() { fatalError("入口意外要求替换安装") }
}

final class ClientInstanceLock {
    static var location: URL { URL(fileURLWithPath: "/unused-entry-contract.lock") }
    init(file: URL) { record("client-lock") }
}

enum NSRunningApplication {
    struct Running {
        let processIdentifier: pid_t
        let isTerminated: Bool
        func activate() { fatalError("入口意外激活真实客户端") }
    }
    static func runningApplications(withBundleIdentifier identifier: String) -> [Running] { [] }
}

@MainActor
final class CompanionModel {
    init(state: URL, resources: URL) { record("client-model") }
}

@MainActor
final class CompanionAppDelegate: NSObject, NSApplicationDelegate {
    private var operation: Task<Void, Never>?
    private var shutdown: Task<Void, Never>?

    init(model: CompanionModel, resources: URL) { super.init() }

    func applicationDidFinishLaunching(_ notification: Notification) {
        record("client-launched")
        operation = Task {
            record("client-ready")
            requestApplicationTermination()
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if shutdown == nil {
            shutdown = Task {
                await operation?.value
                record("client-closed")
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }
}

enum UpdateCredentials {
    static let commands = ["--receive-update-credential", "--remove-update-credential"]

    @MainActor
    static func handle(_ arguments: [String]) async -> Int32? {
        precondition(NSApp == nil, "凭证助手初始化了客户端")
        await withCheckedContinuation { continuation in
            DispatchQueue.global().async { continuation.resume() }
        }
        precondition(NSApp == nil, "凭证助手创建了应用界面")
        record("helper-finished")
        return 0
    }
}
