import CryptoKit
import Foundation

enum UpdateError: Error { case invalidManifest, invalidArchive, invalidLocation, failedProcess, unavailable }

/// 只向界面提供恢复操作所需的原因，不传递路径、请求地址或底层错误正文
enum DesktopUpdateFailure: String, Codable, Sendable {
    case none, network, storage, release, unavailable

    init(error: Error) {
        if let error = error as? UpdateError {
            switch error {
            case .invalidLocation: self = .storage
            case .invalidManifest, .invalidArchive, .unavailable: self = .release
            case .failedProcess: self = .unavailable
            }
        } else if error is DecodingError {
            self = .release
        } else {
            let detail = error as NSError
            if detail.domain == NSURLErrorDomain { self = .network }
            else if detail.domain == NSPOSIXErrorDomain || detail.domain == NSCocoaErrorDomain
                && (NSFileErrorMinimum...NSFileErrorMaximum).contains(detail.code) { self = .storage }
            else { self = .unavailable }
        }
    }
}

struct DesktopReleaseVersion: Comparable, Sendable {
    let components: [Int]
    init(_ value: String) throws {
        guard value.range(of: "^(0|[1-9][0-9]{0,5})\\.(0|[1-9][0-9]{0,5})\\.(0|[1-9][0-9]{0,5})$", options: .regularExpression) != nil else {
            throw UpdateError.invalidManifest
        }
        components = value.split(separator: ".").compactMap { Int($0) }
    }
    static func < (left: Self, right: Self) -> Bool { left.components.lexicographicallyPrecedes(right.components) }
}

/// 签名绑定唯一官方发行包与解压后的完整应用；安装助手必须独立复验
struct DesktopRelease: Codable, Sendable {
    static let repository = "tinkerfin-ai/tinkerfin-harness"
    let version: String
    let target: String
    let bundleID: String
    let archiveURL: String
    let archiveSize: Int64
    let archiveSHA256: String
    let treeSHA256: String
    enum CodingKeys: String, CodingKey {
        case version, target
        case bundleID = "bundle_id", archiveURL = "archive_url", archiveSize = "archive_size"
        case archiveSHA256 = "archive_sha256", treeSHA256 = "tree_sha256"
    }

    static func verify(manifest: Data, signature: Data, publicKey: Data = UpdateTrust.publicKey) throws -> Self {
        guard manifest.count <= 64 * 1024, signature.count == 64,
              let key = try? Curve25519.Signing.PublicKey(rawRepresentation: publicKey),
              key.isValidSignature(signature, for: manifest) else { throw UpdateError.invalidManifest }
        let value = try JSONDecoder().decode(Self.self, from: manifest)
        _ = try DesktopReleaseVersion(value.version)
        guard value.target == "macos-arm64", value.bundleID == "com.tinkerfin.companion",
              value.archiveURL == "https://github.com/\(repository)/releases/download/desktop-v\(value.version)/TinkerFin.zip",
              value.archiveSize > 0, value.archiveSize <= 2 * 1024 * 1024 * 1024,
              [value.archiveSHA256, value.treeSHA256].allSatisfy({ $0.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil }) else {
            throw UpdateError.invalidManifest
        }
        return value
    }
}

/// 有界文件处理在专用串行队列执行；取消需等当前读写退出后才可移除暂存目录
final class UpdateFileWork: @unchecked Sendable {
    private let queue = DispatchQueue(label: "com.tinkerfin.desktop.update-files")
    func perform<T: Sendable>(_ work: @escaping @Sendable (UpdateCancellation) throws -> T) async throws -> T {
        let cancellation = UpdateCancellation()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                queue.async {
                    do { try cancellation.check(); continuation.resume(returning: try work(cancellation)) }
                    catch { continuation.resume(throwing: error) }
                }
            }
        } onCancel: { cancellation.cancel() }
    }
}

final class UpdateCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    func cancel() { lock.withLock { cancelled = true } }
    func check() throws { if lock.withLock({ cancelled }) { throw CancellationError() } }
}

enum UpdateTree {
    static func digest(_ root: URL, cancellation: UpdateCancellation) throws -> String {
        let manager = FileManager.default
        guard root.resolvingSymlinksInPath() == root.standardizedFileURL else { throw UpdateError.invalidArchive }
        let root = root.standardizedFileURL
        guard let enumerator = manager.enumerator(at: root, includingPropertiesForKeys: nil) else { throw UpdateError.invalidArchive }
        var entries: [(String, URL)] = []
        while let path = enumerator.nextObject() as? URL {
            try cancellation.check()
            guard entries.count < 250_000 else { throw UpdateError.invalidArchive }
            let path = path.standardizedFileURL
            guard path.path.hasPrefix(root.path + "/") else { throw UpdateError.invalidArchive }
            let relative = String(path.path.dropFirst(root.path.count + 1)).precomposedStringWithCanonicalMapping
            entries.append((relative, path))
        }
        entries.sort { $0.0.utf8.lexicographicallyPrecedes($1.0.utf8) }
        var digest = SHA256(), total: Int64 = 0, names: Set<String> = []
        for (name, path) in entries {
            try cancellation.check()
            guard name.utf8.count <= 4096, !name.contains(where: { "\0\n\r\\".contains($0) }),
                  names.insert(name.precomposedStringWithCanonicalMapping.folding(options: [.caseInsensitive], locale: Locale(identifier: "en_US_POSIX"))).inserted else { throw UpdateError.invalidArchive }
            let attributes = try manager.attributesOfItem(atPath: path.path)
            guard let kind = attributes[.type] as? FileAttributeType, let mode = attributes[.posixPermissions] as? NSNumber,
                  mode.intValue & ~0o777 == 0 else { throw UpdateError.invalidArchive }
            let type: String, size: Int64, content: String
            switch kind {
            case .typeDirectory: type = "d"; size = 0; content = ""
            case .typeSymbolicLink:
                type = "l"; content = try manager.destinationOfSymbolicLink(atPath: path.path).precomposedStringWithCanonicalMapping; size = Int64(content.utf8.count)
                let resolved = path.resolvingSymlinksInPath().path
                guard !content.hasPrefix("/"), !content.contains(where: { "\0\n\r\\".contains($0) }),
                      (resolved == root.path || resolved.hasPrefix(root.path + "/")), manager.fileExists(atPath: resolved) else { throw UpdateError.invalidArchive }
            case .typeRegular:
                type = "f"; size = (attributes[.size] as? NSNumber)?.int64Value ?? -1
                total += size
                guard size >= 0, size <= 1024 * 1024 * 1024, total <= 8 * 1024 * 1024 * 1024 else { throw UpdateError.invalidArchive }
                content = try fileDigest(path, cancellation: cancellation)
            default: throw UpdateError.invalidArchive
            }
            digest.update(data: Data("\(type)\0\(name)\0\(mode.intValue)\0\(size)\0\(content)\n".utf8))
        }
        return digest.finalize().map { String(format: "%02x", $0) }.joined()
    }

    static func fileDigest(_ path: URL, cancellation: UpdateCancellation) throws -> String {
        let handle = try FileHandle(forReadingFrom: path)
        defer { try? handle.close() }
        var digest = SHA256()
        while let content = try handle.read(upToCount: 1024 * 1024), !content.isEmpty {
            try cancellation.check(); digest.update(data: content)
        }
        return digest.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
