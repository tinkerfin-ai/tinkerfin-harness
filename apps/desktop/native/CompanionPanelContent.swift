import AppKit
import SwiftUI

@MainActor
struct CompanionPanelContent: View {
    @ObservedObject var panel: CompanionPanel
    @ObservedObject var model: CompanionModel
    @ObservedObject var localization: ClientLocalization
    @Environment(\.colorScheme) private var colorScheme
    @State private var bodyHeight: CGFloat = 1
    @State private var footerHeight: CGFloat = 0
    @State private var focusedLanguage: ClientLanguage?
    private enum LanguageFocus: Hashable { case trigger, menu }
    // 菜单与入口共享焦点，关闭列表后恢复入口的键盘操作
    @FocusState private var languageFocus: LanguageFocus?
    private let headerHeight: CGFloat = 44
    private var availableHeight: CGFloat { max(headerHeight, panel.panelHeight) }
    private var viewportHeight: CGFloat {
        min(bodyHeight, max(0, availableHeight - headerHeight - footerHeight))
    }
    private var contentHeight: CGFloat { headerHeight + viewportHeight + footerHeight }
    private var success: Color {
        colorScheme == .dark
            ? Color(red: 156 / 255, green: 219 / 255, blue: 183 / 255)
            : Color(red: 35 / 255, green: 111 / 255, blue: 73 / 255)
    }
    var body: some View {
        VStack(spacing: 0) {
            header
            ScrollView(.vertical) {
                VStack(alignment: .leading, spacing: 0) {
                    pageContent
                    errors
                }
                .padding(.horizontal, 14)
                .padding(.top, 12)
                .padding(.bottom, 16)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
                .onGeometryChange(for: CGFloat.self) { geometry in
                    ceil(geometry.size.height)
                } action: { value in
                    if abs(bodyHeight - value) > 0.5 { bodyHeight = value }
                    resize()
                }
            }
            .scrollBounceBehavior(.basedOnSize)
            .frame(height: viewportHeight)
            footer
                .frame(height: hasFooter ? nil : 0)
                .onGeometryChange(for: CGFloat.self) { geometry in
                    ceil(geometry.size.height)
                } action: { value in
                    if abs(footerHeight - value) > 0.5 { footerHeight = value }
                    resize()
                }
        }
        .font(.system(size: 11))
        .foregroundStyle(.primary)
        .buttonStyle(CompanionPlainButtonStyle())
        .frame(width: PanelGeometry.width, height: contentHeight, alignment: .top)
        .overlayPreferenceValue(LanguageMenuAnchor.self) { anchor in
            if panel.selectingLanguage, let anchor {
                GeometryReader { geometry in
                    let trigger = geometry[anchor]
                    let menuHeight = CGFloat(ClientLanguage.allCases.count * 28 + 8)
                    let below = trigger.maxY + 6
                    let top = below + menuHeight <= geometry.size.height - 8
                        ? below : max(headerHeight, trigger.minY - menuHeight - 6)
                    ZStack(alignment: .topLeading) {
                        Color.clear.contentShape(Rectangle())
                            .onTapGesture { panel.dismissLanguageSelection() }
                            .accessibilityHidden(true)
                        languageChoices
                            .frame(width: 128, height: menuHeight)
                            .offset(x: max(8, trigger.maxX - 128), y: top)
                    }
                }
            }
        }
        .onChange(of: panel.selectingLanguage) { _, open in
            if !open {
                focusedLanguage = nil
                languageFocus = panel.page == .settings ? .trigger : nil
            }
        }
        .onChange(of: panel.panelHeight) { _, _ in resize() }
    }

    private func resize() {
        // 正文按理想高度测量，只有超出屏幕容量的部分滚动，底部操作始终可见
        panel.resize?(Double(contentHeight))
    }

