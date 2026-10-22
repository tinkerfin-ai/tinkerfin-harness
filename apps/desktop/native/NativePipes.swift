import Darwin
import Foundation

/// 每次管道就绪等待独占通知源，取消源完成后才恢复调用方，防止句柄复用
private final class PipeReadyWait: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Error>?
    private var source: (any DispatchSourceProtocol)?
    private var cancelled = false

    func install(_ descriptor: Int32, reading: Bool, _ continuation: CheckedContinuation<Void, Error>) {
        lock.lock()
        if cancelled {
            lock.unlock()
            continuation.resume(throwing: CancellationError())
            return
        }
        self.continuation = continuation
        let source: any DispatchSourceProtocol
        if reading { source = DispatchSource.makeReadSource(fileDescriptor: descriptor, queue: .global(qos: .userInitiated)) }
        else { source = DispatchSource.makeWriteSource(fileDescriptor: descriptor, queue: .global(qos: .userInitiated)) }
        self.source = source
        source.setEventHandler { [weak self] in self?.finish(cancelled: false) }
        source.resume()
        lock.unlock()
    }

    func finish(cancelled: Bool) {
        lock.lock()
        self.cancelled = self.cancelled || cancelled
        let continuation = self.continuation
        let source = self.source
        self.continuation = nil
        self.source = nil
        lock.unlock()
        guard let continuation else { return }
        let complete: @Sendable () -> Void = {
            if cancelled { continuation.resume(throwing: CancellationError()) }
            else { continuation.resume() }
        }
        if let source {
            source.setCancelHandler(handler: complete)
            source.cancel()
        } else { complete() }
    }
}

func writeToPipe(_ data: Data, descriptor: Int32) async throws {
    var offset = 0
    while offset < data.count {
        try Task.checkCancellation()
        let count = data.withUnsafeBytes { bytes in
            Darwin.write(descriptor, bytes.baseAddress!.advanced(by: offset), min(65536, data.count - offset))
        }
        if count > 0 { offset += count; continue }
        if count < 0 && errno == EINTR { continue }
        guard count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) else {
            throw ClientError.message("本机服务通信已断开")
        }
        let wait = PipeReadyWait()
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { wait.install(descriptor, reading: false, $0) }
        } onCancel: { wait.finish(cancelled: true) }
    }
}

/// 空诊断管道不能阻塞状态读取；仅在各自句柄就绪后读取一个有界块
func readFromPipe(_ descriptor: Int32) async throws -> Data? {
    var bytes = Data(count: 65536)
    while true {
        try Task.checkCancellation()
        let count = bytes.withUnsafeMutableBytes { Darwin.read(descriptor, $0.baseAddress!, $0.count) }
        if count > 0 { return bytes.prefix(count) }
        if count == 0 { return nil }
        if errno == EINTR { continue }
        guard errno == EAGAIN || errno == EWOULDBLOCK else {
            throw ClientError.message("本机服务通信已断开")
        }
        let wait = PipeReadyWait()
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { wait.install(descriptor, reading: true, $0) }
        } onCancel: { wait.finish(cancelled: true) }
    }
}
