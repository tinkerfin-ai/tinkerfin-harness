import AppKit
import Darwin
import Foundation

/// 启动时订阅实际父进程退出，安装完成后再等待，避免PID重新使用或状态锁尚未释放
@MainActor
private final class ParentExit {
    private let source: any DispatchSourceProcess
    private var finished = false
    private var waiter: CheckedContinuation<Void, Never>?
    init(_ identifier: Int32) {
        source = DispatchSource.makeProcessSource(identifier: identifier, eventMask: .exit, queue: .main)
        source.setEventHandler { [weak self] in
            guard let self else { return }
            self.finished = true; self.waiter?.resume(); self.waiter = nil
        }
        source.resume()
    }
    func wait() async { if !finished { await withCheckedContinuation { waiter = $0 } } }
    func close() { source.cancel(); waiter?.resume(); waiter = nil }
}

private final class UpdateDialogWindow: NSWindow {
    override var canBecomeKey: Bool { true }
}

/// 主进程退出后由本助手持有重启反馈，成功须匹配新进程的真实启动回执
@MainActor
private final class InstallerDelegate: NSObject, NSApplicationDelegate {
    private let directory: URL
    private let restarting: Bool
    private let files = UpdateFileWork()
    private var handoff: UpdateHandoff?
    private var parent: ParentExit?
    private var operation: Task<Void, Never>?
    private var shutdown: Task<Void, Never>?
    private var interface: DesktopWebView?
    private var window: NSWindow?
    private var phase: DesktopUpdatePhase = .installing
    private var transferred = false
    private var launchedApplication: NSRunningApplication?
    private var installationLock: UpdateTargetLock?
    private var inheritedDescriptor: Int32?
    private var finishing = false
    private var appearance: NSKeyValueObservation?

    init(directory: URL, restarting: Bool, inheritedDescriptor: Int32) {
        self.directory = directory.standardizedFileURL; self.restarting = restarting; self.inheritedDescriptor = inheritedDescriptor
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        operation = Task { [self] in
            defer { operation = nil }
            do {
                let handoff = try await files.perform { [directory] _ in try UpdateHandoff.read(directory) }
                guard handoff.parentPID == getppid() else { throw UpdateError.invalidLocation }
                self.handoff = handoff; parent = ParentExit(handoff.parentPID)
                guard let descriptor = inheritedDescriptor else { throw UpdateError.invalidLocation }
                inheritedDescriptor = nil
                let lock = try await files.perform { _ in try UpdateTargetLock(application: handoff.target, inherited: descriptor) }
                installationLock = lock
                try await files.perform { cancellation in
                    let release = try DesktopRelease.verify(manifest: UpdateWorkspace.read(handoff.directory.appendingPathComponent("update.json"), limit: 64 * 1024),
                        signature: UpdateWorkspace.read(handoff.directory.appendingPathComponent("update.json.sig"), limit: 64))
                    guard release.version == handoff.targetVersion else { throw UpdateError.invalidManifest }
                    // 先保存可恢复的交接指针，原子交换后只剩不会抛错的进度发送
                    try UpdateWorkspace.write(UpdateInstallReceipt(identifier: handoff.identifier, version: handoff.targetVersion,
                        helperPID: ProcessInfo.processInfo.processIdentifier), to: handoff.pending)
                    if self.restarting {
                        try UpdateInstallation.checkIdentity(handoff.target, version: handoff.targetVersion)
                        guard try UpdateTree.digest(handoff.target, cancellation: cancellation) == release.treeSHA256 else { throw UpdateError.invalidArchive }
                    } else {
                        try UpdateInstallation.install(handoff, release: release, lock: lock, cancellation: cancellation) { percent in Self.emit(.init(kind: .progress, percent: percent)) }
                    }
                }
                Self.emit(.init(kind: .installed))
                guard fcntl(STDIN_FILENO, F_SETFL, O_NONBLOCK) == 0 else { throw UpdateError.failedProcess }
                var command = Data()
                while command.count <= 64, !command.contains(10), let bytes = try await readFromPipe(STDIN_FILENO) { command.append(bytes) }
                guard command.isEmpty || command == Data("restart\n".utf8) else { throw UpdateError.failedProcess }
                transferred = true; phase = .restarting
                Self.emit(.init(kind: .restartReady))
                await parent?.wait()
                try? FileHandle.standardInput.close(); try? FileHandle.standardOutput.close()
                showDialog(); await launchApplication()
            } catch {
                if transferred { showDialog(); phase = .restartError; publish() }
                else { Self.emit(.init(kind: .failed)); finish() }
            }
        }
    }

