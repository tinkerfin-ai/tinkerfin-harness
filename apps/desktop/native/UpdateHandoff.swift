import Darwin
import CryptoKit
import Foundation

/// 更新交接只携带界面草稿与本机路径；设备密钥保留在Keychain，不进入交接文件
struct UpdateHandoff: Codable, Sendable {
    let identifier: String
    let stateDirectory: String
    let application: String
    let currentVersion: String
    let targetVersion: String
    let currentTreeSHA256: String
    let parentPID: Int32
    let view: DesktopViewState?
    let language: String
    let locale: String

    var directory: URL { URL(fileURLWithPath: stateDirectory).appendingPathComponent("state/updates/" + identifier, isDirectory: true).standardizedFileURL }
    var candidate: URL { directory.appendingPathComponent("unpacked/TinkerFin.app", isDirectory: true) }
    var target: URL { URL(fileURLWithPath: application, isDirectory: true).standardizedFileURL }
    var pending: URL { directory.deletingLastPathComponent().appendingPathComponent("pending.json") }

    func checked(in directory: URL) throws -> Self {
        try UpdateWorkspace.checkInstallationLocation(target, state: URL(fileURLWithPath: stateDirectory))
        guard identifier.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil,
              directory.standardizedFileURL == self.directory,
              self.directory.resolvingSymlinksInPath() == self.directory,
              target.resolvingSymlinksInPath() == target, target.pathExtension == "app",
              !target.path.hasPrefix(self.directory.path + "/"), parentPID > 1,
              ["system", "en", "zh-Hans"].contains(language), ["en", "zh-Hans"].contains(locale),
              currentTreeSHA256.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
              try DesktopReleaseVersion(targetVersion) > DesktopReleaseVersion(currentVersion) else { throw UpdateError.invalidLocation }
        _ = try view?.checked()
        try UpdateWorkspace.checkPrivate(self.directory)
        return self
    }

    static func read(_ directory: URL) throws -> Self {
        let bytes = try UpdateWorkspace.read(directory.appendingPathComponent("handoff.json"), limit: 64 * 1024)
        return try JSONDecoder().decode(Self.self, from: bytes).checked(in: directory)
    }
}

/// 同一应用安装目标跨状态目录独占；助手继承同一打开文件描述符，父进程退出不释放它的锁
final class UpdateTargetLock: @unchecked Sendable {
    let application: URL
    let descriptor: Int32

    static func path(for application: URL) -> URL {
        let application = application.standardizedFileURL
        let name = SHA256.hash(data: Data(application.path.utf8)).map { String(format: "%02x", $0) }.joined()
        return application.deletingLastPathComponent().appendingPathComponent(".tinkerfin-install-" + name + ".lock")
    }

    static func isBusy(application: URL) throws -> Bool {
        let descriptor = open(path(for: application).path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        if descriptor < 0 {
            if errno == ENOENT { return false }
            throw UpdateError.invalidLocation
        }
        defer { Darwin.close(descriptor) }
        if flock(descriptor, LOCK_EX | LOCK_NB) == 0 { return false }
        guard errno == EWOULDBLOCK else { throw UpdateError.invalidLocation }
        return true
    }

    init(application: URL, inherited: Int32? = nil) throws {
        self.application = application.standardizedFileURL
        let path = Self.path(for: application)
        let descriptor = inherited ?? open(path.path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard descriptor >= 0 else { throw UpdateError.invalidLocation }
        var descriptorInfo = stat(), pathInfo = stat()
        guard fstat(descriptor, &descriptorInfo) == 0, lstat(path.path, &pathInfo) == 0,
              descriptorInfo.st_uid == getuid(), descriptorInfo.st_mode & S_IFMT == S_IFREG,
              descriptorInfo.st_dev == pathInfo.st_dev, descriptorInfo.st_ino == pathInfo.st_ino,
              flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            Darwin.close(descriptor); throw UpdateError.unavailable
        }
        self.descriptor = descriptor
    }

    deinit { Darwin.close(descriptor) }
}

struct UpdateInstallReceipt: Codable, Sendable {
    let identifier: String
    let version: String
    let helperPID: Int32
    var deferred = false
}

struct UpdateLaunchReceipt: Codable, Sendable {
    let identifier: String
    let version: String
    let processID: Int32
}

enum UpdateWorkspace {
    private struct CleanupOwnership: Codable {
        let identifier: String
        let device: Int32
        let inode: UInt64
    }

