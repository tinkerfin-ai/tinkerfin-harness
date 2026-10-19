import AppKit
import ApplicationServices
import CoreGraphics
import CryptoKit
import Foundation
import ImageIO
import OSLog
import ScreenCaptureKit
import UniformTypeIdentifiers

struct DesktopApplication: Encodable, Sendable {
    let id: String
    let name: String
    let bundleID: String
    enum CodingKeys: String, CodingKey { case id, name, bundleID = "bundle_id" }
}

struct DesktopElement: Encodable, Sendable {
    let id: Int
    let role: String
    let title: String
    let value: String?
    let pressable: Bool
    let editable: Bool
}

struct DesktopResult: Encodable, Sendable {
    var applications: [DesktopApplication]?
    var snapshotID: String?
    var application: String?
    var title: String?
    var elements: [DesktopElement]?
    var truncated: Bool?
    var accepted: Bool?
    enum CodingKeys: String, CodingKey {
        case snapshotID = "snapshot_id"
        case applications, application, title, elements, truncated, accepted
    }
}

struct NativeDesktopReply: Encodable, Sendable {
    let kind = "desktop_result"
    let requestID: String
    var result = DesktopResult()
    var image: String?
    var error: String?
    enum CodingKeys: String, CodingKey { case kind, requestID = "request_id", result, image, error }
}

/// 截图候选只保存系统报告的窗口身份；可见性不等于当前活动状态
struct DesktopCaptureWindow: Sendable {
    let id: CGWindowID
    let pid: pid_t
    let layer: Int
    let frame: CGRect
    let title: String
    let active: Bool
}

/// 标题使用完整值，不能用给模型的截短预览关联另一个窗口
func desktopCaptureWindowIndex(_ windows: [DesktopCaptureWindow], pid: pid_t, frame: CGRect, title: String) throws -> Int {
    let matches = windows.indices.filter {
        let window = windows[$0]
        return window.pid == pid && window.layer == 0 && window.active
            && window.frame.integral == frame && window.title == title
    }
    guard matches.count == 1 else { throw ClientError.message("无法确认目标窗口截图，请重新读取窗口") }
    return matches[0]
}

private final class DesktopCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    func cancel() { lock.lock(); cancelled = true; lock.unlock() }
    func check() throws {
        lock.lock(); let value = cancelled; lock.unlock()
        if value { throw CancellationError() }
    }
}

/// 所有跨应用 AX 调用串行且有单次消息超时，取消在下一次系统调用前生效
private final class DesktopWorker: @unchecked Sendable {
    private struct WindowContent: Equatable {
        let title: String
        let fingerprint: SHA256.Digest
    }
    private struct Snapshot {
        let id: String
        let workspaceScopeID: String
        let pid: pid_t
        let application: String
        let window: AXUIElement
        let frame: CGRect
        let windowContent: WindowContent
        let elements: [AXUIElement]
        let descriptions: [DesktopElement]
        let fingerprints: [SHA256.Digest]
    }
    private let queue = DispatchQueue(label: "com.tinkerfin.companion.desktop")
    private let logger = Logger(subsystem: "com.tinkerfin.companion", category: "desktop")
    private var snapshot: Snapshot?

    private struct PreparedReply: Sendable {
        var reply: NativeDesktopReply
        let frame: CGRect?
        let title: String?
    }

