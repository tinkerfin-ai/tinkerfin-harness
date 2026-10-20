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

private func writeToPipe(_ data: Data, descriptor: Int32) async throws {
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
private func readFromPipe(_ descriptor: Int32) async throws -> Data? {
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

/// Foundation 的进程启动和信号操作留在单个专用队列，退出回调不复用旧进程身份
private final class ProcessOwner: @unchecked Sendable {
    private let queue = DispatchQueue(label: "com.tinkerfin.companion.process")
    private let process = Process()

    func launch(resources: URL, state: URL, input: Pipe, output: Pipe, diagnostics: Pipe,
                exited: @escaping @Sendable (Int32) -> Void) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            queue.async { [process] in
                process.executableURL = resources.appendingPathComponent("python/bin/python3")
                process.arguments = ["-I", "-B", "-m", "tinkerfin_desktop", "--resources", resources.path,
                                     "--state-directory", state.path]
                process.environment = [
                    "PATH": resources.appendingPathComponent("node/bin").path + ":/usr/bin:/bin:/usr/sbin:/sbin",
                    "HOME": NSHomeDirectory(), "LANG": "en_US.UTF-8", "LC_CTYPE": "UTF-8",
                ]
                process.standardInput = input
                process.standardOutput = output
                process.standardError = diagnostics
                process.terminationHandler = { child in exited(child.terminationStatus) }
                do { try process.run(); continuation.resume() }
                catch { continuation.resume(throwing: error) }
            }
        }
    }

    func terminate() {
        queue.async { [process] in
            if process.isRunning { process.terminate() }
        }
    }
}

@MainActor
final class BackendProcess {
    private var owner: ProcessOwner?
    private var input: AsyncStream<Data>.Continuation?
    private var writer: Task<Void, Never>?
    private var reader: Task<Void, Never>?
    private var diagnostics: Task<Void, Never>?
    private var stopping: Task<Void, Never>?
    private var identity = UUID()
    private var starting = false
    private var finishing = false
    private var pendingExit: Int32?
    private var outputHandle: FileHandle?
    private var diagnosticHandle: FileHandle?
    private var inputHandle: FileHandle?
    private var stoppedWaiters: [CheckedContinuation<Void, Never>] = []
    private let event: @MainActor (HostEvent) -> Void
    private let exited: @MainActor () -> Void
    var isRunning: Bool { owner != nil }
    var acceptsUpdates: Bool { owner != nil && !starting && !finishing && stopping == nil && input != nil }

    init(event: @escaping @MainActor (HostEvent) -> Void, exited: @escaping @MainActor () -> Void) {
        self.event = event
        self.exited = exited
    }