    private var header: some View {
        HStack(spacing: 6) {
            if let image = panel.brandImage {
                Image(nsImage: image)
                    .resizable()
                    .scaledToFit()
                    .frame(width: 20, height: 18)
                    .accessibilityHidden(true)
            }
            Text("TinkerFin")
                .font(.system(size: 12, weight: .semibold))
                .lineLimit(1)
            Spacer(minLength: 0)
            if panel.showsSettings {
                Button { panel.perform(.settings) } label: {
                    CompanionIcon.gear.view(size: 16)
                        .frame(width: 24, height: 24)
                        .contentShape(Rectangle())
                }
                .foregroundStyle(.secondary)
                .disabled(!panel.canPerform(.settings))
                .accessibilityLabel(localization.text("设置"))
            }
        }
        .padding(.horizontal, 14)
        .frame(height: headerHeight)
    }

    @ViewBuilder
    private var pageContent: some View {
        switch panel.page {
        case .home: home
        case .settings: settings
        case .permissions: permissions
        }
    }

    private var home: some View {
        VStack(alignment: .leading, spacing: 16) {
            deviceHeading
            if model.connection == "online", !model.projects.isEmpty || model.projectsError {
                projectWorkspaces
            }
        }
    }

    private var connectionColor: Color {
        switch model.connection {
        case "online": return success
        default: return .red
        }
    }

    private var deviceHeading: some View {
        HStack(alignment: .top, spacing: 9) {
            CompanionIcon.laptop.view(size: 28)
                .foregroundStyle(.secondary)
                .frame(width: 29, height: 30)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 7) {
                title(model.saved.name)
                    .help(model.saved.name)
                HStack(spacing: 6) {
                    Circle()
                        .fill(connectionColor)
                        .frame(width: 6, height: 6)
                        .accessibilityHidden(true)
                    Text(localization.text(model.statusText))
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var projectWorkspaces: some View {
        VStack(alignment: .leading, spacing: 16) {
            Divider()
            ForEach(model.projects) { project in
                Button { model.revealProject(project.id) } label: {
                    HStack(spacing: 7) {
                        folderSymbol
                        Text(project.name).lineLimit(1).truncationMode(.tail)
                        Spacer(minLength: 0)
                        CompanionIcon.out.view(size: 12).foregroundStyle(.secondary)
                    }.contentShape(Rectangle())
                }.help(project.name)
            }
            if model.projectsError {
                Button(localization.text("重新加载项目")) { model.refreshProjects() }
            }

        }
    }

    private var settings: some View {
        VStack(alignment: .leading, spacing: 0) {
            breadcrumbs([("本机", .home)], current: "设置")
                .padding(.bottom, 16)
            Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 14) {
                if model.saved.device != nil {
                    GridRow {
                        Text(localization.text("登录名")).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary).fixedSize()
                        HStack(spacing: 8) {
                            Text(model.accountName)
                                .lineLimit(1).truncationMode(.middle).foregroundStyle(.secondary)
                                .padding(.leading, CompanionEditableField.horizontalPadding)
                            Spacer(minLength: 0)
                            rowAction("切换", symbol: .swap, enabled: panel.canPerform(.startAuthorization)) { panel.perform(.startAuthorization) }
                        }
                    }
                }
                GridRow {
                    Text(localization.text("设备名称"))
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                        .fixedSize()
                    deviceName
                }
            }
            .padding(.bottom, 12)
            Divider()
            languageSetting
                .padding(.top, 14)
            HStack {
                Text(localization.text("桌面权限"))
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
                rowAction("管理", symbol: .chevron, enabled: panel.canPerform(.permissions)) { panel.perform(.permissions) }
            }
            .padding(.top, 14)
            .padding(.bottom, 2)
            HStack {
                Text(localization.text("配置文件")).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                Spacer(minLength: 0)
                rowAction("打开", symbol: .out) { panel.perform(.openConfiguration) }
            }.padding(.top, 14)

        }
    }

    private var languageSetting: some View {
        HStack {
            Text(localization.text("语言"))
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
            rowAction(localization.label(for: localization.language), symbol: .chevron) {
                panel.toggleLanguageSelection()
            }
            .focusable()
            .focused($languageFocus, equals: .trigger)
            .focusEffectDisabled()
            .background(Color(nsColor: .labelColor).opacity(languageFocus == .trigger ? 0.055 : 0),
                        in: RoundedRectangle(cornerRadius: 4))
            .accessibilityLabel(localization.text("语言"))
            .accessibilityValue(localization.label(for: localization.language))
            .anchorPreference(key: LanguageMenuAnchor.self, value: .bounds) { $0 }
            .onKeyPress(.return) { panel.toggleLanguageSelection(); return .handled }
            .onKeyPress(.space) { panel.toggleLanguageSelection(); return .handled }
        }
    }

    private var languageChoices: some View {
        VStack(spacing: 0) {
            ForEach(ClientLanguage.allCases) { language in
                Button { panel.selectLanguage(language) } label: {
                    HStack(spacing: 8) {
                        Text(localization.label(for: language))
                            .lineLimit(1)
                        Spacer(minLength: 0)
                        if localization.language == language { CompanionIcon.check.view(size: 11) }
                    }
                    .padding(.horizontal, 8)
                    .frame(height: 28)
                    .contentShape(Rectangle())
                }
                .buttonStyle(CompanionLanguageOptionStyle(focused: languageFocus == .menu && focusedLanguage == language))
                .onHover { hovering in
                    if hovering { focusedLanguage = language; languageFocus = .menu }
                }
                .accessibilityAddTraits(localization.language == language ? .isSelected : [])
            }
        }
        .padding(4)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 6))
        .overlay {
            RoundedRectangle(cornerRadius: 6)
                .strokeBorder(Color(nsColor: .separatorColor).opacity(0.6), lineWidth: 0.5)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(localization.text("语言"))
        .focusable()
        .focused($languageFocus, equals: .menu)
        .focusEffectDisabled()
        .onAppear {
            focusedLanguage = localization.language
            languageFocus = .menu
        }
        .onMoveCommand { direction in
            let languages = ClientLanguage.allCases
            guard let index = languages.firstIndex(of: focusedLanguage ?? localization.language) else { return }
            if direction == .down { focusedLanguage = languages[(index + 1) % languages.count] }
            if direction == .up { focusedLanguage = languages[(index + languages.count - 1) % languages.count] }
        }
        .onKeyPress(.return) {
            if let focusedLanguage { panel.selectLanguage(focusedLanguage) }
            return .handled
        }
        .onKeyPress(.space) {
            if let focusedLanguage { panel.selectLanguage(focusedLanguage) }
            return .handled
        }
        .onKeyPress(.tab) {
            panel.dismissLanguageSelection()
            return .handled
        }
    }