    private func onQueue<Value: Sendable>(_ operation: @escaping @Sendable () throws -> Value) async throws -> Value {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                do { continuation.resume(returning: try operation()) }
                catch { continuation.resume(throwing: error) }
            }
        }
    }

    func perform(_ request: DesktopRequest, pid: pid_t?, application: String?,
                 cancellation: DesktopCancellation) async throws -> NativeDesktopReply {
        var prepared = try await onQueue { [self] in
                    try cancellation.check()
                    try Self.requireInteractiveSession()
                    guard AXIsProcessTrusted() else { throw ClientError.message("请在系统设置中为 TinkerFin 开启辅助功能权限") }
                    if request.command.action == "snapshot" {
                        guard let pid, let application else { throw ClientError.message("应用已关闭，请重新读取应用列表") }
                        let reply = try capture(request, pid: pid, application: application, cancellation: cancellation)
                        return PreparedReply(reply: reply, frame: snapshot?.frame, title: snapshot?.windowContent.title)
                    } else {
                        return PreparedReply(reply: try act(request, cancellation: cancellation), frame: nil, title: nil)
                    }
        }
        if request.command.action == "snapshot", request.command.includeImage {
            guard let pid, let bounds = prepared.frame, let title = prepared.title else { throw ClientError.message("无法确认截图窗口") }
            try cancellation.check()
            prepared.reply.image = try await screenshot(pid: pid, frame: bounds, title: title)
            let expectedID = prepared.reply.result.snapshotID
            try await onQueue { [self] in
                try cancellation.check()
                guard let saved = snapshot, saved.id == expectedID, saved.workspaceScopeID == request.workspaceScopeID else {
                    throw ClientError.message("窗口快照已被替换，请重新读取")
                }
                try validate(saved, cancellation: cancellation)
            }
        }
        return prepared.reply
    }

    static func requireInteractiveSession() throws {
        guard let session = CGSessionCopyCurrentDictionary() as? [String: Any],
              session[kCGSessionOnConsoleKey as String] as? Bool == true,
              session[kCGSessionLoginDoneKey as String] as? Bool == true,
              session["CGSSessionScreenIsLocked"] as? Bool != true else {
            throw ClientError.message("请解锁屏幕后重新请求桌面操作")
        }
    }

    private func attribute(_ element: AXUIElement, _ name: CFString,
                           cancellation: DesktopCancellation) throws -> CFTypeRef? {
        try cancellation.check()
        AXUIElementSetMessagingTimeout(element, 2)
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(element, name, &value)
        if result == .attributeUnsupported || result == .noValue { return nil }
        guard result == .success else { throw ClientError.message("目标窗口无法读取，请切换到该应用后重试") }
        return value
    }

    private func text(_ element: AXUIElement, _ name: CFString,
                      cancellation: DesktopCancellation) throws -> String {
        let value = try attribute(element, name, cancellation: cancellation)
        if let text = value as? String { return text }
        if let number = value as? NSNumber { return number.stringValue }
        return ""
    }

    private func window(_ app: AXUIElement, cancellation: DesktopCancellation) throws -> AXUIElement {
        guard let value = try attribute(app, kAXFocusedWindowAttribute as CFString, cancellation: cancellation),
              CFGetTypeID(value) == AXUIElementGetTypeID() else {
            throw ClientError.message("应用没有可读取的当前窗口，请先打开目标窗口")
        }
        return value as! AXUIElement
    }

    private func frame(_ window: AXUIElement, cancellation: DesktopCancellation) throws -> CGRect {
        guard let position = try attribute(window, kAXPositionAttribute as CFString, cancellation: cancellation),
              let size = try attribute(window, kAXSizeAttribute as CFString, cancellation: cancellation),
              CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else {
            throw ClientError.message("目标窗口的位置无法确认，请重新读取窗口")
        }
        var point = CGPoint.zero, dimensions = CGSize.zero
        guard AXValueGetValue(position as! AXValue, .cgPoint, &point),
              AXValueGetValue(size as! AXValue, .cgSize, &dimensions),
              dimensions.width > 0, dimensions.height > 0,
              dimensions.width * dimensions.height <= 8_000_000 else {
            throw ClientError.message("目标窗口尺寸无法处理，请缩小窗口后重试")
        }
        return CGRect(origin: point, size: dimensions).integral
    }

    private func requireFocused(_ pid: pid_t, cancellation: DesktopCancellation) throws -> AXUIElement {
        let system = AXUIElementCreateSystemWide()
        guard let value = try attribute(system, kAXFocusedApplicationAttribute as CFString, cancellation: cancellation),
              CFGetTypeID(value) == AXUIElementGetTypeID() else {
            throw ClientError.message("无法确认当前应用，请切换到目标应用后重试")
        }
        let app = value as! AXUIElement
        var actual: pid_t = 0
        guard AXUIElementGetPid(app, &actual) == .success, actual == pid else {
            throw ClientError.message("当前应用已变化，请重新读取目标窗口")
        }
        return app
    }

    private func capture(_ request: DesktopRequest, pid: pid_t, application: String,
                         cancellation: DesktopCancellation) throws -> NativeDesktopReply {
        snapshot = nil
        let app = try requireFocused(pid, cancellation: cancellation)
        let window = try window(app, cancellation: cancellation)
        let bounds = try frame(window, cancellation: cancellation)
        let windowContent = try windowContent(window, cancellation: cancellation)
        var pending = [window], elements: [AXUIElement] = [], descriptions: [DesktopElement] = []
        var fingerprints: [SHA256.Digest] = []
        while !pending.isEmpty && elements.count < 200 {
            try cancellation.check()
            let element = pending.removeLast()
            guard let observed = try observe(element, id: elements.count, cancellation: cancellation) else { continue }
            descriptions.append(observed.0)
            fingerprints.append(observed.1)
            elements.append(element)
            var children: CFArray?
            let remaining = 200 - elements.count - pending.count
            if remaining > 0 && AXUIElementCopyAttributeValues(element, kAXChildrenAttribute as CFString,
                                                               0, remaining, &children) == .success {
                pending.append(contentsOf: (children as? [AXUIElement] ?? []).reversed())
            }
        }
        let id = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let saved = Snapshot(id: id, workspaceScopeID: request.workspaceScopeID, pid: pid, application: application,
                             window: window, frame: bounds, windowContent: windowContent,
                             elements: elements, descriptions: descriptions, fingerprints: fingerprints)
        var response = NativeDesktopReply(requestID: request.id)
        response.result = DesktopResult(snapshotID: id, application: application,
                                        title: String(windowContent.title.prefix(512)),
                                        elements: descriptions, truncated: elements.count == 200)
        try validate(saved, cancellation: cancellation)
        snapshot = saved
        return response
    }

    private func windowContent(_ window: AXUIElement, cancellation: DesktopCancellation) throws -> WindowContent {
        let values = try [kAXTitleAttribute, kAXDocumentAttribute].map {
            try text(window, $0 as CFString, cancellation: cancellation)
        }
        return WindowContent(title: values[0], fingerprint: SHA256.hash(data: try JSONEncoder().encode(values)))
    }

    private func observe(_ element: AXUIElement, id: Int, cancellation: DesktopCancellation) throws
        -> (DesktopElement, SHA256.Digest)? {
        let role = try text(element, kAXRoleAttribute as CFString, cancellation: cancellation)
        let subrole = try text(element, kAXSubroleAttribute as CFString, cancellation: cancellation)
        if role == "AXSecureTextField" || subrole == "AXSecureTextField" { return nil }
        let title = try text(element, kAXTitleAttribute as CFString, cancellation: cancellation)
        let value = try text(element, kAXValueAttribute as CFString, cancellation: cancellation)
        let label = try text(element, kAXDescriptionAttribute as CFString, cancellation: cancellation)
        let identifier = try text(element, kAXIdentifierAttribute as CFString, cancellation: cancellation)
        let enabled = try attribute(element, kAXEnabledAttribute as CFString, cancellation: cancellation) as? Bool != false
        var actions: CFArray?
        AXUIElementCopyActionNames(element, &actions)
        var editable = DarwinBoolean(false)
        AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &editable)
        let pressable = enabled && (actions as? [String] ?? []).contains(kAXPressAction)
        let writable = enabled && editable.boolValue
        let description = DesktopElement(id: id, role: role, title: String((title.isEmpty ? label : title).prefix(512)),
                                         value: value.isEmpty ? nil : String(value.prefix(512)),
                                         pressable: pressable, editable: writable)
        // 比较完整值的摘要，截短的模型预览不能作为窗口变化的判定依据
        let fingerprint = SHA256.hash(data: try JSONEncoder().encode([
            role, subrole, title, value, label, identifier, String(pressable), String(writable),
        ]))
        return (description, fingerprint)
    }

    private func validate(_ saved: Snapshot, cancellation: DesktopCancellation) throws {
        try Self.requireInteractiveSession()
        let app = try requireFocused(saved.pid, cancellation: cancellation)
        guard CFEqual(try window(app, cancellation: cancellation), saved.window),
              try frame(saved.window, cancellation: cancellation) == saved.frame,
              try windowContent(saved.window, cancellation: cancellation) == saved.windowContent else {
            snapshot = nil
            throw ClientError.message("窗口或文档已变化，请重新读取窗口")
        }
    }

    private func act(_ request: DesktopRequest, cancellation: DesktopCancellation) throws -> NativeDesktopReply {
        guard let saved = snapshot, saved.workspaceScopeID == request.workspaceScopeID,
              saved.id == request.command.snapshotID, let index = request.command.elementID,
              saved.elements.indices.contains(index) else {
            throw ClientError.message("窗口快照或元素已失效，请重新读取窗口")
        }
        try validate(saved, cancellation: cancellation)
        let element = saved.elements[index]
        let description = saved.descriptions[index]
        guard let observed = try observe(element, id: index, cancellation: cancellation),
              observed.1 == saved.fingerprints[index],
              try (CFEqual(element, saved.window) || attribute(element, kAXWindowAttribute as CFString, cancellation: cancellation)
                .map({ CFEqual($0, saved.window) }) == true) else {
            snapshot = nil
            throw ClientError.message("控件内容或所属窗口已变化，请重新读取窗口")
        }
        try validate(saved, cancellation: cancellation)
        try cancellation.check()
        // 动作消费快照，下一次动作必须重新观察，不能连续操作已变化的界面
        snapshot = nil
        let status: AXError
        switch request.command.action {
        case "press" where description.pressable:
            status = AXUIElementPerformAction(element, kAXPressAction as CFString)
        case "set_value" where description.editable:
            guard let value = request.command.value, value.count <= 8192 else { throw ClientError.message("待填写内容无效") }
            status = AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFString)
        default: throw ClientError.message("该元素不支持此操作，请重新读取窗口")
        }
        guard status == .success else { throw ClientError.message("系统未确认操作成功，请重新读取窗口核验") }
        return NativeDesktopReply(requestID: request.id, result: DesktopResult(accepted: true))
    }

    private func screenshot(pid: pid_t, frame: CGRect, title: String) async throws -> String {
        guard CGPreflightScreenCaptureAccess() else { throw ClientError.message("请在系统设置中为 TinkerFin 开启屏幕录制权限") }
        // SC 的屏幕可见过滤在应用激活期间可能暂时漏掉 AX 已确认的窗口；从完整列表严格绑定身份
        let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
        let windows = content.windows.map {
            DesktopCaptureWindow(id: $0.windowID, pid: $0.owningApplication?.processID ?? -1,
                                 layer: $0.windowLayer, frame: $0.frame, title: $0.title ?? "", active: $0.isActive)
        }
        let index: Int
        do { index = try desktopCaptureWindowIndex(windows, pid: pid, frame: frame, title: title) }
        catch {
            let candidates = windows.filter { $0.pid == pid }.prefix(16).map {
                "id=\($0.id) frame=\($0.frame) layer=\($0.layer) active=\($0.active) titleMatches=\($0.title == title)"
            }.joined(separator: "; ")
            logger.error("Capture window refused pid=\(pid) expected=\(String(describing: frame), privacy: .public) candidates=\(candidates, privacy: .public)")
            throw error
        }
        let scale = min(1.0, sqrt(500_000.0 / (frame.width * frame.height)))
        let settings = SCStreamConfiguration()
        settings.width = max(1, Int(frame.width * scale))
        settings.height = max(1, Int(frame.height * scale))
        settings.showsCursor = false
        settings.ignoreShadowsSingleWindow = true
        let image = try await SCScreenshotManager.captureImage(
            contentFilter: SCContentFilter(desktopIndependentWindow: content.windows[index]), configuration: settings)
        return try await onQueue { try Self.encodePNG(image) }
    }

    private static func encodePNG(_ image: CGImage) throws -> String {
        let scale = min(1.0, sqrt(500_000.0 / Double(image.width * image.height)))
        let width = max(1, Int(Double(image.width) * scale)), height = max(1, Int(Double(image.height) * scale))
        guard let canvas = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                                     bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
                                     bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw ClientError.message("无法准备窗口截图")
        }
        canvas.interpolationQuality = .high
        canvas.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        let content = NSMutableData()
        guard let resized = canvas.makeImage(),
              let destination = CGImageDestinationCreateWithData(content, UTType.png.identifier as CFString, 1, nil) else {
            throw ClientError.message("无法编码窗口截图")
        }
        CGImageDestinationAddImage(destination, resized, nil)
        guard CGImageDestinationFinalize(destination), content.length <= 2 * 1024 * 1024 else {
            throw ClientError.message("窗口截图超过容量，请缩小窗口后重试")
        }
        return (content as Data).base64EncodedString()
    }
}