    func start(configuration: HostConfiguration, readiness: NativeReadiness, resources: URL, state: URL) async throws {
        guard owner == nil else { throw ClientError.message("本机服务仍在运行或正在停止") }
        try Task.checkCancellation()
        let generation = UUID()
        identity = generation
        starting = true
        pendingExit = nil
        let owner = ProcessOwner()
        self.owner = owner
        let inputPipe = Pipe(), outputPipe = Pipe(), diagnosticPipe = Pipe()
        let descriptor = inputPipe.fileHandleForWriting.fileDescriptor
        let outputDescriptor = outputPipe.fileHandleForReading.fileDescriptor
        let diagnosticDescriptor = diagnosticPipe.fileHandleForReading.fileDescriptor
        guard [descriptor, outputDescriptor, diagnosticDescriptor].allSatisfy({
            fcntl($0, F_SETFL, fcntl($0, F_GETFL) | O_NONBLOCK) == 0
        }), fcntl(descriptor, F_SETNOSIGPIPE, 1) == 0 else {
            self.owner = nil
            starting = false
            throw ClientError.message("无法创建本机通信管道")
        }
        let messages = AsyncStream.makeStream(of: Data.self, bufferingPolicy: .bufferingOldest(8))
        input = messages.continuation
        inputHandle = inputPipe.fileHandleForWriting
        outputHandle = outputPipe.fileHandleForReading
        diagnosticHandle = diagnosticPipe.fileHandleForReading
        var launched = false
        do {
            try await owner.launch(resources: resources, state: state, input: inputPipe,
                                   output: outputPipe, diagnostics: diagnosticPipe) { [weak self] status in
                Task { @MainActor in await self?.childExited(generation: generation, status: status) }
            }
            launched = true
            try Task.checkCancellation()
            try inputPipe.fileHandleForReading.close()
            try outputPipe.fileHandleForWriting.close()
            try diagnosticPipe.fileHandleForWriting.close()
            writer = Task { [weak self] in
                do {
                    for await message in messages.stream {
                        try await writeToPipe(message, descriptor: descriptor)
                    }
                } catch is CancellationError { }
                catch { self?.failed(generation, "本机服务通信已中断，请重新连接") }
                try? inputPipe.fileHandleForWriting.close()
            }
            reader = Task { [weak self] in
                var buffer = Data()
                do {
                    while let bytes = try await readFromPipe(outputDescriptor) {
                        for byte in bytes {
                            if byte == 10 {
                                let value = try JSONDecoder().decode(HostEvent.self, from: buffer)
                                buffer.removeAll(keepingCapacity: true)
                                guard self?.identity == generation else { return }
                                self?.event(value)
                            } else {
                                guard buffer.count < 4 * 1024 * 1024 else { throw ClientError.message("本机消息超过容量") }
                                buffer.append(byte)
                            }
                        }
                    }
                    if !buffer.isEmpty { throw ClientError.message("本机消息不完整") }
                } catch is CancellationError { }
                catch { self?.failed(generation, "本机服务通信无效，请重新连接") }
            }
            diagnostics = Task { [weak self] in
                var count = 0
                do {
                    while let bytes = try await readFromPipe(diagnosticDescriptor) {
                        count += bytes.count
                        if count > 4 * 1024 * 1024 { throw ClientError.message("本机诊断输出超过容量") }
                    }
                } catch is CancellationError { }
                catch { self?.failed(generation, "本机服务异常，请检查应用安装后重新连接") }
            }
            try send(InitializeMessage(configuration: configuration, readiness: readiness))
            starting = false
            if let status = pendingExit { await finished(generation: generation, status: status) }
        } catch {
            starting = false
            owner.terminate()
            input?.finish()
            input = nil
            if launched {
                if let status = pendingExit { await finished(generation: generation, status: status) }
            } else {
                try? inputPipe.fileHandleForWriting.close()
                try? outputPipe.fileHandleForReading.close()
                try? diagnosticPipe.fileHandleForReading.close()
                self.owner = nil
                inputHandle = nil; outputHandle = nil; diagnosticHandle = nil
            }
            throw error
        }
    }

    func send<Value: Encodable>(_ value: Value) throws {
        guard let input else { throw ClientError.message("本机服务尚未连接") }
        var bytes = try JSONEncoder().encode(value)
        guard bytes.count < 4 * 1024 * 1024 else { throw ClientError.message("本机消息超过容量") }
        bytes.append(10)
        if case .enqueued = input.yield(bytes) { return }
        throw ClientError.message("本机服务尚未处理上一项操作，请稍后重试")
    }

    func stop() {
        guard owner != nil, stopping == nil else { return }
        try? send(["kind": "stop"])
        input?.finish()
        input = nil
        // 正常关闭通过独占管道交给宿主；超时只向仍由 Foundation 持有的进程发送 TERM
        stopping = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(5)) } catch { return }
            self?.owner?.terminate()
        }
    }

    func stopAndWait() async {
        stop()
        guard isRunning else { return }
        await withCheckedContinuation { stoppedWaiters.append($0) }
    }

    private func failed(_ generation: UUID, _ message: String) {
        guard identity == generation else { return }
        event(.failure(message))
        stop()
    }

    private func childExited(generation: UUID, status: Int32) async {
        guard identity == generation else { return }
        if starting { pendingExit = status; return }
        await finished(generation: generation, status: status)
    }

    private func finished(generation: UUID, status: Int32) async {
        guard identity == generation, !finishing else { return }
        finishing = true
        input?.finish()
        input = nil
        writer?.cancel()
        stopping?.cancel()
        stopping = nil
        // 子进程已退出，先结算就绪通知与管道内最后的安全状态，再归还句柄
        await writer?.value
        try? inputHandle?.close()
        await reader?.value
        await diagnostics?.value
        try? outputHandle?.close()
        try? diagnosticHandle?.close()
        writer = nil; reader = nil; diagnostics = nil
        inputHandle = nil; outputHandle = nil; diagnosticHandle = nil
        owner = nil
        finishing = false
        pendingExit = nil
        if status != 0 { event(.failure("连接已中断，请重试")) }
        exited()
        let waiters = stoppedWaiters
        stoppedWaiters = []
        for waiter in waiters { waiter.resume() }
    }
}
