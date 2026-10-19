import Darwin
import Foundation

struct MCPServerSelection: Codable, Identifiable, Equatable, Sendable {
    var serverID: String
    var label: String
    var command: [String]
    var id: String { serverID }
    enum CodingKeys: String, CodingKey {
        case serverID = "server_id"
        case label, command
    }
}

struct WorkspacePermissions: Codable, Equatable, Sendable {
    enum Network: String, Codable, Sendable { case normal, restricted }
    var network: Network = .normal
    var allowedDomains: [String] = []
    var browser = true
    var desktop = true
    var mcpServers: [MCPServerSelection] = []
    enum CodingKeys: String, CodingKey {
        case allowedDomains = "allowed_domains", mcpServers = "mcp_servers"
        case network, browser, desktop
    }
}

extension WorkspacePermissions {
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        network = try values.decode(Network.self, forKey: .network)
        allowedDomains = try checkedDomains(values.decode([String].self, forKey: .allowedDomains))
        browser = try values.decode(Bool.self, forKey: .browser)
        desktop = try values.decode(Bool.self, forKey: .desktop)
        mcpServers = try values.decode([MCPServerSelection].self, forKey: .mcpServers)
        guard network == .restricted || allowedDomains.isEmpty else {
            throw DecodingError.dataCorruptedError(forKey: .allowedDomains, in: values,
                                                  debugDescription: "正常联网不能设置域名列表")
        }
    }

    func encode(to encoder: Encoder) throws {
        guard network == .restricted || allowedDomains.isEmpty else {
            throw EncodingError.invalidValue(allowedDomains, .init(codingPath: encoder.codingPath,
                                                                  debugDescription: "正常联网不能设置域名列表"))
        }
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(network, forKey: .network)
        try values.encode(checkedDomains(allowedDomains), forKey: .allowedDomains)
        try values.encode(browser, forKey: .browser)
        try values.encode(desktop, forKey: .desktop)
        try values.encode(mcpServers, forKey: .mcpServers)
    }
}

struct PairedDevice: Codable, Equatable, Sendable {
    let deviceID: String
    let namespace: String
    var name: String
    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case namespace, name
    }
}

struct SavedConfiguration: Codable, Sendable {
    var server = ""
    var name = ""
    var pending: PendingConnection?
    var keychainAccount: String?
    var device: PairedDevice?
    var permissions = WorkspacePermissions()
    var retiredAccounts: [String] = []

    mutating func acceptAuthorization(_ device: PairedDevice, at server: String) {
        self.device = device
        self.server = server
    }
}

struct HostConfiguration: Encodable, Sendable {
    let server: String
    let deviceID: String
    let namespace: String
    let credential: String
    let workspaceRoot: String
    let permissions: WorkspacePermissions
    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id", workspaceRoot = "workspace_root"
        case server, namespace, credential, permissions
    }
}

struct InitializeMessage: Encodable, Sendable {
    let kind = "initialize"
    let configuration: HostConfiguration
    let readiness: NativeReadiness
}

struct NativeReadiness: Codable, Equatable, Sendable {
    let accessibility: Bool
    let screenCapture: Bool
    enum CodingKeys: String, CodingKey { case accessibility, screenCapture = "screen_capture" }

    func missingPermission(for command: DesktopCommand) -> String? {
        if command.action == "open_url" || command.action == "apps" { return nil }
        if !accessibility { return "请开启辅助功能权限" }
        if command.action == "snapshot" && command.includeImage && !screenCapture { return "请开启屏幕录制权限" }
        return nil
    }
}

struct NativeReadinessMessage: Encodable, Sendable {
    let kind = "native_readiness"
    let readiness: NativeReadiness
}

struct DesktopCommand: Decodable, Sendable {
    let action: String
    let application: String?
    let snapshotID: String?
    let elementID: Int?
    let value: String?
    let includeImage: Bool
    var url: String? = nil
    enum CodingKeys: String, CodingKey {
        case snapshotID = "snapshot_id"
        case elementID = "element_id"
        case includeImage = "include_image"
        case action, application, value, url
    }
}

struct DesktopRequest: Decodable, Identifiable, Sendable {
    let requestID: String
    let workspaceScopeID: String
    let command: DesktopCommand
    var id: String { requestID }
    enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case workspaceScopeID = "workspace_scope_id"
        case command
    }
}

enum HostEvent: Decodable, Sendable {
    case state(String)
    case failure(String)
    case desktopRequest(DesktopRequest)
    case desktopCancel(String)

    enum CodingKeys: String, CodingKey { case kind, state, message, requestID = "request_id" }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        switch try values.decode(String.self, forKey: .kind) {
        case "state": self = .state(try values.decode(String.self, forKey: .state))
        case "failure": self = .failure(try values.decode(String.self, forKey: .message))
        case "desktop_request": self = .desktopRequest(try DesktopRequest(from: decoder))
        case "desktop_cancel": self = .desktopCancel(try values.decode(String.self, forKey: .requestID))
        default:
            throw DecodingError.dataCorruptedError(forKey: .kind, in: values, debugDescription: "无法识别本机服务消息")
        }
    }
}

