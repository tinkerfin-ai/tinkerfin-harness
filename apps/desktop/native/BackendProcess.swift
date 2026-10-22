import Darwin
import Foundation

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
    private enum MaintenanceState { case waiting, ready, cancelling, committing }
    private var maintenanceID: String?
    private var maintenanceState: MaintenanceState?
    private var preparation: CheckedContinuation<Void, Error>?
    private var commitment: CheckedContinuation<Void, Error>?
    private var maintenanceCancellations: [CheckedContinuation<Void, Error>] = []
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
                                let value = try JSONDecoder().decode(HostMessage.self, from: buffer)
                                buffer.removeAll(keepingCapacity: true)
                                guard self?.identity == generation else { return }
                                switch value {
                                case .event(let event): self?.event(event)
                                case .maintenance(let receipt): self?.receiveMaintenance(receipt)
                                }
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
        await waitForExit()
    }

    private func waitForExit() async {
        guard isRunning else { return }
        await withCheckedContinuation { stoppedWaiters.append($0) }
    }

    /// 继续传输已接受操作的回执；调用方取消时等宿主恢复接纳后再传播取消
    func prepareForUpdate() async throws {
        try Task.checkCancellation()
        guard acceptsUpdates, maintenanceID == nil else { throw ClientError.message("本机服务尚未就绪") }
        let request = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        maintenanceID = request; maintenanceState = .waiting
        do {
            try await withTaskCancellationHandler {
                try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                    preparation = continuation
                    do { try send(MaintenanceCommand(kind: .prepare, requestID: request)) }
                    catch { finishMaintenance(error) }
                }
                try Task.checkCancellation()
            } onCancel: {
                DispatchQueue.main.async { [weak self] in self?.requestMaintenanceCancellation(request) }
            }
        } catch {
            if Task.isCancelled {
                try? await cancelUpdatePreparation()
                throw CancellationError()
            }
            throw error
        }
    }

    func cancelUpdatePreparation() async throws {
        guard let request = maintenanceID else { return }
        guard maintenanceState != .committing, maintenanceCancellations.count < 2 else { throw ClientError.message("安装交接已开始") }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            maintenanceCancellations.append(continuation)
            requestMaintenanceCancellation(request)
        }
    }

    /// 就绪回执后才允许结束宿主；真正退出前，更新器不得替换运行中的应用
    func commitUpdatePreparation() async throws {
        guard let request = maintenanceID, maintenanceState == .ready else { throw ClientError.message("本机操作尚未结算") }
        maintenanceState = .committing
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            commitment = continuation
            do { try send(MaintenanceCommand(kind: .commit, requestID: request)) }
            catch { finishMaintenance(error); stop() }
        }
        await waitForExit()
    }

    private func requestMaintenanceCancellation(_ request: String) {
        guard request == maintenanceID, maintenanceState != .committing, maintenanceState != .cancelling else { return }
        maintenanceState = .cancelling
        do { try send(MaintenanceCommand(kind: .cancel, requestID: request)) }
        catch {
            // 取消消息无法交付时关闭宿主，收到退出后才解除等待，不能误报已恢复接纳
            stop()
        }
    }

    private func receiveMaintenance(_ receipt: MaintenanceReceipt) {
        guard receipt.requestID == maintenanceID else { return }
        switch receipt.state {
        case .ready:
            guard maintenanceState == .waiting else { return }
            maintenanceState = .ready
            let waiter = preparation; preparation = nil; waiter?.resume()
        case .cancelled:
            let cancelled = maintenanceCancellations; maintenanceCancellations = []
            finishMaintenance(CancellationError())
            cancelled.forEach { $0.resume() }
        case .failed: finishMaintenance(ClientError.message("本机操作未能结算，请重试"))
        case .committed:
            guard maintenanceState == .committing else { return }
            let waiter = commitment; commitment = nil; waiter?.resume()
        }
    }

    private func finishMaintenance(_ error: Error) {
        maintenanceID = nil; maintenanceState = nil
        let preparing = preparation, committing = commitment, cancelling = maintenanceCancellations
        preparation = nil; commitment = nil; maintenanceCancellations = []
        preparing?.resume(throwing: error); committing?.resume(throwing: error)
        cancelling.forEach { $0.resume(throwing: error) }
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
        finishMaintenance(ClientError.message("本机服务已结束"))
        finishing = false
        pendingExit = nil
        if status != 0 { event(.failure("连接已中断，请重试")) }
        exited()
        let waiters = stoppedWaiters
        stoppedWaiters = []
        for waiter in waiters { waiter.resume() }
    }
}
