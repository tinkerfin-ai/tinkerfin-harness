import AppKit
import Combine
import SwiftUI

enum CompanionPage: Equatable { case home, settings, permissions }
enum CompanionAction: Equatable, Sendable {
    case home, settings, permissions, saveSettings, openStudio, openConfiguration
    case startAuthorization, cancelAuthorization, reopenAuthorization, retry, accessibility, screenCapture

}

/// 面板保留编辑草稿并拥有界面动作；关闭浮层不取消已开始的连接，退出应用才收回任务
@MainActor
final class CompanionPanel: ObservableObject {
    let model: CompanionModel
    let brandImage: NSImage?
    @Published var page: CompanionPage = .home
    @Published private(set) var selectingLanguage = false
    @Published private(set) var isWindowVisible = false
    @Published var name = ""
    @Published private(set) var editingName = false
    @Published private(set) var nameFocusRequest: UUID?
    @Published private(set) var activeAction: CompanionAction?
    @Published var actionError: ClientError?
    @Published var panelHeight = 720.0
    var resize: ((Double) -> Void)?
    var close: (() -> Void)?
    private var subscription: AnyCancellable?
    private var actionTask: Task<Void, Never>?
    private var closing = false
    private var panelView: CompanionPanelView?
    var view: CompanionPanelView {
        if let panelView { return panelView }
        let created = CompanionPanelView(hosted: NSHostingView(rootView: CompanionPanelContent(panel: self, model: model, localization: model.localization)))
        panelView = created
        return created
    }

    init(model: CompanionModel, resources: URL) {
        self.model = model
        brandImage = NSImage(contentsOf: resources.appendingPathComponent("brand.svg"))
        subscription = model.objectWillChange.receive(on: DispatchQueue.main).sink { [weak self] _ in
            MainActor.assumeIsolated { self?.publish() }
        }
    }

    var errors: [String] {
        var seen: Set<ClientError> = []
        return [actionError, model.error].compactMap { $0 }.filter { seen.insert($0).inserted }
            .map(model.localization.message)
    }

    func publish() {
        guard !closing else { return }
        if !editingName, name != model.saved.name { name = model.saved.name }
    }

    func showSettings() async { perform(.settings) }
    func clearFieldError() { actionError = nil }
    func toggleLanguageSelection() {
        guard page == .settings else { return }
        selectingLanguage.toggle()
    }
    func dismissLanguageSelection() { selectingLanguage = false }
    func setWindowVisible(_ visible: Bool) {
        if isWindowVisible != visible { isWindowVisible = visible }
    }
    func selectLanguage(_ language: ClientLanguage) {
        model.localization.select(language)
        selectingLanguage = false
    }

    private var canSubmit: Bool { model.ready && !model.busy && actionTask == nil }
    private var afterNameSave: CompanionAction?
    var canEditName: Bool { !closing && model.ready && actionTask == nil }

    var showsSettings: Bool { page == .home }
    var primaryBusy: Bool {
        model.authorizationWaiting || model.retrying || ["loading", "connecting"].contains(model.connection)
            || activeAction == .startAuthorization || activeAction == .cancelAuthorization || activeAction == .retry
    }
    var primaryAction: CompanionAction {
        switch model.connection {
        case "online": return .openStudio
        case "authorizing": return .reopenAuthorization
        case "offline", "loading", "connecting": return .retry
        default: return .startAuthorization
        }
    }
    var primaryLabel: String {
        if model.authorizationWaiting || activeAction == .startAuthorization || activeAction == .cancelAuthorization { return "授权中" }
        if model.retrying || activeAction == .retry { return "重试中" }
        if ["loading", "connecting"].contains(model.connection) { return "连接中" }
        switch primaryAction {
        case .openStudio: return "打开 Studio"
        case .retry, .reopenAuthorization: return "重试"
        default: return "连接"
        }
    }