    nonisolated private static func emit(_ event: UpdateInstallerEvent) {
        guard var data = try? JSONEncoder().encode(event) else { return }
        data.append(10)
        _ = data.withUnsafeBytes { Darwin.write(STDOUT_FILENO, $0.baseAddress!, $0.count) }
    }

    private func showDialog() {
        guard window == nil, let handoff else { return }
        let interface = DesktopWebView(resources: handoff.candidate.appendingPathComponent("Contents/Resources"))
        self.interface = interface
        interface.refresh = { [weak self] in self?.publish() }
        interface.handle = { [weak self] command in
            guard let self, self.shutdown == nil else { return DesktopReply(ok: false) }
            switch command {
            case .ready: self.publish(); return DesktopReply(ok: true)
            case .updateRetry where self.phase == .restartError && self.operation == nil:
                self.phase = .restarting; self.publish()
                self.operation = Task { defer { self.operation = nil }; await self.launchApplication() }
                return DesktopReply(ok: true)
            case .updateCancel where self.phase == .restartError:
                DispatchQueue.main.async { self.finish() }; return DesktopReply(ok: true)
            default: return DesktopReply(ok: false)
            }
        }
        let window = UpdateDialogWindow(contentRect: NSRect(x: 0, y: 0, width: 320, height: 144), styleMask: .borderless, backing: .buffered, defer: false)
        window.contentView = interface.view; window.isReleasedWhenClosed = false
        window.isOpaque = false; window.backgroundColor = .clear; window.hasShadow = true
        interface.view.wantsLayer = true; interface.view.layer?.cornerRadius = 14; interface.view.layer?.masksToBounds = true
        window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(); self.window = window
        appearance = NSApp.observe(\.effectiveAppearance) { [weak self] _, _ in MainActor.assumeIsolated { self?.publish() } }
    }

    private func publish() {
        guard let handoff else { return }
        var snapshot = DesktopSnapshot()
        snapshot.surface = .updater; snapshot.ready = true
        snapshot.language = handoff.language; snapshot.locale = handoff.locale
        snapshot.theme = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? "dark" : "light"
        snapshot.version = handoff.targetVersion
        snapshot.update = DesktopUpdateState(phase: phase, currentVersion: handoff.targetVersion, targetVersion: handoff.targetVersion)
        interface?.publish(snapshot)
    }

    private func launchApplication() async {
        guard let handoff else { return }
        do {
            phase = .restarting; publish()
            let application: NSRunningApplication
            if let launchedApplication, !launchedApplication.isTerminated {
                application = launchedApplication; application.activate()
            } else {
                let configuration = NSWorkspace.OpenConfiguration()
                configuration.activates = true; configuration.createsNewApplicationInstance = true
                configuration.arguments = ["--state-directory", handoff.stateDirectory, "--update-receipt", handoff.directory.path]
                application = try await NSWorkspace.shared.openApplication(at: handoff.target, configuration: configuration)
                launchedApplication = application
            }
            try await waitForReceipt(application, handoff: handoff)
            do { try await files.perform { _ in try UpdateWorkspace.remove(handoff.directory, root: handoff.directory.deletingLastPathComponent()) } }
            catch { NSLog("TinkerFin 已更新，暂存目录清理未完成") }
            finish()
        } catch { phase = .restartError; publish() }
    }

