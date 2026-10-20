import AppKit
import ApplicationServices
import Combine
import CoreGraphics
import CryptoKit
import Foundation

/// 命令只解析为 argv，不执行 shell 展开；带空格的路径通过引号保留
func parseMCPCommand(_ value: String) throws -> [String] {
    guard value.count <= 16384, !value.contains("\0") else { throw ClientError.message("MCP 命令超过容量") }
    var arguments: [String] = [], word = "", quote: Character?, escaped = false, started = false
    for character in value {
        if escaped { word.append(character); escaped = false; started = true; continue }
        if character == "\\", quote != "'" { escaped = true; started = true; continue }
        if let delimiter = quote {
            if character == delimiter { quote = nil } else { word.append(character) }
            continue
        }
        if character == "'" || character == "\"" { quote = character; started = true }
        else if character.isWhitespace {
            if started { arguments.append(word); word = ""; started = false }
        } else { word.append(character); started = true }
    }
    guard quote == nil, !escaped else { throw ClientError.message("请补全 MCP 命令中的引号或转义") }
    if started { arguments.append(word) }
    guard arguments.count <= 64, arguments.allSatisfy({ !$0.isEmpty && $0.count <= 4096 }) else {
        throw ClientError.message("请检查 MCP 程序与参数")
    }
    return arguments
}

func displayMCPCommand(_ arguments: [String]) -> String {
    arguments.map { "'" + $0.replacingOccurrences(of: "'", with: "'\\''") + "'" }.joined(separator: " ")
}

@MainActor
final class CompanionModel: ObservableObject {
    let localization: ClientLocalization
    @Published private(set) var saved = SavedConfiguration()
    @Published private(set) var status = "loading"
    @Published private(set) var busy = false
    @Published private(set) var retrying = false
    @Published private(set) var authorizationWaiting = false
    @Published private(set) var accountName = ""
    @Published private(set) var projects: [ManagedProject] = []
    @Published private(set) var projectsError = false
    @Published private(set) var error: ClientError?
    @Published private(set) var accessibility = false
    @Published private(set) var screenCapture = false
    var attention: (() -> Void)?

    private let stateSelection: URL
    private let resources: URL
    private let keychain = KeychainStore()
    private let workspaceAccess = WorkspaceAccess()
    private let desktop = DesktopAccess()
    private let client = AuthorizationClient()
    var configuration: ConnectionConfiguration? { saved.connection }
    private var store: ConfigurationStore?
    private var lifetimeLock: ApplicationLock?
    private var operation: Task<Void, Error>?
    private var authorizationTask: Task<Void, Never>?
    private var changingIdentity = false
    private var openProjectOperation: Task<Void, Never>?
    private var projectOperation: Task<Void, Never>?
    private var desktopOperation: Task<Void, Never>?
    private var activeDesktop: DesktopRequest?
    private var reportedReadiness: NativeReadiness?
    private var shuttingDown = false
    private lazy var backend = BackendProcess(event: { [weak self] in self?.receive($0) }, exited: { [weak self] in self?.backendExited() })

    init(state: URL, resources: URL) {
        stateSelection = state; self.resources = resources
        localization = ClientLocalization(resources: resources)
    }
    var ready: Bool { store != nil }
    var connection: String {
        if saved.pending?.authorization != nil { return "authorizing" }
        switch status {
        case "online": return "online"
        case "loading", "connecting": return status
        case "unauthorized": return "revoked"
        default: return saved.device == nil ? "unpaired" : "offline"
        }
    }
    var statusText: String {
        switch connection {
        case "online": return "已连接"
        case "authorizing": return "等待授权"
        case "loading": return "正在启动"
        case "connecting": return "正在连接"
        case "revoked": return "连接已失效"
        default: return "未连接"
        }
    }

    /// 所有配置写入按本机操作顺序结算，后台授权完成也走同一边界
    func run(reportError: Bool = true, _ work: @escaping @MainActor () async throws -> Void) async throws {
        while let previous = operation { _ = try? await previous.value }
        guard !shuttingDown else { throw CancellationError() }
        busy = true; error = nil
        let task = Task { @MainActor in
            defer { busy = false; operation = nil }
            do { try Task.checkCancellation(); try await work() }
            catch {
                if reportError, !(error is CancellationError), !shuttingDown { self.error = userError(error); if ["loading", "connecting"].contains(status) { status = "failed" } }
                throw error
            }
        }
        operation = task
        try await task.value
    }