    @ViewBuilder
    private var deviceName: some View {
        if panel.editingName {
            CompanionInput(text: Binding(get: { panel.name }, set: {
                panel.name = $0
                panel.clearFieldError()
            }), label: localization.text("设备名称"), enabled: panel.canEditName, limit: 128,
               focusRequest: panel.nameFocusRequest,
               submit: { panel.perform(.saveSettings) }, blur: { panel.perform(.saveSettings) })
        } else {
            Button { panel.beginNameEditing() } label: {
                Text(panel.name)
                    .foregroundStyle(Color(nsColor: .labelColor))
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .padding(.horizontal, CompanionEditableField.horizontalPadding)
                    .frame(maxWidth: .infinity, minHeight: 28, alignment: .leading)
                    .contentShape(Rectangle())
            }
            .disabled(!panel.canEditName)
            .help(localization.text("编辑设备名称"))
            .accessibilityLabel(localization.text("编辑设备名称"))
            .accessibilityValue(panel.name)
        }
    }

    private var permissions: some View {
        VStack(alignment: .leading, spacing: 0) {
            breadcrumbs([("本机", .home), ("设置", .settings)], current: "桌面权限")
                .padding(.bottom, 8)
            permissionRow("辅助功能", detail: "读取控件、点击与输入",
                          enabled: model.accessibility, action: .accessibility)
            Divider()
            permissionRow("屏幕录制", detail: "截取应用窗口",
                          enabled: model.screenCapture, action: .screenCapture)
        }
    }

