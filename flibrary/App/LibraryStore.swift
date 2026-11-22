import AppKit
import SwiftUI

@MainActor @Observable
final class LibraryStore {
    let root: URL
    let backend: Backend
    var assets: [Asset] = []
    var freeBytes: Int?
    var error: String?
    var loading = false
    var filter = LibraryFilter()
    var selection: Set<String> = []
    var detail: Asset?
    private var anchor: String?
    private var watcher: DispatchSourceFileSystemObject?
    private var debounce: Task<Void, Never>?

    init(root: URL) {
        self.root = root
        backend = Backend(root: root)
    }

    var visible: [Asset] { filter.apply(assets) }
    var types: [String] { Array(Set(assets.compactMap { $0.tags?.type })).sorted() }
    var selectedAssets: [Asset] { let s = selection; return visible.filter { s.contains($0.id) } }

    func refresh() async {
        loading = true
        defer { loading = false }
        do {
            let list = try await backend.list()
            assets = list.assets
            freeBytes = list.freeBytes
            error = nil
            let ids = Set(assets.map(\.id))
            selection = selection.intersection(ids)
        } catch { self.error = error.localizedDescription }
    }

    /// Watches library/assets for added or replaced sidecars and refreshes (debounced).
    func startWatching() {
        let fd = open(root.appending(path: "library/assets").path, O_EVTONLY)
        guard fd >= 0 else { return }
        let src = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .rename, .delete, .extend], queue: .main)
        src.setEventHandler { [weak self] in
            MainActor.assumeIsolated { self?.scheduleRefresh() }
        }
        src.setCancelHandler { close(fd) }
        src.resume()
        watcher = src
    }

    private func scheduleRefresh() {
        debounce?.cancel()
        debounce = Task { [weak self] in
            try? await Task.sleep(for: .seconds(1))
            guard !Task.isCancelled else { return }
            await self?.refresh()
        }
    }

    // MARK: Selection

    func mouseDown(_ id: String, modifiers: NSEvent.ModifierFlags) {
        let ids = visible.map(\.id)
        if modifiers.contains(.shift), let a = anchor, let i = ids.firstIndex(of: a), let j = ids.firstIndex(of: id) {
            selection = Set(ids[min(i, j)...max(i, j)])
        } else if modifiers.contains(.command) {
            if selection.contains(id) { selection.remove(id) } else { selection.insert(id) }
            anchor = id
        } else if !selection.contains(id) {
            selection = [id]; anchor = id
        }
    }

    /// Plain click on an already-selected item (no drag) collapses the selection to it.
    func click(_ id: String, modifiers: NSEvent.ModifierFlags) {
        guard modifiers.intersection([.shift, .command]).isEmpty else { return }
        selection = [id]; anchor = id
    }

    func openDetail(_ id: String? = nil) {
        if let a = assets.first(where: { $0.id == (id ?? selection.first) }) { detail = a }
    }

    // MARK: Actions

    func reveal(_ list: [Asset]) {
        NSWorkspace.shared.activateFileViewerSelecting(list.map(\.mediaURL))
    }

    func copyPaths(_ list: [Asset]) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(list.map(\.media).joined(separator: "\n"), forType: .string)
    }
}
