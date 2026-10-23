import Darwin
import CryptoKit
import Foundation

/// 凭证助手继承同一把事务锁；父进程崩溃后，恢复流程仍须等待在途写入结束
final class UpdateCredentialLock: @unchecked Sendable {
    let descriptor: Int32

    init(directory: URL, inherited: Int32? = nil) throws {
        var descriptor = inherited ?? -1
        do {
            try UpdateWorkspace.checkPrivate(directory)
            let path = directory.appendingPathComponent("credentials.lock")
            if inherited == nil { descriptor = open(path.path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600) }
            var opened = stat(), entry = stat()
            guard descriptor >= 0, fstat(descriptor, &opened) == 0, lstat(path.path, &entry) == 0,
                  opened.st_uid == getuid(), opened.st_mode & S_IFMT == S_IFREG,
                  opened.st_mode & 0o777 == 0o600, opened.st_nlink == 1,
                  opened.st_dev == entry.st_dev, opened.st_ino == entry.st_ino else { throw UpdateError.invalidLocation }
            guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else { throw UpdateError.unavailable }
            self.descriptor = descriptor
        } catch {
            if descriptor >= 0 { Darwin.close(descriptor) }
            throw error
        }
    }

    deinit { Darwin.close(descriptor) }
}

/// 只记录两端账户的归属；密钥只经匿名管道传给已核验的候选应用
struct UpdateCredentials: Codable, Sendable {
    static let commands: Set<String> = ["--receive-update-credential", "--remove-update-credential"]
    let sourceAccount: String
    let targetAccount: String
    let authentication: Data

    private struct Binding: Encodable {
        let identifier: String
        let stateDirectory: String
        let application: String
        let currentVersion: String
        let targetVersion: String
        let currentTreeSHA256: String
        let sourceAccount: String
        let targetAccount: String
    }

    private static func binding(_ handoff: UpdateHandoff, source: String, target: String) throws -> Data {
        let encoder = JSONEncoder(); encoder.outputFormatting = .sortedKeys
        return try encoder.encode(Binding(identifier: handoff.identifier, stateDirectory: handoff.stateDirectory,
            application: handoff.application, currentVersion: handoff.currentVersion, targetVersion: handoff.targetVersion,
            currentTreeSHA256: handoff.currentTreeSHA256, sourceAccount: source, targetAccount: target))
    }

    func authenticate(_ handoff: UpdateHandoff, secret: String) throws {
        // 恢复时先验证旧包身份未被篡改，再把密钥交给有权限清理它的旧代码
        guard HMAC<SHA256>.isValidAuthenticationCode(authentication,
            authenticating: try Self.binding(handoff, source: sourceAccount, target: targetAccount),
            using: SymmetricKey(data: Data(secret.utf8))) else { throw UpdateError.invalidLocation }
    }

    static func read(_ handoff: UpdateHandoff) throws -> Self? {
        let path = handoff.directory.appendingPathComponent("credentials.json")
        guard FileManager.default.fileExists(atPath: path.path) else { return nil }
        let value = try JSONDecoder().decode(Self.self, from: UpdateWorkspace.read(path, limit: 4096))
        guard value.authentication.count == 32, value.sourceAccount != value.targetAccount,
              [value.sourceAccount, value.targetAccount].allSatisfy({ UUID(uuidString: $0)?.uuidString.lowercased() == $0 }) else {
            throw UpdateError.invalidLocation
        }
        return value
    }

    @MainActor
    static func prepare(_ handoff: UpdateHandoff, account: String, secret: String) async throws {
        let lock = try await UpdateFileWork().perform { _ in try UpdateCredentialLock(directory: handoff.directory) }
        defer { withExtendedLifetime(lock) {} }
        try await UpdateFileWork().perform { _ in
            let record: Self
            if let existing = try read(handoff) { record = existing; try record.authenticate(handoff, secret: secret) }
            else {
                let target = UUID().uuidString.lowercased()
                let authentication = HMAC<SHA256>.authenticationCode(for: try binding(handoff, source: account, target: target), using: SymmetricKey(data: Data(secret.utf8)))
                record = Self(sourceAccount: account, targetAccount: target, authentication: Data(authentication))
            }
            guard record.sourceAccount == account else { throw UpdateError.invalidLocation }
            try UpdateWorkspace.write(record, to: handoff.directory.appendingPathComponent("credentials.json"))
            // 创建Keychain条目前先发布恢复入口，父进程中断不能丢失候选账户归属
            try UpdateWorkspace.write(UpdateInstallReceipt(identifier: handoff.identifier, version: handoff.targetVersion, helperPID: 0), to: handoff.pending)
        }
        try await invoke(handoff.candidate, handoff: handoff, receive: true, input: Data(secret.utf8), lock: lock)
    }

    @MainActor
    static func cancel(_ handoff: UpdateHandoff, secret: String) async throws {
        let lock = try await UpdateFileWork().perform { _ in try UpdateCredentialLock(directory: handoff.directory) }
        defer { withExtendedLifetime(lock) {} }
        guard try await UpdateFileWork().perform({ _ in try read(handoff) != nil }) else { return }
        try await invoke(handoff.candidate, handoff: handoff, receive: false, input: Data(secret.utf8), lock: lock)
        try await clear(handoff)
    }

