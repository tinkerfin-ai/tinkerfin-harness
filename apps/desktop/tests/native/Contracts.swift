import AppKit
import Combine
import Foundation
import Security

struct DomainCase: Decodable {
    let values: [String]
    let accepted: Bool
}

struct WebsiteCase: Decodable {
    let url: String
    let accepted: Bool
}

struct ConnectionFeedbackFixture: Decodable {
    let account: String
    let server: String
    let result: String
}

/// 测试进程显式放行 HTTP 与宿主状态，不依赖轮询间隔或真实超时
func feedbackCheckpoint(_ name: String) async throws {
    FileHandle.standardOutput.write(Data(("checkpoint " + name + "\n").utf8))
    let acknowledgement = await Task.detached { readLine() }.value
    try require(acknowledgement == name, "反馈契约的控制信号不匹配")
}

func require(_ value: Bool, _ message: String) throws {
    if !value { throw ClientError.message(message) }
}

@main
struct NativeContracts {
    @MainActor
    static func main() async throws {
        let arguments = CommandLine.arguments
        let mode = arguments[1]
        let directory = URL(fileURLWithPath: arguments[2])
        if mode == "credential-cleanup" {
            let store = KeychainStore(service: arguments[2])
            try await store.remove(account: "first")
            try await store.remove(account: "second")
        } else if mode == "credential-session" {
            let created = KeychainStore(service: arguments[2])
            let first = try await created.create(account: "first")
            let second = try await created.create(account: "second")
            let restored = KeychainStore(service: arguments[2])
            try require(try await restored.read(account: "first") == first, "首次读取丢失已保存凭证")
            try require(try await restored.read(account: "second") == second, "不同连接的凭证未隔离")
            let storage = KeychainStore(service: arguments[2])
            try await storage.remove(account: "first")
            try await storage.remove(account: "second")
            try require(try await restored.read(account: "first") == first, "同一进程重复访问钥匙串")
            try require(try await restored.read(account: "second") == second, "并存连接的凭证未复用")
            try require(try await created.read(account: "first") == first, "新建连接未复用已保存凭证")
            try await restored.remove(account: "first")
            try require((try? await restored.read(account: "first")) == nil, "删除连接后仍可读取旧凭证")
            await restored.clear()
            try require((try? await restored.read(account: "second")) == nil, "退出后仍持有连接凭证")
            await created.clear()
        } else if mode == "localization" {
            let resources = directory.appendingPathComponent("resources")
            let suiteName = "com.tinkerfin.tests.localization." + UUID().uuidString.lowercased()
            guard let defaults = UserDefaults(suiteName: suiteName) else { throw ClientError.message("无法创建隔离语言偏好") }
            defer { defaults.removePersistentDomain(forName: suiteName) }
            var catalogs: [String: [String: String]] = [:]
            for language in ["zh-Hans", "en"] {
                let file = resources.appendingPathComponent(language + ".lproj/Localizable.strings")
                guard let catalog = try PropertyListSerialization.propertyList(from: Data(contentsOf: file), options: [], format: nil) as? [String: String] else {
                    throw ClientError.message("语言资源格式无效")
                }
                catalogs[language] = catalog
            }
            let chinese = catalogs["zh-Hans"]!, english = catalogs["en"]!
            try require(!english.isEmpty && Set(chinese.keys) == Set(english.keys), "语言资源缺少对应翻译")
            let localization = ClientLocalization(resources: resources, defaults: defaults)
            try require(localization.language == .system && (defaults.persistentDomain(forName: suiteName)?.isEmpty ?? true),
                "初始语言没有跟随系统或写入了显式覆盖")
            let languageCases: [([String], String)] = [
                ([], "en"), (["fr-FR", "de-DE"], "en"), (["en-US", "zh-Hans"], "en"),
                (["fr-FR", "zh-CN", "en"], "zh-Hans"), (["zh", "en"], "zh-Hans"),
                (["zh-SG", "en"], "zh-Hans"), (["zh-TW", "en"], "en"), (["zh-HK"], "en"),
                (["zh-MO", "zh-Hans"], "zh-Hans"), (["zh-Hant-CN"], "en"), (["zh-Hans-TW"], "zh-Hans"),
                (["zh-Hant", "en", "zh-Hans"], "en"),
            ]
            for (preferred, expected) in languageCases {
                try require(ClientLocalization.resolve(.system, preferredLanguages: preferred) == expected,
                    "系统语言优先级或中文文字体系选择错误")
            }
            try require(ClientLocalization.resolve(.english, preferredLanguages: ["zh-Hans"]) == "en"
                && ClientLocalization.resolve(.simplifiedChinese, preferredLanguages: ["en"]) == "zh-Hans",
                "显式语言选择未覆盖系统偏好")
            for language in [ClientLanguage.english, .simplifiedChinese] {
                localization.select(language)
                let restored = ClientLocalization(resources: resources, defaults: UserDefaults(suiteName: suiteName)!)
                try require(localization.identifier == language.rawValue && restored.language == language
                    && restored.identifier == language.rawValue, "语言切换没有立即生效或无法恢复偏好")
                for key in ["设置", "跟随系统", "授权中", "重试中", "连接中", "取消", "退出 TinkerFin"] {
                    guard let expected = catalogs[language.rawValue]?[key] else { throw ClientError.message("缺少关键界面翻译") }
                    try require(localization.text(key) == expected && !expected.isEmpty, "状态或菜单没有使用当前语言资源")
                }
                try require(localization.label(for: .simplifiedChinese) == "简体中文"
                    && localization.label(for: .english) == "English", "语言选项没有使用可辨认的自称")
            }
            for value in ["bad%@/host", "localhost"] {
                let savedError: ClientError
                do { _ = try checkedDomains([value]); throw ClientError.message("无效域名未被拒绝") }
                catch let error as ClientError { savedError = error }
                try require(savedError.arguments == [value], "配置错误未保留原始参数")
                let original = savedError.errorDescription
                localization.select(.english)
                let translated = localization.message(savedError)
                try require(translated.contains(value) && translated != original, "已保存错误未翻译或改变了参数")
                localization.select(.simplifiedChinese)
                try require(localization.message(savedError) == original && savedError.errorDescription == original
                    && savedError.arguments == [value], "语言切换改变了原始错误或协议文案")
            }
            localization.select(.system)
            let restored = ClientLocalization(resources: resources, defaults: UserDefaults(suiteName: suiteName)!)
            try require(restored.language == .system && (defaults.persistentDomain(forName: suiteName)?.isEmpty ?? true),
                "跟随系统未移除语言覆盖")
        } else if mode == "feedback-cleanup" {
            let fixture = try JSONDecoder().decode(ConnectionFeedbackFixture.self,
                from: Data(contentsOf: directory.appendingPathComponent("feedback.json")))
            try require(UUID(uuidString: fixture.account) != nil, "测试凭证身份无效")
            try await KeychainStore().remove(account: fixture.account)
        } else if mode == "retry-feedback" || mode == "authorization-feedback" {
            let fixture = try JSONDecoder().decode(ConnectionFeedbackFixture.self,
                from: Data(contentsOf: directory.appendingPathComponent("feedback.json")))
            let resources = directory.appendingPathComponent("resources")
            try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
            try JSONEncoder().encode(ConnectionConfiguration(studioURL: fixture.server, apiURL: fixture.server))
                .write(to: resources.appendingPathComponent("connection.json"))
            let state = directory.appendingPathComponent("state")
            let credentialStore = KeychainStore()
            _ = try await credentialStore.create(account: fixture.account)
            var saved = SavedConfiguration()
            saved.name = "Contract Mac"
            if mode == "retry-feedback" {
                saved.server = fixture.server
                saved.keychainAccount = fixture.account
                saved.device = PairedDevice(deviceID: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", namespace: "ns_1", name: saved.name)
            } else {
                saved.pending = PendingConnection(account: fixture.account, server: fixture.server,
                    authorization: AuthorizationCreated(authorizationID: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        browserToken: String(repeating: "b", count: 64), expiresAt: "2100-01-01T00:00:00Z"))
            }
            try await ConfigurationStore(directory: state).save(saved)
            let model = CompanionModel(state: state, resources: resources)
            let panel = CompanionPanel(model: model, resources: resources)
            let states = AsyncStream.makeStream(of: String.self, bufferingPolicy: .bufferingNewest(16))
            let actions = AsyncStream.makeStream(of: CompanionAction?.self, bufferingPolicy: .bufferingNewest(8))
            let authorization = AsyncStream.makeStream(of: Bool.self, bufferingPolicy: .bufferingNewest(8))
            let subscriptions = [
                model.$status.sink { states.continuation.yield($0) },
                panel.$activeAction.sink { actions.continuation.yield($0) },
                model.$authorizationWaiting.sink { authorization.continuation.yield($0) },
            ]
            defer {
                for subscription in subscriptions { subscription.cancel() }
                states.continuation.finish(); actions.continuation.finish(); authorization.continuation.finish()
            }
            var stateUpdates = states.stream.makeAsyncIterator()
            var actionUpdates = actions.stream.makeAsyncIterator()
            var authorizationUpdates = authorization.stream.makeAsyncIterator()
            var attentionCount = 0
            model.attention = { attentionCount += 1 }
            do {
                try await model.restore()
                if mode == "retry-feedback" {
                    try require(model.connection == "connecting" && panel.primaryBusy && panel.primaryLabel == "连接中",
                        "宿主启动后尚未收到连接结果，却结束了加载反馈")
                    try await feedbackCheckpoint("initial-started")
                    while model.status != "offline" { _ = await stateUpdates.next() }
                    panel.perform(.retry)
                    try require(panel.primaryBusy && panel.primaryLabel == "重试中", "点击重试未立即显示按钮加载状态")
                    while panel.activeAction != nil { _ = await actionUpdates.next() }
                    try require(!model.busy && model.retrying && model.connection == "connecting"
                        && panel.primaryBusy && panel.primaryLabel == "重试中" && !panel.canPerform(.retry),
                        "宿主刚启动或旧宿主停止时提前结束了重试反馈")
                    let progress = AsyncStream.makeStream(of: String.self, bufferingPolicy: .bufferingNewest(1))
                    let progressSubscription = model.$status.dropFirst().sink { progress.continuation.yield($0) }
                    defer { progressSubscription.cancel(); progress.continuation.finish() }
                    var progressUpdates = progress.stream.makeAsyncIterator()
                    try await feedbackCheckpoint("retry-started")
                    try require(await progressUpdates.next() == "connecting", "本轮宿主未报告正在连接")
                    try require(model.retrying && panel.primaryBusy, "连接中的状态错误地结束了本轮重试")
                    try await feedbackCheckpoint("retry-result")
                    let expected = fixture.result == "failure" ? "failed" : fixture.result == "exit" ? "offline" : fixture.result
                    while model.status != expected { _ = await stateUpdates.next() }
                    try require(!model.retrying && !panel.primaryBusy, "真实连接结果没有结束本轮加载反馈")
                    let action: CompanionAction = fixture.result == "online" ? .openStudio : fixture.result == "unauthorized" ? .startAuthorization : .retry
                    try require(panel.primaryAction == action, "连接结果没有保留对应的恢复操作")
                } else {
                    try require(model.authorizationWaiting && panel.primaryBusy && panel.primaryLabel == "授权中"
                        && panel.canPerform(.cancelAuthorization) && !panel.canPerform(.startAuthorization),
                        "浏览器授权等待没有持续加载、取消入口或重复操作保护")
                    try await feedbackCheckpoint("authorization-started")
                    try require(model.authorizationWaiting && panel.primaryBusy, "授权 HTTP 尚未结算却结束了加载反馈")
                    if fixture.result == "cancel-request" {
                        panel.perform(.cancelAuthorization)
                        try await feedbackCheckpoint("authorization-cancelling")
                        try require(panel.activeAction == .cancelAuthorization && panel.primaryBusy && panel.primaryLabel == "授权中",
                            "取消请求尚未结算却恢复了授权主按钮")
                        try await feedbackCheckpoint("authorization-cancel-result")
                        while panel.activeAction != nil { _ = await actionUpdates.next() }
                    } else {
                        try await feedbackCheckpoint("authorization-result")
                        while model.authorizationWaiting { _ = await authorizationUpdates.next() }
                    }
                    try require(attentionCount == 0, "授权结算自动展开了客户端")
                    if fixture.result == "authorized" {
                        try require(model.saved.pending == nil && model.saved.device != nil
                            && model.connection == "connecting" && panel.primaryBusy && panel.primaryLabel == "连接中",
                            "授权已完成但连接未建立时没有继续反馈连接状态")
                        try await feedbackCheckpoint("authorization-connected")
                        while model.status != "online" { _ = await stateUpdates.next() }
                        try require(!panel.primaryBusy && panel.primaryAction == .openStudio && attentionCount == 0,
                            "连接成功没有结束加载或自动展开了客户端")
                    } else if fixture.result == "error" {
                        try require(model.saved.pending != nil && !panel.primaryBusy && panel.primaryLabel == "重试"
                            && panel.primaryAction == .reopenAuthorization && model.error != nil,
                            "授权轮询失败后永久加载或丢失恢复入口")
                        model.resumeAuthorization()
                        try require(model.authorizationWaiting && panel.primaryBusy && panel.primaryLabel == "授权中",
                            "恢复授权没有重新进入加载状态")
                        try await feedbackCheckpoint("authorization-recovery")
                        while model.authorizationWaiting { _ = await authorizationUpdates.next() }
                        try require(model.saved.pending == nil && !panel.primaryBusy && attentionCount == 0,
                            "恢复后授权终态没有结束等待或自动展开客户端")
                    } else {
                        try require(model.saved.pending == nil && !panel.primaryBusy && panel.primaryAction == .startAuthorization,
                            "授权终态未结束等待或无法重新发起授权")
                    }
                }
                await panel.shutdown(); await model.shutdown()
                try await credentialStore.remove(account: fixture.account)
            } catch {
                await panel.shutdown(); await model.shutdown()
                try await credentialStore.remove(account: fixture.account)
                throw error
            }
        } else if mode == "configuration" {
            let cases = try JSONDecoder().decode([DomainCase].self, from: Data(contentsOf: directory.appendingPathComponent("domains.json")))
            for item in cases {
                try require(((try? checkedDomains(item.values)) != nil) == item.accepted, "域名校验与沙箱不一致")
            }
            try require(try checkedServer("https://studio.example.com/studio/").absoluteString == "https://studio.example.com/studio", "地址规范失败")
            for address in ["http://example.com", "https://user:secret@example.com", "https://example.com?a=1", "file:///tmp"] {
                try require((try? checkedServer(address)) == nil, "不安全的地址被接受")
            }
            let state = directory.appendingPathComponent("state")
            let store = ConfigurationStore(directory: state)
            try require(try await store.read().permissions.allowedDomains.isEmpty, "初始配置错误")
            try require(try await store.read().permissions.network == .normal, "初始配置未启用正常联网")
            let previous = Data(#"{"allowed_domains":[],"browser":true,"desktop":true,"mcp_servers":[]}"#.utf8)
            try require((try? JSONDecoder().decode(WorkspacePermissions.self, from: previous)) == nil,
                        "旧配置被默认字段静默转成正常联网")
            var invalid = WorkspacePermissions()
            invalid.allowedDomains = ["example.com"]
            try require((try? JSONEncoder().encode(invalid)) == nil, "正常联网接受了冲突的域名列表")
            var configuration = SavedConfiguration()
            configuration.server = "https://studio.example.com"
            configuration.keychainAccount = UUID().uuidString
            configuration.permissions.network = .restricted
            configuration.permissions.allowedDomains = ["example.com"]
            try await store.save(configuration)
            let restored = try await ConfigurationStore(directory: state).read()
            try require(restored.permissions == configuration.permissions && restored.keychainAccount == configuration.keychainAccount,
                        "重开配置丢失授权")
            let bytes = try Data(contentsOf: state.appendingPathComponent("settings.json"))
            try require(!String(decoding: bytes, as: UTF8.self).contains("credential"), "配置包含凭证")
            for (url, mode) in [(state, 0o700), (state.appendingPathComponent("settings.json"), 0o600)] {
                let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
                try require((attributes[.posixPermissions] as? NSNumber)?.intValue == mode, "配置权限错误")
            }
            var first: ApplicationLock? = try ApplicationLock(directory: state)
            try require(first != nil && (try? ApplicationLock(directory: state)) == nil, "状态目录可被重复占用")
            first = nil
            _ = try ApplicationLock(directory: state)
            let owner = PairedDevice(deviceID: UUID().uuidString, namespace: "ns_1", name: "Mac")
            configuration.acceptAuthorization(owner, at: configuration.server)
            configuration.acceptAuthorization(owner, at: configuration.server)
            try require(configuration.device == owner && configuration.permissions.allowedDomains == ["example.com"], "重新连接改变了本机工具配置")
        } else if mode == "managed-projects" {
            let root = directory.appendingPathComponent("projects", isDirectory: true)
            let access = WorkspaceAccess()
            let opened = try await access.prepareProjects(root)
            try require(opened.path == root.path, "首次准备改变了根路径")
            let id = "11111111-1111-1111-1111-111111111111"
            let project = try await access.prepareProjects(root, components: ["service", "ns_1", id])
            try Data("retained".utf8).write(to: project.appendingPathComponent("proof.txt"))
            _ = try await access.prepareProjects(root, components: ["service", "ns_2", id])
            let reopened = try await access.prepareProjects(root, components: ["service", "ns_1", id])
            try require(try String(contentsOf: reopened.appendingPathComponent("proof.txt"), encoding: .utf8) == "retained", "切换账号丢失工作文件")
            let linked = directory.appendingPathComponent("linked")
            try FileManager.default.createSymbolicLink(at: linked, withDestinationURL: root)
            do { _ = try await access.prepareProjects(linked); throw ClientError.message("接受了不受信的项目根") }
            catch ClientError.workspaceAccess { }
        } else if mode == "authorization" {
            do {
                let result = try await AuthorizationClient().status(server: checkedServer(arguments[2]),
                    authorizationID: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", credential: String(repeating: "b", count: 64))
                if let device = result.device { print(String(decoding: try JSONEncoder().encode(device), as: UTF8.self)) }
                else { print(result.state.rawValue) }
            } catch {
                print((error as? ClientError)?.errorDescription ?? "授权请求失败")
            }
            return
        } else if mode == "connection-recovery" {
            let account = UUID().uuidString.lowercased()
            let keychain = KeychainStore()
            let resources = directory.appendingPathComponent("resources")
            try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
            try JSONEncoder().encode(ConnectionConfiguration(studioURL: "https://studio.test", apiURL: "https://studio.test"))
                .write(to: resources.appendingPathComponent("connection.json"))
            let state = directory.appendingPathComponent("state")
            var saved = SavedConfiguration()
            saved.server = "https://studio.test"; saved.name = "Contract Mac"; saved.keychainAccount = account
            saved.device = PairedDevice(deviceID: UUID().uuidString, namespace: "ns_1", name: saved.name)
            try await ConfigurationStore(directory: state).save(saved)
            _ = try await keychain.create(account: account)
            let folderModel = CompanionModel(state: state, resources: resources)
            do {
                try await keychain.remove(account: account)
                let credentialModel = CompanionModel(state: state, resources: resources)
                do { try await credentialModel.restore(); throw ClientError.message("缺失凭证被接受") }
                catch ClientError.credentialUnavailable {
                    try require(credentialModel.connection == "revoked" && credentialModel.statusText == "连接已失效", "缺失凭证没有显示明确的连接失效状态")
                }
                await credentialModel.shutdown()
            } catch {
                await folderModel.shutdown()
                try await keychain.remove(account: account)
                throw error
            }
        } else if mode == "panel-geometry" {
            let launchScreen = CGRect(x: 0, y: 72, width: 1512, height: 872)
            let pendingAnchor = CGRect(x: 8, y: -29.5, width: 32, height: 22)
            try require(PanelGeometry(anchor: pendingAnchor, screen: launchScreen, contentHeight: 94) == nil,
                "状态栏窗口落位前生成了不可见的浮层")
            let settledAnchor = CGRect(x: 924, y: 952.5, width: 32, height: 22)
            guard let settled = PanelGeometry(anchor: settledAnchor, screen: launchScreen, contentHeight: 230) else {
                throw ClientError.message("状态栏窗口落位后没有生成浮层")
            }
            try require(settled.frame == CGRect(x: 724, y: 712, width: 232, height: 230),
                "状态栏窗口落位后没有恢复完整尺寸与边缘对齐")
            for screen in [CGRect(x: 0, y: 40, width: 1512, height: 904), CGRect(x: -1440, y: -900, width: 1440, height: 875)] {
                for center in [screen.minX + 40, screen.midX, screen.maxX - 40] {
                    let anchor = CGRect(x: center - 10, y: screen.maxY, width: 20, height: 24)
                    for height in [282.0, 1200.0] {
                        guard let geometry = PanelGeometry(anchor: anchor, screen: screen, contentHeight: height) else {
                            throw ClientError.message("有效菜单栏位置未生成浮层")
                        }
                        try require(geometry.frame.width == 232 && screen.contains(geometry.frame), "浮层越过当前屏幕或未使用约定宽度")
                        try require(geometry.frame.maxY == anchor.minY - 2, "浮层没有贴住菜单栏下边缘")
                        try require(geometry.frame.height == min(height, geometry.maximumContentHeight), "浮层仍包含正文以外的高度")
                        if center <= screen.midX { try require(geometry.frame.minX == anchor.minX, "右侧空间充足时没有按左边对齐") }
                        else { try require(geometry.frame.maxX == anchor.maxX, "左侧空间充足时没有按右边对齐") }
                    }
                }
            }
        } else if mode == "panel-drafts" {
            let resources = directory.appendingPathComponent("resources")
            try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
            try JSONEncoder().encode(ConnectionConfiguration(studioURL: "https://studio.test", apiURL: "https://studio.test"))
                .write(to: resources.appendingPathComponent("connection.json"))
            let state = directory.appendingPathComponent("state")
            var saved = SavedConfiguration(); saved.name = "Contract Mac"
            try await ConfigurationStore(directory: state).save(saved)
            let model = CompanionModel(state: state, resources: resources)
            let panel = CompanionPanel(model: model, resources: resources)
            let actions = AsyncStream.makeStream(of: CompanionAction?.self, bufferingPolicy: .bufferingNewest(1))
            let subscription = panel.$activeAction.sink { actions.continuation.yield($0) }
            defer { subscription.cancel(); actions.continuation.finish() }
            var actionUpdates = actions.stream.makeAsyncIterator()
            var closeCount = 0
            panel.close = { closeCount += 1 }
            do {
                try await model.restore()
                try require(panel.showsSettings && !panel.primaryBusy && panel.primaryLabel == "连接",
                    "未连接首页未提供设置或连接入口")
                panel.toggleLanguageSelection()
                try require(!panel.selectingLanguage, "首页打开了设置中的语言选项")
                panel.perform(.settings)
                try require(!panel.showsSettings, "设置页仍重复展示设置入口")
                let previousStatus = model.status
                panel.toggleLanguageSelection()
                model.checkPermissions(); panel.publish()
                try require(panel.selectingLanguage && model.status == previousStatus && model.saved.pending == nil,
                    "界面刷新关闭了语言选项或语言入口改变了连接")
                panel.escape()
                try require(!panel.selectingLanguage && panel.page == .settings && closeCount == 0,
                    "Escape没有先关闭语言选项并保留设置页")
                panel.toggleLanguageSelection(); panel.perform(.permissions)
                try require(!panel.selectingLanguage && panel.page == .permissions, "页面导航留下了语言选项")
                panel.toggleLanguageSelection()
                try require(!panel.selectingLanguage, "权限页打开了设置中的语言选项")
                panel.perform(.settings)
                panel.toggleLanguageSelection(); panel.dismissLanguageSelection()
                try require(!panel.selectingLanguage && panel.page == .settings, "关闭语言选项改变了设置页")
                panel.perform(.reopenAuthorization)
                try require(panel.page == .home && closeCount == 1 && panel.actionError != nil,
                    "浏览器授权入口未同步收起浮层并返回首页")
                panel.perform(.settings)
                panel.beginNameEditing(); panel.name = "Draft name"; model.checkPermissions(); panel.publish()
                panel.toggleLanguageSelection(); panel.escape()
                try require(!panel.selectingLanguage && panel.editingName && panel.name == "Draft name",
                    "关闭语言选项丢弃了尚未保存的名称草稿")
                try require(panel.page == .settings && panel.name == "Draft name", "设置草稿或目录取消不正确")
                try require(try await ConfigurationStore(directory: state).read().name == "Contract Mac", "名称草稿未经确认被保存")
                panel.perform(.permissions)
                while let action = await actionUpdates.next() { if action == nil { break } }
                try require(panel.page == .permissions && !panel.editingName, "直接激活导航没有保存名称并继续原动作")
                try require(!panel.showsSettings, "桌面权限页仍重复展示设置入口")
                try require(try await ConfigurationStore(directory: state).read().name == "Draft name", "导航保存没有持久化")
                panel.escape()
                panel.perform(.settings)
                try require(panel.name == "Draft name", "重复打开设置丢失已保存名称")
                panel.beginNameEditing(); panel.name = "Cancelled draft"
                panel.escape()
                try require(panel.page == .settings && !panel.editingName && panel.name == "Draft name", "Escape未取消名称编辑并恢复已保存值")
                panel.beginNameEditing(); panel.name = "Contract Mac"
                panel.perform(.home)
                while let action = await actionUpdates.next() { if action == nil { break } }
                try require(panel.page == .home && !panel.editingName && panel.name == "Contract Mac", "离开设置没有自动保存名称")
                panel.perform(.settings)

                panel.beginNameEditing(); panel.name = ""; panel.perform(.saveSettings)
                try require(panel.activeAction == .saveSettings && !panel.canEditName && panel.canPerform(.home), "名称保存没有保留后续导航入口")
                panel.perform(.home)
                panel.escape()
                try require(panel.editingName && panel.name.isEmpty, "Escape在提交期间丢弃了名称草稿")
                while let action = await actionUpdates.next() { if action == nil { break } }
                try require(panel.page == .settings && panel.editingName && panel.name.isEmpty && panel.actionError == .message("请输入设备名称"), "名称保存失败没有保留草稿及错误")
                try require(model.saved.name == "Contract Mac", "失败的名称提交改变了已保存值")
                panel.name = "  测试 Mac  "; panel.perform(.saveSettings)
                panel.perform(.home)
                while let action = await actionUpdates.next() { if action == nil { break } }
                try require(!panel.editingName && panel.name == "测试 Mac" && panel.actionError == nil, "名称确认成功后没有结束编辑并显示规范值")
                try require(try await ConfigurationStore(directory: state).read().name == "测试 Mac", "名称确认没有持久化")
                try require(panel.page == .home, "失焦保存吞掉了原导航动作")
                panel.perform(.settings)

                try await model.run {
                    for action in [CompanionAction.home, .settings, .permissions, .accessibility, .screenCapture, .openStudio, .reopenAuthorization] {
                        try require(panel.canPerform(action), "无关后台操作禁用了导航或外部入口")
                    }
                    panel.beginNameEditing(); panel.name = "Busy draft"
                    try require(panel.canEditName && panel.canPerform(.saveSettings), "后台写入期间无法排队保存名称")
                    for action in [CompanionAction.startAuthorization, .cancelAuthorization, .retry] {
                        try require(!panel.canPerform(action), "后台配置写入期间允许并发操作")
                    }
                    try require(panel.activeAction == nil, "后台忙碌被错误归给界面动作")
                }
                panel.escape(); panel.perform(.home)
                panel.perform(.retry)
                try require(panel.activeAction == .retry && panel.canPerform(.settings) && panel.canPerform(.permissions), "重试改变了无关导航状态")
                panel.perform(.startAuthorization)
                try require(panel.activeAction == .retry, "连接覆盖了尚未结束的重试动作")
                while let action = await actionUpdates.next() { if action == nil { break } }

                panel.perform(.settings); panel.beginNameEditing(); panel.name = ""
                try require(!(await panel.prepareToQuit()) && panel.editingName, "保存失败仍退出并丢弃草稿")
                panel.name = "退出前保存"
                try require(await panel.prepareToQuit(), "退出未结算名称保存")
                try require(try await ConfigurationStore(directory: state).read().name == "退出前保存", "退出前名称未落盘")
                panel.perform(.cancelAuthorization)
                try require(panel.activeAction == .cancelAuthorization && panel.canPerform(.reopenAuthorization), "取消授权的忙碌状态归属不正确")
                while let action = await actionUpdates.next() { if action == nil { break } }

                do { try await model.run { throw ClientError.message("Connection error") } } catch { }
                panel.actionError = .message("Field error"); panel.clearFieldError()
                try require(panel.errors == ["Connection error"], "编辑表单清除了独立连接错误")
                panel.perform(.settings)
                panel.perform(.startAuthorization)
                try require(panel.activeAction == .startAuthorization && panel.canPerform(.settings), "开始连接改变了设置入口状态")
                try require(panel.page == .home && closeCount == 2 && panel.primaryBusy && panel.primaryLabel == "授权中",
                    "连接或切换未立即收起设置页、返回首页并展示持续加载")
                await panel.shutdown()
                try require(panel.activeAction == nil && model.saved.pending == nil, "退出未收回尚未执行的界面动作")
                await model.shutdown()
            } catch { await panel.shutdown(); await model.shutdown(); throw error }
        } else if mode == "converted-configuration" {
            let saved = try await ConfigurationStore(directory: directory).read()
            try require(saved.name == "Contract Mac" && saved.permissions.network == .normal && saved.permissions.allowedDomains.isEmpty,
                "转换后的配置无法完整读取")
        } else if mode == "connect-link" {
            for address in ["tinkerfin://connect", "tinkerfin://connect?state=online",
                            "tinkerfin://connect?state=unauthorized&device=untrusted"] {
                try require(isClientConnectURL(URL(string: address)!), "客户端打开入口错误地依赖网页提供的连接状态")
            }
            for address in ["https://connect", "tinkerfin://other", "tinkerfin:connect",
                            "tinkerfin://user@connect", "tinkerfin://user:password@connect",
                            "tinkerfin://connect:443", "tinkerfin://connect/", "tinkerfin://connect/unexpected",
                            "tinkerfin://workspace?project=untrusted"] {
                try require(!isClientConnectURL(URL(string: address)!), "含凭证、端口、路径或其他用途的链接被当作客户端打开入口")
            }
        } else if mode == "pending-recovery" {
            let resources = directory.appendingPathComponent("resources")
            try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
            try JSONEncoder().encode(ConnectionConfiguration(studioURL: "https://studio.test", apiURL: "https://studio.test"))
                .write(to: resources.appendingPathComponent("connection.json"))
            let state = directory.appendingPathComponent("state")
            var saved = SavedConfiguration()
            saved.name = "Contract Mac"
            saved.pending = PendingConnection(account: UUID().uuidString.lowercased(), server: "https://studio.test",
                authorization: AuthorizationCreated(authorizationID: UUID().uuidString.lowercased(),
                    browserToken: String(repeating: "b", count: 64), expiresAt: "2100-01-01T00:00:00Z"))
            try await ConfigurationStore(directory: state).save(saved)
            let model = CompanionModel(state: state, resources: resources)
            do {
                try await model.restore()
                try await model.cancelAuthorization()
                let persisted = try await ConfigurationStore(directory: state).read()
                try require(model.connection == "unpaired" && model.statusText == "未连接" && model.saved.pending == nil && persisted.pending == nil,
                    "缺失待授权凭证仍阻止重新连接")
                try require(persisted.permissions == saved.permissions, "取消授权丢失本机目录")
                await model.shutdown()
            } catch { await model.shutdown(); throw error }
        } else if mode == "mcp-command" {
            let arguments = ["/Applications/My Tools/python", "server.py", "--title", "A 'quoted' value", "$HOME", ";"]
            try require(try parseMCPCommand(displayMCPCommand(arguments)) == arguments, "MCP 命令引号往返失败")
            try require(try parseMCPCommand("python tools.py --name \"My Tool\"") == ["python", "tools.py", "--name", "My Tool"], "MCP 引号未保留参数")
            for invalid in ["python 'unfinished", "python \\", "python ''", String(repeating: "x", count: 16385)] {
                try require((try? parseMCPCommand(invalid)) == nil, "无效 MCP 命令被接受")
            }
            try require(try parseMCPCommand("").isEmpty, "空命令未移除 MCP")
        } else if mode == "capture-window" {
            let frame = CGRect(x: 80, y: 414, width: 620, height: 428)
            let title = String(repeating: "文档", count: 300) + "A"
            let expected = DesktopCaptureWindow(id: 10, pid: 41, layer: 0, frame: frame, title: title, active: true)
            let unrelated = [
                DesktopCaptureWindow(id: 1, pid: 42, layer: 0, frame: frame, title: title, active: true),
                DesktopCaptureWindow(id: 2, pid: 41, layer: 1, frame: frame, title: title, active: true),
                DesktopCaptureWindow(id: 3, pid: 41, layer: 0, frame: frame.offsetBy(dx: 1, dy: 0), title: title, active: true),
                DesktopCaptureWindow(id: 4, pid: 41, layer: 0, frame: frame, title: title, active: false),
                DesktopCaptureWindow(id: 5, pid: 41, layer: 0, frame: frame, title: String(title.dropLast()) + "B", active: true),
            ]
            try require(try desktopCaptureWindowIndex(unrelated + [expected], pid: 41, frame: frame, title: title) == unrelated.count,
                        "窗口选择未绑定完整身份")
            for candidate in unrelated {
                try require((try? desktopCaptureWindowIndex([candidate], pid: 41, frame: frame, title: title)) == nil,
                            "其他应用、窗口内容或活动状态被接受")
            }
            try require((try? desktopCaptureWindowIndex([], pid: 41, frame: frame, title: title)) == nil, "缺失窗口被接受")
            let ambiguous = DesktopCaptureWindow(id: 11, pid: 41, layer: 0, frame: frame, title: title, active: true)
            try require((try? desktopCaptureWindowIndex([expected, ambiguous], pid: 41, frame: frame, title: title)) == nil,
                        "同名同位置的多个窗口被任意选择")
        } else if mode == "native-readiness" {
            for accessibility in [false, true] {
                for capture in [false, true] {
                    let readiness = NativeReadiness(accessibility: accessibility, screenCapture: capture)
                    for action in ["open_url", "apps", "snapshot", "press", "set_value"] {
                        for includeImage in [false, true] {
                            let command = DesktopCommand(action: action, application: nil, snapshotID: nil, elementID: nil, value: nil, includeImage: includeImage)
                            let expected = action == "open_url" || action == "apps" || accessibility && (action != "snapshot" || !includeImage || capture)
                            try require((readiness.missingPermission(for: command) == nil) == expected, "能力判断混淆了网页与系统权限")
                        }
                    }
                    let message = try JSONEncoder().encode(NativeReadinessMessage(readiness: readiness))
                    let payload = try JSONSerialization.jsonObject(with: message) as? [String: Any]
                    let values = payload?["readiness"] as? [String: Bool]
                    try require(payload?["kind"] as? String == "native_readiness" && values?["accessibility"] == accessibility && values?["screen_capture"] == capture, "权限报告丢失了当前状态")
                }
            }
        } else if mode == "website" {
            let cases = try JSONDecoder().decode([WebsiteCase].self, from: Data(contentsOf: directory.appendingPathComponent("websites.json")))
            for item in cases {
                try require(((try? checkedWebsiteURL(item.url)) != nil) == item.accepted, "网页地址校验与工具不一致")
            }
            let payload = Data(#"{"kind":"desktop_request","request_id":"request","workspace_scope_id":"scope","command":{"action":"open_url","url":"http://localhost:3000","include_image":false}}"#.utf8)
            guard case .desktopRequest(let request) = try JSONDecoder().decode(HostEvent.self, from: payload) else { throw ClientError.message("网页请求未解码") }
            try require(request.command.url == "http://localhost:3000" && request.command.action == "open_url", "网页打开丢失目标地址")
        } else if mode == "desktop" {
            let payload = Data(#"{"kind":"desktop_request","request_id":"request","workspace_scope_id":"scope","command":{"action":"apps","include_image":false}}"#.utf8)
            let event = try JSONDecoder().decode(HostEvent.self, from: payload)
            guard case .desktopRequest(let decoded) = event else { throw ClientError.message("桌面请求未解码") }
            try require(decoded.workspaceScopeID == "scope" && decoded.command.action == "apps", "桌面请求丢失执行作用域")
            let missingScope = Data(#"{"kind":"desktop_request","request_id":"request","command":{"action":"apps","include_image":false}}"#.utf8)
            try require((try? JSONDecoder().decode(HostEvent.self, from: missingScope)) == nil, "缺少执行作用域的桌面请求被接受")
            let desktop = DesktopAccess()
            for action in ["snapshot", "press", "set_value"] {
                let request = DesktopRequest(requestID: "request", workspaceScopeID: "scope",
                    command: DesktopCommand(action: action, application: "unknown", snapshotID: "unknown", elementID: 0, value: "test", includeImage: false))
                try require((try? desktop.review(request)) == nil, "未经观察的桌面目标被接受")
                do { _ = try await desktop.perform(request); throw ClientError.message("未经观察的动作被执行") }
                catch let error as ClientError { try require(error.localizedDescription.contains("失效"), "错误的桌面拒绝原因") }
            }
        } else {
            let events = AsyncStream.makeStream(of: HostEvent.self, bufferingPolicy: .bufferingOldest(16))
            var exits = 0
            let backend = BackendProcess(event: { events.continuation.yield($0) }, exited: { exits += 1; events.continuation.finish() })
            let state = mode == "bundle" ? URL(fileURLWithPath: arguments[3]) : directory
            let server = mode == "bundle" ? arguments[4] : "https://studio.example.com"
            let device = mode == "bundle" ? arguments[5] : "device"
            let configuration = HostConfiguration(server: server, deviceID: device, namespace: "ns_1",
                                                   credential: String(repeating: "b", count: 64), workspaceRoot: directory.appendingPathComponent("projects").path, permissions: WorkspacePermissions())
            let readiness = NativeReadiness(accessibility: false, screenCapture: false)
            if mode == "start-failure" {
                do {
                    try await backend.start(configuration: configuration, readiness: readiness, resources: directory.appendingPathComponent("missing"), state: directory)
                    throw ClientError.message("缺失启动器没有失败")
                } catch {
                    try require(!backend.isRunning, "启动失败遗留运行归属")
                }
            }
            try await backend.start(configuration: configuration, readiness: readiness, resources: directory, state: state)
            var online = false, failure = false, stopped = false
            for await event in events.stream {
                switch event {
                case .state("online"):
                    online = true
                    var rejected = false
                    do {
                        try await backend.start(configuration: configuration, readiness: readiness, resources: directory, state: state)
                    } catch { rejected = true }
                    try require(rejected && backend.isRunning, "并发启动破坏原宿主")
                    async let first: Void = backend.stopAndWait()
                    async let second: Void = backend.stopAndWait()
                    await first; await second
                case .state("stopped"): stopped = true
                case .failure: failure = true
                default: break
                }
            }
            try require(!backend.isRunning && exits == 1, "宿主退出没有完整结算")
            if mode == "early-exit" { try require(stopped && !online, "丢失提前退出的最后状态") }
            else if mode == "bundle" { try require(online && !failure, "随包宿主未正常连接或退出") }
            else if mode == "malformed" { try require(failure, "无效通信没有报错") }
            else { try require(online && stopped && !failure, "正常宿主没有完整启动和停止") }
        }
        print("verified \(mode)")
    }
}
