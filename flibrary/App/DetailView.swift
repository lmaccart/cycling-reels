import AVKit
import SwiftUI

struct DetailView: View {
    let store: LibraryStore
    let asset: Asset
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        HStack(spacing: 0) {
            viewer.frame(maxWidth: .infinity, maxHeight: .infinity).background(.black)
            Divider()
            Inspector(store: store, asset: store.assets.first { $0.id == asset.id } ?? asset).frame(width: 340)
        }
        .frame(minWidth: 1020, minHeight: 620)
        .overlay(alignment: .topTrailing) { Button("Close") { dismiss() }.keyboardShortcut(.cancelAction).padding(8) }
    }

    @ViewBuilder private var viewer: some View {
        if asset.isPhoto { StillView(path: asset.media) }
        else if asset.is360 { ReframeView(store: store, asset: asset) }
        else { PlayerView(url: asset.mediaURL) }
    }
}

struct PlayerView: View {
    let url: URL
    @State private var player: AVPlayer?
    var body: some View {
        Group { if let player { VideoPlayer(player: player) } else { ProgressView() } }
            .task(id: url) { player = AVPlayer(url: url) }
            .onDisappear { player?.pause() }
    }
}

struct StillView: View {
    let path: String
    @State private var image: CGImage?
    var body: some View {
        Group { if let image { Image(decorative: image, scale: 1).resizable().scaledToFit() } else { ProgressView() } }
            .task(id: path) { image = await Media.image(path: path, maxPixel: 3000) }
    }
}

struct Inspector: View {
    let store: LibraryStore
    let asset: Asset
    @Environment(ToolsModel.self) private var tools

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text(asset.originalName ?? asset.id).font(.title3.bold()).textSelection(.enabled)
                if asset.is360 { Tools360Panel(asset: asset) }
                if let ex = asset.exports, !ex.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Exports (drag into Premiere)").font(.headline)
                        ForEach(ex, id: \.path) { e in
                            HStack {
                                Image(systemName: "square.and.arrow.up.on.square")
                                VStack(alignment: .leading) {
                                    Text(e.label ?? e.kind)
                                    Text(URL(fileURLWithPath: e.path).lastPathComponent).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                                }
                                Spacer()
                                if let b = e.bytes { Text(ByteCountFormatter.string(fromByteCount: Int64(b), countStyle: .file)).font(.caption) }
                            }
                            .padding(6).background(.quaternary, in: RoundedRectangle(cornerRadius: 6))
                            .overlay { DragSurface(dragURLs: { [URL(fileURLWithPath: e.path)] },
                                                   menu: { [MenuAction(title: "Reveal in Finder") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: e.path)]) }] }) }
                        }
                    }
                }
                metadata
                HStack {
                    Button("Reveal in Finder") { store.reveal([asset]) }
                    Button("Copy path") { store.copyPaths([asset]) }
                }
            }.padding()
        }
    }

    private var metadata: some View {
        let t = asset.tags
        let rows: [(String, String?)] = [
            ("ID", asset.id), ("Captured", asset.created), ("Duration", asset.duration.map { formatDuration($0) }),
            ("Size", asset.width.flatMap { w in asset.height.map { "\(w)x\($0)" } }), ("FPS", asset.fps.map { String(format: "%.2f", $0) }),
            ("Projection", asset.projection), ("Type / shot", asset.shotLabel), ("Quality", t?.quality.map(String.init)),
            ("People", t?.people), ("Location", t?.location), ("Summary", t?.summary),
            ("Hooks", t?.hooks?.joined(separator: "\n")), ("Notes", t?.notes), ("File", asset.media)]
        return Grid(alignment: .topLeading, horizontalSpacing: 10, verticalSpacing: 6) {
            ForEach(rows.filter { $0.1?.isEmpty == false }, id: \.0) { row in
                GridRow {
                    Text(row.0).foregroundStyle(.secondary).font(.caption)
                    Text(row.1 ?? "").font(.callout).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }
}