    private func waitForReceipt(_ application: NSRunningApplication, handoff: UpdateHandoff) async throws {
        let descriptor = open(handoff.directory.path, O_EVTONLY | O_NOFOLLOW)
        guard descriptor >= 0 else { throw UpdateError.invalidLocation }
        let signals = AsyncStream.makeStream(of: Void.self, bufferingPolicy: .bufferingNewest(1))
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: descriptor, eventMask: [.write, .rename, .delete], queue: .main)
        source.setEventHandler { signals.continuation.yield(()) }
        source.setCancelHandler { Darwin.close(descriptor) }; source.resume()
        let observer = NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didTerminateApplicationNotification, object: nil, queue: .main) { _ in
            signals.continuation.yield(())
        }
        let deadline = Task { try? await Task.sleep(for: .seconds(30)); signals.continuation.finish() }
        defer {
            deadline.cancel(); signals.continuation.finish(); source.cancel()
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
        }
        signals.continuation.yield(())
        for await _ in signals.stream {
            try Task.checkCancellation()
            guard !application.isTerminated else { throw UpdateError.unavailable }
            let receipt = try await files.perform { _ -> UpdateLaunchReceipt? in
                let path = handoff.directory.appendingPathComponent("launched.json")
                guard FileManager.default.fileExists(atPath: path.path) else { return nil }
                return try JSONDecoder().decode(UpdateLaunchReceipt.self, from: UpdateWorkspace.read(path, limit: 4096))
            }
            if let receipt, receipt.identifier == handoff.identifier, receipt.version == handoff.targetVersion,
               receipt.processID == application.processIdentifier { return }
        }
        try Task.checkCancellation(); throw UpdateError.unavailable
    }

    private func finish() {
        finishing = true
        requestApplicationTermination()
    }

    private func waitForExit(_ application: NSRunningApplication) async throws {
        let signals = AsyncStream.makeStream(of: Void.self, bufferingPolicy: .bufferingNewest(1))
        let observer = NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didTerminateApplicationNotification, object: nil, queue: .main) { _ in
            signals.continuation.yield(())
        }
        // terminate只确认请求已发送；对方拒绝或清理未完成时恢复重启反馈
        let deadline = Task { try? await Task.sleep(for: .seconds(30)); signals.continuation.finish() }
        defer { deadline.cancel(); NSWorkspace.shared.notificationCenter.removeObserver(observer); signals.continuation.finish() }
        signals.continuation.yield(())
        for await _ in signals.stream {
            try Task.checkCancellation()
            if application.isTerminated { return }
        }
        try Task.checkCancellation(); throw UpdateError.unavailable
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard finishing || phase == .restartError else { return .terminateCancel }
        if shutdown == nil {
            shutdown = Task {
                if phase == .restartError, let launchedApplication, !launchedApplication.isTerminated {
                    do {
                        guard launchedApplication.terminate() else { throw UpdateError.unavailable }
                        try await waitForExit(launchedApplication)
                    } catch {
                        shutdown = nil; finishing = false; sender.reply(toApplicationShouldTerminate: false); return
                    }
                }
                if phase == .restartError, let handoff {
                    try? await files.perform { _ in
                        try UpdateWorkspace.write(UpdateInstallReceipt(identifier: handoff.identifier, version: handoff.targetVersion,
                            helperPID: ProcessInfo.processInfo.processIdentifier, deferred: true), to: handoff.pending)
                    }
                }
                operation?.cancel(); parent?.close(); await operation?.value
                await interface?.close(); window?.orderOut(nil); window?.contentView = nil; window?.close()
                appearance = nil
                launchedApplication = nil
                installationLock = nil
                if let inheritedDescriptor { Darwin.close(inheritedDescriptor); self.inheritedDescriptor = nil }
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }
}

@main
@MainActor
struct InstallerApplication {
    static func main() {
        signal(SIGPIPE, SIG_IGN)
        let arguments = CommandLine.arguments
        guard arguments.count == 3, ["--install", "--restart"].contains(arguments[1]) else { return }
        let application = NSApplication.shared
        let inherited = dup(STDERR_FILENO)
        guard inherited >= 0, fcntl(inherited, F_SETFD, FD_CLOEXEC) == 0 else { return }
        let diagnostics = open("/dev/null", O_WRONLY)
        guard diagnostics >= 0 else { Darwin.close(inherited); return }
        dup2(diagnostics, STDERR_FILENO); Darwin.close(diagnostics)
        let delegate = InstallerDelegate(directory: URL(fileURLWithPath: arguments[2]), restarting: arguments[1] == "--restart", inheritedDescriptor: inherited)
        application.setActivationPolicy(.accessory); application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
