import AppKit
import Foundation
import WebKit

private enum ContractFailure: Error { case check(String) }
private func check(_ condition: Bool, _ message: String) throws {
    if !condition { throw ContractFailure.check(message) }
}

/// 真实WKWebView读取测试独占资源，通过脚本完成信号驱动验收，不使用固定等待
@MainActor
private final class UIContractDelegate: NSObject, NSApplicationDelegate {
    private let resources: URL
    private var windows: DesktopWindows?
    private var item: NSStatusItem?
    private var startup: Task<Void, Never>?
    private var ready: Set<DesktopSurface> = []
    private var actions: [DesktopUIAction] = []

    init(resources: URL) { self.resources = resources }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        self.item = item
        let windows = DesktopWindows(resources: resources, button: item.button!)
        self.windows = windows
        for (web, surface) in [(windows.workbenchUI, DesktopSurface.workbench), (windows.statusUI, .status)] {
            web.refresh = { [weak self, weak web] in
                var snapshot = DesktopSnapshot()
                snapshot.surface = surface; snapshot.locale = "zh-Hans"; snapshot.language = "zh-Hans"
                snapshot.name = "Bridge Contract"; snapshot.account = "contract"; snapshot.version = "0.1.1"
                snapshot.ready = true; snapshot.connected = true; snapshot.connection = "online"
                snapshot.service = "https://studio.example.com"; snapshot.api = snapshot.service
                web?.publish(snapshot)
                self?.ready.insert(surface)
            }
            web.handle = { [weak self] action in
                self?.actions.append(action)
                if case .ready = action, self?.ready.count == 2, self?.startup == nil {
                    self?.startup = Task { [weak self] in await self?.verify() }
                }
                return DesktopReply(ok: true)
            }
        }
    }

    private func verify() async {
        guard let windows else { return }
        do {
            // ready是在应用脚本执行后发送；随后等待React自身提交DOM，而非等待时钟
            let rendered = try await windows.workbenchUI.view.callAsyncJavaScript("""
                return await new Promise(resolve => {
                  const present = () => document.querySelector('nav') && document.querySelector('main[aria-label="工作区"]');
                  if (present()) { resolve(true); return; }
                  const observer = new MutationObserver(() => { if (present()) { observer.disconnect(); resolve(true); } });
                  observer.observe(document.body, { childList: true, subtree: true });
                });
                """, arguments: [:], in: nil, contentWorld: .page)
            try check(rendered as? Bool == true, "React界面未加载")
            let rejected = try await windows.workbenchUI.view.callAsyncJavaScript("""
                try { await window.webkit.messageHandlers.desktop.postMessage({ type: 'open-studio', url: 'https://unapproved.example' }); return false; }
                catch { return true; }
                """, arguments: [:], in: nil, contentWorld: .page)
            try check(rejected as? Bool == true, "桥接接纳了未声明字段")
            let blocked = try await windows.workbenchUI.view.callAsyncJavaScript("""
                return await new Promise(resolve => {
                  const image = new Image();
                  image.onload = () => resolve(false); image.onerror = () => resolve(true);
                  image.src = 'tinkerfin-ui://app/escaped.svg';
                });
                """, arguments: [:], in: nil, contentWorld: .page)
            try check(blocked as? Bool == true, "包内符号链接逃逸未拒绝")
            windows.showWorkbench()
            try check(windows.workbench.frame.size == NSSize(width: 650, height: 360), "工作台尺寸不匹配")
            try check(windows.workbench.contentView?.bounds.size == NSSize(width: 650, height: 360), "WebView未覆盖完整窗口")
            try check(windows.workbench.standardWindowButton(.closeButton)?.isEnabled == true, "关闭按钮不可用")
            try check(windows.workbench.standardWindowButton(.miniaturizeButton)?.isEnabled == true, "最小化按钮不可用")
            try check(windows.workbench.standardWindowButton(.zoomButton)?.isEnabled != true, "缩放按钮仍可用")
            windows.workbench.miniaturize(nil)
            try check(!windows.workbench.isVisible && !windows.workbench.isMiniaturized, "最小化未收起至菜单栏")
            windows.showWorkbench(); windows.workbench.performClose(nil)
            try check(!windows.workbench.isVisible, "关闭没有隐藏工作台")
            windows.showWorkbench(); windows.workbench.resignKey()
            try check(windows.workbench.isVisible, "失焦关闭了工作台")
            windows.interactionEnabled = false
            windows.workbench.performClose(nil); windows.workbench.miniaturize(nil)
            try check(windows.workbench.isVisible, "更新弹框未锁定系统隐藏按钮")
            await windows.close()
            if let item { NSStatusBar.system.removeStatusItem(item) }
            print("verified webview-and-windows")
            NSApp.terminate(nil)
        } catch {
            FileHandle.standardError.write(Data("\(error)\n".utf8))
            await windows.close()
            if let item { NSStatusBar.system.removeStatusItem(item) }
            exit(1)
        }
    }
}

@main
@MainActor
private struct UIContracts {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = UIContractDelegate(resources: URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true))
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
