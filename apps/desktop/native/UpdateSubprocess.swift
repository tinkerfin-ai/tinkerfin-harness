import Foundation

/// 只执行随包解压程序与系统校验工具；取消或超时终止本进程，并等待两条输出管道关闭
@MainActor
final class UpdateSubprocess {
    private var process: Process?
    private var exit: CheckedContinuation<Int32, Never>?
    private var result: Int32?

    func run(executable: URL, arguments: [String]) async throws {
        try Task.checkCancellation()
        guard process == nil else { throw UpdateError.failedProcess }
        result = nil
        let process = Process(), output = Pipe(), diagnostics = Pipe()
        process.executableURL = executable; process.arguments = arguments
        process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": NSHomeDirectory(), "PYTHONDONTWRITEBYTECODE": "1"]
        process.standardInput = FileHandle.nullDevice; process.standardOutput = output; process.standardError = diagnostics
        process.terminationHandler = { [weak self] child in
            let status = child.terminationStatus
            DispatchQueue.main.async {
                guard let self else { return }
                self.result = status; self.exit?.resume(returning: status); self.exit = nil
            }
        }
        self.process = process
        do { try await UpdateFileWork().perform { cancellation in try cancellation.check(); try process.run() } }
        catch {
            self.process = nil; process.terminationHandler = nil
            try? output.fileHandleForWriting.close(); try? diagnostics.fileHandleForWriting.close()
            try? output.fileHandleForReading.close(); try? diagnostics.fileHandleForReading.close()
            throw error
        }
        defer {
            self.process = nil; process.terminationHandler = nil
            try? output.fileHandleForReading.close(); try? diagnostics.fileHandleForReading.close()
        }
        try? output.fileHandleForWriting.close(); try? diagnostics.fileHandleForWriting.close()
        let outputWork = UpdateFileWork(), errorWork = UpdateFileWork()
        // 已启动进程的管道必须结算；这两个被等待的任务不继承调用方取消
        let stdout = Task { try await outputWork.perform { _ in Self.drain(output.fileHandleForReading) } }
        let stderr = Task { try await errorWork.perform { _ in Self.drain(diagnostics.fileHandleForReading) } }
        let deadline = DispatchWorkItem { [weak self] in self?.stop() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 300, execute: deadline)
        let status = await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                if let result { continuation.resume(returning: result) } else { exit = continuation }
            }
        } onCancel: { DispatchQueue.main.async { [weak self] in self?.stop() } }
        deadline.cancel()
        let out = try await stdout.value, err = try await stderr.value
        try Task.checkCancellation()
        guard status == 0, out, err else { throw UpdateError.failedProcess }
    }

    private func stop() { if let process, process.isRunning { kill(process.processIdentifier, SIGKILL) } }

    nonisolated private static func drain(_ handle: FileHandle) -> Bool {
        defer { try? handle.close() }
        var count = 0
        do {
            while let data = try handle.read(upToCount: 64 * 1024), !data.isEmpty { count += data.count }
            return count <= 1024 * 1024
        } catch { return false }
    }
}
