import AppKit
import ApplicationServices
import Combine
import CoreGraphics
import CryptoKit
import Foundation

@MainActor
final class CompanionModel: ObservableObject {
    let localization: ClientLocalization
    @Published private(set) var saved = SavedConfiguration()
    @Published private(set) var status = "loading"
    @Published private(set) var busy = false
    @Published private(set) var retrying = false
    @Published private(set) var authorizationWaiting = false
    @Published private(set) var authorizationNotice = ""
    @Published private(set) var connectionReason: DesktopConnectionReason = .restore
    @Published private(set) var accountName = ""
    @Published private(set) var projects: [ManagedProject] = []
    @Published private(set) var projectsError = false
    @Published private(set) var projectsLoading = false
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
    private var preparingUpdate = false
    private var updateSession = false
    private var committingAuthorization = false
    private lazy var backend = BackendProcess(event: { [weak self] in self?.receive($0) }, exited: { [weak self] in self?.backendExited() })

    init(state: URL, resources: URL) {
        stateSelection = state; self.resources = resources
        localization = ClientLocalization(resources: resources)
    }
    var ready: Bool { store != nil }
    var connection: String {
        if !ready, status == "failed" { return "offline" }
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

    /// 保存与授权提交独立结算；启动恢复可由调用者取消，但不能取消排在前面的操作
    func run(reportError: Bool = true, cancelWithCaller: Bool = false, _ work: @escaping @MainActor () async throws -> Void) async throws {
        while let previous = operation { _ = try? await previous.value }
        if cancelWithCaller { try Task.checkCancellation() }
        guard !shuttingDown else { throw CancellationError() }
        busy = true; error = nil
        let task = Task { @MainActor in
            defer { busy = false; operation = nil }
            do { try Task.checkCancellation(); try await work() }
            catch {
                if cancelWithCaller, Task.isCancelled { throw CancellationError() }
                if reportError, !(error is CancellationError), !shuttingDown { self.error = userError(error); if ["loading", "connecting"].contains(status) { status = "failed" } }
                throw error
            }
        }
        operation = task
        if cancelWithCaller {
            try await withTaskCancellationHandler { try await task.value } onCancel: { task.cancel() }
            try Task.checkCancellation()
        } else { try await task.value }
    }

    func restore(connect: Bool = true) async throws {
        try await run(cancelWithCaller: true) {
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
            if connect {
                try await self.cleanRetiredAccounts()
                if let pending = self.saved.pending, pending.authorization == nil { try await self.discardPending() }
            }
            self.status = self.saved.device == nil ? "unpaired" : "offline"
            if connect, self.saved.pending == nil, self.saved.device != nil { try await self.connectNow() }
        }
        if connect { resumeAuthorization() }
    }

    /// 启动恢复先取得状态目录独占权，更新事务恢复完成后才重新接收服务端工作
    func connectRestoredState() async throws {
        try await run(cancelWithCaller: true) {
            try await self.cleanRetiredAccounts()
            if let pending = self.saved.pending, pending.authorization == nil { try await self.discardPending() }
            if self.saved.pending == nil, self.saved.device != nil { try await self.connectNow() }
        }
        resumeAuthorization()
    }

    private func connectNow(reason: DesktopConnectionReason = .restore) async throws {
        guard let store, let configuration, let device = saved.device, let account = saved.keychainAccount else {
            status = "unpaired"; return
        }
        if backend.isRunning { return }
        connectionReason = reason
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
        if !ready { lifetimeLock = nil; try await restore(); return }
        do {
            try await run {
                self.cancelDesktop(message: "连接正在重试，请核验已开始操作的结果")
                await self.backend.stopAndWait()
                // 旧进程已结算；新一轮等待真实连接结果，不能在进程刚启动时结束反馈
                self.retrying = true
                try await self.connectNow(reason: .retry)
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
        authorizationWaiting = true; authorizationNotice = ""
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
        guard authorizationTask == nil, let pending = saved.pending, let authorization = pending.authorization, !shuttingDown, !updateSession else { return }
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
        guard !updateSession, !shuttingDown, saved.pending?.account == pending.account, let store else { return }
        if result.state == .authorized, let device = result.device {
            changingIdentity = true
            defer { changingIdentity = false; refreshProjects() }
            projectOperation?.cancel(); openProjectOperation?.cancel()
            await projectOperation?.value; await openProjectOperation?.value
            cancelDesktop(message: "设备连接已更新，请重新读取窗口")
            await backend.stopAndWait()
            // 更新可在停止旧宿主时接管；提交配置后才算新身份，不能在中途接纳更新
            guard !updateSession, !shuttingDown else { return }
            committingAuthorization = true
            defer { committingAuthorization = false }
            var next = saved
            if let previous = next.keychainAccount, previous != pending.account { next.retiredAccounts.append(previous) }
            next.acceptAuthorization(device, at: pending.connection)
            next.keychainAccount = pending.account; next.pending = nil
            try await store.save(next); saved = next
            projects = []; accountName = ""; projectsError = false
            try await cleanRetiredAccounts()
            try await connectNow(reason: .authorization)
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
        authorizationNotice = notice == nil ? "" : "授权已失效，请重新连接"
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

    /// 退出登录撤销本设备及尚未交接的授权；网页账号与本机偏好保持独立
    func revokeAuthorization() async throws {
        authorizationTask?.cancel(); await authorizationTask?.value
        try await run(reportError: false) {
            try await self.abandonPendingAuthorization()
            guard let store = self.store, let configuration = self.configuration,
                  let device = self.saved.device, let account = self.saved.keychainAccount else {
                throw ClientError.message("请重新连接")
            }
            let credential = try await self.keychain.read(account: account)
            try await self.client.revoke(server: checkedServer(configuration.apiURL), deviceID: device.deviceID, credential: credential)
            self.changingIdentity = true
            defer { self.changingIdentity = false }
            self.projectOperation?.cancel(); self.openProjectOperation?.cancel()
            await self.projectOperation?.value; await self.openProjectOperation?.value
            self.cancelDesktop(message: "设备授权已撤销")
            await self.backend.stopAndWait()
            var next = self.saved
            next.device = nil; next.keychainAccount = nil; next.retiredAccounts.append(account)
            try await store.save(next); self.saved = next
            self.projects = []; self.accountName = ""; self.projectsError = false
            self.authorizationNotice = ""; self.status = "unpaired"
            try await self.cleanRetiredAccounts()
        }
    }

    /// 更新或退出不能把未完成交接的网页批准变成新的本机身份
    private func abandonPendingAuthorization() async throws {
        guard let pending = saved.pending else { return }
        if let authorization = pending.authorization {
            do {
                let credential = try await keychain.read(account: pending.account)
                let result = try await client.status(server: checkedServer(pending.connection.apiURL),
                    authorizationID: authorization.authorizationID, credential: credential, cancel: true)
                if result.state == .authorized, let device = result.device {
                    try await client.revoke(server: checkedServer(pending.connection.apiURL), deviceID: device.deviceID, credential: credential)
                }
            } catch ClientError.authorizationExpired {
                // 已过期申请不再可能授予本机身份
            } catch ClientError.credentialUnavailable {
                // 本机已无密钥，清除未提交记录，不能在下次启动重新接纳
            }
        }
        try await discardPending()
    }

    var updateDirectory: URL { (store?.directory ?? stateSelection).appendingPathComponent("state/updates", isDirectory: true) }
    var stateDirectory: URL { store?.directory ?? stateSelection }

    var canBeginUpdate: Bool { !updateSession && !committingAuthorization && !shuttingDown }

    /// 从检查更新开始冻结授权交接；已进入不可分割保存的身份必须先完成
    func beginUpdateSession() throws {
        guard canBeginUpdate else { throw ClientError.message("请稍后重试") }
        updateSession = true
    }

    /// 安装准备只暂停授权交接；已接受的原生工具仍可完成并向工作端提交回执
    func prepareForUpdate() async throws {
        guard !preparingUpdate, !committingAuthorization, !shuttingDown else { throw ClientError.message("更新准备已开始") }
        updateSession = true
        preparingUpdate = true
        do {
            authorizationTask?.cancel(); await authorizationTask?.value
            try await run(reportError: false) { try await self.abandonPendingAuthorization() }
            await projectOperation?.value; await openProjectOperation?.value
            if backend.isRunning { try await backend.prepareForUpdate() }
            await desktopOperation?.value
        } catch { preparingUpdate = false; throw error }
    }

    func commitUpdatePreparation() async throws {
        guard preparingUpdate else { throw ClientError.message("本机操作尚未结算") }
        if backend.isRunning { try await backend.commitUpdatePreparation() }
    }

    /// 安装前保留当前账户，候选身份创建的账户由更新记录负责结算
    func prepareUpdateCredential(handoff: UpdateHandoff) async throws {
        guard preparingUpdate, let account = saved.keychainAccount else { throw ClientError.credentialUnavailable }
        try await run(reportError: false) {
            try await self.cleanRetiredAccounts()
            let secret = try await self.keychain.read(account: account)
            try await UpdateCredentials.prepare(handoff, account: account, secret: secret)
        }
    }

    /// 新应用验证可读后才采用候选账户，旧账户仍由保留的旧身份清理
    func adoptUpdateCredential(_ handoff: UpdateHandoff) async throws {
        guard let store else { throw ClientError.credentialUnavailable }
        guard let record = try await UpdateFileWork().perform({ _ in try UpdateCredentials.read(handoff) }) else { return }
        try await run(reportError: false) {
            guard [record.sourceAccount, record.targetAccount].contains(self.saved.keychainAccount ?? ""), self.saved.pending == nil else {
                throw UpdateError.invalidLocation
            }
            let secret = try await self.keychain.read(account: record.targetAccount)
            try record.authenticate(handoff, secret: secret)
            var next = self.saved
            next.keychainAccount = record.targetAccount
            try await store.save(next); self.saved = next
        }
    }

    func settleUpdateCredential(_ handoff: UpdateHandoff, installed: Bool) async throws {
        guard try await UpdateFileWork().perform({ _ in try UpdateCredentials.read(handoff) != nil }) else { return }
        guard let account = saved.keychainAccount else { throw ClientError.credentialUnavailable }
        let secret = try await keychain.read(account: account)
        if installed { try await UpdateCredentials.finish(handoff, secret: secret) }
        else { try await UpdateCredentials.cancel(handoff, secret: secret) }
    }

    func resumeAfterUpdate() async throws {
        defer { preparingUpdate = false; updateSession = false; resumeAuthorization() }
        try await backend.cancelUpdatePreparation()
        if !backend.isRunning, saved.device != nil { try await connectNow() }
    }

    var projectRoot: URL { (store?.directory ?? stateSelection).appendingPathComponent("projects", isDirectory: true) }
    func refreshProjects() {
        guard !shuttingDown, !changingIdentity, !preparingUpdate, projectOperation == nil, let device = saved.device, let account = saved.keychainAccount, let server = saved.connection?.apiURL, store != nil else { return }
        projectsLoading = true
        projectOperation = Task {
            defer { projectOperation = nil; projectsLoading = false }
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
    func reloadProjects() async { refreshProjects(); await projectOperation?.value }
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
            catch { _ = userError(error) }
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
