import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct FlibraryApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var store: LibraryStore?
    @State private var tools = ToolsModel()
    @AppStorage(GridZoom.key) private var tileWidth = GridZoom.standard

    var body: some Scene {
        WindowGroup("flibrary") {
            Group {
                if let store {
                    LibraryView(store: store).environment(tools).onAppear { tools.store = store }
                } else {
                    VStack(spacing: 12) {
                        Text("Choose your cycling folder").font(.title3)
                        Text("flibrary needs the folder that holds ./reels and library/.").foregroundStyle(.secondary)
                        Button("Choose Folder…") { if let r = RepositoryLocator.pick() { store = LibraryStore(root: r) } }
                            .keyboardShortcut(.defaultAction)
                    }
                    .frame(minWidth: 480, minHeight: 240)
                }
            }
            .task {
                guard store == nil, !isTestHost else { return }
                if let root = RepositoryLocator.resolve() ?? RepositoryLocator.pick() { store = LibraryStore(root: root) }
            }
        }
        .defaultSize(width: 1240, height: 800)
        .commands {
            CommandGroup(after: .toolbar) {
                Button("Bigger Thumbnails") { tileWidth = GridZoom.clamp(tileWidth * 1.2) }.keyboardShortcut("=", modifiers: .command)
                Button("Smaller Thumbnails") { tileWidth = GridZoom.clamp(tileWidth / 1.2) }.keyboardShortcut("-", modifiers: .command)
                Button("Actual Size Thumbnails") { tileWidth = GridZoom.standard }.keyboardShortcut("0", modifiers: .command)
            }
        }
    }

    private var isTestHost: Bool { ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil }
}
