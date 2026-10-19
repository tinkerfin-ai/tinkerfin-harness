import AppKit

/// 浮层按菜单栏按钮的单侧边缘对齐；优先向当前屏幕空间更充足的一侧展开
struct PanelGeometry {
    static let width = 232.0
    static let cornerRadius = 10.0
    let frame: NSRect
    let maximumContentHeight: Double

    /// 锚点尚无可用显示空间时返回 nil，等待状态栏窗口完成布局
    init?(anchor: NSRect, screen: NSRect, contentHeight: Double) {
        let width = min(Self.width, screen.width - 16)
        let top = min(anchor.minY, screen.maxY) - 2
        maximumContentHeight = top - screen.minY - 8
        guard !anchor.isEmpty, width > 0, maximumContentHeight > 0, contentHeight > 0 else { return nil }
        let leftSpace = anchor.maxX - screen.minX - 8
        let rightSpace = screen.maxX - 8 - anchor.minX
        let preferredX = rightSpace >= leftSpace ? anchor.minX : anchor.maxX - width
        let x = min(max(preferredX, screen.minX + 8), screen.maxX - 8 - width)
        let height = min(contentHeight, maximumContentHeight)
        frame = NSRect(x: x, y: top - height, width: width, height: height)
    }
}

/// 浮层采用系统材质，由 AppKit 跟随浅深色与辅助功能透明度设置
@MainActor
final class CompanionPanelView: NSVisualEffectView {
    private var hosted: NSView?

    init(hosted: NSView) {
        self.hosted = hosted
        super.init(frame: .zero)
        material = .popover
        blendingMode = .behindWindow
        state = .active
        wantsLayer = true
        layer?.cornerRadius = PanelGeometry.cornerRadius
        layer?.masksToBounds = true
        let radius = PanelGeometry.cornerRadius
        let mask = NSImage(size: NSSize(width: radius * 2 + 1, height: radius * 2 + 1), flipped: false) { rect in
            NSColor.black.setFill()
            NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
            return true
        }
        mask.capInsets = NSEdgeInsets(top: radius, left: radius, bottom: radius, right: radius)
        mask.resizingMode = .stretch
        maskImage = mask
        addSubview(hosted)
    }

    required init?(coder: NSCoder) { nil }

    override func layout() {
        super.layout()
        hosted?.frame = bounds
        window?.invalidateShadow()
    }

    func detachContent() { hosted?.removeFromSuperview(); hosted = nil }
}
