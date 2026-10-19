import Combine
import Foundation

enum ClientLanguage: String, CaseIterable, Identifiable {
    case system, simplifiedChinese = "zh-Hans", english = "en"
    var id: String { rawValue }
}

/// 语言只属于本应用界面；切换时保留连接、编辑草稿和原始错误，展示时才翻译
@MainActor
final class ClientLocalization: ObservableObject {
    @Published private(set) var language: ClientLanguage
    @Published private(set) var identifier: String
    private let defaults: UserDefaults
    private let bundles: [String: Bundle]
    private static let preferenceKey = "interfaceLanguage"

    init(resources: URL, defaults: UserDefaults = .standard) {
        self.defaults = defaults
        let selected = defaults.string(forKey: Self.preferenceKey).flatMap(ClientLanguage.init(rawValue:)) ?? .system
        language = selected
        identifier = Self.resolve(selected, preferredLanguages: Locale.preferredLanguages)
        bundles = Dictionary(uniqueKeysWithValues: ["zh-Hans", "en"].compactMap { identifier in
            Bundle(url: resources.appendingPathComponent(identifier + ".lproj"))
                .map { (identifier, $0) }
        })
    }

    static func resolve(_ language: ClientLanguage, preferredLanguages: [String]) -> String {
        if language != .system { return language.rawValue }
        for value in preferredLanguages {
            let locale = Locale.Language(identifier: value)
            if locale.languageCode?.identifier == "en" { return "en" }
            if locale.languageCode?.identifier == "zh", locale.script?.identifier == "Hans" { return "zh-Hans" }
        }
        return "en"
    }

    func select(_ language: ClientLanguage) {
        guard language != self.language else { return }
        if language == .system { defaults.removeObject(forKey: Self.preferenceKey) }
        else { defaults.set(language.rawValue, forKey: Self.preferenceKey) }
        self.language = language
        refreshSystemLanguage()
    }

    func refreshSystemLanguage() {
        let next = Self.resolve(language, preferredLanguages: Locale.preferredLanguages)
        if identifier != next { identifier = next }
    }

    func text(_ key: String) -> String {
        bundles[identifier]?.localizedString(forKey: key, value: key, table: nil) ?? key
    }

    func label(for language: ClientLanguage) -> String {
        switch language {
        case .system: return text("跟随系统")
        case .simplifiedChinese: return "简体中文"
        case .english: return "English"
        }
    }

    func message(_ error: ClientError) -> String {
        let format = text(error.textKey)
        return error.arguments.isEmpty ? format : String(format: format, arguments: error.arguments)
    }
}
