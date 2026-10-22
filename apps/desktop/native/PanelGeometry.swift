import AppKit

/// 浮层按菜单栏按钮的单侧边缘对齐；优先向当前屏幕空间更充足的一侧展开
struct PanelGeometry {
    static let width = 346.0
    let frame: NSRect
    let maximumContentHeight: Double

    /// 锚点尚无可用显示空间时返回 nil，等待状态栏窗口完成布局
    init?(anchor: NSRect, screen: NSRect, contentHeight: Double, preferredWidth: Double = Self.width) {
        let width = min(preferredWidth, screen.width - 16)
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
