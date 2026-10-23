import Foundation
import LocalAuthentication
import Security

/// Keychain 同步调用只在独占工作队列执行，凭证缓存只属于当前进程
final class KeychainStore: @unchecked Sendable {
    private let service: String
    private let queue = DispatchQueue(label: "com.tinkerfin.companion.keychain")
    // 仅由独占队列访问；同一原生进程已获准读取的凭证不因面板刷新再次访问钥匙串
    private var credentials: [String: String] = [:]

    init(service: String = "com.tinkerfin.companion.device") { self.service = service }

    private func perform<T: Sendable>(_ operation: @escaping @Sendable () throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                do {
                    // 文件钥匙串的创建路径不保证遵守LAContext，禁止后台等待系统授权窗口
                    guard SecKeychainSetUserInteractionAllowed(false) == errSecSuccess else { throw ClientError.credentialUnavailable }
                    continuation.resume(returning: try operation())
                }
                catch { continuation.resume(throwing: error) }
            }
        }
    }

    func create(account: String) async throws -> String {
        try await perform { [self] in
            // 授权网络响应可能丢失；同一待授权记录必须继续使用已保存的凭证
            if let existing = credentials[account] { return existing }
            if let existing = try Self.find(service: service, account: account) {
                credentials[account] = existing
                return existing
            }
            var bytes = [UInt8](repeating: 0, count: 32)
            guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
                throw ClientError.message("系统无法创建安全设备凭证，请重试")
            }
            let secret = bytes.map { String(format: "%02x", $0) }.joined()
            // 文件钥匙串 ACL 只信任本应用的代码身份；开发包无需共享钥匙串 entitlement
            var application: SecTrustedApplication?
            guard SecTrustedApplicationCreateFromPath(nil, &application) == errSecSuccess,
                  let application else { throw ClientError.message("无法确认当前应用身份") }
            var access: SecAccess?
            guard SecAccessCreate("TinkerFin 设备连接" as CFString, [application] as CFArray, &access) == errSecSuccess,
                  let access else { throw ClientError.message("无法创建设备凭证的应用权限") }
            let status = SecItemAdd([
                kSecClass: kSecClassGenericPassword,
                kSecUseDataProtectionKeychain: false,
                kSecAttrService: service,
                kSecAttrAccount: account,
                kSecAttrLabel: "TinkerFin 设备连接",
                kSecAttrAccess: access,
                kSecValueData: Data(secret.utf8),
                kSecUseAuthenticationContext: Self.noInteraction(),
            ] as CFDictionary, nil)
            guard status == errSecSuccess else {
                throw ClientError.message("设备凭证尚未保存到 Keychain，请解锁钥匙串后重试")
            }
            credentials[account] = secret
            return secret
        }
    }

    func read(account: String) async throws -> String {
        do { return try await perform { [self] in
            if let value = credentials[account] { return value }
            guard let value = try Self.find(service: service, account: account) else {
                throw ClientError.credentialUnavailable
            }
            credentials[account] = value
            return value
        } } catch { throw ClientError.credentialUnavailable }
    }

    /// 接收方用自身代码身份创建条目，设备密钥仅由更新父进程经匿名管道传入
    func receiveForUpdate(account: String, secret: String) async throws {
        try await perform { [self] in
            guard secret.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else { throw ClientError.credentialUnavailable }
            if let existing = try Self.find(service: service, account: account) {
                guard existing == secret else { throw ClientError.credentialUnavailable }
                return
            }
            var current: SecTrustedApplication?
            guard SecTrustedApplicationCreateFromPath(nil, &current) == errSecSuccess,
                  let current else { throw ClientError.message("无法确认更新应用身份") }
            var access: SecAccess?
            guard SecAccessCreate("TinkerFin 设备连接" as CFString, [current] as CFArray, &access) == errSecSuccess,
                  let access else { throw ClientError.message("无法保留更新后的设备凭证") }
            let status = SecItemAdd([
                kSecClass: kSecClassGenericPassword, kSecUseDataProtectionKeychain: false,
                kSecAttrService: service, kSecAttrAccount: account,
                kSecAttrLabel: "TinkerFin 设备连接", kSecAttrAccess: access,
                kSecValueData: Data(secret.utf8), kSecUseAuthenticationContext: Self.noInteraction(),
            ] as CFDictionary, nil)
            guard status == errSecSuccess else { throw ClientError.message("设备凭证尚未保存到 Keychain，请解锁钥匙串后重试") }
            guard try Self.find(service: service, account: account) == secret else { throw ClientError.credentialUnavailable }
            credentials[account] = secret
        }
    }

    /// 仅供已持久化退休的账户清理；旧签名的密钥不可读也不应阻止精确删除
    func remove(account: String) async throws {
        try await perform { [self] in
            try Self.delete(service: service, account: account)
            credentials.removeValue(forKey: account)
        }
    }

    /// 删除辅助入口也须证明持有同一设备密钥，不能仅凭公开账户ID借用代码权限
    func removeForUpdate(account: String, secret: String) async throws {
        try await perform { [self] in
            guard let existing = try Self.find(service: service, account: account) else { return }
            let left = Array(existing.utf8), right = Array(secret.utf8)
            guard left.count == right.count, zip(left, right).reduce(UInt8(0), { $0 | ($1.0 ^ $1.1) }) == 0 else {
                throw ClientError.credentialUnavailable
            }
            try Self.delete(service: service, account: account)
            credentials.removeValue(forKey: account)
        }
    }

    // 内部调用由退休记录确认归属，公开更新入口另行验证密钥；精确引用避免owner-edit失败
    private static func delete(service: String, account: String) throws {
        var item: CFTypeRef?
        let found = SecItemCopyMatching([
            kSecClass: kSecClassGenericPassword, kSecUseDataProtectionKeychain: false,
            kSecAttrService: service, kSecAttrAccount: account,
            kSecReturnRef: true, kSecMatchLimit: kSecMatchLimitOne,
            kSecUseAuthenticationContext: noInteraction(),
        ] as CFDictionary, &item)
        if found == errSecItemNotFound { return }
        guard found == errSecSuccess, let item, CFGetTypeID(item) == SecKeychainItemGetTypeID() else {
            throw ClientError.credentialUnavailable
        }
        let status = SecKeychainItemDelete(item as! SecKeychainItem)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw ClientError.message("设备凭证未能从 Keychain 删除，请解锁钥匙串后重试")
        }
    }

    func clear() async {
        await withCheckedContinuation { continuation in
            queue.async { [self] in
                credentials.removeAll()
                continuation.resume()
            }
        }
    }

    private static func noInteraction() -> LAContext {
        let context = LAContext()
        context.interactionNotAllowed = true
        return context
    }

    private static func find(service: String, account: String) throws -> String? {
        var item: CFTypeRef?
        let status = SecItemCopyMatching([
            kSecClass: kSecClassGenericPassword, kSecUseDataProtectionKeychain: false,
            kSecAttrService: service, kSecAttrAccount: account,
            kSecReturnData: true, kSecMatchLimit: kSecMatchLimitOne,
            kSecUseAuthenticationContext: noInteraction(),
        ] as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data,
              let value = String(data: data, encoding: .utf8),
              value.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else {
            throw ClientError.message("无法读取设备凭证，请解锁 Keychain 或重新连接")
        }
        return value
    }
}
