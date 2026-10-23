import AppKit
import Darwin

/// 安装位置固定，下载副本与自定义状态目录不能产生第二个客户端实例
enum ClientInstallation {
    static let application = URL(fileURLWithPath: "/Applications/TinkerFin.app", isDirectory: true)

    static func accepts(_ location: URL) -> Bool {
        location.standardizedFileURL.path == application.path && location.resolvingSymlinksInPath().path == application.path
    }

    @MainActor
    static func showReplacementRequired() {
        let chinese = Locale.preferredLanguages.first?.hasPrefix("zh") == true
        let alert = NSAlert()
        alert.messageText = chinese ? "请替换应用程序中的 TinkerFin" : "Replace TinkerFin in Applications"
        alert.informativeText = chinese ? "退出旧版后，将此应用移到“应用程序”并选择“替换”" : "Quit the installed app, move this app to Applications, then choose Replace"
        alert.addButton(withTitle: chinese ? "好" : "OK")
        NSApp.activate(); alert.runModal()
    }
}

/// 所有安装和状态目录共享此机器锁；不保存用户数据，跨用户只读打开即可互斥
final class ClientInstanceLock: @unchecked Sendable {
    static let location = URL(fileURLWithPath: "/private/tmp/com.tinkerfin.companion.client.lock")
    private let descriptor: Int32

    init(file: URL) throws {
        var descriptor = open(file.path, O_CREAT | O_EXCL | O_RDONLY | O_NOFOLLOW | O_CLOEXEC, 0o444)
        if descriptor >= 0 {
            guard fchmod(descriptor, 0o444) == 0 else { Darwin.close(descriptor); throw UpdateError.invalidLocation }
        } else {
            guard errno == EEXIST else { throw UpdateError.invalidLocation }
            descriptor = open(file.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        }
        guard descriptor >= 0 else { throw UpdateError.invalidLocation }
        var entry = stat(), opened = stat()
        guard lstat(file.path, &entry) == 0, fstat(descriptor, &opened) == 0,
              opened.st_mode & S_IFMT == S_IFREG, opened.st_mode & 0o777 == 0o444,
              opened.st_nlink == 1, opened.st_size == 0,
              opened.st_dev == entry.st_dev, opened.st_ino == entry.st_ino else {
            Darwin.close(descriptor); throw UpdateError.invalidLocation
        }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            Darwin.close(descriptor); throw ApplicationLockError.alreadyRunning
        }
        self.descriptor = descriptor
    }

    deinit { Darwin.close(descriptor) }
}
