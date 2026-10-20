import Foundation

/// 连接链接只唤醒客户端，查询参数不提供设备身份或连接状态
func isClientConnectURL(_ url: URL) -> Bool {
    guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
          parts.scheme == "tinkerfin", parts.host == "connect", parts.user == nil, parts.password == nil,
          parts.port == nil, parts.path.isEmpty else { return false }
    return true
}

struct ConnectionConfiguration: Codable, Equatable, Sendable {
    let studioURL: String
    let apiURL: String
    enum CodingKeys: String, CodingKey { case studioURL = "studio_url", apiURL = "api_url" }

    func checked() throws -> Self {
        Self(studioURL: try checkedServer(studioURL).absoluteString,
             apiURL: try checkedServer(apiURL).absoluteString)
    }
}

struct AuthorizationCreated: Codable, Sendable {
    let authorizationID: String
    let browserToken: String
    let expiresAt: String
    enum CodingKeys: String, CodingKey {
        case authorizationID = "authorization_id", browserToken = "browser_token", expiresAt = "expires_at"
    }

    func browserURL(configuration: ConnectionConfiguration) throws -> URL {
        guard var parts = URLComponents(url: try checkedServer(configuration.studioURL), resolvingAgainstBaseURL: false) else {
            throw ClientError.message("浏览器连接地址无效")
        }
        parts.queryItems = [URLQueryItem(name: "device_authorization", value: authorizationID)]
        parts.fragment = "token=" + browserToken
        guard let url = parts.url else { throw ClientError.message("浏览器连接地址无效") }
        return url
    }
}

struct PendingConnection: Codable, Sendable {
    let account: String
    let connection: ConnectionConfiguration
    var authorization: AuthorizationCreated?
}

enum AuthorizationState: String, Decodable, Sendable { case waiting, authorized, expired, cancelled, superseded }
struct AuthorizationResult: Decodable, Sendable {
    let authorizationID: String
    let state: AuthorizationState
    let device: PairedDevice?
    enum CodingKeys: String, CodingKey { case authorizationID = "authorization_id", state, device }
}
private struct AuthorizationStart: Encodable { let name: String; let credential: String }
private struct AuthorizationProof: Encodable { let credential: String }
private struct DeviceName: Encodable { let name: String }
private struct Envelope<Value: Decodable>: Decodable { let code: Int; let data: Value? }

final class NoRedirects: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping @Sendable (URLRequest?) -> Void) { completionHandler(nil) }
}

struct ManagedProject: Decodable, Identifiable, Sendable {
    let projectID: String
    let name: String
    var id: String { projectID }
    enum CodingKeys: String, CodingKey { case projectID = "project_id", name }
}
private struct ManagedProjectPage: Decodable, Sendable {
    let accountName: String
    let items: [ManagedProject]
    let nextCursor: String?
    enum CodingKeys: String, CodingKey { case accountName = "account_name", items, nextCursor = "next_cursor" }
}

struct AuthorizationClient: Sendable {
    func projects(server: URL, token: String) async throws -> (String, [ManagedProject]) {
        var items: [ManagedProject] = []
        var accountName: String?
        var after: String?
        repeat {
            let path = "projects" + (after.map { "?after=" + $0 } ?? "")
            let page: ManagedProjectPage = try await request(server: server, path: path, body: [String: String](), method: "GET", token: token)
            guard page.items.count <= 128, items.count + page.items.count <= 8192,
                  page.items.allSatisfy({ UUID(uuidString: $0.projectID)?.uuidString.lowercased() == $0.projectID }),
                  page.nextCursor == nil || (page.nextCursor == page.items.last?.projectID && page.nextCursor != after) else {
                throw ClientError.message("项目清单无效")
            }
            guard accountName == nil || accountName == page.accountName else { throw ClientError.message("账号已变化，请重新打开客户端") }
            accountName = page.accountName
            items.append(contentsOf: page.items); after = page.nextCursor
        } while after != nil
        guard Set(items.map(\.id)).count == items.count else { throw ClientError.message("项目身份重复") }
        return (accountName ?? "", items)
    }
    func project(_ id: String, server: URL, token: String) async throws -> ManagedProject {
        guard UUID(uuidString: id)?.uuidString.lowercased() == id else { throw ClientError.message("项目身份无效") }
        let item: ManagedProject = try await request(server: server, path: "projects/" + id, body: [String: String](), method: "GET", token: token)
        guard item.projectID == id else { throw ClientError.message("项目身份不匹配") }
        return item
    }

