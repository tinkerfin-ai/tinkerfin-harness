import AppKit
import Combine
import Foundation

/// 原生业务动作与无凭证快照的边界，窗口草稿由React拥有，原生只保存交接副本
@MainActor
final class DesktopPresentation {
    private enum Activity: Hashable { case connection, name, logout, retry, authorization, project, refresh }
    let model: CompanionModel
    private var subscriptions: [AnyCancellable] = []
    private var activities: Set<Activity> = []
    private var nameWaiters: [CheckedContinuation<Void, Never>] = []
    private var actionWaiter: (UUID, CheckedContinuation<Void, Error>)?
    private var stopping = false
    private var openingProject = ""
    private(set) var viewState: DesktopViewState?
    var changed: (() -> Void)?
    var update = DesktopUpdateState() { didSet { changed?() } }

    init(model: CompanionModel) {
        self.model = model
        for publisher in [model.objectWillChange, model.localization.objectWillChange] {
            subscriptions.append(publisher.receive(on: DispatchQueue.main).sink { [weak self] _ in
                MainActor.assumeIsolated { self?.changed?() }
            })
        }
    }

    var canCheckUpdates: Bool { !stopping && activities.isEmpty && model.canBeginUpdate && connected }
    private var connected: Bool { model.ready && model.status == "online" && model.saved.device != nil }

    func snapshot(surface: DesktopSurface) -> DesktopSnapshot {
        var value = DesktopSnapshot()
        value.surface = surface
        value.language = model.localization.language.rawValue; value.locale = model.localization.identifier
        value.theme = NSApplication.shared.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? "dark" : "light"
        value.ready = model.ready; value.connected = connected
        value.connection = model.connection; value.connectionReason = model.connectionReason
        value.service = model.configuration?.studioURL ?? ""; value.api = model.configuration?.apiURL ?? ""
        value.name = model.saved.name; value.account = model.accountName
        value.version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
        value.projects = model.projects.map { .init(id: $0.projectID, name: $0.name) }
        value.projectsLoading = model.projectsLoading; value.openingProject = openingProject
        value.permissions = .init(accessibility: model.accessibility, screenCapture: model.screenCapture)
        if activities.contains(.logout) { value.busy = "logout" }
        else if activities.contains(.name) { value.busy = "name" }
        else if activities.contains(.connection) { value.busy = "connect" }
        else if activities.contains(.retry) || model.retrying { value.busy = "retry" }
        if activities.contains(.authorization) { value.authorizationPhase = "cancelling" }
        else if let pending = model.saved.pending {
            value.authorizationPhase = pending.authorization == nil && model.authorizationWaiting ? "starting" : "waiting"
            value.switching = model.saved.device != nil
            value.authorizationError = model.error.map(model.localization.message) ?? ""
        }
        value.connectionError = !model.authorizationNotice.isEmpty ? model.localization.text(model.authorizationNotice)
            : !model.ready && model.status == "failed" ? "请检查本机配置后重试"
            : model.status == "failed" ? model.localization.text("请检查服务地址后重试")
            : model.connection == "offline" ? model.error.map(model.localization.message) ?? "" : ""
        value.update = update; value.update.currentVersion = value.version; value.resume = viewState
        return value
    }

    func preserve(_ view: DesktopViewState) throws { viewState = try view.checked() }

    func handle(_ command: DesktopUIAction) async -> DesktopReply {
        if case .ready = command { return DesktopReply(ok: true) }
        if case .saveView(let view) = command {
            do { try preserve(view); return DesktopReply(ok: true) }
            catch { return DesktopReply(ok: false, message: "界面草稿无效") }
        }
        guard !stopping, update.phase == .idle else { return DesktopReply(ok: false, message: "请重试") }
        let activity: Activity
        switch command {
        case .connect: activity = .connection
        case .saveName: activity = .name
        case .logout: activity = .logout
        case .retry: activity = .retry
        case .cancelAuthorization: activity = .authorization
        case .openProject: activity = .project
        case .refreshProjects: activity = .refresh
        case .language(let selected):
            guard connected, let language = ClientLanguage(rawValue: selected) else { return DesktopReply(ok: false, message: "请重新连接") }
            model.localization.select(language); changed?(); return DesktopReply(ok: true)
        case .permission(let permission):
            guard connected else { return DesktopReply(ok: false, message: "请重新连接") }
            model.openPermissionSettings(permission == "screenCapture"); return DesktopReply(ok: true)
        case .openStudio:
            do { try model.openStudio(); return DesktopReply(ok: true) }
            catch { return DesktopReply(ok: false, message: model.localization.message(model.userError(error))) }
        case .openAuthorization:
            do { try model.reopenAuthorization(); return DesktopReply(ok: true) }
            catch { return DesktopReply(ok: false, message: model.localization.message(model.userError(error))) }
        default: return DesktopReply(ok: false, message: "界面请求无效")
        }
        guard !activities.contains(activity) else { return DesktopReply(ok: false, message: "请重试") }
        if [.name, .logout, .project, .refresh].contains(activity), !connected { return DesktopReply(ok: false, message: "请重新连接") }
        activities.insert(activity); changed?()
        defer {
            activities.remove(activity)
            if activity == .name { let waiting = nameWaiters; nameWaiters = []; waiting.forEach { $0.resume() } }
            if activity == .project { openingProject = "" }
            if activities.isEmpty { let waiter = actionWaiter; actionWaiter = nil; waiter?.1.resume() }
            changed?()
        }
        do {
            try Task.checkCancellation()
            switch command {
            case .connect(let service, let api):
                let target = try await ServiceDiscovery().resolve(studio: service, api: api)
                try Task.checkCancellation(); try await model.startAuthorization(to: target)
            case .saveName(let name): try await model.saveName(name)
            case .logout: try await model.revokeAuthorization()
            case .retry: try await model.retry()
            case .cancelAuthorization: try await model.cancelAuthorization()
            case .openProject(let id): openingProject = id; changed?(); try await model.openProject(id)
            case .refreshProjects: await model.reloadProjects()
            default: return DesktopReply(ok: false, message: "界面请求无效")
            }
            return DesktopReply(ok: true)
        } catch is CancellationError { return DesktopReply(ok: false, message: "请重试") }
        catch {
            let error = model.userError(error)
            let field = activity == .name ? "name" : error == .message("无法识别 API 地址，请展开填写") ? "api" : activity == .connection ? "service" : ""
            return DesktopReply(ok: false, message: model.localization.message(error), field: field)
        }
    }

    /// 只等待使用者已经提交的名称保存；未提交草稿不产生退出时隐式写入
    func prepareToQuit() async {
        stopping = true
        if activities.contains(.name) { await withCheckedContinuation { nameWaiters.append($0) } }
    }

    func finishAcceptedActions() async throws {
        try Task.checkCancellation()
        guard actionWaiter == nil else { throw ClientError.message("更新准备已开始") }
        guard !activities.isEmpty else { return }
        let identifier = UUID()
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { actionWaiter = (identifier, $0) }
        } onCancel: {
            DispatchQueue.main.async { [weak self] in
                guard let self, self.actionWaiter?.0 == identifier else { return }
                let waiter = self.actionWaiter; self.actionWaiter = nil; waiter?.1.resume(throwing: CancellationError())
            }
        }
    }

    func close() { subscriptions.removeAll(); changed = nil }
}
