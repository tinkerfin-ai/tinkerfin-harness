import AppKit
import Foundation
import WebKit

enum DesktopPage: String, Codable, Sendable { case connection, workspace, permissions, account }
enum DesktopSurface: String, Codable, Sendable { case workbench, status, updater }
enum DesktopConnectionReason: String, Codable, Sendable { case restore, authorization, retry }

/// 界面草稿只用于窗口恢复和更新交接，不包含设备凭证或已批准的授权记录
struct DesktopViewState: Codable, Equatable, Sendable {
    var page: DesktopPage
    var connectionForm: Bool
    var service: String
    var api: String
    var manualAPI: Bool
    var editingName: Bool
    var name: String

    func checked() throws -> Self {
        guard service.utf16.count <= 4096, api.utf16.count <= 4096, name.utf16.count <= 128,
              ![service, api, name].contains(where: { $0.contains("\0") }) else {
            throw ClientError.message("界面草稿无效")
        }
        return self
    }
}

enum DesktopUpdatePhase: String, Codable, Sendable {
    case idle, checking, available, latest, downloading, verifying, waiting, installing, restarting, success
    case checkError = "check-error", downloadError = "download-error", verifyError = "verify-error"
    case installError = "install-error", restartError = "restart-error"

    var locked: Bool { self == .installing || self == .restarting }
}

struct DesktopUpdateState: Codable, Sendable {
    var phase: DesktopUpdatePhase = .idle
    var percent: Double = 0
    var currentVersion = ""
    var targetVersion = ""
}

/// WebView接收的完整展示状态；网络地址仅供展示，任何打开操作仍由原生复核
struct DesktopSnapshot: Encodable, Sendable {
    struct Project: Encodable, Sendable { let id: String; let name: String }
    struct Permissions: Encodable, Sendable { let accessibility: Bool; let screenCapture: Bool }
    var revision = 0
    var surface: DesktopSurface = .workbench
    var language = "system"
    var locale = "en"
    var theme = "light"
    var ready = false
    var connected = false
    var connection = "loading"
    var connectionReason: DesktopConnectionReason = .restore
    var authorizationPhase = "idle"
    var authorizationError = ""
    var connectionError = ""
    var service = ""
    var api = ""
    var switching = false
    var name = ""
    var account = ""
    var version = ""
    var busy = ""
    var projects: [Project] = []
    var projectsLoading = false
    var openingProject = ""
    var permissions = Permissions(accessibility: false, screenCapture: false)
    var update = DesktopUpdateState()
    var resume: DesktopViewState?

    enum CodingKeys: String, CodingKey {
        case revision, surface, language, locale, theme, ready, connected, connection, connectionReason
        case authorizationPhase, authorizationError, connectionError, service, api, switching, name, account, version
        case busy, projects, projectsLoading, openingProject, permissions, update, resume
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(revision, forKey: .revision); try values.encode(surface, forKey: .surface)
        try values.encode(language, forKey: .language); try values.encode(locale, forKey: .locale); try values.encode(theme, forKey: .theme)
        try values.encode(ready, forKey: .ready); try values.encode(connected, forKey: .connected); try values.encode(connection, forKey: .connection)
        try values.encode(connectionReason, forKey: .connectionReason)
        try values.encode(authorizationPhase, forKey: .authorizationPhase); try values.encode(authorizationError, forKey: .authorizationError)
        try values.encode(connectionError, forKey: .connectionError); try values.encode(service, forKey: .service); try values.encode(api, forKey: .api)
        try values.encode(switching, forKey: .switching); try values.encode(name, forKey: .name); try values.encode(account, forKey: .account)
        try values.encode(version, forKey: .version); try values.encode(busy, forKey: .busy); try values.encode(projects, forKey: .projects)
        try values.encode(projectsLoading, forKey: .projectsLoading); try values.encode(openingProject, forKey: .openingProject)
        try values.encode(permissions, forKey: .permissions); try values.encode(update, forKey: .update)
        try values.encode(resume, forKey: .resume)
    }
}

enum DesktopUIAction: Decodable, Sendable {
    case ready, openWorkbench, quit, openStudio, retry, cancelAuthorization, openAuthorization, refreshProjects, logout
    case updateConfirm, updateCancel, updateRetry
    case connect(service: String, api: String), saveName(String), language(String), permission(String), openProject(String)
    case saveView(DesktopViewState), checkUpdate(DesktopViewState)

    private struct Key: CodingKey {
        let stringValue: String
        let intValue: Int? = nil
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { return nil }
        init(_ value: String) { stringValue = value }
    }