    private func permissionRow(_ title: String, detail: String, enabled: Bool,
                               action: CompanionAction) -> some View {
        HStack(alignment: .center, spacing: 7) {
            VStack(alignment: .leading, spacing: 4) {
                Text(localization.text(title))
                    .font(.system(size: 12, weight: .medium))
                    .lineLimit(1)
                Text(localization.text(detail))
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            VStack(alignment: .trailing, spacing: 5) {
                if enabled {
                    Text(localization.text("已开启"))
                        .font(.system(size: 11))
                        .foregroundStyle(success)
                        .fixedSize()
                }
                rowAction(enabled ? "管理" : "开启", symbol: .out, enabled: panel.canPerform(action)) {
                    panel.perform(action)
                }
            }
            .fixedSize()
        }
        .padding(.vertical, 14)
    }

    @ViewBuilder
    private var errors: some View {
        if !panel.errors.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(panel.errors.enumerated()), id: \.offset) { _, message in
                    Text(message)
                        .font(.system(size: 11))
                        .foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.top, 12)
        }
    }

    private var hasFooter: Bool {
        switch panel.page {
        case .settings, .permissions: return false
        case .home: return true
        }
    }

    @ViewBuilder
    private var footer: some View {
        if hasFooter {
            HStack(spacing: 8) {
                if case .home = panel.page, model.connection == "authorizing" {
                    actionButton("取消", action: .cancelAuthorization)
                }
                Spacer(minLength: 0)
                switch panel.page {
                case .settings, .permissions:
                    EmptyView()
                case .home:
                    actionButton(panel.primaryLabel, action: panel.primaryAction, primary: true, loading: panel.primaryBusy)
                }
            }
            .padding(.horizontal, 14)
            .padding(.top, 10)
            .padding(.bottom, 12)
            .overlay(alignment: .top) {
                Rectangle()
                    .fill(Color(nsColor: .separatorColor).opacity(0.6))
                    .frame(height: 0.5)
            }
        }
    }

    private func actionButton(_ label: String, action: CompanionAction,
                              primary: Bool = false, danger: Bool = false, loading: Bool = false) -> some View {
        let busy = loading || panel.activeAction == action
        return Button { panel.perform(action) } label: {
            ZStack {
                if action == .retry {
                    Text(localization.text("重试中")).hidden().accessibilityHidden(true)
                    Text(localization.text("连接中")).hidden().accessibilityHidden(true)
                } else if action == .startAuthorization || action == .reopenAuthorization {
                    Text(localization.text("授权中")).hidden().accessibilityHidden(true)
                }
                Text(localization.text(label))
            }
            .lineLimit(1)
            .fixedSize()
            .frame(minWidth: primary ? nil : 40)
        }
        .buttonStyle(CompanionActionStyle(primary: primary, danger: danger, busy: busy,
                                          animationActive: panel.isWindowVisible))
        .disabled(busy || !panel.canPerform(action))
    }

    private func breadcrumbs(_ parents: [(String, CompanionAction)], current: String) -> some View {
        HStack(spacing: 4) {
            ForEach(Array(parents.enumerated()), id: \.offset) { _, parent in
                Button(localization.text(parent.0)) { panel.perform(parent.1) }
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .fixedSize()
                    .disabled(!panel.canPerform(parent.1))
                CompanionIcon.chevron.view(size: 9)
                    .foregroundStyle(.tertiary)
                    .accessibilityHidden(true)
            }
            Text(localization.text(current))
                .font(.system(size: 12, weight: .semibold))
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityAddTraits(.isHeader)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(localization.text("面包屑"))
    }

    private func title(_ value: String) -> some View {
        Text(value)
            .font(.system(size: 16, weight: .semibold))
            .lineLimit(1)
            .truncationMode(.tail)
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityAddTraits(.isHeader)
    }

    private var folderSymbol: some View {
        CompanionIcon.folder.view(size: 20)
            .foregroundStyle(Color(nsColor: .systemYellow))
            .frame(width: 20, height: 24)
            .accessibilityHidden(true)
    }

    private func rowAction(_ label: String, symbol: CompanionIcon, enabled: Bool = true,
                           action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 3) {
                Text(localization.text(label))
                    .font(.system(size: 11))
                symbol.view(size: 11)
            }
            .lineLimit(1)
            .fixedSize()
            .padding(.vertical, 3)
            .contentShape(Rectangle())
        }
        .foregroundStyle(.secondary)
        .disabled(!enabled)
    }


}

