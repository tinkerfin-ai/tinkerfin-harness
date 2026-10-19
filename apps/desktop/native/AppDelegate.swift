import AppKit
import Combine

private final class MenuPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class CompanionAppDelegate: NSObject, NSApplicationDelegate {
    private let model: CompanionModel
    private let resources: URL
    private var panel: NSPanel?
    private var content: CompanionPanel?
    private var statusItem: NSStatusItem?
    private var subscription: AnyCancellable?
    private var languageSubscription: AnyCancellable?
    private var appearance: NSKeyValueObservation?
    private var anchorObservers: [NSObjectProtocol] = []
    private var localMonitor: Any?
    private var globalMonitor: Any?
    private var startup: Task<Void, Never>?
    private var navigation: Task<Void, Never>?
    private var shutdown: Task<Void, Never>?
    private var requestedHeight = 440.0
    // 状态栏窗口落位前保留显示请求；关闭后，迟到的布局事件不能重开浮层
    private var wantsPanelVisible = false
    private var trackingStatusMenu = false

    init(model: CompanionModel, resources: URL) { self.model = model; self.resources = resources; super.init() }

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMainMenu()
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        let image = NSImage(contentsOf: resources.appendingPathComponent("brand.svg"))
        if let image {
            image.size = NSSize(width: 18, height: 18 * image.size.height / image.size.width)
            image.isTemplate = true
        }
        item.button?.image = image
        item.button?.imagePosition = .imageOnly
        item.button?.setButtonType(.pushOnPushOff)
        item.button?.setAccessibilityLabel("TinkerFin")
        item.button?.target = self; item.button?.action = #selector(togglePanel)
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        statusItem = item
        let content = CompanionPanel(model: model, resources: resources)
        self.content = content
        let panel = MenuPanel(contentRect: NSRect(x: 0, y: 0, width: PanelGeometry.width, height: requestedHeight), styleMask: [.borderless], backing: .buffered, defer: false)
        panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = true
        panel.isFloatingPanel = true; panel.level = .popUpMenu; panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        panel.isReleasedWhenClosed = false; panel.contentView = content.view
        panel.setAccessibilityLabel(model.localization.text("TinkerFin 菜单栏客户端"))
        self.panel = panel
        content.resize = { [weak self] height in self?.requestedHeight = height; self?.positionPanel() }
        content.close = { [weak self] in self?.hidePanel() }
        model.attention = { [weak self] in self?.showPanel() }
        for name in [NSWindow.didMoveNotification, NSWindow.didResizeNotification, NSWindow.didChangeScreenNotification] {
            anchorObservers.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] notification in
                guard let window = notification.object as? NSWindow else { return }
                MainActor.assumeIsolated {
                    guard let self, window === self.statusItem?.button?.window else { return }
                    self.positionPanel()
                }
            })
        }
        subscription = model.objectWillChange.receive(on: DispatchQueue.main).sink { [weak self] _ in
            MainActor.assumeIsolated { self?.statusItem?.button?.toolTip = "TinkerFin · " + (self.map { $0.model.localization.text($0.model.statusText) } ?? "") }
        }
        languageSubscription = model.localization.objectWillChange.receive(on: DispatchQueue.main).sink { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.installMainMenu()
                self.panel?.setAccessibilityLabel(self.model.localization.text("TinkerFin 菜单栏客户端"))
                self.statusItem?.button?.toolTip = "TinkerFin · " + self.model.localization.text(self.model.statusText)
            }
        }
        anchorObservers.append(NotificationCenter.default.addObserver(forName: NSLocale.currentLocaleDidChangeNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.model.localization.refreshSystemLanguage() }
        })
        appearance = NSApp.observe(\.effectiveAppearance) { [weak self] _, _ in
            MainActor.assumeIsolated { self?.content?.publish() }
        }
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .keyDown]) { [weak self] event in
            let consumed = MainActor.assumeIsolated { () -> Bool in
                if event.type == .keyDown {
                    if event.keyCode == 53, let panel = self?.panel, panel.isVisible {
                        if let editor = panel.firstResponder as? NSTextInputClient, editor.hasMarkedText() { return false }
                        self?.content?.escape(); return true
                    }
                    return false
                }
                if event.window === self?.panel { CompanionEditableField.resignFocusOutside(event) }
                if let self, self.panel?.isVisible == true, event.window !== self.panel,
                   event.window !== self.panel?.attachedSheet, event.window !== self.statusItem?.button?.window { self.hidePanel() }
                return false
            }
            return consumed ? nil : event
        }
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            MainActor.assumeIsolated { self?.hidePanel() }
        }
        startup = Task { [weak self] in
            guard let self else { return }
            do { try await model.restore() } catch { }
            if !Task.isCancelled, model.saved.pending == nil { showPanel() }
        }
    }

    private func installMainMenu() {
        let main = NSMenu(), application = NSMenuItem(), applicationMenu = NSMenu(title: "TinkerFin")
        let settings = NSMenuItem(title: model.localization.text("设置"), action: #selector(showSettings), keyEquivalent: ","); settings.target = self
        applicationMenu.addItem(settings); applicationMenu.addItem(.separator())
        let exit = NSMenuItem(title: model.localization.text("退出 TinkerFin"), action: #selector(quit), keyEquivalent: "q"); exit.target = self
        applicationMenu.addItem(exit); application.submenu = applicationMenu; main.addItem(application)
        let editing = NSMenuItem(), editMenu = NSMenu(title: model.localization.text("编辑"))
        for (title, selector, key) in [("撤销", "undo:", "z"), ("重做", "redo:", "Z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            editMenu.addItem(NSMenuItem(title: model.localization.text(title), action: Selector(selector), keyEquivalent: key))
        }
        editing.submenu = editMenu; main.addItem(editing); NSApp.mainMenu = main
    }

    @objc private func togglePanel() {
        defer { updateStatusSelection() }
        if let event = NSApp.currentEvent,
           event.type == .rightMouseUp || (event.type == .leftMouseUp && event.modifierFlags.contains(.control)),
           let button = statusItem?.button {
            hidePanel()
            trackingStatusMenu = true; updateStatusSelection()
            defer { trackingStatusMenu = false; positionPanel() }
            let menu = NSMenu()
            let settings = NSMenuItem(title: model.localization.text("设置"), action: #selector(showSettings), keyEquivalent: ""); settings.target = self; menu.addItem(settings)
            menu.addItem(.separator())
            let quit = NSMenuItem(title: model.localization.text("退出 TinkerFin"), action: #selector(quit), keyEquivalent: ""); quit.target = self; menu.addItem(quit)
            NSMenu.popUpContextMenu(menu, with: event, for: button)
        } else if wantsPanelVisible { hidePanel() } else { showPanel() }
    }
    private func updateStatusSelection() {
        statusItem?.button?.state = trackingStatusMenu || panel?.isVisible == true ? .on : .off
    }
    private func positionPanel() {
        defer { updateStatusSelection() }
        guard wantsPanelVisible, !trackingStatusMenu, shutdown == nil, let panel, let button = statusItem?.button,
              let window = button.window, let screen = window.screen else { return }
        let buttonFrame = window.convertToScreen(button.convert(button.bounds, to: nil))
        guard let geometry = PanelGeometry(anchor: buttonFrame, screen: screen.visibleFrame, contentHeight: requestedHeight) else { return }
        if content?.panelHeight != geometry.maximumContentHeight { content?.panelHeight = geometry.maximumContentHeight }
        if panel.frame != geometry.frame { panel.setFrame(geometry.frame, display: true) }
        panel.contentView?.needsLayout = true
        panel.contentView?.layoutSubtreeIfNeeded()
        panel.invalidateShadow()
        if !panel.isVisible { panel.makeKeyAndOrderFront(nil) }
        content?.setWindowVisible(panel.isVisible)
    }
    private func showPanel() {
        defer { updateStatusSelection() }
        guard shutdown == nil else { return }
        wantsPanelVisible = true
        NSApp.activate()
        if model.saved.pending != nil { content?.perform(.home) }
        model.checkPermissions(); model.refreshProjects(); model.resumeAuthorization(); content?.publish(); positionPanel()
    }
    private func hidePanel() {
        wantsPanelVisible = false; content?.setWindowVisible(false); content?.dismissLanguageSelection(); panel?.makeFirstResponder(nil); panel?.orderOut(nil); updateStatusSelection()
    }
    @objc private func showSettings() {
        showPanel(); navigation?.cancel()
        navigation = Task { [weak self] in await self?.content?.showSettings() }
    }
    @objc private func quit() { NSApp.terminate(nil) }
    func applicationDidBecomeActive(_ notification: Notification) { model.localization.refreshSystemLanguage(); model.checkPermissions() }
    func applicationDidResignActive(_ notification: Notification) { hidePanel() }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showPanel(); return true }
    func applicationDidChangeScreenParameters(_ notification: Notification) { positionPanel(); content?.publish() }
    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls {
            if isClientConnectURL(url) { content?.perform(.home); showPanel(); continue }
            if url.scheme == "tinkerfin", url.host == "workspace" {
                navigation?.cancel()
                navigation = Task { [weak self] in
                    do { try await self?.model.openProjectURL(url) } catch { self?.showPanel() }
                }
                continue
            }
        }
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if shutdown == nil {
            shutdown = Task {
                hidePanel()
                if let content, !(await content.prepareToQuit()) {
                    shutdown = nil; showPanel(); sender.reply(toApplicationShouldTerminate: false)
                    return
                }
                for observer in anchorObservers { NotificationCenter.default.removeObserver(observer) }
                anchorObservers = []
                startup?.cancel(); navigation?.cancel()
                await model.shutdown(); await startup?.value; await navigation?.value; await content?.shutdown()
                if let localMonitor { NSEvent.removeMonitor(localMonitor) }
                if let globalMonitor { NSEvent.removeMonitor(globalMonitor) }
                appearance = nil; subscription = nil; languageSubscription = nil
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }
}
