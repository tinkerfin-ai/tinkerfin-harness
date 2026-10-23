import AppKit

@main
@MainActor
struct CompanionApplication {
    static func main() {
        let arguments = CommandLine.arguments
        if arguments.count > 1, UpdateCredentials.commands.contains(arguments[1]) {
            // 辅助模式仅运行这一个任务，结果或调用方取消负责结束进程
            Task { Darwin.exit(await UpdateCredentials.handle(arguments) ?? 1) }
            dispatchMain()
        }
        let app = NSApplication.shared
        guard ClientInstallation.accepts(Bundle.main.bundleURL) else {
            ClientInstallation.showReplacementRequired(); return
        }
        if let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tinkerfin.companion")
            .first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier && !$0.isTerminated }) {
            running.activate(); return
        }
        let instance: ClientInstanceLock
        // AppKit事件循环须从同步main进入，不能占住Swift并发的主执行器
        do { instance = try ClientInstanceLock(file: ClientInstanceLock.location) }
        catch { return }
        let state: URL
        if let position = arguments.firstIndex(of: "--state-directory"), arguments.indices.contains(position + 1) {
            state = URL(fileURLWithPath: arguments[position + 1], isDirectory: true)
        } else {
            state = URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
                .appendingPathComponent(".tinkerfin", isDirectory: true)
        }
        let resources = Bundle.main.resourceURL ?? Bundle.main.bundleURL
        let delegate = CompanionAppDelegate(model: CompanionModel(state: state, resources: resources), resources: resources)
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        withExtendedLifetime((delegate, instance)) { app.run() }
    }
}