private struct LanguageMenuAnchor: PreferenceKey {
    static var defaultValue: Anchor<CGRect>? { nil }
    static func reduce(value: inout Anchor<CGRect>?, nextValue: () -> Anchor<CGRect>?) {
        value = nextValue() ?? value
    }
}

private struct CompanionLanguageOptionStyle: ButtonStyle {
    let focused: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 11))
            .foregroundStyle(.primary)
            .background(Color(nsColor: .labelColor).opacity(configuration.isPressed ? 0.12 : focused ? 0.085 : 0),
                        in: RoundedRectangle(cornerRadius: 4))
            .modifier(CompanionCursor(cursor: .pointingHand))
    }
}

@MainActor
private enum CompanionIcon: String, CaseIterable {
    case gear, out, check, chevron, swap, plus, close, folder, laptop, more, link, offline

    private static let images: [Self: NSImage] = Dictionary(uniqueKeysWithValues: allCases.map { icon in
        guard let url = Bundle.main.url(forResource: icon.rawValue, withExtension: "pdf", subdirectory: "icons"),
              let image = NSImage(contentsOf: url) else {
            preconditionFailure("缺少面板图标：\(icon.rawValue)")
        }
        image.isTemplate = true
        return (icon, image)
    })

    func view(size: CGFloat) -> some View {
        Image(nsImage: Self.images[self]!)
            .renderingMode(.template)
            .resizable()
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}

@MainActor
private enum CompanionPalette {
    static var accent: Color { Color(red: 57 / 255, green: 100 / 255, blue: 254 / 255) }
    static var loadingHighlight: Color { Color(red: 0, green: 200 / 255, blue: 252 / 255) }
    static var loadingTrail: Color { Color(red: 122 / 255, green: 7 / 255, blue: 250 / 255) }
}

@MainActor
private struct CompanionCursor: ViewModifier {
    let cursor: NSCursor
    @Environment(\.isEnabled) private var enabled
    @State private var hovering = false

    func body(content: Content) -> some View {
        content
            .onContinuousHover { phase in
                switch phase {
                case .active:
                    hovering = true
                    (enabled ? cursor : NSCursor.arrow).set()
                case .ended:
                    hovering = false
                    NSCursor.arrow.set()
                }
            }
            .onChange(of: enabled) { _, _ in
                if hovering { (enabled ? cursor : NSCursor.arrow).set() }
            }
            .onDisappear { if hovering { NSCursor.arrow.set() } }
    }
}

private struct CompanionPlainButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(enabled ? (configuration.isPressed ? 0.65 : 1) : 0.5)
            .modifier(CompanionCursor(cursor: .pointingHand))
    }
}

private struct CompanionActionStyle: ButtonStyle {
    let primary: Bool
    let danger: Bool
    let busy: Bool
    let animationActive: Bool
    @Environment(\.isEnabled) private var enabled
    @Environment(\.colorScheme) private var scheme

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(foreground)
            .tint(foreground)
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .frame(minHeight: 28)
            .background {
                RoundedRectangle(cornerRadius: 6)
                    .fill(primary ? accent : Color(nsColor: .labelColor).opacity(configuration.isPressed ? 0.09 : 0.035))
            }
            .opacity(enabled ? (configuration.isPressed ? 0.85 : 1) : 0.5)
            .overlay {
                if busy {
                    CompanionBusyBorder(active: animationActive)
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
            }
            .modifier(CompanionCursor(cursor: .pointingHand))
    }

