import AppKit

@MainActor
final class TerminationDelegate: NSObject, NSApplicationDelegate {
    private var operation: Task<Void, Never>?
    private var shutdown: Task<Void, Never>?

    func applicationDidFinishLaunching(_ notification: Notification) {
        operation = Task {
            if CommandLine.arguments[1] == "dispatch" {
                DispatchQueue.main.async { requestApplicationTermination() }
            } else {
                requestApplicationTermination()
            }
            try! FileHandle.standardOutput.write(contentsOf: Data("operation returned\n".utf8))
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if shutdown == nil {
            shutdown = Task {
                await operation?.value
                try! FileHandle.standardOutput.write(contentsOf: Data("operation joined\n".utf8))
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }
}

@main
@MainActor
struct TerminationContracts {
    static func main() {
        let application = NSApplication.shared
        let delegate = TerminationDelegate()
        application.setActivationPolicy(.prohibited)
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