    @MainActor
    static func finish(_ handoff: UpdateHandoff, secret: String) async throws {
        let lock = try await UpdateFileWork().perform { _ in try UpdateCredentialLock(directory: handoff.directory) }
        defer { withExtendedLifetime(lock) {} }
        guard try await UpdateFileWork().perform({ _ in try read(handoff) != nil }) else { return }
        let source = try await UpdateFileWork().perform { _ in try UpdateInstallation.retainedApplication(handoff) }
        try await invoke(source, handoff: handoff, receive: false, input: Data(secret.utf8), lock: lock)
        try await clear(handoff)
    }

    @MainActor
    private static func clear(_ handoff: UpdateHandoff) async throws {
        try await UpdateFileWork().perform { _ in
            try FileManager.default.removeItem(at: handoff.directory.appendingPathComponent("credentials.json"))
        }
    }

    @MainActor
    private static func invoke(_ application: URL, handoff: UpdateHandoff, receive: Bool, input: Data, lock: UpdateCredentialLock) async throws {
        try await UpdateFileWork().perform { cancellation in
            guard let secret = String(data: input, encoding: .utf8), let record = try read(handoff) else { throw UpdateError.invalidLocation }
            try record.authenticate(handoff, secret: secret)
            let release = try verifiedRelease(handoff)
            let candidate = application.standardizedFileURL == handoff.candidate.standardizedFileURL
            try UpdateInstallation.checkIdentity(application, version: candidate ? handoff.targetVersion : handoff.currentVersion)
            guard try UpdateTree.digest(application, cancellation: cancellation) == (candidate ? release.treeSHA256 : handoff.currentTreeSHA256) else {
                throw UpdateError.invalidArchive
            }
        }
        try await UpdateSubprocess().run(executable: application.appendingPathComponent("Contents/MacOS/TinkerFin"),
            arguments: [receive ? "--receive-update-credential" : "--remove-update-credential", handoff.directory.path], input: input, lock: lock)
    }

    private static func verifiedRelease(_ handoff: UpdateHandoff) throws -> DesktopRelease {
        let release = try DesktopRelease.verify(manifest: UpdateWorkspace.read(handoff.directory.appendingPathComponent("update.json"), limit: 64 * 1024),
            signature: UpdateWorkspace.read(handoff.directory.appendingPathComponent("update.json.sig"), limit: 64))
        guard release.version == handoff.targetVersion else { throw UpdateError.invalidManifest }
        return release
    }

    /// 辅助入口不启动界面或导出密钥；清理须同时验证事务和调用方持有的设备密钥
    @MainActor
    static func handle(_ arguments: [String]) async -> Int32? {
        guard arguments.count > 1, commands.contains(arguments[1]) else { return nil }
        guard arguments.count == 3 else { return 1 }
        let inherited = dup(STDERR_FILENO)
        guard inherited >= 0 else { return 1 }
        guard fcntl(inherited, F_SETFD, FD_CLOEXEC) == 0 else { Darwin.close(inherited); return 1 }
        let null = open("/dev/null", O_WRONLY | O_CLOEXEC)
        guard null >= 0 else { Darwin.close(inherited); return 1 }
        let redirected = dup2(null, STDERR_FILENO); Darwin.close(null)
        guard redirected == STDERR_FILENO else { Darwin.close(inherited); return 1 }
        do {
            let directory = URL(fileURLWithPath: arguments[2], isDirectory: true)
            let lock = try await UpdateFileWork().perform { _ in try UpdateCredentialLock(directory: directory, inherited: inherited) }
            defer { withExtendedLifetime(lock) {} }
            let (handoff, record, target) = try await UpdateFileWork().perform { cancellation in
                let handoff = try UpdateHandoff.read(directory)
                guard let record = try read(handoff) else { throw UpdateError.invalidLocation }
                let release = try verifiedRelease(handoff)
                let application = Bundle.main.bundleURL
                let target = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String == handoff.targetVersion
                try UpdateInstallation.checkIdentity(application, version: target ? handoff.targetVersion : handoff.currentVersion)
                guard try UpdateTree.digest(application, cancellation: cancellation) == (target ? release.treeSHA256 : handoff.currentTreeSHA256) else {
                    throw UpdateError.invalidArchive
                }
                return (handoff, record, target)
            }
            var info = stat()
            guard fstat(STDIN_FILENO, &info) == 0, info.st_mode & S_IFMT == S_IFIFO,
                  fcntl(STDIN_FILENO, F_SETFL, O_NONBLOCK) == 0 else { throw UpdateError.invalidLocation }
            var data = Data()
            while let bytes = try await readFromPipe(STDIN_FILENO) {
                guard data.count + bytes.count <= 64 else { throw UpdateError.invalidLocation }
                data.append(bytes)
            }
            guard data.count == 64, let secret = String(data: data, encoding: .utf8) else { throw UpdateError.invalidLocation }
            try record.authenticate(handoff, secret: secret)
            let saved = try await ConfigurationStore(directory: URL(fileURLWithPath: handoff.stateDirectory)).read()
            let account = target ? record.targetAccount : record.sourceAccount
            let keychain = KeychainStore()
            if arguments[1] == "--receive-update-credential" {
                guard target, saved.keychainAccount == record.sourceAccount, saved.pending == nil, saved.device != nil else { throw UpdateError.invalidLocation }
                try await keychain.receiveForUpdate(account: account, secret: secret)
            } else {
                guard saved.keychainAccount == (target ? record.sourceAccount : record.targetAccount), saved.keychainAccount != account else {
                    throw UpdateError.invalidLocation
                }
                try await keychain.removeForUpdate(account: account, secret: secret)
            }
            await keychain.clear()
            return 0
        } catch { return 1 }
    }
}
