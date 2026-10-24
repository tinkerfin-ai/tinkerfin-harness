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
    let previousAccount: String?
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
        if mode == "model-startup-cancel" || mode == "model-accepted-cancel" {
            try await modelCallerCancellationContract(directory, startup: mode == "model-startup-cancel")
        } else if mode == "startup-queued-restore" || mode == "startup-queued-connect" {
            try await queuedStartupCancellationContract(directory, restore: mode == "startup-queued-restore")
        } else if mode == "client-installation" {
            try require(ClientInstallation.accepts(URL(fileURLWithPath: "/Applications/TinkerFin.app", isDirectory: true)), "正式安装位置被拒绝")
            for path in ["/Applications/TinkerFin 2.app", directory.appendingPathComponent("TinkerFin.app").path] {
                try require(!ClientInstallation.accepts(URL(fileURLWithPath: path)), "下载副本被允许作为独立客户端启动")
            }
        } else if mode == "client-instance-hold" || mode == "client-instance-probe" {
            do {
                let lock = try ClientInstanceLock(file: directory.appendingPathComponent("client.lock"))
                if mode == "client-instance-hold" { try await feedbackCheckpoint("client-active") }
                withExtendedLifetime(lock) {}
            } catch ApplicationLockError.alreadyRunning { print("already-running"); return }
        } else if mode == "initialization-recovery" {
            let state = directory.appendingPathComponent("state")
            try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
            try Data("invalid".utf8).write(to: state.appendingPathComponent("settings.json"))
            let model = CompanionModel(state: state, resources: directory)
            do {
                do { try await model.restore(); throw ClientError.message("损坏配置被接受") }
                catch is DecodingError { }
                let snapshot = DesktopPresentation(model: model).snapshot(surface: .workbench)
                try require(!snapshot.ready && snapshot.connection == "offline" && snapshot.connectionError == "请检查本机配置后重试", "配置恢复没有提供现有重试入口")
                try await ConfigurationStore(directory: state).save(SavedConfiguration())
                try await model.retry()
                try require(model.ready && model.connection == "unpaired", "修复配置后未恢复连接入口")
                let duplicate = CompanionModel(state: state, resources: directory)
                do { try await duplicate.restore(); throw ClientError.message("重复实例被接受") }
                catch ApplicationLockError.alreadyRunning { }
                await duplicate.shutdown(); await model.shutdown()
            } catch { await model.shutdown(); throw error }
        } else if mode == "update-completion" {
            let state = directory.appendingPathComponent("state")
            var saved = SavedConfiguration()
            saved.keychainAccount = UUID().uuidString.lowercased()
            saved.connection = ConnectionConfiguration(studioURL: "http://127.0.0.1:1", apiURL: "http://127.0.0.1:1")
            saved.device = PairedDevice(deviceID: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", namespace: "ns_test", name: "Contract Mac")
            try await ConfigurationStore(directory: state).save(saved)
            let model = CompanionModel(state: state, resources: directory)
            let presentation = DesktopPresentation(model: model)
            let updater = DesktopUpdateController(presentation: presentation, resources: directory)
            do {
                try await model.restore(connect: false)
                let configuration = try Data(contentsOf: state.appendingPathComponent("settings.json"))
                presentation.update = DesktopUpdateState(phase: .success)
                let reply = await updater.handle(.updateCancel)
                try require(reply.ok && presentation.update.phase == .idle, "离线更新完成确认没有关闭弹窗")
                try require(model.connection == "offline", "更新完成确认触发了重新连接")
                try require(try Data(contentsOf: state.appendingPathComponent("settings.json")) == configuration, "更新完成确认改变了已保存配置")
                await updater.close(); presentation.close(); await model.shutdown()
            } catch { await updater.close(); presentation.close(); await model.shutdown(); throw error }
        } else if mode == "update-workspace" {
            try await updateWorkspaceContract(directory)
        } else if mode == "update-installation" {
            try await updateInstallationContract(directory)
        } else if mode == "update-subprocess" {
            let runner = UpdateSubprocess()
            try await runner.run(executable: URL(fileURLWithPath: "/usr/bin/true"), arguments: [])
            do {
                try await runner.run(executable: URL(fileURLWithPath: "/usr/bin/false"), arguments: [])
                throw ClientError.message("重复执行复用了旧退出码")
            } catch UpdateError.failedProcess { }
            let running = Task { try await runner.run(executable: directory.appendingPathComponent("child"), arguments: []) }
            try await feedbackCheckpoint("update-child-started")
            running.cancel()
            do { try await running.value; throw ClientError.message("更新子进程取消未传播") }
            catch is CancellationError { }
            try await runner.run(executable: URL(fileURLWithPath: "/usr/bin/true"), arguments: [])
        } else if mode == "update-artifact" {
            try await updateArtifactContract(directory)
        } else if mode.hasPrefix("authorization-update-") {
            try await authorizationUpdateContract(directory, mode: mode)
        } else if mode == "credential-cleanup" {
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
            if let previous = fixture.previousAccount {
                try require(UUID(uuidString: previous) != nil, "测试旧凭证身份无效")
                try await KeychainStore().remove(account: previous)
            }
        } else if ["retry-feedback", "authorization-feedback", "authorization-replacement", "authorization-pending-reconnect"].contains(mode) {
            let fixture = try JSONDecoder().decode(ConnectionFeedbackFixture.self,
                from: Data(contentsOf: directory.appendingPathComponent("feedback.json")))
            let resources = directory.appendingPathComponent("resources")
            try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
            let state = directory.appendingPathComponent("state")
            let credentialStore = KeychainStore()
            _ = try await credentialStore.create(account: fixture.account)
            var saved = SavedConfiguration()
            saved.name = "Contract Mac"
            if mode != "authorization-feedback" {
                saved.connection = ConnectionConfiguration(studioURL: fixture.server, apiURL: fixture.server)
                saved.keychainAccount = fixture.previousAccount ?? fixture.account
                saved.device = PairedDevice(deviceID: fixture.previousAccount != nil
                    ? "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" : "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    namespace: "ns_1", name: saved.name)
            }
            if mode != "retry-feedback" {
                saved.pending = PendingConnection(account: fixture.account, connection: ConnectionConfiguration(studioURL: fixture.server, apiURL: fixture.server),
                    authorization: AuthorizationCreated(authorizationID: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        browserToken: String(repeating: "b", count: 64), expiresAt: "2100-01-01T00:00:00Z"))
            }
            if mode == "authorization-replacement" || mode == "authorization-pending-reconnect" {
                guard let previous = fixture.previousAccount else { throw ClientError.message("测试缺少旧凭证身份") }
                _ = try await credentialStore.create(account: previous)
                if fixture.result == "cleanup-failure" { saved.retiredAccounts = [fixture.account] }
            }
            try await ConfigurationStore(directory: state).save(saved)
            let model = CompanionModel(state: state, resources: resources)
            let presentation = DesktopPresentation(model: model)
            let states = AsyncStream.makeStream(of: String.self, bufferingPolicy: .bufferingNewest(16))
            let authorization = AsyncStream.makeStream(of: Bool.self, bufferingPolicy: .bufferingNewest(8))
            let subscriptions = [
                model.$status.sink { states.continuation.yield($0) },
                model.$authorizationWaiting.sink { authorization.continuation.yield($0) },
            ]
            defer {
                for subscription in subscriptions { subscription.cancel() }
                states.continuation.finish(); authorization.continuation.finish()
            }
            var stateUpdates = states.stream.makeAsyncIterator()
            var authorizationUpdates = authorization.stream.makeAsyncIterator()
            var cancellation: Task<DesktopReply, Never>?
            var attentionCount = 0
            model.attention = { attentionCount += 1 }
            do {
                try await model.restore(connect: mode == "retry-feedback" || mode == "authorization-feedback")
                if mode == "retry-feedback" {
                    try require(presentation.snapshot(surface: .workbench).connection == "connecting",
                        "宿主启动后尚未收到连接结果，却结束了连接反馈")
                    try await feedbackCheckpoint("initial-started")
                    while model.status != "offline" { _ = await stateUpdates.next() }
                    try require(await presentation.handle(.retry).ok, "重试请求未接纳")
                    try require(!model.busy && model.retrying && model.connection == "connecting"
                        && presentation.snapshot(surface: .workbench).busy == "retry",
                        "宿主刚启动或旧宿主停止时提前结束了重试反馈")
                    let progress = AsyncStream.makeStream(of: String.self, bufferingPolicy: .bufferingNewest(1))
                    let progressSubscription = model.$status.dropFirst().sink { progress.continuation.yield($0) }
                    defer { progressSubscription.cancel(); progress.continuation.finish() }
                    var progressUpdates = progress.stream.makeAsyncIterator()
                    try await feedbackCheckpoint("retry-started")
                    try require(await progressUpdates.next() == "connecting", "本轮宿主未报告正在连接")
                    try require(model.retrying, "连接中的状态错误地结束了本轮重试")
                    try await feedbackCheckpoint("retry-result")
                    let expected = fixture.result == "failure" ? "failed" : fixture.result == "exit" ? "offline" : fixture.result
                    while model.status != expected { _ = await stateUpdates.next() }
                    try require(!model.retrying && presentation.snapshot(surface: .workbench).busy.isEmpty,
                        "真实连接结果没有结束本轮加载反馈")
                    let expectedConnection = fixture.result == "online" ? "online" : fixture.result == "unauthorized" ? "revoked" : "offline"
                    try require(presentation.snapshot(surface: .workbench).connection == expectedConnection,
                        "连接结果未向界面发布正确状态")
                } else if mode == "authorization-pending-reconnect" {
                    try await model.retry()
                    try await feedbackCheckpoint("pending-current-started")
                    while model.status != "online" { _ = await stateUpdates.next() }
                    model.resumeAuthorization()
                    try await feedbackCheckpoint("authorization-started")
                    try await feedbackCheckpoint("authorization-result")
                    while model.authorizationWaiting { _ = await authorizationUpdates.next() }
                    let failure = model.error
                    let message = presentation.snapshot(surface: .workbench).authorizationError
                    try require(failure != nil && !message.isEmpty, "待授权请求失败没有显示恢复原因")
                    try await feedbackCheckpoint("pending-current-offline")
                    while model.status != "offline" { _ = await stateUpdates.next() }
                    try await feedbackCheckpoint("pending-current-recovered")
                    while model.status != "online" { _ = await stateUpdates.next() }
                    let snapshot = presentation.snapshot(surface: .workbench)
                    try require(model.saved.keychainAccount == fixture.previousAccount
                        && model.saved.pending?.account == fixture.account
                        && model.saved.pending?.connection == saved.pending?.connection
                        && model.saved.pending?.authorization?.authorizationID == saved.pending?.authorization?.authorizationID,
                        "旧连接恢复改变了当前身份或待授权记录")
                    try require(snapshot.connected && snapshot.authorizationPhase == "waiting"
                        && model.error == failure && snapshot.authorizationError == message,
                        "旧连接恢复在线清除了另一次待授权请求的错误")
                } else if mode == "authorization-replacement" {
                    if fixture.result != "cleanup-failure" {
                        try await model.retry()
                        try await feedbackCheckpoint("replacement-started")
                        while model.status != "unauthorized" { _ = await stateUpdates.next() }
                    }
                    model.resumeAuthorization()
                    try await feedbackCheckpoint("authorization-started")
                    try require(model.authorizationWaiting, "替换授权回执尚未到达却结束了等待")
                    try await feedbackCheckpoint("authorization-result")
                    while model.authorizationWaiting { _ = await authorizationUpdates.next() }
                    let persisted = try await ConfigurationStore(directory: state).read()
                    try require(model.saved.pending == nil && model.saved.keychainAccount == fixture.account
                        && persisted.pending == nil && persisted.keychainAccount == fixture.account
                        && model.saved.device == persisted.device && model.saved.device?.deviceID != saved.device?.deviceID,
                        "授权提交后的失败丢失或回退了新身份")
                    try require(model.authorizationNotice.isEmpty, "新身份仍显示旧授权失效通知")
                    let snapshot = presentation.snapshot(surface: .workbench)
                    if fixture.result == "online" {
                        try require(model.error == nil && snapshot.connection == "connecting"
                            && snapshot.connectionReason == .authorization && !snapshot.connected,
                            "替换授权未等待新宿主在线回执")
                        try await feedbackCheckpoint("authorization-connected")
                        while model.status != "online" { _ = await stateUpdates.next() }
                        try require(presentation.snapshot(surface: .workbench).connected && model.error == nil,
                            "新宿主在线回执未恢复连接")
                    } else {
                        try require(model.status == "offline" && snapshot.connection == "offline" && !snapshot.connected,
                            "新身份提交后的本机失败仍显示旧授权失效")
                        let expected: ClientError = .message(fixture.result == "cleanup-failure"
                            ? "设备凭证清理记录无效" : "操作尚未完成，请重试")
                        try require(model.error == expected && snapshot.connectionError == model.localization.message(expected),
                            "清理或宿主启动失败被吞掉或未向界面保留真实原因")
                        if fixture.result == "cleanup-failure" {
                            let retry = await presentation.handle(.retry)
                            if retry.ok { try await feedbackCheckpoint("cleanup-retry-started") }
                            try require(!retry.ok && retry.message == model.localization.message(expected),
                                "重新连接绕过了仍非法的退休凭证集合")
                            let retried = presentation.snapshot(surface: .workbench)
                            let retained = try await ConfigurationStore(directory: state).read()
                            try require(model.status == "offline" && retried.connection == "offline" && !retried.connected
                                && model.error == expected && retried.connectionError == model.localization.message(expected),
                                "清理失败后的重试丢失了离线状态或真实错误")
                            try require(retained.keychainAccount == persisted.keychainAccount && retained.device == persisted.device
                                && retained.pending == nil && retained.retiredAccounts == persisted.retiredAccounts
                                && model.saved.keychainAccount == fixture.account,
                                "清理失败后的重试改变了已提交身份或退休记录")
                            await credentialStore.clear()
                            _ = try await credentialStore.read(account: fixture.account)
                            _ = try await credentialStore.read(account: fixture.previousAccount!)
                            try require(persisted.retiredAccounts.contains(fixture.account), "清理失败丢失了待处理记录")
                        }
                    }
                    try require(attentionCount == 0, "替换授权自动展开了客户端")
                } else {
                    try require(model.authorizationWaiting && presentation.snapshot(surface: .workbench).authorizationPhase == "waiting",
                        "浏览器授权等待没有保持当前阶段")
                    try await feedbackCheckpoint("authorization-started")
                    try require(model.authorizationWaiting, "授权HTTP尚未结算却结束了等待")
                    if fixture.result == "cancel-request" {
                        cancellation = Task { await presentation.handle(.cancelAuthorization) }
                        try await feedbackCheckpoint("authorization-cancelling")
                        try require(presentation.snapshot(surface: .workbench).authorizationPhase == "cancelling",
                            "取消请求尚未结算却恢复了授权动作")
                        try await feedbackCheckpoint("authorization-cancel-result")
                        try require(await cancellation!.value.ok, "授权取消未成功结算")
                    } else {
                        try await feedbackCheckpoint("authorization-result")
                        while model.authorizationWaiting { _ = await authorizationUpdates.next() }
                    }
                    try require(attentionCount == 0, "授权结算自动展开了客户端")
                    if fixture.result == "authorized" {
                        let snapshot = presentation.snapshot(surface: .workbench)
                        try require(model.saved.pending == nil && model.saved.device != nil
                            && snapshot.connection == "connecting" && snapshot.connectionReason == .authorization && !snapshot.connected,
                            "授权回执被误当作服务已经在线")
                        try await feedbackCheckpoint("authorization-connected")
                        while model.status != "online" { _ = await stateUpdates.next() }
                        try require(presentation.snapshot(surface: .workbench).connected && attentionCount == 0,
                            "连接成功未发布在线状态或自动展开了客户端")
                    } else if fixture.result == "error" {
                        try require(model.saved.pending != nil && !model.authorizationWaiting
                            && !presentation.snapshot(surface: .workbench).authorizationError.isEmpty,
                            "授权轮询失败丢失了待授权记录或恢复信息")
                        model.resumeAuthorization()
                        try require(model.authorizationWaiting, "恢复授权没有重新进入等待")
                        try await feedbackCheckpoint("authorization-recovery")
                        while model.authorizationWaiting { _ = await authorizationUpdates.next() }
                        try require(model.saved.pending == nil && attentionCount == 0,
                            "恢复后授权终态没有结束等待或自动展开客户端")
                    } else {
                        try require(model.saved.pending == nil && presentation.snapshot(surface: .workbench).authorizationPhase == "idle",
                            "授权终态未结束等待")
                    }
                }
                presentation.close(); await model.shutdown()
                try await credentialStore.remove(account: fixture.account)
                if let previous = fixture.previousAccount { try await credentialStore.remove(account: previous) }
            } catch {
                cancellation?.cancel(); _ = await cancellation?.value
                presentation.close(); await model.shutdown()
                try await credentialStore.remove(account: fixture.account)
                if let previous = fixture.previousAccount { try await credentialStore.remove(account: previous) }
                throw error
            }
        } else if mode == "revocation-success" || mode == "revocation-failure" {
            let fixture = try JSONDecoder().decode(ConnectionFeedbackFixture.self,
                from: Data(contentsOf: directory.appendingPathComponent("feedback.json")))
            let credentials = KeychainStore()
            _ = try await credentials.create(account: fixture.account)
            let state = directory.appendingPathComponent("state")
            var saved = SavedConfiguration()
            saved.name = "Contract Mac"; saved.connection = ConnectionConfiguration(studioURL: fixture.server, apiURL: fixture.server)
            saved.device = PairedDevice(deviceID: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", namespace: "ns_1", name: saved.name)
            saved.keychainAccount = fixture.account
            try await ConfigurationStore(directory: state).save(saved)
            let model = CompanionModel(state: state, resources: directory.appendingPathComponent("resources"))
            let presentation = DesktopPresentation(model: model)
            let states = AsyncStream.makeStream(of: String.self, bufferingPolicy: .bufferingNewest(16))
            let subscription = model.$status.sink { states.continuation.yield($0) }
            var updates = states.stream.makeAsyncIterator()
            var revocation: Task<DesktopReply, Never>?
            do {
                try await model.restore()
                while model.status != "online" { _ = await updates.next() }
                revocation = Task { await presentation.handle(.logout) }
                try await feedbackCheckpoint("revocation-pending")
                try require(model.saved.device == saved.device && model.status == "online"
                    && presentation.snapshot(surface: .workbench).busy == "logout", "撤销回应前改变了原身份或连接")
                try await feedbackCheckpoint("revocation-result")
                let result = await revocation!.value
                let persisted = try await ConfigurationStore(directory: state).read()
                try require(persisted.connection == saved.connection && persisted.name == saved.name && persisted.permissions == saved.permissions,
                    "设备自撤销改变了地址、名称或系统工具偏好")
                if mode == "revocation-success" {
                    try require(result.ok && model.saved.device == nil && model.status == "unpaired"
                        && persisted.device == nil && persisted.keychainAccount == nil && persisted.retiredAccounts.isEmpty,
                        "设备撤销成功后未结清本机身份")
                    var removed = false
                    do { _ = try await KeychainStore().read(account: fixture.account) } catch ClientError.credentialUnavailable { removed = true }
                    try require(removed, "设备撤销成功后钥匙串凭证仍存在")
                } else {
                    try require(!result.ok && model.saved.device == saved.device && model.status == "online"
                        && persisted.device == saved.device && persisted.keychainAccount == fixture.account,
                        "撤销请求失败改变了原身份或连接")
                }
                presentation.close(); await model.shutdown()
                try await credentials.remove(account: fixture.account)
            } catch {
                revocation?.cancel(); _ = await revocation?.value
                presentation.close(); await model.shutdown()
                try await credentials.remove(account: fixture.account)
                throw error
            }
            subscription.cancel(); states.continuation.finish()
        } else if mode == "service-cancel" {
            let request = Task { try await ServiceDiscovery().resolve(studio: arguments[2], api: "") }
            try await feedbackCheckpoint("discovery-request")
            request.cancel()
            do { _ = try await request.value; throw ClientError.message("发现请求未保持取消语义") }
            catch is CancellationError { }
        } else if mode == "service-discovery" || mode == "service-manual" {
            do {
                let value = try await ServiceDiscovery().resolve(studio: arguments[2],
                    api: mode == "service-manual" ? "https://api.example.com/root/" : "")
                print(String(decoding: try JSONEncoder().encode(value), as: UTF8.self))
            } catch {
                print((error as? ClientError)?.errorDescription ?? "unexpected-error")
            }
            return
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
            configuration.connection = ConnectionConfiguration(studioURL: "https://studio.example.com", apiURL: "https://api.example.com")
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
            configuration.acceptAuthorization(owner, at: configuration.connection!)
            configuration.acceptAuthorization(owner, at: configuration.connection!)
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
        } else if mode == "revocation" {
            do {
                try await AuthorizationClient().revoke(server: checkedServer(arguments[2]),
                    deviceID: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", credential: String(repeating: "b", count: 64))
                print("revoked")
            } catch {
                print((error as? ClientError)?.errorDescription ?? "撤销请求失败")
            }
            return
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
            let state = directory.appendingPathComponent("state")
            var saved = SavedConfiguration()
            saved.connection = ConnectionConfiguration(studioURL: "https://studio.test", apiURL: "https://studio.test"); saved.name = "Contract Mac"; saved.keychainAccount = account
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
            try require(settled.frame == CGRect(x: 644, y: 712, width: 312, height: 230),
                "状态栏窗口落位后没有恢复完整尺寸与边缘对齐")
            for screen in [CGRect(x: 0, y: 40, width: 1512, height: 904), CGRect(x: -1440, y: -900, width: 1440, height: 875)] {
                for center in [screen.minX + 40, screen.midX, screen.maxX - 40] {
                    let anchor = CGRect(x: center - 10, y: screen.maxY, width: 20, height: 24)
                    for height in [282.0, 1200.0] {
                        guard let geometry = PanelGeometry(anchor: anchor, screen: screen, contentHeight: height) else {
                            throw ClientError.message("有效菜单栏位置未生成浮层")
                        }
                        try require(geometry.frame.width == 312 && screen.contains(geometry.frame), "浮层越过当前屏幕或未使用约定宽度")
                        try require(geometry.frame.maxY == anchor.minY - 2, "浮层没有贴住菜单栏下边缘")
                        try require(geometry.frame.height == min(height, geometry.maximumContentHeight), "浮层仍包含正文以外的高度")
                        if center <= screen.midX { try require(geometry.frame.minX == anchor.minX, "右侧空间充足时没有按左边对齐") }
                        else { try require(geometry.frame.maxX == anchor.maxX, "左侧空间充足时没有按右边对齐") }
                    }
                }
            }
        } else if mode == "presentation-drafts" {
            let resources = directory.appendingPathComponent("resources")
            try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
            let state = directory.appendingPathComponent("state")
            var saved = SavedConfiguration(); saved.name = "Contract Mac"
            try await ConfigurationStore(directory: state).save(saved)
            let model = CompanionModel(state: state, resources: resources)
            let presentation = DesktopPresentation(model: model)
            do {
                try await model.restore()
                let draft = DesktopViewState(page: .account, connectionForm: true, service: "https://example.test",
                    api: "", manualAPI: false, editingName: true, name: "尚未提交")
                try presentation.preserve(draft)
                let snapshot = presentation.snapshot(surface: .workbench)
                try require(snapshot.resume == draft && snapshot.name == "Contract Mac", "界面草稿覆盖了保存值")
                let payload = try JSONSerialization.jsonObject(with: JSONEncoder().encode(snapshot)) as! [String: Any]
                try require(payload["device"] == nil && payload["credential"] == nil && payload["keychainAccount"] == nil,
                    "界面快照泄漏原生身份存储")
                try require(!(await presentation.handle(.saveName("不应保存"))).ok, "未授权状态接受了隐藏页面的名称修改")
                await presentation.prepareToQuit()
                try require(try await ConfigurationStore(directory: state).read().name == "Contract Mac",
                    "退出客户端隐式保存了未提交草稿")
                presentation.close(); await model.shutdown()
            } catch { presentation.close(); await model.shutdown(); throw error }
        } else if mode == "converted-configuration" {
            let saved = try await ConfigurationStore(directory: directory).read()
            try require(saved.name == "Contract Mac" && saved.permissions.network == .normal && saved.permissions.allowedDomains.isEmpty,
                "转换后的配置无法完整读取")
            try require(saved.connection == ConnectionConfiguration(studioURL: "https://studio.example.com", apiURL: "https://api.example.com/root"),
                "转换后的服务地址不完整")
            try require(saved.pending?.connection == saved.connection, "转换后的待授权服务归属变化")
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
            let state = directory.appendingPathComponent("state")
            var saved = SavedConfiguration()
            saved.name = "Contract Mac"
            saved.pending = PendingConnection(account: UUID().uuidString.lowercased(), connection: ConnectionConfiguration(studioURL: "https://studio.test", apiURL: "https://studio.test"),
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
        } else if mode.hasPrefix("maintenance-") {
            let events = AsyncStream.makeStream(of: String.self, bufferingPolicy: .bufferingOldest(8))
            let backend = BackendProcess(event: { event in
                if case .state(let state) = event { events.continuation.yield(state) }
            }, exited: { events.continuation.finish() })
            var prepared: Task<Void, Error>?
            do {
                try await backend.start(configuration: HostConfiguration(server: "https://studio.example.com", deviceID: "device", namespace: "ns_1",
                    credential: String(repeating: "b", count: 64), workspaceRoot: directory.path, permissions: WorkspacePermissions()),
                    readiness: NativeReadiness(accessibility: false, screenCapture: false), resources: directory, state: directory)
                var updates = events.stream.makeAsyncIterator()
                try require(await updates.next() == "online", "维护测试宿主没有就绪")
                if mode == "maintenance-wait-cancel" {
                    prepared = Task { try await backend.prepareForUpdate() }
                    try require(await updates.next() == "connecting", "维护请求没有到达宿主")
                    prepared!.cancel()
                    do { try await prepared!.value; throw ClientError.message("取消没有传播") }
                    catch is CancellationError { }
                    try require(backend.isRunning, "取消更新等待关闭了原连接")
                } else if mode == "maintenance-failure" || mode == "maintenance-exit" {
                    var rejected = false
                    do { try await backend.prepareForUpdate() } catch { rejected = true }
                    try require(rejected, "维护故障没有结束等待")
                } else {
                    try await backend.prepareForUpdate()
                    try require(backend.isRunning, "就绪回执被误当作进程已经退出")
                    if mode == "maintenance-commit" {
                        try await backend.commitUpdatePreparation()
                        try require(!backend.isRunning, "安装提交未等待宿主真正退出")
                    } else {
                        try await backend.cancelUpdatePreparation()
                        try require(backend.isRunning, "取消更新关闭了原连接")
                        try await backend.prepareForUpdate()
                        try await backend.cancelUpdatePreparation()
                    }
                }
                await backend.stopAndWait()
            } catch {
                prepared?.cancel(); _ = try? await prepared?.value
                await backend.stopAndWait()
                throw error
            }
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

@MainActor
private func modelCallerCancellationContract(_ directory: URL, startup: Bool) async throws {
    let model = CompanionModel(state: directory.appendingPathComponent("state"), resources: directory)
    let server = try checkedServer(String(contentsOf: directory.appendingPathComponent("server.txt"), encoding: .utf8))
    var completed = false
    let request = Task {
        try await model.run(cancelWithCaller: startup) {
            _ = try await AuthorizationClient().projects(server: server, token: "contract-token")
            completed = true
        }
    }
    do {
        try await feedbackCheckpoint("request-started")
        request.cancel()
        try await feedbackCheckpoint("caller-cancelled")
        if startup {
            do { try await request.value; throw ClientError.message("启动请求未传播取消") }
            catch is CancellationError { }
            try require(!completed && model.error == nil, "取消的启动请求被保存为业务错误")
        } else {
            try await request.value
            try require(completed, "已接受操作被调用者取消")
        }
        try require(!model.busy, "已结算操作仍占用模型")
        await model.shutdown()
    } catch {
        request.cancel(); await model.shutdown(); _ = try? await request.value
        throw error
    }
}

@MainActor
private func queuedStartupCancellationContract(_ directory: URL, restore: Bool) async throws {
    let model = CompanionModel(state: directory.appendingPathComponent("state"), resources: directory)
    let acceptedEntered = AsyncStream.makeStream(of: Void.self, bufferingPolicy: .bufferingNewest(1))
    let startupEntered = AsyncStream.makeStream(of: Void.self, bufferingPolicy: .bufferingNewest(1))
    let release = AsyncStream.makeStream(of: Void.self, bufferingPolicy: .bufferingNewest(1))
    defer { acceptedEntered.continuation.finish(); startupEntered.continuation.finish(); release.continuation.finish() }
    var committed = false
    let accepted = Task {
        try await model.run {
            acceptedEntered.continuation.yield(())
            for await _ in release.stream { break }
            try Task.checkCancellation()
            committed = true
        }
    }
    for await _ in acceptedEntered.stream { break }
    let startup = Task {
        startupEntered.continuation.yield(())
        if restore { try await model.restore(connect: false) }
        else { try await model.connectRestoredState() }
    }
    for await _ in startupEntered.stream { break }
    startup.cancel()
    release.continuation.yield(())
    do {
        try await accepted.value
        do { try await startup.value; throw ClientError.message("排队启动在取消后仍然执行") }
        catch is CancellationError { }
        try require(committed && !model.ready && !model.busy, "启动取消影响了先前提交或仍恢复了模型")
        try require(!FileManager.default.fileExists(atPath: directory.appendingPathComponent("state").path), "已取消启动仍创建状态目录")
        await model.shutdown()
    } catch {
        accepted.cancel(); startup.cancel(); await model.shutdown()
        _ = try? await accepted.value; _ = try? await startup.value
        throw error
    }
}

@MainActor
func updateWorkspaceContract(_ input: URL) async throws {
    let base = input.resolvingSymlinksInPath(), manager = FileManager.default
    for hint in [false, true] {
        let root = base.appendingPathComponent(hint ? "new-directory" : "new-path", isDirectory: hint)
        let first = try UpdateWorkspace.create(root: root)
        try UpdateWorkspace.checkPrivate(root)
        try UpdateWorkspace.checkPrivate(first)
        let second = try UpdateWorkspace.create(root: root)
        try require(first != second, "重复下载共享了暂存目录")
        let retained = root.appendingPathComponent("retained.txt")
        try Data("retained".utf8).write(to: retained)
        try UpdateWorkspace.remove(first, root: root)
        try require(manager.fileExists(atPath: second.path) && manager.fileExists(atPath: retained.path), "清理删除了其他下载或用户文件")
        try UpdateWorkspace.remove(second, root: root)
    }
    let privateRoot = base.appendingPathComponent("private", isDirectory: true)
    try manager.createDirectory(at: privateRoot, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    let receipt = privateRoot.appendingPathComponent("receipt.json")
    for value in [["first"], ["replacement"]] {
        try UpdateWorkspace.write(value, to: receipt)
        try require(try JSONDecoder().decode([String].self, from: UpdateWorkspace.read(receipt, limit: 4096)) == value, "更新回执未完整发布")
        let attributes = try manager.attributesOfItem(atPath: receipt.path)
        try require((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600, "更新回执发布时权限不正确")
    }
    let blocked = privateRoot.appendingPathComponent("retained", isDirectory: true)
    try manager.createDirectory(at: blocked, withIntermediateDirectories: false)
    let retained = blocked.appendingPathComponent("user.txt")
    try Data("retained".utf8).write(to: retained)
    let entries = try manager.contentsOfDirectory(atPath: privateRoot.path).sorted()
    do { try UpdateWorkspace.write(["rejected"], to: blocked); throw ClientError.message("更新回执覆盖了目录") }
    catch UpdateError.invalidLocation { }
    try require(try manager.contentsOfDirectory(atPath: privateRoot.path).sorted() == entries, "回执失败遗留了临时文件")
    try require(try Data(contentsOf: retained) == Data("retained".utf8), "回执失败改动了已有目录")
    let link = base.appendingPathComponent("link", isDirectory: true)
    try manager.createSymbolicLink(at: link, withDestinationURL: privateRoot)
    for root in [link, link.appendingPathComponent("updates", isDirectory: true)] {
        do { _ = try UpdateWorkspace.create(root: root); throw ClientError.message("更新暂存目录接受符号链接") }
        catch UpdateError.invalidLocation { }
    }
    let shared = base.appendingPathComponent("shared", isDirectory: true)
    try manager.createDirectory(at: shared, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o755])
    do { _ = try UpdateWorkspace.create(root: shared); throw ClientError.message("更新暂存目录允许其他用户访问") }
    catch UpdateError.invalidLocation { }
    try require(try manager.contentsOfDirectory(atPath: shared.path).isEmpty, "权限拒绝仍产生下载目录")
    let file = base.appendingPathComponent("not-a-directory")
    try Data("retained".utf8).write(to: file)
    do { _ = try UpdateWorkspace.create(root: file); throw ClientError.message("更新暂存目录接受普通文件") }
    catch is CocoaError { }
    try require(try Data(contentsOf: file) == Data("retained".utf8), "路径拒绝覆盖了原文件")

    let cases: [(Error, DesktopUpdateFailure)] = [
        (UpdateError.invalidLocation, .storage), (CocoaError(.fileWriteOutOfSpace), .storage),
        (NSError(domain: NSPOSIXErrorDomain, code: Int(EACCES)), .storage),
        (URLError(.notConnectedToInternet), .network), (UpdateError.invalidManifest, .release),
        (UpdateError.unavailable, .release), (UpdateError.failedProcess, .unavailable),
    ]
    for (error, expected) in cases { try require(DesktopUpdateFailure(error: error) == expected, "更新失败原因误导恢复操作") }
    let work = Task { try await UpdateFileWork().perform { cancellation in try cancellation.check(); return 1 } }
    work.cancel()
    do { _ = try await work.value; throw ClientError.message("已取消的文件准备仍然执行") }
    catch is CancellationError { }
}


private struct AuthorizationUpdateFixture: Decodable {
    let server: String
    let oldAccount: String
    let newAccount: String
}

/// HTTP回执与旧宿主退出由父进程放行，更新不能提交尚在交接的新身份
@MainActor
private func authorizationUpdateContract(_ directory: URL, mode: String) async throws {
    let fixture = try JSONDecoder().decode(AuthorizationUpdateFixture.self, from: Data(contentsOf: directory.appendingPathComponent("fixture.json")))
    let keychain = KeychainStore()
    if mode == "authorization-update-cleanup" {
        try await keychain.remove(account: fixture.oldAccount)
        try await keychain.remove(account: fixture.newAccount)
        return
    }
    _ = try await keychain.create(account: fixture.oldAccount)
    _ = try await keychain.create(account: fixture.newAccount)
    let state = directory.appendingPathComponent("state")
    let configuration = ConnectionConfiguration(studioURL: fixture.server, apiURL: fixture.server)
    var saved = SavedConfiguration()
    saved.name = "Contract Mac"; saved.connection = configuration
    saved.device = PairedDevice(deviceID: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", namespace: "ns_1", name: saved.name)
    saved.keychainAccount = fixture.oldAccount
    saved.pending = PendingConnection(account: fixture.newAccount, connection: configuration,
        authorization: AuthorizationCreated(authorizationID: "cccccccc-cccc-cccc-cccc-cccccccccccc", browserToken: String(repeating: "b", count: 64), expiresAt: "2100-01-01T00:00:00Z"))
    try await ConfigurationStore(directory: state).save(saved)
    let model = CompanionModel(state: state, resources: directory.appendingPathComponent("resources"))
    let changes = AsyncStream.makeStream(of: String.self, bufferingPolicy: .bufferingNewest(16))
    let subscription = model.$status.sink { changes.continuation.yield($0) }
    var iterator = changes.stream.makeAsyncIterator()
    do {
        try await model.restore(); try await model.retry()
        while model.status != "online" { _ = await iterator.next() }
        if mode == "authorization-update-check" { try model.beginUpdateSession() }
        try await feedbackCheckpoint("old-online")
        if mode == "authorization-update-stop" {
            while model.status != "offline" { _ = await iterator.next() }
        }
        let release = Task { @MainActor in try await feedbackCheckpoint("maintenance-entered") }
        try await model.prepareForUpdate()
        try await release.value
        try require(model.saved.device?.deviceID == saved.device?.deviceID, "更新准备提交了尚未交接的新身份")
        try require(model.saved.pending == nil, "安装准备保留了待授权请求")
        let persisted = try await ConfigurationStore(directory: state).read()
        try require(persisted.device == saved.device && persisted.pending == nil, "更新准备的持久化身份不一致")
        await keychain.clear()
        try require((try? await keychain.read(account: fixture.newAccount)) == nil, "更新准备保留了放弃授权的凭证")
        try await model.resumeAfterUpdate()
        while model.status != "online" { _ = await iterator.next() }
        await model.shutdown()
    } catch { await model.shutdown(); throw error }
    subscription.cancel(); changes.continuation.finish()
}


private struct UpdateArtifactFixture: Decodable {
    struct Case: Decodable { let manifest: Data; let signature: Data; let valid: Bool }
    let publicKey: Data
    let cases: [Case]
    let tree: String
    let app: String
}

@MainActor
private func updateArtifactContract(_ directory: URL) async throws {
    let fixture = try JSONDecoder().decode(UpdateArtifactFixture.self, from: Data(contentsOf: directory.appendingPathComponent("update-contract.json")))
    for value in fixture.cases {
        let accepted = (try? DesktopRelease.verify(manifest: value.manifest, signature: value.signature, publicKey: fixture.publicKey)) != nil
        try require(accepted == value.valid, "更新清单签名或应用身份校验错误")
    }
    let digest = try await UpdateFileWork().perform { cancellation in
        try UpdateTree.digest(URL(fileURLWithPath: fixture.app), cancellation: cancellation)
    }
    try require(digest == fixture.tree, "原生文件树摘要与发布工具不一致")
    try require(try DesktopReleaseVersion("0.1.10") > DesktopReleaseVersion("0.1.9"), "发行版本排序错误")
    try require(!UpdateTransfer.permitted(URL(string: "http://github.com/download")!), "更新允许不加密传输")
    try require(!UpdateTransfer.permitted(URL(string: "https://github.com.evil.test/download")!), "更新允许外部重定向")
}


private struct UpdateInstallationFixture: Decodable, Sendable {
    let handoff: UpdateHandoff
    let publicKey: Data
    let manifest: Data
    let signature: Data
    let cancelled: Bool
    let installed: Bool
    let interrupt: Bool
}

@MainActor
private func updateInstallationContract(_ directory: URL) async throws {
    let fixture = try JSONDecoder().decode(UpdateInstallationFixture.self, from: Data(contentsOf: directory.appendingPathComponent("installation.json")))
    try require((try? UpdateWorkspace.checkInstallationLocation(fixture.handoff.target, state: fixture.handoff.target.appendingPathComponent("state"))) == nil,
        "更新允许将用户状态目录随应用替换")
    do {
        try await UpdateFileWork().perform { cancellation in
            let handoff = try fixture.handoff.checked(in: fixture.handoff.directory)
            let release = try DesktopRelease.verify(manifest: fixture.manifest, signature: fixture.signature, publicKey: fixture.publicKey)
            if fixture.cancelled { cancellation.cancel() }
            let lock = try UpdateTargetLock(application: handoff.target)
            try UpdateInstallation.install(handoff, release: release, lock: lock, cancellation: cancellation) { percent in
                if fixture.interrupt && percent == 65 { _exit(41) }
            }
        }
        try require(fixture.installed, "更新本应拒绝不安全安装")
    } catch {
        if fixture.installed { throw error }
    }
    try UpdateInstallation.checkIdentity(fixture.handoff.target, version: fixture.installed ? "0.1.2" : "0.1.1")
    if fixture.installed {
        let source = try UpdateInstallation.retainedApplication(fixture.handoff)
        try UpdateInstallation.checkIdentity(source, version: fixture.handoff.currentVersion)
        try UpdateWorkspace.write(fixture.handoff, to: fixture.handoff.directory.appendingPathComponent("handoff.json"))
        try UpdateWorkspace.remove(fixture.handoff.directory, root: fixture.handoff.directory.deletingLastPathComponent())
    }
}
