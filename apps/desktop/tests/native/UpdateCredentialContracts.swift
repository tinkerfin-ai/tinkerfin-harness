import Darwin
import Foundation
import Security

/// 仅在本轮FIFO存在时暂停真实Keychain写入，由测试显式控制崩溃边界
func SecItemAdd(_ attributes: CFDictionary, _ result: UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus {
    let arguments = CommandLine.arguments
    guard arguments.count == 3, arguments[1] == "--receive-update-credential" else {
        return Security.SecItemAdd(attributes, result)
    }
    let directory = URL(fileURLWithPath: arguments[2], isDirectory: true)
    let notification = directory.appendingPathComponent("keychain-ready.fifo")
    var info = stat()
    guard lstat(notification.path, &info) == 0 else { return Security.SecItemAdd(attributes, result) }
    guard info.st_mode & S_IFMT == S_IFIFO, info.st_uid == getuid() else { return errSecParam }
    let ready = open(notification.path, O_WRONLY | O_NOFOLLOW | O_CLOEXEC)
    guard ready >= 0 else { return errSecInternalComponent }
    defer { Darwin.close(ready) }
    let gate = open(directory.appendingPathComponent("keychain-continue.fifo").path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
    guard gate >= 0 else { return errSecInternalComponent }
    defer { Darwin.close(gate) }
    let event = Data("\(getpid())\n".utf8)
    guard event.withUnsafeBytes({ Darwin.write(ready, $0.baseAddress!, $0.count) }) == event.count else { return errSecInternalComponent }
    var signal: UInt8 = 0
    var count: Int
    repeat { count = Darwin.read(gate, &signal, 1) } while count < 0 && errno == EINTR
    guard count == 1 else { return errSecInternalComponent }
    return Security.SecItemAdd(attributes, result)
}

/// 两个实际签名应用使用正式交接入口；合成账户和密钥不输出到日志或文件
@main
@MainActor
struct CredentialContracts {
    static func main() async {
        let arguments = CommandLine.arguments
        if arguments.count == 3, arguments[1] == "--receive-update-credential" {
            let notification = URL(fileURLWithPath: arguments[2], isDirectory: true).appendingPathComponent("keychain-ready.fifo")
            var info = stat()
            if lstat(notification.path, &info) == 0 {
                guard info.st_mode & S_IFMT == S_IFIFO, info.st_uid == getuid(),
                      open(notification.path, O_WRONLY | O_NOFOLLOW | O_CLOEXEC) >= 0 else { Darwin.exit(2) }
                // 由进程退出关闭写端，测试据EOF确认失败清理没有遗留助手
            }
        }
        if let status = await UpdateCredentials.handle(arguments) { Darwin.exit(status) }
        do {
            let mode = arguments[1], directory = URL(fileURLWithPath: arguments[2], isDirectory: true)
            let keychain = KeychainStore()
            if mode == "read" || mode == "remove" {
                if mode == "read" { _ = try await keychain.read(account: arguments[3]) }
                else { try await keychain.remove(account: arguments[3]) }
            } else if mode == "setup" {
                let handoff = try UpdateHandoff.read(directory)
                var saved = SavedConfiguration()
                saved.connection = ConnectionConfiguration(studioURL: "http://127.0.0.1:1", apiURL: "http://127.0.0.1:1")
                saved.name = "Update test"; saved.keychainAccount = arguments[3]
                saved.device = PairedDevice(deviceID: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", namespace: "ns_test", name: saved.name)
                _ = try await keychain.create(account: arguments[3])
                try await ConfigurationStore(directory: URL(fileURLWithPath: handoff.stateDirectory)).save(saved)
            } else {
                let handoff = try UpdateHandoff.read(directory)
                let state = URL(fileURLWithPath: handoff.stateDirectory, isDirectory: true)
                if mode == "prepare" || mode == "prepared-crash" {
                    let saved = try await ConfigurationStore(directory: state).read()
                    guard let account = saved.keychainAccount else { Darwin.exit(2) }
                    let secret = try await keychain.read(account: account)
                    try await UpdateCredentials.prepare(handoff, account: account, secret: secret)
                    if mode == "prepared-crash" { _exit(41) }
                } else if mode == "cancel" {
                    let saved = try await ConfigurationStore(directory: state).read()
                    guard let account = saved.keychainAccount else { Darwin.exit(2) }
                    try await UpdateCredentials.cancel(handoff, secret: keychain.read(account: account))
                } else if mode == "install" {
                    let release = try DesktopRelease.verify(manifest: UpdateWorkspace.read(directory.appendingPathComponent("update.json"), limit: 65536),
                        signature: UpdateWorkspace.read(directory.appendingPathComponent("update.json.sig"), limit: 64))
                    try await UpdateFileWork().perform { cancellation in
                        let lock = try UpdateTargetLock(application: handoff.target)
                        try UpdateInstallation.install(handoff, release: release, lock: lock, cancellation: cancellation) { _ in }
                    }
                } else if mode == "adopt" || mode == "adopted-crash" {
                    let model = CompanionModel(state: state, resources: Bundle.main.resourceURL!)
                    do {
                        try await model.restore(connect: false)
                        try await model.adoptUpdateCredential(handoff)
                        if mode == "adopted-crash" { _exit(42) }
                        try await model.settleUpdateCredential(handoff, installed: true)
                        await model.shutdown()
                    } catch { await model.shutdown(); throw error }
                } else if mode == "cleanup" {
                    try UpdateWorkspace.remove(directory, root: directory.deletingLastPathComponent())
                } else { Darwin.exit(2) }
            }
            await keychain.clear()
            print("verified " + mode)
        } catch {
            try? FileHandle.standardError.write(contentsOf: Data((String(describing: error) + "\n").utf8))
            Darwin.exit(1)
        }
    }
}