    func restore() async throws {
        try await run {
            let (lock, directory) = try await self.workspaceAccess.lockState(self.stateSelection)
            self.lifetimeLock = lock
            let store = ConfigurationStore(directory: directory)
            var saved = try await store.read()
            saved.connection = try saved.connection?.checked()
            if let pending = saved.pending {
                guard try pending.connection.checked() == pending.connection else {
                    throw ClientError.message("本机授权记录无效，请检查应用配置")
                }
            }
            guard saved.device == nil || saved.connection != nil else {
                throw ClientError.message("本机连接配置不完整，请先转换应用配置")
            }
            guard saved.retiredAccounts.count <= 64,
                  (saved.keychainAccount.map { UUID(uuidString: $0) != nil } ?? true),
                  saved.retiredAccounts.allSatisfy({ UUID(uuidString: $0) != nil }),
                  saved.pending.map({ UUID(uuidString: $0.account) != nil }) ?? true else {
                throw ClientError.message("本机授权记录无效，请检查应用配置")
            }
            _ = try checkedDomains(saved.permissions.allowedDomains)
            if saved.name.isEmpty { saved.name = Host.current().localizedName ?? self.localization.text("我的 Mac") }
            _ = try await self.workspaceAccess.prepareProjects(directory.appendingPathComponent("projects", isDirectory: true))
            try await store.save(saved)
            self.store = store; self.saved = saved
            self.checkPermissions()
            try await self.cleanRetiredAccounts()
            if let pending = self.saved.pending, pending.authorization == nil {
                try await self.discardPending()
            }
            self.status = self.saved.device == nil ? "unpaired" : "offline"
            if self.saved.pending == nil, self.saved.device != nil { try await self.connectNow() }
        }
        resumeAuthorization()
    }

    private func connectNow() async throws {
        guard let store, let configuration, let device = saved.device, let account = saved.keychainAccount else {
            status = "unpaired"; return
        }
        if backend.isRunning { return }
        let secret: String
        do { secret = try await keychain.read(account: account) }
        catch { status = "unauthorized"; throw error }
        if device.name != saved.name {
            var next = saved
            next.device = try await client.rename(server: checkedServer(configuration.apiURL), deviceID: device.deviceID, credential: secret, name: saved.name)
            try await store.save(next); saved = next
        }
        do {
            try Task.checkCancellation(); status = "connecting"
            let readiness = checkPermissions()
            try await backend.start(configuration: HostConfiguration(server: configuration.apiURL, deviceID: device.deviceID,
                namespace: device.namespace, credential: secret,
                workspaceRoot: store.directory.appendingPathComponent("projects", isDirectory: true).path,
                permissions: saved.permissions), readiness: readiness, resources: resources,
                state: store.directory.appendingPathComponent("state", isDirectory: true))
            // 初始化必须先于状态更新；随后补报进程启动期间发生的权限变化
            reportedReadiness = readiness
            checkPermissions()
        } catch { status = "offline"; throw error }
    }

    func retry() async throws {
        guard !retrying else { return }
        do {
            try await run {
                self.cancelDesktop(message: "连接正在重试，请核验已开始操作的结果")
                await self.backend.stopAndWait()
                // 旧进程已结算；新一轮等待真实连接结果，不能在进程刚启动时结束反馈
                self.retrying = true
                try await self.connectNow()
                if !self.backend.isRunning { self.retrying = false }
            }
        } catch { retrying = false; throw error }
    }

    func startAuthorization(to target: ConnectionConfiguration? = nil) async throws {
        if let pending = saved.pending, pending.authorization != nil {
            guard target == nil || pending.connection == target else {
                throw ClientError.message("请先取消当前授权")
            }
            try reopenAuthorization(); return
        }
        authorizationWaiting = true
        do { try await run {
            guard let store = self.store, let selected = target ?? self.configuration else { throw ClientError.message("请填写 Studio 地址") }
            let configuration = try selected.checked()
            if self.saved.pending != nil { try await self.discardPending() }
            let account = UUID().uuidString.lowercased()
            var next = self.saved
            next.pending = PendingConnection(account: account, connection: configuration, authorization: nil)
            try await store.save(next); self.saved = next
            let credential = try await self.keychain.create(account: account)
            let created = try await self.client.start(server: checkedServer(configuration.apiURL), name: next.name, credential: credential)
            next.pending?.authorization = created
            try await store.save(next); self.saved = next
            try self.openAuthorizationBrowser()
        } } catch { authorizationWaiting = false; throw error }
        resumeAuthorization()
    }

