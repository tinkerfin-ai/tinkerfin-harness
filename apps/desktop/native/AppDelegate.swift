import AppKit
import Combine

@MainActor
final class CompanionAppDelegate: NSObject, NSApplicationDelegate {
    private let model: CompanionModel
    private let resources: URL
    private let presentation: DesktopPresentation
    private let updater: DesktopUpdateController
    private var windows: DesktopWindows?
    private var statusItem: NSStatusItem?
    private var languageSubscription: AnyCancellable?
    private var appearance: NSKeyValueObservation?
    private var localeObserver: NSObjectProtocol?
    private var startup: Task<Void, Never>?
    private var navigation: Task<Void, Never>?
    private var shutdown: Task<Void, Never>?
    private var requestedUpdateExit = false
    private var restoring = true
    private var quitMenuItem: NSMenuItem?
    private var acknowledgement: Task<Void, Never>?

    init(model: CompanionModel, resources: URL) {
        self.model = model; self.resources = resources
        presentation = DesktopPresentation(model: model)
        updater = DesktopUpdateController(presentation: presentation, resources: resources)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMainMenu()
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let image = NSImage(contentsOf: resources.appendingPathComponent("brand.svg")) {
            image.size = NSSize(width: 18, height: 18 * image.size.height / image.size.width); image.isTemplate = true
            item.button?.image = image
        }
        item.button?.imagePosition = .imageOnly; item.button?.setButtonType(.pushOnPushOff)
        item.button?.setAccessibilityLabel("TinkerFin")
        item.button?.target = self; item.button?.action = #selector(toggleStatus)
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp]); statusItem = item
        guard let button = item.button else { requestApplicationTermination(); return }
        let windows = DesktopWindows(resources: resources, button: button); self.windows = windows
        for ui in [windows.workbenchUI, windows.statusUI] {
            ui.handle = { [weak self] command in await self?.handle(command) ?? DesktopReply(ok: false) }
            ui.refresh = { [weak self] in self?.publish() }
        }
        windows.activated = { [weak self] in
            guard let self, self.shutdown == nil else { return }
            self.model.localization.refreshSystemLanguage(); self.model.checkPermissions()
            self.model.refreshProjects(); self.model.resumeAuthorization(); self.publish()
        }
        presentation.changed = { [weak self] in self?.publish() }
        updater.restart = { [weak self] in
            guard let self else { return }
            self.requestedUpdateExit = true
            requestApplicationTermination()
        }
        updater.quit = { [weak self] in
            guard let self else { return }
            self.requestedUpdateExit = true
            requestApplicationTermination()
        }
        languageSubscription = model.localization.objectWillChange.receive(on: DispatchQueue.main).sink { [weak self] _ in
            MainActor.assumeIsolated { self?.installMainMenu(); self?.publish() }
        }
        localeObserver = NotificationCenter.default.addObserver(forName: NSLocale.currentLocaleDidChangeNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.model.localization.refreshSystemLanguage() }
        }
        appearance = NSApp.observe(\.effectiveAppearance) { [weak self] _, _ in MainActor.assumeIsolated { self?.publish() } }
        startup = Task { [weak self] in
            guard let self else { return }
            do { try await model.restore(connect: false) }
            catch is CancellationError { return }
            catch ApplicationLockError.alreadyRunning { requestApplicationTermination(); return }
            catch { }
            if model.ready {
                do { try await updater.restoreHandoff() }
                catch is CancellationError { return }
                catch {
                    NSLog("TinkerFin 更新交接恢复失败：%@", String(describing: error))
                    requestApplicationTermination(); return
                }
                if updater.otherInstallerActive { requestApplicationTermination(); return }
                do { try await model.connectRestoredState() }
                catch is CancellationError { return }
                catch { }
            }
            guard !Task.isCancelled else { return }
            restoring = false
            windows.showWorkbench(); publish()
            await updater.acknowledgeLaunch()
        }
    }

    private func handle(_ command: DesktopUIAction) async -> DesktopReply {
        guard shutdown == nil else { return DesktopReply(ok: false) }
        switch command {
        case .ready: publish(); await updater.acknowledgeLaunch(interfaceReady: true); return DesktopReply(ok: true)
        case .openWorkbench:
            guard presentation.update.phase == .idle else { return DesktopReply(ok: false) }
            windows?.showWorkbench(); return DesktopReply(ok: true)
        case .quit:
            guard presentation.update.phase == .idle else { return DesktopReply(ok: false) }
            requestApplicationTermination(); return DesktopReply(ok: true)
        case .checkUpdate, .updateConfirm, .updateCancel, .updateRetry: return await updater.handle(command)
        default: return await presentation.handle(command)
        }
    }

    private func publish() {
        guard shutdown == nil else { return }
        windows?.interactionEnabled = presentation.update.phase == .idle
        quitMenuItem?.isEnabled = presentation.update.phase == .idle
        var workbench = presentation.snapshot(surface: .workbench), status = presentation.snapshot(surface: .status)
        if restoring { workbench.ready = false; status.ready = false }
        windows?.workbenchUI.publish(workbench)
        windows?.statusUI.publish(status)
    }

    private func exitItem() -> NSMenuItem {
        let item = NSMenuItem(title: model.localization.identifier == "en" ? "Quit" : "退出", action: #selector(quit), keyEquivalent: "q")
        item.target = self
        if let image = NSImage(contentsOf: resources.appendingPathComponent("icons/log-out.svg")) {
            image.isTemplate = true; image.size = NSSize(width: 16, height: 16); item.image = image
        }
        item.isEnabled = presentation.update.phase == .idle
        return item
    }

    private func installMainMenu() {
        let main = NSMenu(), application = NSMenuItem(), applicationMenu = NSMenu()
        let exit = exitItem(); quitMenuItem = exit; applicationMenu.autoenablesItems = false
        applicationMenu.addItem(exit); application.submenu = applicationMenu; main.addItem(application)
        let editing = NSMenuItem(), editMenu = NSMenu(title: model.localization.text("编辑"))
        for (title, selector, key) in [("撤销", "undo:", "z"), ("重做", "redo:", "Z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            editMenu.addItem(NSMenuItem(title: model.localization.text(title), action: Selector(selector), keyEquivalent: key))
        }
        editing.submenu = editMenu; main.addItem(editing); NSApp.mainMenu = main
    }

    @objc private func toggleStatus() {
        guard presentation.update.phase == .idle, shutdown == nil else { return }
        if let event = NSApp.currentEvent,
           event.type == .rightMouseUp || (event.type == .leftMouseUp && event.modifierFlags.contains(.control)),
           let button = statusItem?.button {
            windows?.hideStatus()
            let menu = NSMenu(); menu.autoenablesItems = false; menu.addItem(exitItem())
            button.state = .on
            NSMenu.popUpContextMenu(menu, with: event, for: button)
            button.state = .off
        } else { windows?.toggleStatus() }
    }

    @objc private func quit() { requestApplicationTermination() }
    func applicationDidBecomeActive(_ notification: Notification) {
        model.localization.refreshSystemLanguage(); model.checkPermissions()
        guard shutdown == nil, acknowledgement == nil else { return }
        acknowledgement = Task { defer { acknowledgement = nil }; await updater.acknowledgeLaunch() }
    }
    func applicationDidResignActive(_ notification: Notification) { windows?.hideStatus() }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { windows?.showWorkbench(); return true }
    func applicationDidChangeScreenParameters(_ notification: Notification) { windows?.positionStatus(); publish() }
    func application(_ application: NSApplication, open urls: [URL]) {
        guard presentation.update.phase == .idle, shutdown == nil else { return }
        for url in urls {
            if isClientConnectURL(url) { windows?.showWorkbench(); continue }
            if url.scheme == "tinkerfin", url.host == "workspace" {
                navigation?.cancel()
                navigation = Task { [weak self] in
                    guard let self else { return }
                    await startup?.value
                    do { try Task.checkCancellation(); try await model.openProjectURL(url) } catch { }
                }
            }
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard requestedUpdateExit || presentation.update.phase == .idle || (updater.isUpdateLaunch && presentation.update.phase == .success) else { return .terminateCancel }
        if shutdown == nil {
            shutdown = Task {
                startup?.cancel(); navigation?.cancel(); acknowledgement?.cancel()
                await startup?.value; await navigation?.value; await acknowledgement?.value
                await presentation.prepareToQuit()
                if !updater.hasTransferredRestart { await updater.close() }
                await model.shutdown()
                await windows?.close(); windows = nil
                if let localeObserver { NotificationCenter.default.removeObserver(localeObserver) }
                localeObserver = nil; appearance = nil; languageSubscription = nil; presentation.close()
                if let statusItem { NSStatusBar.system.removeStatusItem(statusItem) }; statusItem = nil
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }
}