    init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: Key.self)
        let kind = try fields.decode(String.self, forKey: Key("type"))
        var allowed: Set<String> = ["type"]
        func string(_ name: String, limit: Int) throws -> String {
            let value = try fields.decode(String.self, forKey: Key(name))
            guard value.utf16.count <= limit, !value.contains("\0") else { throw ClientError.message("界面请求无效") }
            return value
        }
        switch kind {
        case "ready": self = .ready
        case "open-workbench": self = .openWorkbench
        case "quit": self = .quit
        case "open-studio": self = .openStudio
        case "retry": self = .retry
        case "cancel-authorization": self = .cancelAuthorization
        case "open-authorization": self = .openAuthorization
        case "refresh-projects": self = .refreshProjects
        case "logout": self = .logout
        case "update-confirm": self = .updateConfirm
        case "update-cancel": self = .updateCancel
        case "update-retry": self = .updateRetry
        case "connect": allowed.formUnion(["service", "api"]); self = .connect(service: try string("service", limit: 4096), api: try string("api", limit: 4096))
        case "save-name": allowed.insert("name"); self = .saveName(try string("name", limit: 128))
        case "language":
            allowed.insert("language"); let value = try string("language", limit: 16)
            guard ["system", "zh-Hans", "en"].contains(value) else { throw ClientError.message("界面语言无效") }; self = .language(value)
        case "permission":
            allowed.insert("permission"); let value = try string("permission", limit: 16)
            guard ["accessibility", "screenCapture"].contains(value) else { throw ClientError.message("系统权限请求无效") }; self = .permission(value)
        case "open-project":
            allowed.insert("id"); let value = try string("id", limit: 36)
            guard UUID(uuidString: value)?.uuidString.lowercased() == value else { throw ClientError.message("项目身份无效") }; self = .openProject(value)
        case "save-view", "check-update":
            allowed.insert("view")
            let view = try fields.decode(DesktopViewState.self, forKey: Key("view")).checked()
            self = kind == "save-view" ? .saveView(view) : .checkUpdate(view)
        default: throw ClientError.message("界面请求无效")
        }
        guard Set(fields.allKeys.map(\.stringValue)) == allowed else { throw ClientError.message("界面请求字段无效") }
    }
}

struct DesktopReply: Encodable, Sendable {
    let ok: Bool
    var message = ""
    var field = ""
}

/// 只读取应用包内前端资源；专用有界队列隔离同步文件读取，停止后不再回调WebKit任务
@MainActor
private final class DesktopResources: NSObject, WKURLSchemeHandler {
    private let root: URL
    private let queue: OperationQueue
    private var loading: [ObjectIdentifier: (any WKURLSchemeTask, Operation)] = [:]

    init(root: URL) {
        self.root = root.standardizedFileURL
        queue = OperationQueue(); queue.maxConcurrentOperationCount = 2; queue.qualityOfService = .userInitiated
        super.init()
    }

    func webView(_ webView: WKWebView, start urlSchemeTask: any WKURLSchemeTask) {
        guard loading.count < 64, urlSchemeTask.request.httpMethod == "GET",
              let url = urlSchemeTask.request.url, url.scheme == "tinkerfin-ui", url.host == "app",
              url.user == nil, url.password == nil, url.port == nil, url.query == nil,
              let encodedPath = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath,
              let decoded = encodedPath.removingPercentEncoding else {
            urlSchemeTask.didFailWithError(URLError(.unsupportedURL)); return
        }
        let name = decoded.hasPrefix("/") ? String(decoded.dropFirst()) : decoded
        let candidate = root.appendingPathComponent(name).standardizedFileURL
        guard candidate.path.hasPrefix(root.path + "/"), !name.contains("\0") else {
            urlSchemeTask.didFailWithError(URLError(.noPermissionsToReadFile)); return
        }
        let mime: String
        switch candidate.pathExtension {
        case "html": mime = "text/html"
        case "js": mime = "text/javascript"
        case "css": mime = "text/css"
        case "svg": mime = "image/svg+xml"
        case "woff2": mime = "font/woff2"
        default: urlSchemeTask.didFailWithError(URLError(.unsupportedURL)); return
        }
        let identifier = ObjectIdentifier(urlSchemeTask)
        let root = root
        let operation = BlockOperation { [weak self] in
            let result: Result<Data, Error> = Result {
                let canonical = candidate.resolvingSymlinksInPath()
                guard canonical.path.hasPrefix(root.resolvingSymlinksInPath().path + "/") else { throw URLError(.noPermissionsToReadFile) }
                let info = try canonical.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
                guard info.isRegularFile == true, let size = info.fileSize, size <= 8 * 1024 * 1024 else { throw URLError(.dataLengthExceedsMaximum) }
                return try Data(contentsOf: canonical, options: .mappedIfSafe)
            }
            DispatchQueue.main.async {
                guard let task = self?.loading.removeValue(forKey: identifier)?.0 else { return }
                switch result {
                case .success(let data):
                    task.didReceive(URLResponse(url: url, mimeType: mime, expectedContentLength: data.count, textEncodingName: mime.hasPrefix("text/") ? "utf-8" : nil))
                    task.didReceive(data); task.didFinish()
                case .failure: task.didFailWithError(URLError(.fileDoesNotExist))
                }
            }
        }
        loading[identifier] = (urlSchemeTask, operation); queue.addOperation(operation)
    }

