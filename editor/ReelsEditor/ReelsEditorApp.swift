import SwiftUI

@main
struct ReelsEditorApp: App {
    @NSApplicationDelegateAdaptor(EditorAppDelegate.self) private var appDelegate
    @State private var store = EditorStore()

    var body: some Scene {
        WindowGroup {
            ContentView(store: store)
                .frame(minWidth: 1280, minHeight: 768)
                .preferredColorScheme(.dark)
                .onAppear {
                    let testHost = ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
                        || NSClassFromString("XCTestCase") != nil
                    if !testHost && store.hasLaunchConfiguration { store.connect() }
                }
        }
        .defaultSize(width: 1520, height: 940)
        .commands {
            CommandGroup(after: .undoRedo) {
                Divider()
                Button("Undo Reel Edit") { store.undo() }
                    .disabled(!store.hasDocument)
                Button("Redo Reel Edit") { store.redo() }
                    .disabled(!store.hasDocument)
            }
            CommandGroup(after: .textEditing) {
                Divider()
                Button("Split at Playhead") { store.splitAtPlayhead() }
                Button("Delete Selected Clips") { store.deleteSelection() }
            }
        }
    }
}
