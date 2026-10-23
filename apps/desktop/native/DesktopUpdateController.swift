import AppKit
import Foundation

/// 唯一更新流程所有者：每个阶段等待真实结果，取消先结算网络/解压和维护，再删除独占暂存目录
@MainActor
final class DesktopUpdateController {
    private let presentation: DesktopPresentation
    private let resources: URL
    private let files = UpdateFileWork()
    private var operation: Task<Void, Never>?
    private var selected: SignedDesktopRelease?
    private var directory: URL?
    private var installer: UpdateInstallerProcess?
    private var cancelling = false
    private var restored: UpdateHandoff?
    private var interfaceReady = false
    private var acknowledging = false
    private var installed = false
    private var installationLock: UpdateTargetLock?
    private(set) var otherInstallerActive = false
    private(set) var hasTransferredRestart = false
    private(set) var isUpdateLaunch = false
    var restart: (() -> Void)?
    var quit: (() -> Void)?
    private var model: CompanionModel { presentation.model }
    private var version: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "" }

    init(presentation: DesktopPresentation, resources: URL) { self.presentation = presentation; self.resources = resources }

    func handle(_ command: DesktopUIAction) async -> DesktopReply {
        guard !cancelling else { return DesktopReply(ok: false) }
        do {
            switch command {
            case .checkUpdate(let view):
                guard presentation.update.phase == .idle, presentation.canCheckUpdates, operation == nil else { return DesktopReply(ok: false) }
                try UpdateWorkspace.checkInstallationLocation(Bundle.main.bundleURL, state: model.stateDirectory)
                try presentation.preserve(view); try model.beginUpdateSession()
                set(.checking); launch { await self.check() }
            case .updateConfirm:
                guard presentation.update.phase == .available, operation == nil else { return DesktopReply(ok: false) }
                set(.downloading); launch { await self.download() }
            case .updateRetry:
                guard operation == nil else { return DesktopReply(ok: false) }
                switch presentation.update.phase {
                case .checkError: set(.checking); launch { await self.check() }
                case .downloadError, .verifyError: set(.downloading); launch { await self.download() }
                case .installError:
                    cancelling = true; defer { cancelling = false }
                    try await model.resumeAfterUpdate(); try model.beginUpdateSession()
                    set(.verifying); launch { await self.verifyAndInstall() }
                case .restartError: set(.restarting); launch { await self.restartInstalled() }
                default: return DesktopReply(ok: false)
                }
            case .updateCancel:
                guard !presentation.update.phase.locked, presentation.update.phase != .idle else { return DesktopReply(ok: false) }
                if presentation.update.phase == .restartError {
                    quit?(); return DesktopReply(ok: true)
                }
                cancelling = true; defer { cancelling = false }
                operation?.cancel(); await operation?.value
                try await model.resumeAfterUpdate()
                try await cleanup(); installationLock = nil; selected = nil; set(.idle)
            default: return DesktopReply(ok: false)
            }
            return DesktopReply(ok: true)
        } catch { return DesktopReply(ok: false, message: model.localization.text("请重试")) }
    }

    private func launch(_ work: @escaping @MainActor () async -> Void) {
        operation = Task { defer { operation = nil }; await work() }
    }

    private func set(_ phase: DesktopUpdatePhase, percent: Double = 0, failure: DesktopUpdateFailure = .none) {
        presentation.update = DesktopUpdateState(phase: phase, percent: percent, currentVersion: version,
            targetVersion: selected?.release.version ?? presentation.update.targetVersion, failure: failure)
    }

    private func fail(_ phase: DesktopUpdatePhase, error: Error) {
        let failure = DesktopUpdateFailure(error: error), detail = error as NSError
        NSLog("TinkerFin 更新失败：stage=%@ reason=%@ domain=%@ code=%ld", phase.rawValue, failure.rawValue, detail.domain, detail.code)
        set(phase, failure: failure)
    }

    private func check() async {
        do {
            selected = try await DesktopReleaseService.check(current: version)
            try Task.checkCancellation(); set(selected == nil ? .latest : .available)
        } catch is CancellationError { }
        catch { fail(.checkError, error: error) }
    }

    private func download() async {
        guard let selected else { set(.checkError); return }
        do {
            try await cleanup()
            let root = model.updateDirectory
            let directory = try await files.perform { _ in try UpdateWorkspace.create(root: root) }
            self.directory = directory
            let release = selected.release
            _ = try await UpdateTransfer().fetch(URL(string: release.archiveURL)!, limit: release.archiveSize,
                destination: directory.appendingPathComponent("TinkerFin.zip")) { [weak self] size in
                    DispatchQueue.main.async {
                        guard let self, self.directory == directory, self.presentation.update.phase == .downloading else { return }
                        self.set(.downloading, percent: Double(size) * 100 / Double(release.archiveSize))
                    }
                }
            try Task.checkCancellation(); set(.verifying)
            await verifyAndInstall()
        } catch is CancellationError { }
        catch { fail(.downloadError, error: error) }
    }

    private func verifyAndInstall() async {
        guard let selected, let directory else { set(.verifyError); return }
        do {
            try await cancelPreparedCredential(directory)
            try await files.perform { cancellation in
                let archive = directory.appendingPathComponent("TinkerFin.zip")
                guard try UpdateTree.fileDigest(archive, cancellation: cancellation) == selected.release.archiveSHA256 else { throw UpdateError.invalidArchive }
                try selected.manifest.write(to: directory.appendingPathComponent("update.json"), options: .atomic)
                try selected.signature.write(to: directory.appendingPathComponent("update.json.sig"), options: .atomic)
                let unpacked = directory.appendingPathComponent("unpacked")
                if FileManager.default.fileExists(atPath: unpacked.path) { try FileManager.default.removeItem(at: unpacked) }
            }
            let release = selected.release
            try await UpdateSubprocess().run(executable: resources.appendingPathComponent("python/bin/python3"), arguments: [
                "-I", "-B", resources.appendingPathComponent("update_archive.py").path,
                "--archive", directory.appendingPathComponent("TinkerFin.zip").path,
                "--destination", directory.appendingPathComponent("unpacked").path,
                "--size", String(release.archiveSize), "--sha256", release.archiveSHA256,
                "--tree", release.treeSHA256, "--release", release.version,
            ])
            try await UpdateSubprocess().run(executable: URL(fileURLWithPath: "/usr/bin/codesign"),
                arguments: ["--verify", "--deep", "--strict", directory.appendingPathComponent("unpacked/TinkerFin.app").path])
            let application = Bundle.main.bundleURL, currentVersion = version, root = model.updateDirectory
            let existingLock = installationLock
            let (lock, currentTree) = try await files.perform { cancellation in
                let lock = try existingLock ?? UpdateTargetLock(application: application)
                try UpdateInstallation.checkIdentity(application, version: currentVersion)
                try UpdateWorkspace.settlePrevious(root: root, application: application, currentVersion: currentVersion, excluding: directory.lastPathComponent)
                let digest = try UpdateTree.digest(application, cancellation: cancellation)
                return (lock, digest)
            }
            installationLock = lock
            try Task.checkCancellation(); set(.waiting)
            try await presentation.finishAcceptedActions()
            try await model.prepareForUpdate()
            try Task.checkCancellation()
            let handoff = UpdateHandoff(identifier: directory.lastPathComponent, stateDirectory: model.stateDirectory.path,
                application: Bundle.main.bundleURL.path, currentVersion: version, targetVersion: release.version,
                currentTreeSHA256: currentTree,
                parentPID: ProcessInfo.processInfo.processIdentifier, view: presentation.viewState,
                language: model.localization.language.rawValue, locale: model.localization.identifier)
            let helper = resources.appendingPathComponent("TinkerFinUpdater")
            try await files.perform { _ in
                _ = try handoff.checked(in: directory)
                try UpdateWorkspace.write(handoff, to: directory.appendingPathComponent("handoff.json"))
                let target = directory.appendingPathComponent("TinkerFinUpdater")
                if FileManager.default.fileExists(atPath: target.path) { try FileManager.default.removeItem(at: target) }
                try FileManager.default.copyItem(at: helper, to: target)
            }
            try await model.prepareUpdateCredential(handoff: handoff)
            try await model.commitUpdatePreparation()
            try Task.checkCancellation(); set(.installing)
            let installer = UpdateInstallerProcess(); self.installer = installer
            try await installer.install(directory: directory, lock: lock) { [weak self] value in self?.set(.installing, percent: value) }
            installed = true
            set(.restarting)
            try await installer.requestRestart()
            hasTransferredRestart = true
            restart?()
        } catch is CancellationError { }
        catch {
            await installer?.close(); installer = nil
            if !installed, presentation.update.phase == .installing {
                installed = (try? await files.perform { cancellation in
                    let target = Bundle.main.bundleURL
                    try UpdateInstallation.checkIdentity(target, version: selected.release.version)
                    return try UpdateTree.digest(target, cancellation: cancellation) == selected.release.treeSHA256
                }) ?? false
            }
            fail(installed ? .restartError : presentation.update.phase == .verifying ? .verifyError : .installError, error: error)
        }
    }

    private func restartInstalled() async {
        guard installed, let directory, let lock = installationLock else { set(.restartError); return }
        do {
            let installer = UpdateInstallerProcess(); self.installer = installer
            try await installer.install(directory: directory, lock: lock, restarting: true) { _ in }
            try await installer.requestRestart(); hasTransferredRestart = true; restart?()
        } catch { await installer?.close(); installer = nil; fail(.restartError, error: error) }
    }

    private func cleanup() async throws {
        guard let directory else { return }
        if !installed { try await cancelPreparedCredential(directory) }
        let root = model.updateDirectory
        try await files.perform { _ in try UpdateWorkspace.remove(directory, root: root) }
        self.directory = nil
    }

    private func cancelPreparedCredential(_ directory: URL) async throws {
        let handoff = try await files.perform { _ -> UpdateHandoff? in
            guard FileManager.default.fileExists(atPath: directory.appendingPathComponent("credentials.json").path) else { return nil }
            return try UpdateHandoff.read(directory)
        }
        if let handoff { try await model.settleUpdateCredential(handoff, installed: false) }
    }

    func restoreHandoff() async throws {
        guard model.ready else { return }
        let state = model.stateDirectory, current = Bundle.main.bundleURL.standardizedFileURL, version = version
        let arguments = CommandLine.arguments
        let specified = arguments.firstIndex(of: "--update-receipt").flatMap { arguments.indices.contains($0 + 1) ? URL(fileURLWithPath: arguments[$0 + 1]) : nil }
        let recovery = try await files.perform { _ -> (UpdateHandoff?, Bool, UpdateTargetLock?) in
            try UpdateWorkspace.resumeCleanup(root: state.appendingPathComponent("state/updates"))
            let busy = try UpdateTargetLock.isBusy(application: current)
            let pending = state.appendingPathComponent("state/updates/pending.json")
            let directory: URL
            if let specified { directory = specified }
            else {
                guard FileManager.default.fileExists(atPath: pending.path) else { return (nil, busy, nil) }
                let receipt = try JSONDecoder().decode(UpdateInstallReceipt.self, from: UpdateWorkspace.read(pending, limit: 4096))
                guard receipt.identifier.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil else { throw UpdateError.invalidLocation }
                directory = pending.deletingLastPathComponent().appendingPathComponent(receipt.identifier)
            }
            let handoff = try UpdateHandoff.read(directory)
            guard handoff.target == current,
                  URL(fileURLWithPath: handoff.stateDirectory).standardizedFileURL == state.standardizedFileURL else { throw UpdateError.invalidLocation }
            let release = try DesktopRelease.verify(manifest: UpdateWorkspace.read(directory.appendingPathComponent("update.json"), limit: 64 * 1024),
                signature: UpdateWorkspace.read(directory.appendingPathComponent("update.json.sig"), limit: 64))
            guard release.version == handoff.targetVersion else { throw UpdateError.invalidManifest }
            if version == handoff.currentVersion {
                if busy { return (nil, true, nil) }
                let lock = try UpdateTargetLock(application: current)
                try UpdateInstallation.checkIdentity(current, version: version)
                return (handoff, false, lock)
            }
            guard release.version == version else { throw UpdateError.invalidManifest }
            let lock = busy ? nil : try UpdateTargetLock(application: current)
            return (handoff, false, lock)
        }
        otherInstallerActive = recovery.1; installationLock = recovery.2
        if let handoff = recovery.0, version == handoff.currentVersion {
            try await model.settleUpdateCredential(handoff, installed: false)
            try await files.perform { _ in try UpdateWorkspace.remove(handoff.directory, root: handoff.directory.deletingLastPathComponent()) }
            installationLock = nil
            return
        }
        if let restored = recovery.0 {
            directory = restored.directory; installed = true
            isUpdateLaunch = true
            try await model.adoptUpdateCredential(restored)
            try await model.settleUpdateCredential(restored, installed: true)
            self.restored = restored
            if let view = restored.view { try presentation.preserve(view) }
            presentation.update = DesktopUpdateState(phase: .success, percent: 100, currentVersion: version, targetVersion: version)
        }
    }

    func acknowledgeLaunch(interfaceReady: Bool = false) async {
        self.interfaceReady = self.interfaceReady || interfaceReady
        guard self.interfaceReady, model.ready, let restored, !acknowledging else { return }
        acknowledging = true
        do {
            let receipt = UpdateLaunchReceipt(identifier: restored.identifier, version: version, processID: ProcessInfo.processInfo.processIdentifier)
            let ownedLock = installationLock
            try await files.perform { _ in
                try UpdateWorkspace.write(receipt, to: restored.directory.appendingPathComponent("launched.json"))
                // 助手仍持锁时保留pending；它退出后任一新进程都能接管尚未完成的清理
                if try ownedLock != nil || !UpdateTargetLock.isBusy(application: restored.target) {
                    let lock = try ownedLock ?? UpdateTargetLock(application: restored.target)
                    try UpdateWorkspace.remove(restored.directory, root: restored.directory.deletingLastPathComponent())
                    withExtendedLifetime(lock) {}
                }
            }
            self.restored = nil
            if ownedLock != nil { directory = nil; installationLock = nil }
        } catch { acknowledging = false; NSLog("TinkerFin 更新启动回执未保存") }
    }

    func close() async {
        operation?.cancel(); await operation?.value
        await installer?.close(); installer = nil
        if installed, let directory {
            do { try await files.perform { _ in
                let handoff = try UpdateHandoff.read(directory)
                try UpdateWorkspace.write(UpdateInstallReceipt(identifier: handoff.identifier, version: handoff.targetVersion,
                    helperPID: 0, deferred: true), to: handoff.pending)
            } } catch { NSLog("TinkerFin 更新交接保留未完成") }
        } else { do { try await cleanup() } catch { NSLog("TinkerFin 更新目录未清理") } }
        installationLock = nil
        restart = nil; quit = nil
    }
}