@MainActor
final class DesktopAccess {
    private let worker = DesktopWorker()
    private var applications: [String: NSRunningApplication] = [:]
    private var workspaceScopeID: String?
    private var pending: [String: DesktopCancellation] = [:]
    private var snapshotApplication: NSRunningApplication?
    private var snapshotScopeID: String?
    private var snapshotID: String?
    private var elements: [DesktopElement] = []

    func list(_ request: DesktopRequest) throws -> NativeDesktopReply {
        try DesktopWorker.requireInteractiveSession()
        workspaceScopeID = request.workspaceScopeID
        snapshotApplication = nil
        snapshotScopeID = nil
        snapshotID = nil
        elements = []
        applications.removeAll()
        let choices = NSWorkspace.shared.runningApplications.filter {
            $0.activationPolicy == .regular && !$0.isTerminated && $0.processIdentifier != getpid()
        }.prefix(128).map { app in
            let id = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
            applications[id] = app
            return DesktopApplication(id: id, name: app.localizedName ?? "应用", bundleID: app.bundleIdentifier ?? "")
        }
        return NativeDesktopReply(requestID: request.id, result: DesktopResult(applications: Array(choices)))
    }

    func targetName(_ request: DesktopRequest) throws -> String {
        if request.command.action == "snapshot", workspaceScopeID == request.workspaceScopeID,
           let id = request.command.application, let app = applications[id], !app.isTerminated {
            return app.localizedName ?? "目标应用"
        }
        if ["press", "set_value"].contains(request.command.action),
           snapshotScopeID == request.workspaceScopeID, snapshotID == request.command.snapshotID,
           let index = request.command.elementID, elements.indices.contains(index),
           let app = snapshotApplication, !app.isTerminated {
            return app.localizedName ?? "目标应用"
        }
        throw ClientError.message("应用或快照已失效，请重新读取应用列表")
    }

