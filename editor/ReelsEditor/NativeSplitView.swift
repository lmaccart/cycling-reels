import AppKit
import SwiftUI

struct NativeSplitView: NSViewRepresentable {
    let views: [AnyView]
    let vertical: Bool
    let autosaveName: String
    let minimumSizes: [CGFloat]
    let initialFractions: [CGFloat]

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSSplitView {
        let split = NSSplitView()
        split.isVertical = vertical
        split.dividerStyle = .thin
        split.autosaveName = NSSplitView.AutosaveName(autosaveName)
        split.delegate = context.coordinator
        for (index, view) in views.enumerated() {
            let host = NSHostingView(rootView: view)
            host.translatesAutoresizingMaskIntoConstraints = true
            host.autoresizingMask = vertical ? [.height] : [.width]
            split.addSubview(host)
            if index < minimumSizes.count {
                split.setHoldingPriority(.defaultHigh, forSubviewAt: index)
            }
        }
        return split
    }

    func updateNSView(_ split: NSSplitView, context: Context) {
        context.coordinator.parent = self
        guard split.subviews.count == views.count else { return }
        for (index, view) in views.enumerated() {
            (split.subviews[index] as? NSHostingView<AnyView>)?.rootView = view
        }
    }

    @MainActor
    final class Coordinator: NSObject, NSSplitViewDelegate {
        var parent: NativeSplitView
        private var didApplyInitial = false
        init(_ parent: NativeSplitView) { self.parent = parent }

        func splitView(_ splitView: NSSplitView, constrainMinCoordinate proposed: CGFloat, ofSubviewAt dividerIndex: Int) -> CGFloat {
            let prior = parent.minimumSizes.prefix(dividerIndex + 1).reduce(CGFloat.zero, +)
            return max(proposed, prior)
        }

        func splitView(_ splitView: NSSplitView, constrainMaxCoordinate proposed: CGFloat, ofSubviewAt dividerIndex: Int) -> CGFloat {
            let dimension = parent.vertical ? splitView.bounds.width : splitView.bounds.height
            let trailing = parent.minimumSizes.dropFirst(dividerIndex + 1).reduce(CGFloat.zero, +)
            return min(proposed, dimension - trailing)
        }

        func splitView(_ splitView: NSSplitView, canCollapseSubview subview: NSView) -> Bool { false }

        func splitViewDidResizeSubviews(_ notification: Notification) {
            guard let split = notification.object as? NSSplitView else { return }
            let total = (parent.vertical ? split.bounds.width : split.bounds.height)
                - CGFloat(max(0, split.subviews.count - 1)) * split.dividerThickness
            guard total > 0 else { return }
            let key = "ReelsEditor.Split.\(parent.autosaveName)"
            if !didApplyInitial {
                didApplyInitial = true
                let saved = UserDefaults.standard.array(forKey: key) as? [Double]
                let fractions = saved?.map { CGFloat($0) } ?? parent.initialFractions
                var accumulated: CGFloat = 0
                for index in 0..<max(0, split.subviews.count - 1) {
                    accumulated += index < fractions.count ? fractions[index] : 1 / CGFloat(split.subviews.count)
                    split.setPosition(total * accumulated + CGFloat(index) * split.dividerThickness, ofDividerAt: index)
                }
            } else {
                let sizes = split.subviews.map { parent.vertical ? $0.frame.width : $0.frame.height }
                let sum = sizes.reduce(CGFloat.zero, +)
                guard sum > 0 else { return }
                UserDefaults.standard.set(sizes.map { Double($0 / sum) }, forKey: key)
            }
        }
    }
}