enum ClientError: LocalizedError, Hashable, Sendable {
    case message(String)
    case formatted(String, [String])
    case authorizationExpired
    case credentialUnavailable
    case workspaceAccess
    var textKey: String {
        switch self {
        case .message(let text), .formatted(let text, _): return text
        case .authorizationExpired: return "授权已过期，请重新连接"
        case .credentialUnavailable: return "请重新连接 Studio"
        case .workspaceAccess: return "暂时无法打开工作区，请重试"
        }
    }
    var arguments: [String] {
        if case .formatted(_, let values) = self { return values }
        return []
    }
    var errorDescription: String? {
        arguments.isEmpty ? textKey : String(format: textKey, arguments: arguments)
    }
}

func checkedWebsiteURL(_ text: String) throws -> URL {
    guard !text.isEmpty, text.count <= 8192,
          text == text.trimmingCharacters(in: .whitespacesAndNewlines),
          !text.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }),
          let parts = URLComponents(string: text),
          ["http", "https"].contains(parts.scheme?.lowercased() ?? ""),
          let host = parts.host, !host.isEmpty,
          parts.user == nil, parts.password == nil,
          parts.port.map({ (1...65535).contains($0) }) ?? true,
          let url = parts.url else {
        throw ClientError.message("请提供不含登录凭证的 HTTP 或 HTTPS 网页地址")
    }
    return url
}

func checkedServer(_ text: String) throws -> URL {
    guard var parts = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
          let host = parts.host, !host.isEmpty,
          parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
          parts.scheme == "https" || (parts.scheme == "http" && ["localhost", "127.0.0.1", "[::1]", "::1"].contains(host)) else {
        throw ClientError.message("连接配置必须使用 HTTPS；HTTP 仅用于本机开发服务")
    }
    parts.scheme = parts.scheme?.lowercased()
    parts.host = host.lowercased()
    if (parts.scheme == "https" && parts.port == 443) || (parts.scheme == "http" && parts.port == 80) { parts.port = nil }
    while parts.path.hasSuffix("/") { parts.path.removeLast() }
    guard let url = parts.url else { throw ClientError.message("Studio 地址格式无效") }
    return url
}

/// 受限联网使用外网域名列表；普通联网不需要填写此列表
func checkedDomains(_ values: [String]) throws -> [String] {
    guard values.count <= 128 else { throw ClientError.message("最多允许 128 项外网域名") }
    let pattern = #"^(\*\.)?([a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?)(?::([0-9]{1,5}))?$"#
    let expression = try NSRegularExpression(pattern: pattern)
    for value in values {
        let range = NSRange(value.startIndex..., in: value)
        guard let match = expression.firstMatch(in: value, range: range), match.range == range,
              let hostRange = Range(match.range(at: 2), in: value) else {
            throw ClientError.formatted("域名格式无效：%@，请填写 example.com 或 *.example.com", [value])
        }
        var host = String(value[hostRange]).lowercased()
        while host.hasSuffix(".") { host.removeLast() }
        var address = in_addr()
        let isIP = host.withCString { inet_aton($0, &address) != 0 }
        let wildcard = match.range(at: 1).location != NSNotFound
        let port = Range(match.range(at: 3), in: value).flatMap { Int(value[$0]) }
        guard !isIP, host != "localhost", !host.hasSuffix(".localhost"), !host.contains(".."),
              !wildcard || host.contains("."), port == nil || (1...65535).contains(port!) else {
            throw ClientError.formatted("仅支持外网域名，不能授权 IP、localhost 或无效端口：%@", [value])
        }
    }
    return values
}

/// 配置保存设备标识、工具权限和短期浏览器入口；设备凭证始终由 Keychain 管理
final class ConfigurationStore: @unchecked Sendable {
    let directory: URL
    private let queue = DispatchQueue(label: "com.tinkerfin.companion.configuration")

    init(directory: URL) { self.directory = directory }

    func readConnection(resources: URL) async throws -> ConnectionConfiguration {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                do {
                    let data = try Data(contentsOf: resources.appendingPathComponent("connection.json"))
                    guard data.count <= 4096 else { throw ClientError.message("连接配置超过容量") }
                    let value = try JSONDecoder().decode(ConnectionConfiguration.self, from: data)
                    continuation.resume(returning: ConnectionConfiguration(
                        studioURL: try checkedServer(value.studioURL).absoluteString,
                        apiURL: try checkedServer(value.apiURL).absoluteString))
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

    func read() async throws -> SavedConfiguration {
        try await withCheckedThrowingContinuation { continuation in
            queue.async { [directory] in
                do {
                    let target = directory.appendingPathComponent("settings.json")
                    if !FileManager.default.fileExists(atPath: target.path) {
                        continuation.resume(returning: SavedConfiguration())
                        return
                    }
                    let attributes = try FileManager.default.attributesOfItem(atPath: target.path)
                    guard let size = attributes[.size] as? NSNumber, size.intValue <= 1024 * 1024 else {
                        throw ClientError.message("本机配置超过容量，无法安全读取")
                    }
                    continuation.resume(returning: try JSONDecoder().decode(SavedConfiguration.self, from: Data(contentsOf: target)))
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

    func save(_ configuration: SavedConfiguration) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            queue.async { [directory] in
                do {
                    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                            attributes: [.posixPermissions: 0o700])
                    let target = directory.appendingPathComponent("settings.json")
                    let encoder = JSONEncoder()
                    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
                    let content = try encoder.encode(configuration)
                    guard content.count <= 1024 * 1024 else { throw ClientError.message("本机配置超过容量") }
                    try content.write(to: target, options: [.atomic, .completeFileProtection])
                    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
                    continuation.resume()
                } catch { continuation.resume(throwing: error) }
            }
        }
    }
}