    func review(_ request: DesktopRequest) throws -> (String, String) {
        let app = try targetName(request)
        if request.command.action == "snapshot" {
            return ("读取 \(app) 的窗口", request.command.includeImage
                    ? "读取当前窗口内容与截图，经 Studio 交给所选模型处理"
                    : "读取当前窗口的按钮、输入框和文字，经 Studio 交给所选模型处理")
        }
        guard let index = request.command.elementID, elements.indices.contains(index) else {
            throw ClientError.message("元素已失效，请重新读取窗口")
        }
        let element = elements[index]
        let label = element.title.isEmpty ? "所选控件" : element.title
        if request.command.action == "press", element.pressable {
            return ("在 \(app) 点击“\(label)”", "点击可能提交表单或改变应用数据，请核对当前窗口")
        }
        if request.command.action == "set_value", element.editable, let value = request.command.value {
            return ("在 \(app) 填写“\(label)”", "替换为：\n" + value)
        }
        throw ClientError.message("该元素不支持请求的操作，请重新读取窗口")
    }

    func perform(_ request: DesktopRequest) async throws -> NativeDesktopReply {
        try Task.checkCancellation()
        let application: NSRunningApplication
        if request.command.action == "snapshot", workspaceScopeID == request.workspaceScopeID,
           let id = request.command.application, let selected = applications[id] {
            application = selected
        } else if snapshotScopeID == request.workspaceScopeID, snapshotID == request.command.snapshotID,
                  let saved = snapshotApplication {
            application = saved
        } else { throw ClientError.message("窗口快照已失效，请重新读取") }
        guard !application.isTerminated, application.activate(options: []) else {
            throw ClientError.message("无法激活目标应用，请先打开目标窗口")
        }
        let cancellation = DesktopCancellation()
        snapshotID = nil
        snapshotScopeID = nil
        snapshotApplication = nil
        elements = []
        pending[request.id] = cancellation
        defer { pending.removeValue(forKey: request.id) }
        let result = try await withTaskCancellationHandler {
            try await worker.perform(request, pid: application.processIdentifier,
                                     application: application.localizedName ?? "应用", cancellation: cancellation)
        } onCancel: { cancellation.cancel() }
        try Task.checkCancellation()
        if request.command.action == "snapshot" {
            snapshotApplication = application
            snapshotScopeID = request.workspaceScopeID
            snapshotID = result.result.snapshotID
            elements = result.result.elements ?? []
        }
        return result
    }

    func cancel(_ requestID: String) { pending[requestID]?.cancel() }
    /// 失效已观测目标；调用者只取消受撤权影响的动作，录屏撤销不停止普通控件操作
    func invalidateTargets() {
        applications = [:]
        workspaceScopeID = nil
        snapshotApplication = nil
        snapshotScopeID = nil
        snapshotID = nil
        elements = []
    }
    func cancelAll() { for token in pending.values { token.cancel() } }
}