    func reopenAuthorization() throws { try openAuthorizationBrowser(); resumeAuthorization() }
    private func openAuthorizationBrowser() throws {
        guard let pending = saved.pending, let authorization = pending.authorization else { throw ClientError.message("请重新连接") }
        guard NSWorkspace.shared.open(try authorization.browserURL(configuration: pending.connection)) else {
            throw ClientError.message("浏览器未能打开，请重试")
        }
    }

    func resumeAuthorization() {
        guard authorizationTask == nil, let pending = saved.pending, let authorization = pending.authorization, !shuttingDown else { return }
        authorizationWaiting = true
        authorizationTask = Task {
            defer { authorizationTask = nil; authorizationWaiting = false }
            do {
                let secret = try await keychain.read(account: pending.account)
                while !Task.isCancelled, saved.pending?.account == pending.account {
                    let result = try await client.status(server: checkedServer(pending.connection.apiURL), authorizationID: authorization.authorizationID, credential: secret)
                    if result.state != .waiting {
                        try await run { try await self.finishAuthorization(result, pending: pending) }
                        return
                    }
                    try await Task.sleep(for: .seconds(2))
                }
            } catch is CancellationError { }
            catch ClientError.authorizationExpired {
                do { try await run { try await self.endPending(pending, notice: "授权已过期，请重新连接") } }
                catch { self.error = userError(error) }
            }
            catch ClientError.credentialUnavailable {
                do { try await run { try await self.endPending(pending, notice: "授权凭证不可用，请重新连接") } }
                catch { self.error = userError(error) }
            }
            catch { if saved.pending?.account == pending.account, !shuttingDown { self.error = userError(error) } }
        }
    }

    func cancelAuthorization() async throws {
        authorizationTask?.cancel(); await authorizationTask?.value
        try await run {
            guard let pending = self.saved.pending else { return }
            guard let authorization = pending.authorization else { try await self.endPending(pending); return }
            do {
                let credential = try await self.keychain.read(account: pending.account)
                let result = try await self.client.status(server: checkedServer(pending.connection.apiURL), authorizationID: authorization.authorizationID, credential: credential, cancel: true)
                try await self.finishAuthorization(result, pending: pending)
            } catch ClientError.authorizationExpired { try await self.endPending(pending) }
            catch ClientError.credentialUnavailable { try await self.endPending(pending) }
        }
    }

    private func finishAuthorization(_ result: AuthorizationResult, pending: PendingConnection) async throws {
        guard saved.pending?.account == pending.account, let store else { return }
        if result.state == .authorized, let device = result.device {
            changingIdentity = true
            defer { changingIdentity = false; refreshProjects() }
            projectOperation?.cancel(); openProjectOperation?.cancel()
            await projectOperation?.value; await openProjectOperation?.value
            projects = []; accountName = ""; projectsError = false
            cancelDesktop(message: "设备连接已更新，请重新读取窗口")
            await backend.stopAndWait()
            var next = saved
            if let previous = next.keychainAccount, previous != pending.account { next.retiredAccounts.append(previous) }
            next.acceptAuthorization(device, at: pending.connection)
            next.keychainAccount = pending.account; next.pending = nil
            try await store.save(next); saved = next
            try await cleanRetiredAccounts()
            try await connectNow()
        } else if result.state == .superseded {
            try await endPending(pending, notice: "授权已被替换，请重新连接")
        } else {
            try await endPending(pending, notice: result.state == .expired ? "授权已过期，请重新连接" : nil)
        }
    }

    /// 结束的只是本次待授权记录；迟到回执不能清除新的请求或现有连接
    private func endPending(_ pending: PendingConnection, notice: String? = nil) async throws {
        guard saved.pending?.account == pending.account else { return }
        try await discardPending()
        if saved.device == nil { status = "unpaired" }
        else { try await connectNow() }
        error = notice.map(ClientError.message)
    }

    private func discardPending() async throws {
        guard let store, let pending = saved.pending else { return }
        var next = saved; next.pending = nil; next.retiredAccounts.append(pending.account)
        try await store.save(next); saved = next
        try await cleanRetiredAccounts()
    }
    private func cleanRetiredAccounts() async throws {
        guard let store, !saved.retiredAccounts.isEmpty else { return }
        for account in saved.retiredAccounts {
            guard account != saved.keychainAccount, account != saved.pending?.account else { throw ClientError.message("设备凭证清理记录无效") }
            try await keychain.remove(account: account)
        }
        var next = saved; next.retiredAccounts = []
        try await store.save(next); saved = next
    }

