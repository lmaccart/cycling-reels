import SwiftUI

/// XCTest needs an AppKit run loop, not SwiftUI's launch/restoration of the editor scene.
@main
enum EditorEntryPoint {
    @MainActor
    static func main() {
        if EditorAppDelegate.isTestHost {
            let application = NSApplication.shared
            application.setActivationPolicy(.prohibited)
            let delegate = EditorAppDelegate()
            application.delegate = delegate
            withExtendedLifetime(delegate) { application.run() }
        } else {
            ReelsEditorApp.main()
        }
    }
}

struct ReelsEditorApp: App {
    @NSApplicationDelegateAdaptor(EditorAppDelegate.self) private var appDelegate
    @State private var store = EditorStore.shared

    var body: some Scene {
        // A single Window (not WindowGroup) so there is one store and one editing service.
        Window("Reels Editor", id: "main") {
            RootView(store: store)
                .frame(minWidth: 1280, minHeight: 768)
                .onAppear {
                    if store.serviceState == .stopped { store.connect() }
                }
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1520, height: 940)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Reel") { store.beginNewReel() }
                    .keyboardShortcut("n", modifiers: .command)
                Button("Open Project…") { store.showPicker = true }
                    .keyboardShortcut("o", modifiers: .command)
                Divider()
                Button("Import Footage…") { store.importer.chooseFiles() }
                    .keyboardShortcut("i", modifiers: .command)
                Divider()
                Button("Schedule…") { store.beginSchedule() }
                    .keyboardShortcut("s", modifiers: [.command, .shift])
                    .disabled(!store.hasDocument)
                Divider()
                Button("Open in Claude Code") { store.openInClaudeCode() }
                    .keyboardShortcut("c", modifiers: [.command, .option])
                    .disabled(store.repositoryRoot == nil)
            }
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