    /// 配置写入保持单一操作；只阻止会丢弃当前提交草稿的导航
    func canPerform(_ action: CompanionAction) -> Bool {
        guard !closing else { return false }
        if activeAction == .saveSettings { return action != .saveSettings }
        switch action {
        case .home: return activeAction != .saveSettings
        case .settings, .permissions: return true
        case .accessibility, .screenCapture: return true
        case .openStudio, .openConfiguration, .reopenAuthorization: return model.ready
        case .saveSettings: return editingName && model.ready && actionTask == nil
        case .cancelAuthorization: return canSubmit
        case .startAuthorization, .retry: return canSubmit && !model.retrying && !model.authorizationWaiting
        }
    }

    func beginNameEditing() {
        guard page == .settings, canEditName else { return }
        if !editingName { name = model.saved.name }
        editingName = true; nameFocusRequest = UUID(); actionError = nil
    }

    private func cancelNameEditing() {
        guard activeAction != .saveSettings else { return }
        name = model.saved.name; editingName = false; nameFocusRequest = nil; actionError = nil
    }

    func perform(_ action: CompanionAction) {
        guard canPerform(action) else { return }
        if activeAction == .saveSettings { afterNameSave = action; return }
        // 键盘与辅助功能激活导航时同样先保存，不能依赖鼠标失焦事件
        if editingName, action != .saveSettings {
            afterNameSave = action
            perform(.saveSettings)
            return
        }
        do {
            if action == .startAuthorization || action == .reopenAuthorization {
                navigate(.home)
                close?()
            }
            switch action {
            case .home: navigate(.home)
            case .settings: navigate(.settings)
            case .permissions: model.checkPermissions(); navigate(.permissions)
            case .openConfiguration: model.openConfiguration()
            case .openStudio: try model.openStudio()
            case .reopenAuthorization: try model.reopenAuthorization()
            case .accessibility: model.openPermissionSettings(false)
            case .screenCapture: model.openPermissionSettings(true)
            default:
                activeAction = action
                actionTask = Task { @MainActor in
                    defer {
                        actionTask = nil; activeAction = nil; publish()
                        let next = afterNameSave; afterNameSave = nil
                        if !editingName, let next { perform(next) }
                    }
                    do { try Task.checkCancellation(); try await execute(action) }
                    catch is CancellationError { }
                    catch { report(error) }
                }
            }
        } catch { report(error) }
    }

    private func report(_ error: Error) {
        if !closing {
            let message = model.userError(error)
            actionError = model.error == message ? nil : message
        }
    }

    private func navigate(_ next: CompanionPage) {
        dismissLanguageSelection()
        if next != .settings, next != .permissions { cancelNameEditing() }
        if !editingName { name = model.saved.name }
        page = next; actionError = nil
    }

    private func execute(_ action: CompanionAction) async throws {
        actionError = nil
        switch action {
        case .saveSettings:
            try await model.saveName(name)
            name = model.saved.name; editingName = false; nameFocusRequest = nil
        case .startAuthorization: try await model.startAuthorization()
        case .cancelAuthorization: try await model.cancelAuthorization()
        case .retry: try await model.retry()
        default: break
        }
    }

    func escape() {
        if selectingLanguage { dismissLanguageSelection(); return }
        if page == .settings, editingName {
            cancelNameEditing()
            return
        }
        switch page {
        case .permissions: perform(.settings)
        case .settings: perform(.home)
        case .home: close?()
        }
    }

    func shutdown() async {
        closing = true; subscription = nil
        actionTask?.cancel(); await actionTask?.value
        panelView?.detachContent(); panelView = nil
    }

    /// 退出前结算失焦保存；失败时保持编辑页，让使用者修正或取消草稿
    func prepareToQuit() async -> Bool {
        afterNameSave = nil
        if editingName, activeAction != .saveSettings { perform(.saveSettings) }
        if activeAction == .saveSettings { await actionTask?.value }
        return !editingName
    }
}