    private func request<Value: Decodable & Sendable, Body: Encodable>(
        server: URL, path: String, body: Body, method: String = "POST", token: String? = nil
    ) async throws -> Value {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 30
        let session = URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let pieces = path.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)
        var address = URLComponents(url: server.appendingPathComponent("api/devices/" + pieces[0]), resolvingAgainstBaseURL: false)!
        if pieces.count == 2 { address.percentEncodedQuery = String(pieces[1]) }
        var request = URLRequest(url: address.url!)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        if let token { request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
        if method != "GET" { request.httpBody = try JSONEncoder().encode(body) }
        let (bytes, response) = try await session.bytes(for: request)
        guard let response = response as? HTTPURLResponse else { throw ClientError.message("Studio 未返回有效响应") }
        var content = Data()
        for try await byte in bytes {
            guard content.count < 128 * 1024 else { throw ClientError.message("Studio 响应超过容量") }
            content.append(byte)
        }
        let envelope = try? JSONDecoder().decode(Envelope<Value>.self, from: content)
        guard response.statusCode == 200,
              let result = envelope, result.code == 0,
              let value = result.data else {
            if response.statusCode == 410 || response.statusCode == 404 && envelope?.code == 1_001_012_000 {
                throw ClientError.authorizationExpired
            }
            if response.statusCode == 404 { throw ClientError.message("本机连接服务暂不可用") }
            if response.statusCode == 401 { throw ClientError.message("连接已失效，请重新连接") }
            throw ClientError.message("连接尚未完成，请检查网络后重试")
        }
        return value
    }

    func start(server: URL, name: String, credential: String) async throws -> AuthorizationCreated {
        try validateCredential(credential)
        guard !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, name.count <= 128 else {
            throw ClientError.message("请输入设备名称")
        }
        let value: AuthorizationCreated = try await request(server: server, path: "authorizations", body: AuthorizationStart(name: name, credential: credential))
        guard UUID(uuidString: value.authorizationID) != nil,
              value.browserToken.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else {
            throw ClientError.message("Studio 返回的授权入口无效")
        }
        return value
    }

    func status(server: URL, authorizationID: String, credential: String, cancel: Bool = false) async throws -> AuthorizationResult {
        try validateCredential(credential)
        guard UUID(uuidString: authorizationID) != nil else { throw ClientError.message("授权入口无效") }
        let value: AuthorizationResult = try await request(server: server,
            path: "authorizations/\(authorizationID)/\(cancel ? "cancel" : "status")", body: AuthorizationProof(credential: credential))
        guard value.authorizationID == authorizationID else { throw ClientError.message("Studio 返回了其他授权申请") }
        if value.state == .authorized {
            guard let device = value.device else { throw ClientError.message("Studio 返回的设备身份缺失") }
            try validateDevice(device)
        } else if value.device != nil { throw ClientError.message("Studio 返回的授权状态无效") }
        return value
    }

    func rename(server: URL, deviceID: String, credential: String, name: String) async throws -> PairedDevice {
        try validateCredential(credential)
        let value: PairedDevice = try await request(server: server, path: "current", body: DeviceName(name: name),
            method: "PATCH", token: deviceID + "." + credential)
        try validateDevice(value)
        guard value.deviceID == deviceID else { throw ClientError.message("Studio 返回了其他设备身份") }
        return value
    }

    private func validateCredential(_ credential: String) throws {
        guard credential.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else { throw ClientError.message("设备凭证无效") }
    }
    private func validateDevice(_ device: PairedDevice) throws {
        guard UUID(uuidString: device.deviceID) != nil,
              device.namespace.range(of: "^ns_[1-9][0-9]*$", options: .regularExpression) != nil,
              !device.name.isEmpty, device.name.count <= 128 else { throw ClientError.message("Studio 返回的设备身份无效") }
    }
}
