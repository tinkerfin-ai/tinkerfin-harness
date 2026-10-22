import AppKit

/// 最小化与关闭只隐藏工作台，窗口与WebView持续持有草稿，不生成Dock缩略图
@MainActor
final class DesktopWorkbenchWindow: NSWindow {
    var permitsHiding = true
    override func miniaturize(_ sender: Any?) {
        if permitsHiding { orderOut(sender) }
    }
}

@MainActor
private final class DesktopStatusWindow: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

/// 状态浮层与常驻工作台独立显示；外部点击只收起浮层，工作台由系统按钮控制
@MainActor
final class DesktopWindows: NSObject, NSWindowDelegate {
    let workbench: DesktopWorkbenchWindow
    let workbenchUI: DesktopWebView
    let statusUI: DesktopWebView
    private let status: DesktopStatusWindow
    private weak var button: NSStatusBarButton?
    private var observers: [NSObjectProtocol] = []
    private var localMonitor: Any?
    private var globalMonitor: Any?
    private var wantsStatus = false
    private var closing = false
    var activated: (() -> Void)?
    var interactionEnabled = true {
        didSet {
            workbench.permitsHiding = interactionEnabled
            workbench.standardWindowButton(.closeButton)?.isEnabled = interactionEnabled
            workbench.standardWindowButton(.miniaturizeButton)?.isEnabled = interactionEnabled
            button?.isEnabled = interactionEnabled
            if !interactionEnabled { hideStatus() }
        }
    }

    init(resources: URL, button: NSStatusBarButton) {
        self.button = button
        workbenchUI = DesktopWebView(resources: resources)
        statusUI = DesktopWebView(resources: resources)
        workbench = DesktopWorkbenchWindow(contentRect: NSRect(x: 0, y: 0, width: 650, height: 360),
            styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView], backing: .buffered, defer: false)
        status = DesktopStatusWindow(contentRect: NSRect(x: 0, y: 0, width: 346, height: 78), styleMask: .borderless, backing: .buffered, defer: false)
        super.init()
        workbench.delegate = self
        workbench.title = "TinkerFin"; workbench.titleVisibility = .hidden
        workbench.titlebarAppearsTransparent = true; workbench.titlebarSeparatorStyle = .none
        workbench.isReleasedWhenClosed = false; workbench.collectionBehavior = [.moveToActiveSpace, .fullScreenNone]
        workbench.standardWindowButton(.zoomButton)?.isEnabled = false
        workbench.contentView = workbenchUI.view
        workbench.setFrame(NSRect(x: 0, y: 0, width: 650, height: 360), display: false)
        workbench.center()
        let titlebar = NSTitlebarAccessoryViewController()
        titlebar.view = NSView(frame: NSRect(x: 0, y: 0, width: 0, height: 12)); titlebar.layoutAttribute = .bottom
        workbench.addTitlebarAccessoryViewController(titlebar)
        status.isOpaque = false; status.backgroundColor = .clear; status.hasShadow = true
        status.isFloatingPanel = true; status.level = .popUpMenu; status.hidesOnDeactivate = false
        status.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        status.isReleasedWhenClosed = false; status.contentView = statusUI.view
        statusUI.view.wantsLayer = true; statusUI.view.layer?.cornerRadius = 14; statusUI.view.layer?.masksToBounds = true
        for name in [NSWindow.didMoveNotification, NSWindow.didResizeNotification, NSWindow.didChangeScreenNotification] {
            observers.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] notification in
                guard let window = notification.object as? NSWindow else { return }
                MainActor.assumeIsolated { if window === self?.button?.window { self?.positionStatus() } }
            })
        }
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .keyDown]) { [weak self] event in
            let consumed = MainActor.assumeIsolated { () -> Bool in
                guard let self, self.status.isVisible else { return false }
                if event.type == .keyDown {
                    if event.keyCode == 53, event.window === self.status { self.hideStatus(); return true }
                } else if event.window !== self.status, event.window !== self.button?.window { self.hideStatus() }
                return false
            }
            return consumed ? nil : event
        }
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            MainActor.assumeIsolated { self?.hideStatus() }
        }
    }

    func showWorkbench() {
        guard !closing else { return }
        hideStatus(); activated?(); NSApp.activate(); workbench.makeKeyAndOrderFront(nil)
    }

    func toggleStatus() {
        guard interactionEnabled, !closing else { return }
        if wantsStatus { hideStatus() }
        else { wantsStatus = true; activated?(); NSApp.activate(); positionStatus() }
    }

    func positionStatus() {
        guard wantsStatus, !closing, let button, let window = button.window, let screen = window.screen else { return }
        let anchor = window.convertToScreen(button.convert(button.bounds, to: nil))
        guard let geometry = PanelGeometry(anchor: anchor, screen: screen.visibleFrame, contentHeight: 78, preferredWidth: 346) else { return }
        status.setFrame(geometry.frame, display: true); status.makeKeyAndOrderFront(nil); button.state = .on
    }

    func hideStatus() { wantsStatus = false; status.orderOut(nil); button?.state = .off }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender === workbench, interactionEnabled { workbench.orderOut(nil) }
        return false
    }
    func windowDidBecomeKey(_ notification: Notification) { activated?() }

    func close() async {
        closing = true; hideStatus(); workbench.orderOut(nil); activated = nil
        if let localMonitor { NSEvent.removeMonitor(localMonitor) }
        if let globalMonitor { NSEvent.removeMonitor(globalMonitor) }
        localMonitor = nil; globalMonitor = nil
        observers.forEach(NotificationCenter.default.removeObserver); observers.removeAll()
        await workbenchUI.close(); await statusUI.close()
        workbench.contentView = nil; status.contentView = nil
        workbench.delegate = nil; workbench.close(); status.close()
    }
}