    func webView(_ webView: WKWebView, stop urlSchemeTask: any WKURLSchemeTask) { loading.removeValue(forKey: ObjectIdentifier(urlSchemeTask))?.1.cancel() }
    func close() async {
        loading.values.forEach { $0.1.cancel() }; loading.removeAll(); queue.cancelAllOperations()
        await withCheckedContinuation { continuation in queue.addBarrierBlock { continuation.resume() } }
    }
}

/// 弱引用解除WebKit消息处理器与窗口之间的所有权循环
@MainActor
private final class DesktopMessages: NSObject, WKScriptMessageHandlerWithReply {
    weak var owner: DesktopWebView?
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping @MainActor (Any?, String?) -> Void) {
        guard let owner else { replyHandler(nil, "Window closed"); return }
        owner.receive(message, reply: replyHandler)
    }
}

@MainActor
final class DesktopWebView: NSObject, WKNavigationDelegate {
    let view: WKWebView
    private let resources: DesktopResources
    private var commands: [UUID: Task<Void, Never>] = [:]
    private var ready = false
    private var revision = 0
    private var closed = false
    var handle: ((DesktopUIAction) async -> DesktopReply)?
    var refresh: (() -> Void)?

    init(resources root: URL) {
        resources = DesktopResources(root: root.appendingPathComponent("ui", isDirectory: true))
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        configuration.setURLSchemeHandler(resources, forURLScheme: "tinkerfin-ui")
        let messages = DesktopMessages()
        configuration.userContentController.addScriptMessageHandler(messages, contentWorld: .page, name: "desktop")
        view = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        messages.owner = self
        view.navigationDelegate = self; view.isInspectable = false
        view.underPageBackgroundColor = .clear
        view.load(URLRequest(url: URL(string: "tinkerfin-ui://app/index.html")!))
    }

    func receive(_ message: WKScriptMessage, reply: @escaping @MainActor (Any?, String?) -> Void) {
        guard !closed, commands.count < 64, message.frameInfo.isMainFrame,
              message.frameInfo.securityOrigin.protocol == "tinkerfin-ui", message.frameInfo.securityOrigin.host == "app",
              let address = message.frameInfo.request.url, address.scheme == "tinkerfin-ui", address.host == "app", address.path == "/index.html",
              JSONSerialization.isValidJSONObject(message.body),
              let payload = try? JSONSerialization.data(withJSONObject: message.body), payload.count <= 64 * 1024,
              let command = try? JSONDecoder().decode(DesktopUIAction.self, from: payload) else {
            reply(nil, "Invalid desktop request"); return
        }
        if case .ready = command { ready = true; refresh?() }
        let identifier = UUID()
        commands[identifier] = Task { [weak self] in
            guard let self else { reply(nil, "Window closed"); return }
            defer { commands[identifier] = nil }
            let result = await handle?(command) ?? DesktopReply(ok: false, message: "界面暂不可用")
            guard !closed else { reply(nil, "Window closed"); return }
            reply(["ok": result.ok, "message": result.message, "field": result.field], nil)
        }
    }

    func publish(_ value: DesktopSnapshot) {
        guard ready, !closed else { return }
        var snapshot = value; revision += 1; snapshot.revision = revision
        guard let data = try? JSONEncoder().encode(snapshot), let payload = String(data: data, encoding: .utf8) else { return }
        view.callAsyncJavaScript("window.receiveDesktopState(JSON.parse(payload))", arguments: ["payload": payload], in: nil, in: .page, completionHandler: nil)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        let url = navigationAction.request.url
        decisionHandler(!closed && navigationAction.targetFrame?.isMainFrame == true && url?.scheme == "tinkerfin-ui" && url?.host == "app" && url?.path == "/index.html" && url?.query == nil ? .allow : .cancel)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        guard !closed else { return }
        ready = false; webView.reload()
    }

    func close() async {
        closed = true; ready = false
        view.stopLoading(); view.configuration.userContentController.removeScriptMessageHandler(forName: "desktop", contentWorld: .page)
        await resources.close(); handle = nil; refresh = nil
        let pending = Array(commands.values)
        pending.forEach { $0.cancel() }
        for task in pending { await task.value }
    }
}