    private var accent: Color {
        scheme == .dark ? Color(red: 171 / 255, green: 192 / 255, blue: 1) : CompanionPalette.accent
    }
    private var foreground: Color {
        if primary { return scheme == .dark ? Color(red: 23 / 255, green: 36 / 255, blue: 65 / 255) : .white }
        return danger ? .red : .primary
    }
}

private struct CompanionBusyBorder: View {
    let active: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        if active && !reduceMotion {
            CompanionOrbitingBorder()
        } else {
            RoundedRectangle(cornerRadius: 6)
                .strokeBorder(LinearGradient(colors: [CompanionPalette.loadingTrail.opacity(0.5),
                                                      CompanionPalette.loadingHighlight.opacity(0.65)],
                                             startPoint: .topLeading, endPoint: .bottomTrailing), lineWidth: 1)
        }
    }
}

/// 动画视图只在浮层可见时存在；隐藏或减少动态效果时移除，避免后台持续绘制
private struct CompanionOrbitingBorder: View {
    @State private var phase = 0.0

    var body: some View {
        Color.clear
            .modifier(CompanionEdgeLight(phase: phase))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .animation(.linear(duration: 2.4).repeatForever(autoreverses: false), value: phase)
            .onAppear { phase = 1 }
    }
}

private struct CompanionEdgeLight: AnimatableModifier {
    nonisolated var phase: Double
    nonisolated var animatableData: Double {
        get { phase }
        set { phase = newValue }
    }

    func body(content: Content) -> some View {
        let trail = CompanionPalette.loadingTrail
        let highlight = CompanionPalette.loadingHighlight
        let light = AngularGradient(stops: [
            .init(color: trail.opacity(0), location: 0),
            .init(color: trail.opacity(0), location: 0.67),
            .init(color: trail.opacity(0.55), location: 0.79),
            .init(color: highlight.opacity(0.9), location: 0.93),
            .init(color: highlight.opacity(0), location: 1)
        ], center: .center, angle: .degrees(phase * 360))
        content.overlay {
            ZStack {
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(light, lineWidth: 2)
                    .opacity(0.28)
                    .blur(radius: 0.8)
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(light, lineWidth: 1)
            }
        }
    }
}

@MainActor
private struct CompanionInput: View {
    @Binding var text: String
    let label: String
    var placeholder = ""
    var enabled = true
    var limit = 16384
    var focusRequest: UUID?
    var submit: () -> Void = {}
    var blur: () -> Void = {}
    @State private var focused = false
    @State private var hovered = false

    var body: some View {
        CompanionTextField(text: $text, label: label, placeholder: placeholder, enabled: enabled,
                           limit: limit, focusRequest: focusRequest, focused: { focused = $0 },
                           submit: submit, blur: blur)
            .frame(maxWidth: .infinity)
            .frame(height: 16)
            .padding(.horizontal, CompanionEditableField.horizontalPadding)
            .padding(.vertical, CompanionEditableField.verticalPadding)
            .background(Color(nsColor: .labelColor).opacity(focused ? 0.12 : hovered ? 0.085 : 0.055),
                        in: RoundedRectangle(cornerRadius: 5))
            .onHover { hovered = $0 }
            .modifier(CompanionCursor(cursor: .iBeam))
            .disabled(!enabled)
            .clipped()
    }
}

@MainActor
private struct CompanionTextField: NSViewRepresentable {
    @Binding var text: String
    let label: String
    let placeholder: String
    let enabled: Bool
    let limit: Int
    let focusRequest: UUID?
    let focused: (Bool) -> Void
    let submit: () -> Void
    let blur: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> CompanionEditableField {
        let field = CompanionEditableField(string: text)
        field.delegate = context.coordinator
        field.isBordered = false
        field.isBezeled = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.font = .systemFont(ofSize: 11)
        field.textColor = .labelColor
        field.lineBreakMode = .byClipping
        field.usesSingleLineMode = true
        field.cell?.isScrollable = true
        field.setContentHuggingPriority(.defaultLow, for: .horizontal)
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        field.setAccessibilityLabel(label)
        return field
    }