    func saveName(_ value: String) async throws {
        try await run(reportError: false) {
            guard let store = self.store else { return }
            let name = value.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !name.isEmpty, name.count <= 128 else { throw ClientError.message("请输入设备名称") }
            var next = self.saved
            if let device = next.device, let account = next.keychainAccount {
                guard let configuration = next.connection else { throw ClientError.message("请重新连接后保存设备名称") }
                next.device = try await self.client.rename(server: checkedServer(configuration.apiURL), deviceID: device.deviceID,
                    credential: self.keychain.read(account: account), name: name)
            }
            next.name = name; try await store.save(next); self.saved = next
        }
    }

    var projectRoot: URL { (store?.directory ?? stateSelection).appendingPathComponent("projects", isDirectory: true) }
    func openConfiguration() {
        guard let store else { return }
        NSWorkspace.shared.open(store.directory.appendingPathComponent("settings.json"))
    }
    func refreshProjects() {
        guard !shuttingDown, !changingIdentity, projectOperation == nil, let device = saved.device, let account = saved.keychainAccount, let server = saved.connection?.apiURL, store != nil else { return }
        projectOperation = Task {
            defer { projectOperation = nil }
            do {
                let secret = try await keychain.read(account: account)
                try Task.checkCancellation()
                let values = try await client.projects(server: checkedServer(server), token: device.deviceID + "." + secret)
                try Task.checkCancellation()
                guard saved.device?.deviceID == device.deviceID, saved.connection?.apiURL == server else { return }
                accountName = values.0; projects = values.1; projectsError = false
            } catch is CancellationError { }
            catch { if saved.device?.deviceID == device.deviceID { projectsError = true } }
        }
    }
    func openProject(_ id: String) async throws {
        guard !shuttingDown, !changingIdentity, let device = saved.device, let account = saved.keychainAccount, let server = saved.connection?.apiURL else { throw ClientError.message("请先连接 Studio") }
        let secret = try await keychain.read(account: account)
        try Task.checkCancellation()
        _ = try await client.project(id, server: checkedServer(server), token: device.deviceID + "." + secret)
        guard !shuttingDown, !changingIdentity, saved.device?.deviceID == device.deviceID, saved.connection?.apiURL == server else { throw ClientError.message("连接已变化") }
        let address = SHA256.hash(data: Data(server.utf8)).map { String(format: "%02x", $0) }.joined()
        let selected = try await workspaceAccess.prepareProjects(projectRoot, components: [address, device.namespace, id])
        try Task.checkCancellation()
        guard !shuttingDown, !changingIdentity, saved.device?.deviceID == device.deviceID, saved.connection?.apiURL == server else { throw ClientError.message("连接已变化") }
        NSWorkspace.shared.open(selected)
    }
    func revealProject(_ id: String) {
        guard !shuttingDown, !changingIdentity, openProjectOperation == nil else { return }
        openProjectOperation = Task {
            defer { openProjectOperation = nil }
            do { try await openProject(id) }
            catch is CancellationError { }
            catch { self.error = userError(error); attention?() }
        }
    }
    func openProjectURL(_ url: URL) async throws {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false), parts.host == "workspace",
              let device = saved.device else { throw ClientError.message("请从 Studio 重新打开此项目") }
        let values = Dictionary(parts.queryItems?.map { ($0.name, $0.value ?? "") } ?? [], uniquingKeysWith: { first, _ in first })
        guard values["server"] == saved.connection?.apiURL, values["namespace"] == device.namespace,
              values["device"] == device.deviceID, let id = values["project"] else { throw ClientError.message("请切换到此项目所属的账号") }
        try await openProject(id)
    }
    @discardableResult
    func checkPermissions() -> NativeReadiness {
        // 这里只读取系统状态；窗口激活和设置返回不能再次申请系统授权
        let readiness = NativeReadiness(accessibility: AXIsProcessTrusted(), screenCapture: CGPreflightScreenCaptureAccess())
        let revoked = accessibility && !readiness.accessibility || screenCapture && !readiness.screenCapture
        if accessibility != readiness.accessibility { accessibility = readiness.accessibility }
        if screenCapture != readiness.screenCapture { screenCapture = readiness.screenCapture }
        if let activeDesktop, let message = readiness.missingPermission(for: activeDesktop.command) {
            cancelDesktop(message: message)
        }
        if revoked { desktop.invalidateTargets() }
        if backend.acceptsUpdates, reportedReadiness != readiness {
            do {
                try backend.send(NativeReadinessMessage(readiness: readiness))
                reportedReadiness = readiness
            } catch { self.error = .message("本机状态同步未完成，请重新连接") }
        }
        return readiness
    }
    func openStudio() throws {
        guard let configuration else { throw ClientError.message("连接配置不可用") }
        NSWorkspace.shared.open(try checkedServer(configuration.studioURL))
    }
    func openPermissionSettings(_ screen: Bool) {
        let anchor = screen ? "Privacy_ScreenCapture" : "Privacy_Accessibility"
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(anchor)") { NSWorkspace.shared.open(url) }
    }

    private func receive(_ event: HostEvent) {
        guard !shuttingDown else { return }
        switch event {
        case .state(let state):
            guard ["connecting", "online", "offline", "unauthorized", "stopped"].contains(state) else { error = .message("连接状态无效，请重试"); backend.stop(); return }
            status = state
            if state != "connecting" { retrying = false }
            if state == "online" { checkPermissions(); refreshProjects() }
            if state == "unauthorized" {
                cancelDesktop(message: "连接已失效")
                if saved.pending == nil { attention?() }
            }
        case .failure(let message): error = .message(String(message.prefix(512))); status = "failed"; retrying = false
        case .desktopCancel(let id): if activeDesktop?.id == id { cancelDesktop(message: nil) }
        case .desktopRequest(let request): executeDesktop(request)
        }
    }
    private func executeDesktop(_ request: DesktopRequest) {
        guard configuration != nil, saved.device != nil, !changingIdentity, status != "unauthorized" else { reply(NativeDesktopReply(requestID: request.id, error: "设备授权不可用")); return }
        guard desktopOperation == nil else { if activeDesktop?.id != request.id { reply(NativeDesktopReply(requestID: request.id, error: "另一项本机操作尚未结束")) }; return }
        if request.command.action == "open_url" {
            do {
                guard let value = request.command.url else { throw ClientError.message("缺少网页地址") }
                let url = try checkedWebsiteURL(value)
                guard NSWorkspace.shared.open(url) else { throw ClientError.message("未能打开默认浏览器，请检查浏览器设置") }
                reply(NativeDesktopReply(requestID: request.id, result: DesktopResult(accepted: true)))
            } catch { reply(NativeDesktopReply(requestID: request.id, error: userMessage(error))) }
            return
        }
        guard saved.permissions.desktop else { reply(NativeDesktopReply(requestID: request.id, error: "本机未开启桌面操作")); return }
        do {
            if request.command.action == "apps" { reply(try desktop.list(request)); return }
            let readiness = checkPermissions()
            if let message = readiness.missingPermission(for: request.command) { throw ClientError.message(message) }
            _ = try desktop.review(request)
            activeDesktop = request
            desktopOperation = Task {
                defer { if activeDesktop?.id == request.id { activeDesktop = nil }; desktopOperation = nil }
                do { let result = try await desktop.perform(request); if activeDesktop?.id == request.id { reply(result) } }
                catch is CancellationError { }
                catch { if activeDesktop?.id == request.id { reply(NativeDesktopReply(requestID: request.id, error: userMessage(error))) } }
            }
        } catch { reply(NativeDesktopReply(requestID: request.id, error: userMessage(error))) }
    }
    private func cancelDesktop(message: String?) {
        if let request = activeDesktop { desktop.cancel(request.id); if let message { reply(NativeDesktopReply(requestID: request.id, error: message)) } }
        activeDesktop = nil; desktopOperation?.cancel()
    }
    private func reply(_ message: NativeDesktopReply) { do { try backend.send(message) } catch { self.error = .message("本机通信已中断，请重试"); backend.stop() } }
    private func backendExited() { reportedReadiness = nil; retrying = false; cancelDesktop(message: nil); if status != "unauthorized" { status = saved.device == nil ? "unpaired" : "offline" } }
    func userMessage(_ error: Error) -> String {
        userError(error).errorDescription ?? "操作尚未完成，请重试"
    }
    func userError(_ error: Error) -> ClientError {
        NSLog("TinkerFin 操作未完成：%@", String(describing: error))
        if error is URLError { return .message("无法连接到 Studio。请检查地址后重试。") }
        return (error as? ClientError) ?? .message("操作尚未完成，请重试")
    }
    func shutdown() async {
        shuttingDown = true
        openProjectOperation?.cancel(); projectOperation?.cancel(); authorizationTask?.cancel(); operation?.cancel(); desktop.cancelAll(); cancelDesktop(message: nil); backend.stop()
        await openProjectOperation?.value; await projectOperation?.value; await authorizationTask?.value; _ = try? await operation?.value; await desktopOperation?.value; await backend.stopAndWait()
        await keychain.clear()
        lifetimeLock = nil
    }
}
