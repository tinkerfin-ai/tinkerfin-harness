import AppKit

@main
@MainActor
struct CompanionApplication {
    static func main() {
        let app = NSApplication.shared
        let arguments = CommandLine.arguments
        let state: URL
        if let position = arguments.firstIndex(of: "--state-directory"), arguments.indices.contains(position + 1) {
            state = URL(fileURLWithPath: arguments[position + 1], isDirectory: true)
        } else {
            state = URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
                .appendingPathComponent(".tinkerfin", isDirectory: true)
        }
        let resources = Bundle.main.resourceURL ?? Bundle.main.bundleURL
        let delegate = CompanionAppDelegate(model: CompanionModel(state: state, resources: resources), resources: resources)
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        withExtendedLifetime(delegate) { app.run() }
    }
}