    func updateNSView(_ field: CompanionEditableField, context: Context) {
        context.coordinator.parent = self
        if field.stringValue != text { field.stringValue = text }
        field.placeholderAttributedString = NSAttributedString(string: placeholder, attributes: [
            .foregroundColor: NSColor.labelColor,
            .font: NSFont.systemFont(ofSize: 11)
        ])
        field.isEnabled = enabled
        field.setAccessibilityLabel(label)
        if let focusRequest, focusRequest != context.coordinator.lastFocus {
            context.coordinator.lastFocus = focusRequest
            field.focusWhenAttached = true
            field.focusIfAttached()
        }
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: CompanionEditableField, context: Context) -> CGSize? {
        CGSize(width: proposal.width ?? 100, height: 16)
    }

    final class Coordinator: NSObject, NSTextFieldDelegate {
        var parent: CompanionTextField
        var lastFocus: UUID?
        init(_ parent: CompanionTextField) { self.parent = parent }

        func controlTextDidBeginEditing(_ notification: Notification) {
            if let field = notification.object as? NSTextField,
               let editor = field.currentEditor() as? NSTextView {
                editor.textColor = field.textColor
                editor.selectedTextAttributes = [
                    .backgroundColor: NSColor.labelColor.withAlphaComponent(0.16),
                    .foregroundColor: NSColor.labelColor
                ]
                editor.insertionPointColor = .labelColor
            }
            parent.focused(true)
        }
        func controlTextDidEndEditing(_ notification: Notification) {
            parent.focused(false)
            parent.blur()
        }
        func controlTextDidChange(_ notification: Notification) {
            guard let field = notification.object as? NSTextField else { return }
            let value = String(field.stringValue.prefix(parent.limit))
            if field.stringValue != value { field.stringValue = value }
            parent.text = value
        }
        func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
            if commandSelector == #selector(NSResponder.insertNewline(_:)) {
                parent.submit()
                return true
            }
            return false
        }
    }
}

@MainActor
final class CompanionEditableField: NSTextField {
    static let horizontalPadding = 8.0
    static let verticalPadding = 6.0
    var focusWhenAttached = false

    /// 点击字段以外移交焦点，由编辑结束回调保存；失败时保留草稿
    static func resignFocusOutside(_ event: NSEvent) {
        guard let window = event.window, let root = window.contentView,
              let editor = window.firstResponder as? NSText,
              let field = editingField(in: root, editor: editor) else { return }
        let inputBounds = field.bounds.insetBy(dx: -horizontalPadding, dy: -verticalPadding)
        if !inputBounds.contains(field.convert(event.locationInWindow, from: nil)) { window.makeFirstResponder(nil) }
    }

    private static func editingField(in view: NSView, editor: NSText) -> CompanionEditableField? {
        if let field = view as? CompanionEditableField, field.currentEditor() === editor { return field }
        for child in view.subviews {
            if let field = editingField(in: child, editor: editor) { return field }
        }
        return nil
    }

    // 长内容只在字段内部滚动，不参与固定宽度面板的横向测量
    override var intrinsicContentSize: NSSize {
        var size = super.intrinsicContentSize
        size.width = NSView.noIntrinsicMetric
        return size
    }
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        focusIfAttached()
    }
    func focusIfAttached() {
        if focusWhenAttached, let window, isEnabled {
            focusWhenAttached = false
            window.makeFirstResponder(self)
            if let editor = currentEditor() as? NSTextView {
                let end = NSRange(location: stringValue.utf16.count, length: 0)
                editor.setSelectedRange(end)
                editor.scrollRangeToVisible(end)
            }
        }
    }
}
