import Darwin
import Foundation

struct UpdateInstallerEvent: Codable, Sendable {
    enum Kind: String, Codable, Sendable { case progress, installed, failed, restartReady = "restart-ready" }
    let kind: Kind
    var percent: Double = 0
}

/// 安装前由主应用持有子进程；助手确认接管重启后，生命周期独立于即将退出的主应用
@MainActor
final class UpdateInstallerProcess {
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var buffer = Data()
    private var exitStatus: Int32?
    private var exitWaiter: CheckedContinuation<Void, Never>?
    private var transferred = false
    private var installationLock: UpdateTargetLock?

    func install(directory: URL, lock: UpdateTargetLock, restarting: Bool = false, progress: @escaping @MainActor (Double) -> Void) async throws {
        guard process == nil else { throw UpdateError.failedProcess }
        installationLock = lock
        let process = Process(), inputPipe = Pipe(), outputPipe = Pipe()
        process.executableURL = directory.appendingPathComponent("TinkerFinUpdater")
        process.arguments = [restarting ? "--restart" : "--install", directory.path]
        process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": NSHomeDirectory()]
        process.standardInput = inputPipe; process.standardOutput = outputPipe
        process.standardError = FileHandle(fileDescriptor: lock.descriptor, closeOnDealloc: false)
        process.terminationHandler = { [weak self] child in
            let status = child.terminationStatus
            DispatchQueue.main.async {
                guard let self else { return }
                self.exitStatus = status; self.exitWaiter?.resume(); self.exitWaiter = nil
            }
        }
        self.process = process; exitStatus = nil
        var launched = false
        do {
            try await UpdateFileWork().perform { cancellation in try cancellation.check(); try process.run() }
            launched = true
            try? inputPipe.fileHandleForReading.close(); try? outputPipe.fileHandleForWriting.close()
            input = inputPipe.fileHandleForWriting; output = outputPipe.fileHandleForReading
            guard fcntl(input!.fileDescriptor, F_SETFL, O_NONBLOCK) == 0,
                  fcntl(input!.fileDescriptor, F_SETNOSIGPIPE, 1) == 0,
                  fcntl(output!.fileDescriptor, F_SETFL, O_NONBLOCK) == 0 else { throw UpdateError.failedProcess }
            while true {
                let event = try await next()
                switch event.kind {
                case .progress:
                    guard event.percent.isFinite, (0...100).contains(event.percent) else { throw UpdateError.failedProcess }
                    progress(event.percent)
                case .installed: progress(100); return
                default: throw UpdateError.failedProcess
                }
            }
        } catch {
            if !launched {
                self.process = nil; process.terminationHandler = nil
                try? inputPipe.fileHandleForReading.close(); try? inputPipe.fileHandleForWriting.close()
                try? outputPipe.fileHandleForReading.close(); try? outputPipe.fileHandleForWriting.close()
            }
            await close(); throw error
        }
    }

    func requestRestart() async throws {
        guard let input else { throw UpdateError.failedProcess }
        try await writeToPipe(Data("restart\n".utf8), descriptor: input.fileDescriptor)
        guard try await next().kind == .restartReady else { throw UpdateError.failedProcess }
        transferred = true
        try? input.close(); self.input = nil
        try? output?.close(); output = nil
    }

    private func next() async throws -> UpdateInstallerEvent {
        guard let output else { throw UpdateError.failedProcess }
        while true {
            if let newline = buffer.firstIndex(of: 10) {
                let line = buffer[..<newline]; buffer.removeSubrange(...newline)
                return try JSONDecoder().decode(UpdateInstallerEvent.self, from: line)
            }
            guard buffer.count <= 8192, let data = try await readFromPipe(output.fileDescriptor) else { throw UpdateError.failedProcess }
            buffer.append(data)
        }
    }

    func close() async {
        guard !transferred else { return }
        try? input?.close(); input = nil
        if let process, process.isRunning { kill(process.processIdentifier, SIGKILL) }
        if process != nil, exitStatus == nil { await withCheckedContinuation { exitWaiter = $0 } }
        try? output?.close(); output = nil; buffer = Data()
        process?.terminationHandler = nil; process = nil
        installationLock = nil
    }
}
