import Foundation
import LocalAuthentication
import Security

private struct ACLRecord: Encodable {
    let authorizations: [String]
    let description: String
    let applicationCount: Int
}

private struct ProbeResult: Encodable {
    let build: String
    let status: OSStatus
    var length: Int?
    var acl: [ACLRecord]?
}

@main
struct SigningIdentityContracts {
    #if BUILD_A
    static let build = "A"
    #else
    static let build = "B"
    #endif

    private static func emit(_ result: ProbeResult) {
        let data = try! JSONEncoder().encode(result)
        print(String(decoding: data, as: UTF8.self))
    }

    static func main() {
        guard CommandLine.arguments.count == 5 else { exit(64) }
        let mode = CommandLine.arguments[1], path = CommandLine.arguments[2]
        let service = CommandLine.arguments[3], account = CommandLine.arguments[4]
        guard SecKeychainSetUserInteractionAllowed(false) == errSecSuccess else { exit(65) }
        var opened: SecKeychain?
        let openStatus = SecKeychainOpen(path, &opened)
        guard openStatus == errSecSuccess, let keychain = opened else {
            emit(ProbeResult(build: build, status: openStatus)); return
        }
        let context = LAContext()
        context.interactionNotAllowed = true
        // 所有条目读写显式限定测试拥有的钥匙串，禁止落入默认或其他搜索路径
        var query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword, kSecUseDataProtectionKeychain: false,
            kSecMatchSearchList: [keychain], kSecAttrService: service, kSecAttrAccount: account,
            kSecUseAuthenticationContext: context,
        ]
        switch mode {
        case "create":
            var application: SecTrustedApplication?
            let trusted = SecTrustedApplicationCreateFromPath(nil, &application)
            guard trusted == errSecSuccess, let application else {
                emit(ProbeResult(build: build, status: trusted)); return
            }
            var access: SecAccess?
            let accessStatus = SecAccessCreate("独占签名测试" as CFString, [application] as CFArray, &access)
            guard accessStatus == errSecSuccess, let access else {
                emit(ProbeResult(build: build, status: accessStatus)); return
            }
            var bytes = [UInt8](repeating: 0, count: 32)
            let randomStatus = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
            guard randomStatus == errSecSuccess else {
                emit(ProbeResult(build: build, status: randomStatus)); return
            }
            let status = SecItemAdd([
                kSecClass: kSecClassGenericPassword, kSecUseDataProtectionKeychain: false,
                kSecUseKeychain: keychain, kSecAttrService: service, kSecAttrAccount: account,
                kSecAttrAccess: access, kSecValueData: Data(bytes), kSecUseAuthenticationContext: context,
            ] as CFDictionary, nil)
            emit(ProbeResult(build: build, status: status))
        case "read":
            query[kSecReturnData] = true
            query[kSecMatchLimit] = kSecMatchLimitOne
            var value: CFTypeRef?
            let status = SecItemCopyMatching(query as CFDictionary, &value)
            emit(ProbeResult(build: build, status: status, length: (value as? Data)?.count ?? 0))
        case "acl":
            query[kSecReturnRef] = true
            query[kSecMatchLimit] = kSecMatchLimitOne
            var value: CFTypeRef?
            let status = SecItemCopyMatching(query as CFDictionary, &value)
            guard status == errSecSuccess, let value, CFGetTypeID(value) == SecKeychainItemGetTypeID() else {
                emit(ProbeResult(build: build, status: status)); return
            }
            var access: SecAccess?
            let accessStatus = SecKeychainItemCopyAccess(value as! SecKeychainItem, &access)
            guard accessStatus == errSecSuccess, let access else {
                emit(ProbeResult(build: build, status: accessStatus)); return
            }
            var entries: CFArray?
            let listStatus = SecAccessCopyACLList(access, &entries)
            guard listStatus == errSecSuccess, let entries else {
                emit(ProbeResult(build: build, status: listStatus)); return
            }
            var records: [ACLRecord] = []
            for acl in entries as! [SecACL] {
                var applications: CFArray?, description: CFString?
                var selector = SecKeychainPromptSelector()
                let contents = SecACLCopyContents(acl, &applications, &description, &selector)
                guard contents == errSecSuccess else {
                    emit(ProbeResult(build: build, status: contents)); return
                }
                records.append(ACLRecord(
                    authorizations: SecACLCopyAuthorizations(acl) as? [String] ?? [],
                    description: description as String? ?? "",
                    applicationCount: (applications as? [SecTrustedApplication])?.count ?? 0
                ))
            }
            emit(ProbeResult(build: build, status: errSecSuccess, acl: records))
        default: exit(64)
        }
    }
}
