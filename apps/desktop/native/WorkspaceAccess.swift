import Darwin
import Foundation

/// 应用目录的独占锁由客户端持有至退出
final class WorkspaceAccess: @unchecked Sendable {
    private let queue = DispatchQueue(label: "com.tinkerfin.companion.workspaces")

    func prepareProjects(_ root: URL, components: [String] = []) async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                do {
                    guard root.standardizedFileURL.path == root.resolvingSymlinksInPath().standardizedFileURL.path else {
                        throw ClientError.workspaceAccess
                    }
                    var selected = root
                    for component in [""] + components {
                        if !component.isEmpty { selected.appendPathComponent(component, isDirectory: true) }
                        if !FileManager.default.fileExists(atPath: selected.path) {
                            try FileManager.default.createDirectory(at: selected, withIntermediateDirectories: false,
                                attributes: [.posixPermissions: 0o700])
                        }
                        let info = try selected.resourceValues(forKeys: [.isSymbolicLinkKey, .isDirectoryKey])
                        let attributes = try FileManager.default.attributesOfItem(atPath: selected.path)
                        guard info.isSymbolicLink != true, info.isDirectory == true,
                              (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid() else {
                            throw ClientError.workspaceAccess
                        }
                    }
                    continuation.resume(returning: selected)
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

    func lockState(_ directory: URL) async throws -> (ApplicationLock, URL) {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                do {
                    let canonical = directory.standardizedFileURL.resolvingSymlinksInPath()
                    guard canonical.path != "/" else { throw ClientError.message("应用状态不能保存在系统根目录") }
                    continuation.resume(returning: (try ApplicationLock(directory: canonical), canonical))
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

}

enum ApplicationLockError: Error { case alreadyRunning }

/// 同一状态目录只允许一个原生客户端修改配对和授权
final class ApplicationLock: @unchecked Sendable {
    private let descriptor: Int32
    init(directory: URL) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let descriptor = Darwin.open(directory.appendingPathComponent("application.lock").path,
                                     O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard descriptor >= 0 else { throw ClientError.message("无法打开本机状态目录") }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            Darwin.close(descriptor)
            throw ApplicationLockError.alreadyRunning
        }
        self.descriptor = descriptor
    }
    deinit { Darwin.close(descriptor) }
}