    static func checkInstallationLocation(_ application: URL, state: URL) throws {
        let application = application.standardizedFileURL, state = state.standardizedFileURL
        guard application.resolvingSymlinksInPath() == application, state.resolvingSymlinksInPath() == state,
              application.pathExtension == "app", application != state,
              !application.path.hasPrefix(state.path + "/"), !state.path.hasPrefix(application.path + "/") else { throw UpdateError.invalidLocation }
    }

    static func checkPrivate(_ directory: URL) throws {
        let attributes = try FileManager.default.attributesOfItem(atPath: directory.path)
        guard directory.resolvingSymlinksInPath() == directory.standardizedFileURL,
              attributes[.type] as? FileAttributeType == .typeDirectory,
              (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              let mode = attributes[.posixPermissions] as? NSNumber, mode.intValue & 0o077 == 0 else { throw UpdateError.invalidLocation }
    }

    static func create(root: URL) throws -> URL {
        let root = root.standardizedFileURL
        // 尚不存在的目录解析后可能丢失 URL 的目录标记，路径相等仍须拒绝符号链接
        guard root.resolvingSymlinksInPath().path == root.path else { throw UpdateError.invalidLocation }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try checkPrivate(root)
        let directory = root.appendingPathComponent(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(), isDirectory: true)
        guard mkdir(directory.path, 0o700) == 0 else { throw UpdateError.invalidLocation }
        return directory
    }

    static func remove(_ directory: URL, root: URL) throws {
        guard directory.deletingLastPathComponent().standardizedFileURL == root.standardizedFileURL,
              directory.lastPathComponent.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil,
              !FileManager.default.fileExists(atPath: directory.appendingPathComponent("credentials.json").path) else { throw UpdateError.invalidLocation }
        let journal = root.appendingPathComponent(directory.lastPathComponent + ".cleanup.json")
        if FileManager.default.fileExists(atPath: journal.path) {
            let ownership = try JSONDecoder().decode(CleanupOwnership.self, from: read(journal, limit: 4096))
            guard ownership.identifier == directory.lastPathComponent else { throw UpdateError.invalidLocation }
            try finishCleanup(ownership, root: root, journal: journal)
            return
        }
        try checkPrivate(directory)
        if FileManager.default.fileExists(atPath: directory.appendingPathComponent("handoff.json").path) {
            try UpdateInstallation.cleanAdjacent(try UpdateHandoff.read(directory))
        }
        var info = stat()
        guard lstat(directory.path, &info) == 0 else { throw UpdateError.invalidLocation }
        let ownership = CleanupOwnership(identifier: directory.lastPathComponent, device: info.st_dev, inode: info.st_ino)
        try write(ownership, to: journal)
        try finishCleanup(ownership, root: root, journal: journal)
    }

    /// 清理凭据放在被删除目录之外，进程退出不能丢失剩余文件的精确归属
    static func resumeCleanup(root: URL) throws {
        guard FileManager.default.fileExists(atPath: root.path) else { return }
        try checkPrivate(root)
        for journal in try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
            where journal.lastPathComponent.range(of: "^[0-9a-f]{32}\\.cleanup\\.json$", options: .regularExpression) != nil {
            let ownership = try JSONDecoder().decode(CleanupOwnership.self, from: read(journal, limit: 4096))
            guard journal.lastPathComponent == ownership.identifier + ".cleanup.json" else { throw UpdateError.invalidLocation }
            try finishCleanup(ownership, root: root, journal: journal)
        }
    }

    /// 开始另一轮安装前，在已有安装锁下结算本状态目录上一次中断的事务
    static func settlePrevious(root: URL, application: URL, currentVersion: String, excluding identifier: String) throws {
        try resumeCleanup(root: root)
        let pending = root.appendingPathComponent("pending.json")
        guard FileManager.default.fileExists(atPath: pending.path) else { return }
        let receipt = try JSONDecoder().decode(UpdateInstallReceipt.self, from: read(pending, limit: 4096))
        guard receipt.identifier != identifier else { return }
        guard receipt.identifier.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil else { throw UpdateError.invalidLocation }
        let directory = root.appendingPathComponent(receipt.identifier)
        let handoff = try UpdateHandoff.read(directory)
        guard handoff.target == application.standardizedFileURL,
              [handoff.currentVersion, handoff.targetVersion].contains(currentVersion) else { throw UpdateError.invalidLocation }
        let release = try DesktopRelease.verify(manifest: read(directory.appendingPathComponent("update.json"), limit: 64 * 1024),
            signature: read(directory.appendingPathComponent("update.json.sig"), limit: 64))
        guard release.version == handoff.targetVersion else { throw UpdateError.invalidManifest }
        try remove(directory, root: root)
    }

    private static func finishCleanup(_ ownership: CleanupOwnership, root: URL, journal: URL) throws {
        let directory = root.appendingPathComponent(ownership.identifier)
        var info = stat()
        if lstat(directory.path, &info) == 0 {
            guard info.st_dev == ownership.device, info.st_ino == ownership.inode else { throw UpdateError.invalidLocation }
            try checkPrivate(directory)
            try FileManager.default.removeItem(at: directory)
        } else if errno != ENOENT { throw UpdateError.invalidLocation }
        let pending = root.appendingPathComponent("pending.json")
        if FileManager.default.fileExists(atPath: pending.path),
           let receipt = try? JSONDecoder().decode(UpdateInstallReceipt.self, from: read(pending, limit: 4096)),
           receipt.identifier == directory.lastPathComponent { try FileManager.default.removeItem(at: pending) }
        try FileManager.default.removeItem(at: journal)
    }

    static func write<T: Encodable>(_ value: T, to path: URL) throws {
        let data = try JSONEncoder().encode(value)
        guard data.count <= 64 * 1024 else { throw UpdateError.invalidLocation }
        try data.write(to: path, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
    }

    static func read(_ path: URL, limit: Int) throws -> Data {
        let attributes = try FileManager.default.attributesOfItem(atPath: path.path)
        guard attributes[.type] as? FileAttributeType == .typeRegular,
              let size = attributes[.size] as? NSNumber, size.intValue <= limit else { throw UpdateError.invalidLocation }
        return try Data(contentsOf: path)
    }
}

/// 助手先复制到目标同一文件系统，复验完成才原子交换；交换前的失败不修改旧应用
enum UpdateInstallation {
    private struct AdjacentOwnership: Codable {
        let device: Int32
        let inode: UInt64
    }

    /// 仅删除持久化文件身份仍匹配的本轮相邻目录，异常退出不能把同名借用目录视为己有
    static func cleanAdjacent(_ handoff: UpdateHandoff) throws {
        let record = handoff.directory.appendingPathComponent("adjacent.json")
        let manager = FileManager.default
        guard manager.fileExists(atPath: record.path) else { return }
        let owner = try JSONDecoder().decode(AdjacentOwnership.self, from: UpdateWorkspace.read(record, limit: 4096))
        let path = adjacentDirectory(handoff)
        var info = stat()
        if lstat(path.path, &info) == 0 {
            guard info.st_dev == owner.device, info.st_ino == owner.inode,
                  info.st_uid == getuid(), info.st_mode & S_IFMT == S_IFDIR else { throw UpdateError.invalidLocation }
            try manager.removeItem(at: path)
        } else if errno != ENOENT { throw UpdateError.invalidLocation }
        try manager.removeItem(at: record)
    }

    private static func adjacentDirectory(_ handoff: UpdateHandoff) -> URL {
        handoff.target.deletingLastPathComponent().appendingPathComponent(".TinkerFin-update-" + handoff.identifier, isDirectory: true)
    }

    static func retainedApplication(_ handoff: UpdateHandoff) throws -> URL {
        let owner = try JSONDecoder().decode(AdjacentOwnership.self,
            from: UpdateWorkspace.read(handoff.directory.appendingPathComponent("adjacent.json"), limit: 4096))
        let path = adjacentDirectory(handoff)
        var info = stat()
        guard lstat(path.path, &info) == 0, info.st_dev == owner.device, info.st_ino == owner.inode,
              info.st_uid == getuid(), info.st_mode & S_IFMT == S_IFDIR else { throw UpdateError.invalidLocation }
        return path.appendingPathComponent("TinkerFin.app", isDirectory: true)
    }

    static func install(_ handoff: UpdateHandoff, release: DesktopRelease, lock: UpdateTargetLock, cancellation: UpdateCancellation,
                        progress: @Sendable (Double) -> Void) throws {
        let manager = FileManager.default
        guard lock.application == handoff.target else { throw UpdateError.invalidLocation }
        guard release.version == handoff.targetVersion else { throw UpdateError.invalidManifest }
        try checkIdentity(handoff.target, version: handoff.currentVersion)
        try checkIdentity(handoff.candidate, version: release.version)
        guard try UpdateTree.digest(handoff.candidate, cancellation: cancellation) == release.treeSHA256 else { throw UpdateError.invalidArchive }
        progress(15)
        try cleanAdjacent(handoff)
        let temporary = adjacentDirectory(handoff)
        guard mkdir(temporary.path, 0o700) == 0 else { throw UpdateError.invalidLocation }
        let adjacent = temporary.appendingPathComponent("TinkerFin.app", isDirectory: true)
        var exchanged = false
        defer {
            if !exchanged { do {
                try manager.removeItem(at: temporary)
                let record = handoff.directory.appendingPathComponent("adjacent.json")
                if manager.fileExists(atPath: record.path) { try manager.removeItem(at: record) }
            }
            catch { NSLog("TinkerFin 更新暂存应用清理失败：%@", String(describing: error)) } }
        }
        var ownership = stat()
        guard lstat(temporary.path, &ownership) == 0 else { throw UpdateError.invalidLocation }
        try UpdateWorkspace.write(AdjacentOwnership(device: ownership.st_dev, inode: ownership.st_ino), to: handoff.directory.appendingPathComponent("adjacent.json"))
        try manager.copyItem(at: handoff.candidate, to: adjacent)
        progress(65)
        guard try UpdateTree.digest(adjacent, cancellation: cancellation) == release.treeSHA256 else { throw UpdateError.invalidArchive }
        try checkIdentity(adjacent, version: release.version)
        try cancellation.check()
        progress(90)
        try checkIdentity(handoff.target, version: handoff.currentVersion)
        guard try UpdateTree.digest(handoff.target, cancellation: cancellation) == handoff.currentTreeSHA256 else { throw UpdateError.invalidArchive }
        guard renameatx_np(AT_FDCWD, adjacent.path, AT_FDCWD, handoff.target.path, UInt32(RENAME_SWAP)) == 0 else { throw UpdateError.invalidLocation }
        // 旧包保留到新身份采用凭证并完成清理，崩溃恢复仍需用旧代码删除旧Keychain条目
        exchanged = true
        progress(100)
        withExtendedLifetime(lock) {}
    }

    static func checkIdentity(_ application: URL, version: String) throws {
        let data = try UpdateWorkspace.read(application.appendingPathComponent("Contents/Info.plist"), limit: 1024 * 1024)
        guard let info = try PropertyListSerialization.propertyList(from: data, options: [], format: nil) as? [String: Any],
              info["CFBundleIdentifier"] as? String == "com.tinkerfin.companion",
              info["CFBundleShortVersionString"] as? String == version,
              info["CFBundleExecutable"] as? String == "TinkerFin" else { throw UpdateError.invalidArchive }
    }
}
