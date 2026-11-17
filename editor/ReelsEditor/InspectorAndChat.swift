import AppKit
import SwiftUI

struct RightRail: View {
    @Bindable var store: EditorStore

    var body: some View {
        InspectorPane(store: store)
    }
}

// MARK: - Section styling (Premiere "Effect Controls" look)

private extension InspectorSection {
    var tint: Color {
        switch self {
        case .motion: Color(red: 0.27, green: 0.55, blue: 0.98)
        case .timing: Color(red: 0.98, green: 0.60, blue: 0.20)
        case .colour: Color(red: 0.93, green: 0.35, blue: 0.67)
        case .text: Color(red: 0.66, green: 0.42, blue: 0.92)
        case .audio: Color(red: 0.30, green: 0.73, blue: 0.45)
        case .overlays: Color(red: 0.20, green: 0.72, blue: 0.74)
        case .post, .advanced: Color(red: 0.58, green: 0.60, blue: 0.64)
        }
    }
}

private struct InspectorTintKey: EnvironmentKey {
    static let defaultValue: Color = EditorPalette.accent
}

private extension EnvironmentValues {
    var inspectorTint: Color {
        get { self[InspectorTintKey.self] }
        set { self[InspectorTintKey.self] = newValue }
    }
}

/// A collapsible section with a coloured accent bar and SF Symbol, like an Effect Controls group.
private struct InspectorSectionBox<Content: View>: View {
    let section: InspectorSection
    let title: String
    @State private var expanded = true
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeOut(duration: 0.12)) { expanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .bold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                        .frame(width: 8)
                    Image(systemName: section.symbol)
                        .font(EditorType.section)
                        .foregroundStyle(section.tint)
                        .frame(width: 16)
                    Text(title).font(EditorType.section)
                    Spacer(minLength: 0)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(title)
            .accessibilityValue(expanded ? "expanded" : "collapsed")
            if expanded {
                VStack(alignment: .leading, spacing: 7) { content() }
                    .padding(.top, 8)
            }
        }
        .padding(.vertical, 7)
        .padding(.leading, 11)
        .padding(.trailing, 9)
        .background(EditorPalette.raised.opacity(0.55))
        .overlay(alignment: .leading) { Rectangle().fill(section.tint).frame(width: 3) }
        .clipShape(RoundedRectangle(cornerRadius: 5))
        .environment(\.inspectorTint, section.tint)
    }
}

private enum FieldRowMetrics {
    /// Dropdowns and text fields never grow past this, so a long value (a moment label,
    /// a clip ID) truncates inside the control instead of widening the whole panel.
    static let controlMaxWidth: CGFloat = 170
}

/// Label on the left, control right-aligned. The label keeps its width first and the
/// control shrinks to whatever is left, so rows always fit the panel.
private struct FieldRow<Control: View>: View {
    let label: String
    @ViewBuilder var control: () -> Control

    var body: some View {
        HStack(spacing: 6) {
            Text(label).font(EditorType.body).lineLimit(1).truncationMode(.tail)
                .layoutPriority(1)
            Spacer(minLength: 4)
            control()
        }
        .frame(maxWidth: .infinity)
    }
}

private func kindChip(_ node: EditorNode) -> some View {
    Text(humanize(node.kind))
        .font(EditorType.bodyEmphasis)
        .foregroundStyle(laneColor(node.lane))
        .padding(.horizontal, 7)
        .padding(.vertical, 2)
        .background(laneColor(node.lane).opacity(0.18), in: Capsule())
}

private struct PermissionGrantSummary: View {
    @Bindable var store: EditorStore
    @State private var expanded = false

    var body: some View {
        if !store.currentGrants.isEmpty {
            DisclosureGroup(isExpanded: $expanded) {
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(Array(store.currentGrants.enumerated()), id: \.offset) { _, grant in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(grant["kind"]?.stringValue ?? "Permission")
                                .font(EditorType.bodyEmphasis)
                            if let scope = grant["scope"] {
                                Text(scope.displayValue)
                                    .font(EditorType.timecode)
                                    .foregroundStyle(.tertiary)
                            }
                        }
                    }
                }
                .padding(.leading, 6)
                .padding(.top, 4)
            } label: {
                Label("Granted permissions", systemImage: "checkmark.shield")
                    .font(EditorType.section)
            }
            .tint(.secondary)
        }
    }
}

private struct InspectorPane: View {
    @Bindable var store: EditorStore

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                PanelTitle("Effect Controls")
                if let node = store.inspector.selectedNode {
                    Text(node.title)
                        .font(EditorType.bodyEmphasis)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 4)
                    kindChip(node)
                } else {
                    Spacer()
                }
            }
            .padding(.horizontal, 12)
            .frame(height: 34)
            .background(EditorPalette.panel)
            Divider()

            if let snapshot = store.inspector.snapshot, let node = store.inspector.selectedNode {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        clipProperties(node)
                        if SchemaInspector.hasDescriptors(store.inspectorSchema) {
                            SchemaInspector(schema: store.inspectorSchema, fields: node.raw, documentID: snapshot.documentId, nodeID: node.id, store: store)
                        } else {
                            nativeFallbackGroups(node: node, documentID: snapshot.documentId, store: store)
                        }
                        if let words = node.raw["words"]?.arrayValue {
                            TranscriptWordEditor(node: node, words: words, documentID: snapshot.documentId, store: store)
                        }
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            } else if let snapshot = store.inspector.snapshot {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        reelProperties(snapshot)
                        if SchemaInspector.hasDescriptors(store.inspectorSchema) {
                            SchemaInspector(schema: store.inspectorSchema,
                                            fields: snapshot.plan,
                                            documentID: snapshot.documentId, nodeID: "document", store: store,
                                            targetKeys: ["document", "post", "hook", "endCard", "titleCard"])
                            if snapshot.projectConfig == nil && store.workspaceInfo["config"] == nil {
                                Text("Showing default values: this reel's saved settings haven't loaded yet.")
                                    .font(EditorType.caption)
                                    .foregroundStyle(.tertiary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            SchemaInspector(schema: store.inspectorSchema,
                                            fields: snapshot.projectConfig ?? store.workspaceInfo["config"] ?? .object([:]),
                                            documentID: snapshot.documentId, nodeID: "project", store: store,
                                            targetKeys: InspectorSchemaSupport.projectConfigTargets)
                            PermissionGrantSummary(store: store)
                        } else {
                            InspectorGroup(title: "Project settings", icon: "film.stack",
                                           fields: snapshot.projectConfig ?? .object([:]),
                                           documentID: snapshot.documentId, nodeID: "project", store: store)
                            InspectorGroup(title: "Project content", icon: "text.alignleft", fields: snapshot.plan,
                                           excluding: ["segments", "cutaways", "render", "fps", "width", "height", "dimensions"],
                                           documentID: snapshot.documentId, nodeID: "document", store: store)
                        }
                        if !snapshot.warnings.isEmpty {
                            VStack(alignment: .leading, spacing: 6) {
                                Label("Validation", systemImage: "exclamationmark.triangle.fill")
                                    .font(EditorType.section)
                                    .foregroundStyle(.orange)
                                ForEach(Array(snapshot.warnings.enumerated()), id: \.offset) { _, warning in
                                    Text(warning)
                                        .font(EditorType.caption)
                                        .foregroundStyle(.secondary)
                                        .textSelection(.enabled)
                                }
                            }
                            .padding(8)
                            .background(Color.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 5))
                        }
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            } else {
                ContentUnavailableView("Nothing to inspect", systemImage: "slider.horizontal.3",
                                       description: Text("Open a reel, then select a clip on the timeline to edit its settings."))
            }
        }
        .background(EditorPalette.panel)
    }

    private func clipProperties(_ node: EditorNode) -> some View {
        let fps = max(store.inspector.snapshot?.fps ?? 30, 1)
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text("CLIP PROPERTIES")
                    .font(EditorType.panelTitle).tracking(EditorType.panelTitleTracking)
                    .foregroundStyle(.secondary)
                Spacer()
                kindChip(node)
            }
            Text(node.title).font(.system(size: 13, weight: .semibold)).lineLimit(2)
            HStack(spacing: 14) {
                timecodeCell("In", TimeFormat.timecode(node.startFrame, fps: fps))
                timecodeCell("Out", TimeFormat.timecode(node.endFrame, fps: fps))
                timecodeCell("Duration", TimeFormat.timecode(node.frames, fps: fps))
            }
            Text("ID  \(node.id)")
                .font(EditorType.timecode)
                .foregroundStyle(.tertiary)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
                .help("Stable ID of this item.")
            if node.isReadOnly {
                Label("Caption timing comes from the transcript and can't be moved", systemImage: "lock.fill")
                    .font(EditorType.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(EditorPalette.raised.opacity(0.55), in: RoundedRectangle(cornerRadius: 5))
    }

    private func timecodeCell(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label).font(EditorType.caption).foregroundStyle(.secondary)
            Text(value).font(EditorType.timecode).foregroundStyle(EditorPalette.accent)
        }
    }

    private func reelProperties(_ snapshot: EditorSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("REEL PROPERTIES")
                .font(EditorType.panelTitle).tracking(EditorType.panelTitleTracking)
                .foregroundStyle(.secondary)
            Text(snapshot.reelTitle).font(.system(size: 13, weight: .semibold)).lineLimit(2)
            HStack(spacing: 14) {
                timecodeCell("Duration", TimeFormat.timecode(snapshot.durationFrames, fps: snapshot.fps))
                timecodeCell("Frame rate", "\(snapshot.fps.formatted(.number.precision(.fractionLength(0...2)))) fps")
                timecodeCell("Format", snapshot.plan["format"]?.displayValue ?? "none")
            }
            Label("Nothing selected. Click a clip on the timeline to edit its effects. Below are the whole-reel settings: music, captions and post.",
                  systemImage: "cursorarrow.click")
                .font(EditorType.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 2)
        }
        .padding(9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(EditorPalette.raised.opacity(0.55), in: RoundedRectangle(cornerRadius: 5))
    }
}

@ViewBuilder
@MainActor
private func nativeFallbackGroups(node: EditorNode, documentID: String, store: EditorStore) -> some View {
    InspectorGroup(title: "Source & content", icon: "film", fields: node.raw, keys: [
        "asset", "moment", "in", "out", "captions", "text", "hook", "endCard", "payoff", "label", "title"
    ], documentID: documentID, nodeID: node.id, store: store)
    InspectorGroup(title: "Framing & motion", icon: "crop.rotate", fields: node.raw, keys: [
        "focusX", "focusY", "crop", "framing", "frameMode", "speed", "motion", "zoom", "pan", "rotation", "stabilize", "view"
    ], documentID: documentID, nodeID: node.id, store: store)
    InspectorGroup(title: "Cutaways", icon: "rectangle.on.rectangle", fields: node.raw, keys: [
        "cutaways", "broll", "overlays"
    ], documentID: documentID, nodeID: node.id, store: store)
    InspectorGroup(title: "Effects & graphics", icon: "sparkles", fields: node.raw, keys: [
        "punch", "punches", "blur", "view360", "360", "route", "stats", "progress", "exhibits",
        "foreground", "handAnchor", "titleCard", "projection", "path"
    ], documentID: documentID, nodeID: node.id, store: store)
    InspectorGroup(title: "Audio", icon: "waveform", fields: node.raw, keys: [
        "volume", "sound", "naturalSound", "bleeps", "sfx", "music", "musicStart", "mix", "beatSnap", "drop", "audio"
    ], documentID: documentID, nodeID: node.id, store: store)
    InspectorGroup(title: "Timing & transitions", icon: "clock", fields: node.raw, keys: [
        "transition", "fade", "at", "for", "duration", "start", "loop", "beats"
    ], documentID: documentID, nodeID: node.id, store: store)
    InspectorGroup(title: "Other format fields", icon: "slider.horizontal.3", fields: node.raw,
                   excluding: ["asset", "moment", "in", "out", "captions", "text", "hook", "endCard", "payoff",
                               "label", "title", "focusX", "focusY", "crop", "framing", "frameMode", "speed",
                               "motion", "zoom", "pan", "rotation", "stabilize", "view", "cutaways", "broll",
                               "overlays", "punch", "punches", "blur", "view360", "360", "route", "stats", "progress",
                               "exhibits", "foreground", "handAnchor", "titleCard", "projection", "path", "volume",
                               "sound", "naturalSound", "bleeps", "sfx", "music", "musicStart", "mix", "beatSnap",
                               "drop", "audio", "transition", "fade", "at", "for", "duration", "start", "loop", "beats", "words"],
                   documentID: documentID, nodeID: node.id, store: store)
}

private struct InspectorGroup: View {
    let title: String
    let icon: String
    let fields: JSONValue
    var keys: [String]?
    var excluding: [String] = []
    let documentID: String
    let nodeID: String
    @Bindable var store: EditorStore

    private var selectedFields: [(String, JSONValue)] {
        guard let object = fields.objectValue else { return [] }
        let selected = Set(keys ?? [])
        return object.keys.sorted().compactMap { key in
            guard !excluding.contains(key), !key.hasPrefix("_"), !key.hasPrefix("$"),
                  keys == nil || selected.contains(key),
                  let value = object[key] else { return nil }
            return (key, value)
        }
    }

    var body: some View {
        if !selectedFields.isEmpty {
            InspectorSectionBox(
                section: InspectorSection.classify(groupID: title, label: title),
                title: title
            ) {
                ForEach(selectedFields, id: \.0) { key, value in
                    JSONFieldEditor(
                        name: key,
                        value: value,
                        path: [key],
                        documentID: documentID,
                        nodeID: nodeID,
                        store: store
                    )
                }
            }
        }
    }
}

private struct SchemaInspector: View {
    let schema: JSONValue
    let fields: JSONValue
    let documentID: String
    let nodeID: String
    @Bindable var store: EditorStore
    var targetKeys: [String]? = nil

    static func hasDescriptors(_ schema: JSONValue) -> Bool {
        !(schema["targets"]?.objectValue ?? [:]).isEmpty
    }

    private var selectedNode: EditorNode? {
        store.inspector.snapshot?.nodes.first(where: { $0.id == nodeID })
    }
    private var isReadOnly: Bool { selectedNode?.isReadOnly == true }

    private var assetKind: String? {
        guard let id = selectedNode?.assetId else { return nil }
        let asset = store.library.first(where: { $0["id"]?.stringValue == id })
        return selectedNode?.assetKind
            ?? asset?["type"]?.stringValue
            ?? asset?["mimeType"]?.stringValue
            ?? asset?["media"]?.stringValue
            ?? store.timelineSourceURLs[id]?.pathExtension
    }

    private var activeTargetKeys: [String] {
        targetKeys ?? InspectorSchemaSupport.targetNames(
            node: selectedNode,
            isProject: nodeID == "project",
            assetKind: assetKind,
            available: schema
        )
    }

    private var coveredRoots: [String] {
        let targets = schema["targets"]
        let stableNodeScope = nodeID != "project" && nodeID != "document"
        let roots = activeTargetKeys.flatMap { key -> [String] in
            let target = targets?[key]
            let scope = target?["scopePath"]?.stringValue ?? ""
            return (target?["groups"]?.arrayValue ?? []).flatMap { group in
                (group["fields"]?.arrayValue ?? []).compactMap { field in
                    guard let path = field["path"]?.stringValue else { return nil }
                    let parts = stableNodeScope
                        ? InspectorSchemaSupport.relativeComponents(path: path, scope: scope)
                        : InspectorSchemaSupport.components(path)
                    return parts.first
                }
            }
        }
        return Array(Set(roots)).sorted()
    }

    var body: some View {
        Group {
            ForEach(activeTargetKeys, id: \.self) { key in
                SchemaTargetGroups(schema: schema, targetKey: key, fields: fields, documentID: documentID, nodeID: nodeID, store: store)
            }
            if !isReadOnly {
                InspectorGroup(
                    title: "Other settings (advanced)",
                    icon: "curlybraces",
                    fields: fields,
                    excluding: Array(Set(coveredRoots + ["_editor"])),
                    documentID: documentID,
                    nodeID: nodeID,
                    store: store
                )
            }
        }
    }
}

private struct SchemaTargetGroups: View {
    let schema: JSONValue
    let targetKey: String
    let fields: JSONValue
    let documentID: String
    let nodeID: String
    @Bindable var store: EditorStore
    private var target: JSONValue { schema["targets"]?[targetKey] ?? .object([:]) }
    private var scope: String { target["scopePath"]?.stringValue ?? "" }
    private var groups: [JSONValue] { target["groups"]?.arrayValue ?? [] }
    private var scopedToSelectedNode: Bool { nodeID != "project" && nodeID != "document" }
    private var selectedNode: EditorNode? { store.inspector.snapshot?.nodes.first(where: { $0.id == nodeID }) }
    private var selectedNodeReadOnly: Bool { selectedNode?.isReadOnly == true }
    private var assetRecord: JSONValue? {
        guard let id = selectedNode?.assetId else { return nil }
        return store.library.first(where: { $0["id"]?.stringValue == id })
    }

    var body: some View {
        ForEach(Array(groups.enumerated()), id: \.offset) { _, group in
            let groupID = group["id"]?.stringValue ?? group["label"]?.stringValue ?? targetKey
            let groupLabel = group["label"]?.stringValue ?? target["label"]?.stringValue ?? targetKey
            InspectorSectionBox(
                section: InspectorSection.classify(groupID: groupID, label: groupLabel),
                title: groupLabel
            ) {
                    ForEach(Array((group["fields"]?.arrayValue ?? []).enumerated()), id: \.offset) { _, descriptor in
                        let schemaPath = descriptor["path"]?.stringValue ?? ""
                        let path = scopedToSelectedNode
                            ? InspectorSchemaSupport.relativeComponents(path: schemaPath, scope: scope)
                            : InspectorSchemaSupport.components(schemaPath)
                        if !path.isEmpty,
                           InspectorSchemaSupport.isVisible(
                            descriptor, fields: fields, scope: scope, scoped: scopedToSelectedNode
                           ) {
                            let value = fields.value(at: path) ?? InspectorSchemaSupport.defaultValue(for: descriptor)
                            if descriptor["id"]?.stringValue == "moment" || path.last == "moment" {
                                let moments = AssetPresentation.moments(assetRecord)
                                if !moments.isEmpty {
                                    MomentPickerField(
                                        name: descriptor["label"]?.stringValue ?? "Tagged moment",
                                        moments: moments,
                                        value: value,
                                        readOnly: descriptor["readOnly"]?.boolValue == true || selectedNodeReadOnly
                                    ) { newValue in
                                        let pointer = "/" + path.map {
                                            $0.replacingOccurrences(of: "~", with: "~0").replacingOccurrences(of: "/", with: "~1")
                                        }.joined(separator: "/")
                                        store.editField(documentID: documentID, nodeID: nodeID, path: pointer, value: newValue)
                                    }
                                }
                            } else {
                            SchemaFieldEditor(
                                name: descriptor["label"]?.stringValue ?? path.last ?? targetKey,
                                value: value,
                                path: path,
                                schemaPath: schemaPath,
                                rootFields: fields,
                                documentID: documentID,
                                nodeID: nodeID,
                                schema: schema,
                                descriptor: descriptor,
                                store: store
                            )
                            if let help = descriptor["help"]?.stringValue, !help.isEmpty {
                                Text(help).font(EditorType.caption).foregroundStyle(.tertiary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            }
                        }
                    }
            }
        }
    }
}

private struct SchemaFieldEditor: View {
    let name: String
    let value: JSONValue
    let path: [String]
    let schemaPath: String
    let rootFields: JSONValue
    let documentID: String
    let nodeID: String
    let schema: JSONValue
    let descriptor: JSONValue
    @Bindable var store: EditorStore

    private var type: String { descriptor["type"]?.stringValue ?? "" }
    private var currentValue: JSONValue {
        value == .null ? InspectorSchemaSupport.defaultValue(for: descriptor) : value
    }
    private var nodeReadOnly: Bool {
        store.inspector.snapshot?.nodes.first(where: { $0.id == nodeID })?.isReadOnly == true
    }

    var body: some View {
        if descriptor["readOnly"]?.boolValue == true || nodeReadOnly {
            FieldRow(label: name) {
                Text(currentValue.displayValue)
                    .font(EditorType.timecode)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
            .accessibilityElement(children: .combine)
        } else {
            switch type {
            case "collection":
                SchemaCollectionField(
                    name: name, value: currentValue, path: path, schemaPath: schemaPath,
                    rootFields: rootFields, documentID: documentID, nodeID: nodeID, schema: schema,
                    descriptor: descriptor, store: store
                )
            case "group":
                SchemaGroupField(
                    name: name, value: currentValue, path: path, schemaPath: schemaPath,
                    rootFields: rootFields, documentID: documentID, nodeID: nodeID, schema: schema,
                    descriptor: descriptor, store: store
                )
            case "keyframe", "keyframes":
                KeyframeField(name: name, value: currentValue, path: path, documentID: documentID, nodeID: nodeID, store: store)
            case "boolean":
                ToggleRow(label: name, isOn: Binding(
                    get: { currentValue.boolValue ?? false },
                    set: { commit(.bool($0)) }
                ))
            case "number" where descriptor["display"]?.stringValue == "slider"
                && descriptor["min"]?.numberValue != nil && descriptor["max"]?.numberValue != nil:
                SliderField(
                    name: name, value: currentValue.numberValue ?? descriptor["default"]?.numberValue ?? 0,
                    minimum: descriptor["min"]?.numberValue ?? 0,
                    maximum: descriptor["max"]?.numberValue ?? 1,
                    stepValue: descriptor["step"]?.numberValue,
                    defaultValue: descriptor["default"]?.numberValue,
                    unit: descriptor["unit"]?.stringValue
                ) { commit(.number($0)) }
            case "number", "timing":
                if let number = currentValue.numberValue {
                    NumberField(
                        name: name, value: number,
                        minimum: descriptor["min"]?.numberValue,
                        maximum: descriptor["max"]?.numberValue,
                        stepValue: descriptor["step"]?.numberValue,
                        unit: descriptor["unit"]?.stringValue
                    ) { commit(.number($0)) }
                } else {
                    StringField(name: name, value: currentValue.stringValue ?? "", options: nil) {
                        commit(.string($0))
                    }
                }
            case "enum":
                SchemaEnumField(name: name, value: currentValue, choices: choices) {
                    commit($0)
                }
            case "color":
                ColorStringField(name: name, value: currentValue.stringValue ?? "") {
                    commit(.string($0))
                }
            case "text":
                StringField(name: name, value: currentValue.stringValue ?? "", options: nil) {
                    commit(.string($0))
                }
            default:
                JSONFieldEditor(name: name, value: currentValue, path: path, documentID: documentID, nodeID: nodeID, store: store)
            }
        }
    }

    private var choices: [JSONValue] {
        descriptor["choices"]?.arrayValue ?? []
    }

    private func commit(_ value: JSONValue) {
        let pointer = "/" + path.map {
            $0.replacingOccurrences(of: "~", with: "~0").replacingOccurrences(of: "/", with: "~1")
        }.joined(separator: "/")
        store.editField(documentID: documentID, nodeID: nodeID, path: pointer, value: value)
    }
}

/// Menu of the tagged moments of a clip; plans store the moment ID.
private struct MomentPickerField: View {
    let name: String
    let moments: [AssetPresentation.Moment]
    let value: JSONValue
    let readOnly: Bool
    let commit: (JSONValue) -> Void

    var body: some View {
        let selected = AssetPresentation.selectedMomentID(value, moments: moments)
        FieldRow(label: name) {
        Picker(name, selection: Binding<String?>(
            get: { selected },
            set: { commit($0.map(JSONValue.string) ?? .string("")) }
        )) {
            Text("None").tag(String?.none)
            Divider()
            ForEach(moments) { moment in
                Text(moment.label).tag(Optional(moment.id))
            }
        }
        .labelsHidden()
        .controlSize(.small)
        .disabled(readOnly)
        .frame(maxWidth: FieldRowMetrics.controlMaxWidth)
        .accessibilityLabel(name)
        }
    }
}

private struct ToggleRow: View {
    let label: String
    @Binding var isOn: Bool
    @Environment(\.inspectorTint) private var tint

    var body: some View {
        FieldRow(label: label) {
            Toggle(label, isOn: $isOn)
                .labelsHidden()
                .toggleStyle(.switch)
                .controlSize(.mini)
                .tint(tint)
        }
        .accessibilityElement(children: .combine)
    }
}

private struct SchemaEnumField: View {
    let name: String
    let value: JSONValue
    let choices: [JSONValue]
    let commit: (JSONValue) -> Void

    var body: some View {
        FieldRow(label: name) {
            Picker(name, selection: Binding(
                get: { choices.contains(value) ? value : (choices.first ?? value) },
                set: { selected in commit(selected) }
            )) {
                ForEach(Array(choices.enumerated()), id: \.offset) { _, choice in
                    Text(choice.displayValue).tag(choice)
                }
            }
            .labelsHidden()
            .controlSize(.small)
            .frame(maxWidth: FieldRowMetrics.controlMaxWidth)
            .accessibilityLabel(name)
        }
    }
}

private struct SchemaGroupField: View {
    let name: String
    let value: JSONValue
    let path: [String]
    let schemaPath: String
    let rootFields: JSONValue
    let documentID: String
    let nodeID: String
    let schema: JSONValue
    let descriptor: JSONValue
    @Bindable var store: EditorStore
    @State private var expanded = true

    var body: some View {
        if case .bool(let enabled) = value {
            VStack(alignment: .leading, spacing: 5) {
                ToggleRow(label: name, isOn: Binding(
                    get: { enabled },
                    set: { commit(.bool($0)) }
                ))
                if enabled { childEditors }
            }
        } else if case .array = value {
            var collection = descriptor.objectValue ?? [:]
            let _ = collection["type"] = .string("collection")
            let _ = collection["itemFields"] = descriptor["fields"] ?? .array([])
            SchemaCollectionField(
                name: name, value: value, path: path, schemaPath: schemaPath,
                rootFields: rootFields, documentID: documentID, nodeID: nodeID, schema: schema,
                descriptor: .object(collection), store: store
            )
        } else {
            DisclosureGroup(isExpanded: $expanded) {
                childEditors
            } label: {
                HStack {
                    Text(name).font(EditorType.bodyEmphasis)
                    Spacer()
                    Text(value.displayValue).font(EditorType.caption).foregroundStyle(.tertiary)
                }
            }
            .tint(.secondary)
        }
    }

    private var childEditors: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array((descriptor["fields"]?.arrayValue ?? []).enumerated()), id: \.offset) { _, child in
                let childSchemaPath = child["path"]?.stringValue ?? ""
                let suffix = InspectorSchemaSupport.relativeComponents(path: childSchemaPath, scope: schemaPath)
                    .filter { !($0.hasPrefix("{") && $0.hasSuffix("}")) }
                let childPath = path + suffix
                let childValue = rootFields.value(at: childPath) ?? InspectorSchemaSupport.defaultValue(for: child)
                if !suffix.isEmpty {
                    SchemaFieldEditor(
                        name: child["label"]?.stringValue ?? suffix.last ?? name,
                        value: childValue,
                        path: childPath,
                        schemaPath: childSchemaPath,
                        rootFields: rootFields,
                        documentID: documentID,
                        nodeID: nodeID,
                        schema: schema,
                        descriptor: child,
                        store: store
                    )
                }
            }
        }
        .padding(.leading, 6)
        .padding(.top, 4)
    }

    private func commit(_ value: JSONValue) {
        let pointer = "/" + path.map {
            $0.replacingOccurrences(of: "~", with: "~0").replacingOccurrences(of: "/", with: "~1")
        }.joined(separator: "/")
        store.editField(documentID: documentID, nodeID: nodeID, path: pointer, value: value)
    }
}

private struct SchemaCollectionField: View {
    let name: String
    let value: JSONValue
    let path: [String]
    let schemaPath: String
    let rootFields: JSONValue
    let documentID: String
    let nodeID: String
    let schema: JSONValue
    let descriptor: JSONValue
    @Bindable var store: EditorStore
    @State private var expanded = true

    private var values: [JSONValue] { value.arrayValue ?? [] }
    private var collectionName: String { path.last ?? name }
    private var itemTargets: [String] {
        if let one = descriptor["itemTarget"]?.stringValue { return [one] }
        return descriptor["itemTargets"]?.arrayValue?.compactMap(\.stringValue) ?? []
    }
    private var itemFields: [JSONValue] { descriptor["itemFields"]?.arrayValue ?? [] }
    private var minItems: Int { Int(descriptor["minItems"]?.numberValue ?? 0) }
    private var maxItems: Int { Int(descriptor["maxItems"]?.numberValue ?? 100) }
    private var supportsOverlayInsertion: Bool {
        nodeID != "project"
            && store.inspector.snapshot?.nodes.first(where: { $0.id == nodeID })?.isReadOnly != true
            && ["nat", "bleep", "windows", "punch", "text", "stats", "exhibits", "sfx", "blur", "cutaways", "path"]
                .contains(collectionName)
    }

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            LazyVStack(alignment: .leading, spacing: 7) {
                ForEach(Array(values.enumerated()), id: \.offset) { index, item in
                    let targetKey = targetKey(for: item)
                    let child = childNode(for: item, targetKey: targetKey)
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(alignment: .firstTextBaseline, spacing: 5) {
                            Text(itemTitle(item, index: index))
                                .font(EditorType.bodyEmphasis)
                                .lineLimit(1)
                            Spacer()
                            if let child, targetKey?.hasPrefix("segment.") == true {
                                Button("Select") {
                                    guard store.snapshot?.documentId == documentID,
                                          store.snapshot?.nodes.contains(where: { $0.id == child.id }) == true else { return }
                                    store.selectNode(child.id)
                                }
                                    .buttonStyle(.borderless)
                                    .controlSize(.mini)
                            }
                            if let child, values.count > minItems,
                               store.inspector.snapshot?.nodes.first(where: { $0.id == nodeID })?.isReadOnly != true {
                                Button {
                                    store.removeInspectorItem(child.id, documentID: documentID)
                                } label: {
                                    Image(systemName: "minus.circle").foregroundStyle(.secondary)
                                }
                                .buttonStyle(.plain)
                                .help("Remove \(name) item \(index + 1)")
                            } else if child == nil, !itemFields.isEmpty, values.count > minItems,
                                      store.inspector.snapshot?.nodes.first(where: { $0.id == nodeID })?.isReadOnly != true {
                                Button {
                                    var next = values
                                    next.remove(at: index)
                                    setCollection(next)
                                } label: {
                                    Image(systemName: "minus.circle").foregroundStyle(.secondary)
                                }
                                .buttonStyle(.plain)
                                .help("Remove \(name) item \(index + 1)")
                            }
                        }
                        if let targetKey, let child, !targetKey.hasPrefix("segment.") {
                            AnyView(SchemaInspector(
                                schema: schema, fields: item, documentID: documentID, nodeID: child.id,
                                store: store, targetKeys: [targetKey]
                            ))
                        } else if !itemFields.isEmpty {
                            ForEach(Array(itemFields.enumerated()), id: \.offset) { _, field in
                                let suffix = InspectorSchemaSupport.relativeComponents(
                                    path: field["path"]?.stringValue ?? "",
                                    scope: schemaPath
                                ).filter { !($0.hasPrefix("{") && $0.hasSuffix("}")) }
                                let itemPath = child == nil ? path + [String(index)] + suffix : suffix
                                let itemRoot = child == nil ? rootFields : item
                                SchemaFieldEditor(
                                    name: field["label"]?.stringValue ?? name,
                                    value: itemRoot.value(at: itemPath) ?? item,
                                    path: itemPath,
                                    schemaPath: field["path"]?.stringValue ?? schemaPath,
                                    rootFields: itemRoot,
                                    documentID: documentID,
                                    nodeID: child?.id ?? nodeID,
                                    schema: schema,
                                    descriptor: field,
                                    store: store
                                )
                            }
                        } else if targetKey == nil || child == nil {
                            Text(item.displayValue).font(EditorType.caption).foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 3)
                    if index < values.count - 1 { Divider().opacity(0.35) }
                }
                addButtons
            }
            .padding(.leading, 6)
            .padding(.top, 4)
        } label: {
            HStack {
                Text(name).font(EditorType.bodyEmphasis)
                Spacer()
                Text("\(values.count)").font(EditorType.timecode).foregroundStyle(.tertiary)
            }
        }
        .tint(.secondary)
    }

    @ViewBuilder
    private var addButtons: some View {
        if !itemTargets.isEmpty, nodeID != "project", values.count < maxItems {
            ForEach(itemTargets.filter { !$0.hasPrefix("segment.") }, id: \.self) { targetKey in
                let target = schema["targets"]?[targetKey] ?? .object([:])
                let (template, valid) = InspectorSchemaSupport.defaultObject(for: target)
                if valid {
                    Button {
                        store.insertInspectorOverlay(
                            documentID: documentID,
                            parentID: nodeID == "document" ? "document" : nodeID,
                            collection: collectionName,
                            value: template
                        )
                    } label: {
                        Label("Add \(target["label"]?.stringValue ?? name)", systemImage: "plus")
                    }
                    .buttonStyle(.borderless)
                    .controlSize(.mini)
                }
            }
        } else if itemTargets.isEmpty, !itemFields.isEmpty, values.count < maxItems {
            if supportsOverlayInsertion {
                let (template, valid) = InspectorSchemaSupport.defaultObject(for: itemFields, scope: schemaPath)
                if valid {
                    Button {
                        store.insertInspectorOverlay(
                            documentID: documentID,
                            parentID: nodeID == "document" ? "document" : nodeID,
                            collection: collectionName,
                            value: template
                        )
                    } label: {
                        Label("Add \(name) item", systemImage: "plus")
                    }
                    .buttonStyle(.borderless)
                    .controlSize(.mini)
                }
            } else {
                Button {
                    setCollection(values + [InspectorSchemaSupport.defaultCollectionItem(for: descriptor)])
                } label: {
                    Label("Add \(name) item", systemImage: "plus")
                }
                .buttonStyle(.borderless)
                .controlSize(.mini)
            }
        }
    }

    private func targetKey(for item: JSONValue) -> String? {
        if itemTargets.count < 2 { return itemTargets.first }
        if item["words"] != nil { return itemTargets.first(where: { $0 == "punch.words" }) }
        return itemTargets.first(where: { $0 != "punch.words" }) ?? itemTargets.first
    }

    private func childNode(for item: JSONValue, targetKey: String?) -> EditorNode? {
        guard let snapshot = store.inspector.snapshot else { return nil }
        let expectedParent: String? = nodeID == "document" ? nil : nodeID
        let expectedKind: Set<String> = {
            guard let targetKey else { return [] }
            if targetKey.hasPrefix("segment.") { return ["segment"] }
            if targetKey.hasPrefix("cutaway.") { return ["cutaway"] }
            switch targetKey {
            case "textBeat.segment", "textBeat.reel": return ["text"]
            case "stat": return ["stats", "stat"]
            case "exhibit.segment", "exhibit.reel": return ["exhibits", "exhibit"]
            case "punch.manual", "punch.words": return ["punch"]
            case "blur.patch": return ["blur"]
            case "bleep.window": return ["bleep"]
            case "musicDrop": return ["music_drop"]
            case "sfxCue.segment", "sfxCue.reel": return ["sfx", "generated_sfx"]
            case "route": return ["path", "route"]
            default: return []
            }
        }()
        return snapshot.nodes.first {
            $0.raw == item && $0.parentId == expectedParent
                && (expectedKind.isEmpty || expectedKind.contains($0.kind.lowercased()))
        }
    }

    private func itemTitle(_ item: JSONValue, index: Int) -> String {
        item["text"]?.stringValue ?? item["label"]?.stringValue
            ?? item["asset"]?.stringValue ?? item["moment"]?.stringValue
            ?? "\(name) \(index + 1)"
    }

    private func setCollection(_ values: [JSONValue]) {
        let pointer = "/" + path.map {
            $0.replacingOccurrences(of: "~", with: "~0").replacingOccurrences(of: "/", with: "~1")
        }.joined(separator: "/")
        store.editField(documentID: documentID, nodeID: nodeID, path: pointer, value: .array(values))
    }

}

private struct JSONFieldEditor: View {
    let name: String
    let value: JSONValue
    let path: [String]
    let documentID: String
    let nodeID: String
    @Bindable var store: EditorStore
    var descriptor: JSONValue? = nil
    @State private var expanded = true

    var body: some View {
        if descriptor?["type"]?.stringValue == "keyframes" || descriptor?["type"]?.stringValue == "keyframe" {
            KeyframeField(name: name, value: value, path: path, documentID: documentID, nodeID: nodeID, store: store)
        } else {
        switch value {
        case .object(let object):
            DisclosureGroup(isExpanded: $expanded) {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(object.keys.sorted(), id: \.self) { key in
                        if let child = object[key] {
                            JSONFieldEditor(name: key, value: child, path: path + [key], documentID: documentID, nodeID: nodeID, store: store)
                        }
                    }
                }
                .padding(.leading, 7)
                .padding(.top, 4)
            } label: {
                HStack {
                    Text(humanize(name)).font(EditorType.bodyEmphasis)
                    Spacer()
                    Text("\(object.count) fields").font(EditorType.caption).foregroundStyle(.tertiary)
                }
            }
            .tint(.secondary)
        case .array(let values):
            DisclosureGroup(isExpanded: $expanded) {
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(Array(values.enumerated()), id: \.offset) { index, child in
                        HStack(alignment: .top, spacing: 4) {
                            JSONFieldEditor(name: "\(index + 1)", value: child, path: path + [String(index)], documentID: documentID, nodeID: nodeID, store: store)
                            Button {
                                var next = values
                                next.remove(at: index)
                                commit(.array(next))
                            } label: { Image(systemName: "minus.circle").foregroundStyle(.secondary) }
                            .buttonStyle(.plain)
                            .help("Remove \(humanize(name)) item \(index + 1)")
                        }
                    }
                    HStack {
                        Button {
                            let template = values.last ?? .object([:])
                            commit(.array(values + [template]))
                        } label: {
                            Label("Add item", systemImage: "plus")
                        }
                        .buttonStyle(.borderless)
                        .controlSize(.mini)
                        Spacer()
                    }
                }
                .padding(.leading, 7)
                .padding(.top, 4)
            } label: {
                HStack {
                    Text(humanize(name)).font(EditorType.bodyEmphasis)
                    Spacer()
                    Text("\(values.count)").font(EditorType.timecode).foregroundStyle(.tertiary)
                }
            }
            .tint(.secondary)
        case .bool(let current):
            ToggleRow(label: humanize(name), isOn: Binding(
                get: { current },
                set: { commit(.bool($0)) }
            ))
        case .number(let current):
            NumberField(name: name, value: current) { commit(.number($0)) }
        case .string(let current):
            if descriptor?["type"]?.stringValue == "color" || descriptor?["type"]?.stringValue == "colour" {
                ColorStringField(name: name, value: current) { commit(.string($0)) }
            } else {
                StringField(name: name, value: current, options: descriptorOptions) { commit(.string($0)) }
            }
        case .null:
            HStack {
                Text(humanize(name)).font(EditorType.caption)
                Spacer()
                Text("Not set").font(EditorType.caption).foregroundStyle(.tertiary)
                Button {
                    commit(.string(""))
                } label: { Image(systemName: "plus.circle").foregroundStyle(.secondary) }
                    .buttonStyle(.plain)
                    .help("Set \(humanize(name))")
            }
        }
        }
    }

    private var descriptorOptions: [String]? {
        (descriptor?["options"]?.arrayValue ?? descriptor?["enum"]?.arrayValue)?
            .compactMap(\.stringValue)
    }

    private func commit(_ value: JSONValue) {
        let pointer = "/" + path.map { $0.replacingOccurrences(of: "~", with: "~0").replacingOccurrences(of: "/", with: "~1") }.joined(separator: "/")
        store.editField(documentID: documentID, nodeID: nodeID, path: pointer, value: value)
    }
}

private struct NumberField: View {
    let name: String
    let value: Double
    var minimum: Double? = nil
    var maximum: Double? = nil
    var stepValue: Double? = nil
    var unit: String? = nil
    let commit: (Double) -> Void
    @State private var draft = ""
    @State private var scrubStart: Double?
    @Environment(\.inspectorTint) private var tint

    var body: some View {
        HStack(spacing: 5) {
            Text(humanize(name))
                .font(EditorType.body)
                .lineLimit(1)
                .contentShape(Rectangle())
                .onHover { inside in if inside { NSCursor.resizeLeftRight.push() } else { NSCursor.pop() } }
                .gesture(scrub)
                .help("Drag left or right to scrub the value")
            Spacer(minLength: 3)
            TextField("", text: $draft)
                .font(EditorType.timecode)
                .foregroundStyle(tint)
                .multilineTextAlignment(.trailing)
                .textFieldStyle(.roundedBorder)
                .frame(width: 74)
                .onSubmit(save)
                .onChange(of: value) { _, newValue in if scrubStart == nil { draft = Self.format(newValue) } }
                .accessibilityLabel(humanize(name))
            if let unit, !unit.isEmpty {
                Text(unit).font(EditorType.caption).foregroundStyle(.tertiary).lineLimit(1)
            }
            Stepper("", onIncrement: { step(1) }, onDecrement: { step(-1) })
                .labelsHidden()
                .controlSize(.mini)
                .frame(width: 34)
        }
        .onAppear { draft = Self.format(value) }
    }

    private var increment: Double {
        let lower = name.lowercased()
        return stepValue ?? (lower.contains("volume") ? 0.05
            : (lower.contains("at") || name == "in" || name == "out" ? 0.1 : 1))
    }

    private var scrub: some Gesture {
        DragGesture(minimumDistance: 3)
            .onChanged { drag in
                let start = scrubStart ?? (Double(draft) ?? value)
                scrubStart = start
                draft = Self.format(clamp(start + (drag.translation.width / 6).rounded() * increment))
            }
            .onEnded { _ in
                scrubStart = nil
                save()
            }
    }

    private func step(_ direction: Double) {
        let next = clamp((Double(draft) ?? value) + direction * increment)
        draft = Self.format(next)
        commit(next)
    }
    private func save() {
        if let number = Double(draft) {
            let next = clamp(number)
            draft = Self.format(next)
            if abs(next - value) > 1e-9 { commit(next) }
        }
    }
    private func clamp(_ number: Double) -> Double {
        min(maximum ?? .greatestFiniteMagnitude, max(minimum ?? -.greatestFiniteMagnitude, number))
    }
    private static func format(_ number: Double) -> String { number.formatted(.number.precision(.fractionLength(0...3))) }
}

/// A number with a known range shown as a slider (colour grade strength, warmth, exposure).
/// Commits once when the drag ends, so one drag is one undoable edit and one preview refresh.
private struct SliderField: View {
    let name: String
    let value: Double
    let minimum: Double
    let maximum: Double
    var stepValue: Double? = nil
    var defaultValue: Double? = nil
    var unit: String? = nil
    let commit: (Double) -> Void
    @State private var draft = 0.0
    @State private var dragging = false
    @Environment(\.inspectorTint) private var tint

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 5) {
                Text(humanize(name)).font(EditorType.body).lineLimit(1)
                Spacer(minLength: 3)
                Text(Self.format(draft) + (unit.map { " \($0)" } ?? ""))
                    .font(EditorType.timecode)
                    .foregroundStyle(tint)
                if let defaultValue, abs(draft - defaultValue) > 1e-9 {
                    Button {
                        draft = defaultValue
                        commit(defaultValue)
                    } label: {
                        Image(systemName: "arrow.uturn.backward")
                    }
                    .buttonStyle(.borderless)
                    .controlSize(.mini)
                    .help("Reset to \(Self.format(defaultValue))")
                    .accessibilityLabel("Reset \(humanize(name))")
                }
            }
            slider
                .tint(tint)
                .controlSize(.mini)
                .accessibilityLabel(humanize(name))
                .accessibilityValue(Self.format(draft))
        }
        .onAppear { draft = value }
        .onChange(of: value) { _, newValue in if !dragging { draft = newValue } }
    }

    @ViewBuilder private var slider: some View {
        if let stepValue, stepValue > 0 {
            Slider(value: $draft, in: minimum...maximum, step: stepValue, onEditingChanged: finished)
        } else {
            Slider(value: $draft, in: minimum...maximum, onEditingChanged: finished)
        }
    }

    private func finished(_ editing: Bool) {
        dragging = editing
        if !editing, abs(draft - value) > 1e-9 { commit(draft) }
    }

    private static func format(_ number: Double) -> String { number.formatted(.number.precision(.fractionLength(0...2))) }
}

private struct StringField: View {
    let name: String
    let value: String
    var options: [String]? = nil
    let commit: (String) -> Void
    @State private var draft = ""

    var body: some View {
        Group {
            if value.count > 70 || value.contains("\n") {
                VStack(alignment: .leading, spacing: 3) {
                    Text(humanize(name)).font(EditorType.body)
                    VStack(alignment: .trailing, spacing: 4) {
                        TextEditor(text: $draft)
                            .font(EditorType.body)
                            .frame(minHeight: 44, maxHeight: 96)
                            .scrollContentBackground(.hidden)
                            .padding(3)
                            .background(EditorPalette.canvas, in: RoundedRectangle(cornerRadius: 4))
                            .onChange(of: value) { _, text in draft = text }
                            .accessibilityLabel(humanize(name))
                        if draft != value {
                            Button("Apply") { commit(draft) }
                                .controlSize(.mini)
                        }
                    }
                }
            } else if let options = (options ?? optionsForName(name)), options.contains(value) {
                FieldRow(label: humanize(name)) {
                    Picker(humanize(name), selection: Binding(
                        get: { value },
                        set: { commit($0) }
                    )) {
                        ForEach(options, id: \.self) { option in
                            Text(humanize(option)).tag(option)
                        }
                    }
                    .labelsHidden()
                    .controlSize(.small)
                    .frame(maxWidth: FieldRowMetrics.controlMaxWidth)
                }
            } else {
                FieldRow(label: humanize(name)) {
                    TextField(humanize(name), text: $draft)
                        .font(EditorType.body)
                        .multilineTextAlignment(.trailing)
                        .textFieldStyle(.roundedBorder)
                        .frame(minWidth: 60, maxWidth: FieldRowMetrics.controlMaxWidth)
                        .onSubmit { commit(draft) }
                        .onChange(of: value) { _, text in draft = text }
                        .accessibilityLabel(humanize(name))
                }
            }
        }
        .onAppear { draft = value }
    }

    private func optionsForName(_ key: String) -> [String]? {
        switch key.lowercased() {
        case "position": ["top", "center", "bottom", "left", "right"]
        case "style": ["dark", "light", "outlined"]
        case "transition": ["cut", "fade"]
        case "framemode": ["fit", "fill", "cover"]
        case "captions": ["transcript", "off"]
        case "projection": ["equirectangular", "cubemap"]
        default: nil
        }
    }
}

private struct ColorStringField: View {
    let name: String
    let value: String
    let commit: (String) -> Void

    var body: some View {
        HStack {
            Text(humanize(name)).font(EditorType.body)
            Spacer()
            ColorPicker(humanize(name), selection: Binding(
                get: { Color(nsColor: Self.color(value)) },
                set: { commit(Self.hex($0)) }
            ))
            .labelsHidden()
            .frame(width: 44)
        }
        .accessibilityLabel(humanize(name))
    }

    private static func color(_ value: String) -> NSColor {
        let hex = value.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        guard hex.count == 6, let number = UInt32(hex, radix: 16) else { return .white }
        return NSColor(srgbRed: CGFloat((number >> 16) & 0xff) / 255,
                       green: CGFloat((number >> 8) & 0xff) / 255,
                       blue: CGFloat(number & 0xff) / 255,
                       alpha: 1)
    }

    private static func hex(_ color: Color) -> String {
        let components = NSColor(color).usingColorSpace(.deviceRGB) ?? .white
        return String(format: "#%02X%02X%02X", Int(components.redComponent * 255),
                      Int(components.greenComponent * 255), Int(components.blueComponent * 255))
    }
}

private struct KeyframeField: View {
    let name: String
    let value: JSONValue
    let path: [String]
    let documentID: String
    let nodeID: String
    @Bindable var store: EditorStore

    private var frames: [JSONValue] { value.arrayValue ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Label(humanize(name), systemImage: "point.3.connected.trianglepath.dotted")
                    .font(EditorType.bodyEmphasis)
                Spacer()
                Button {
                    let time = Double(store.playheadFrame) / store.fps
                    write(frames + [.object(["at": .number(time), "value": .number(0)])])
                } label: { Image(systemName: "plus.circle") }
                    .buttonStyle(.plain)
                    .help("Add keyframe at playhead")
            }
            ForEach(Array(frames.enumerated()), id: \.offset) { index, frame in
                HStack(spacing: 4) {
                    NumberField(name: "At", value: frame["at"]?.numberValue ?? 0) {
                        update(index: index, key: "at", number: $0)
                    }
                    NumberField(name: "Value", value: frame["value"]?.numberValue ?? 0) {
                        update(index: index, key: "value", number: $0)
                    }
                    Button {
                        var next = frames
                        next.remove(at: index)
                        write(next)
                    } label: { Image(systemName: "minus.circle").foregroundStyle(.secondary) }
                        .buttonStyle(.plain)
                        .help("Remove keyframe")
                }
            }
            Text("Keyframe values are edited from the full format fields below.")
                .font(EditorType.caption)
                .foregroundStyle(.tertiary)
        }
        .padding(7)
        .background(EditorPalette.raised, in: RoundedRectangle(cornerRadius: 5))
    }

    private func write(_ value: [JSONValue]) {
        let pointer = "/" + path.map { $0.replacingOccurrences(of: "~", with: "~0").replacingOccurrences(of: "/", with: "~1") }.joined(separator: "/")
        store.setKeyframes(documentID: documentID, nodeID: nodeID, path: pointer, value: .array(value))
    }

    private func update(index: Int, key: String, number: Double) {
        guard frames.indices.contains(index) else { return }
        var next = frames
        var frame = next[index].objectValue ?? [:]
        frame[key] = .number(number)
        next[index] = .object(frame)
        write(next)
    }
}

private struct TranscriptWordEditor: View {
    let node: EditorNode
    let words: [JSONValue]
    let documentID: String
    @Bindable var store: EditorStore
    @State private var drafts: [String] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Label("Transcript words", systemImage: "text.quote")
                .font(EditorType.section)
            ForEach(Array(words.enumerated()), id: \.offset) { index, word in
                HStack(spacing: 5) {
                    Text("\(word["s"]?.numberValue ?? 0, format: .number.precision(.fractionLength(1)))")
                        .font(EditorType.timecode)
                        .foregroundStyle(.tertiary)
                        .frame(width: 30, alignment: .trailing)
                    TextField("Word", text: Binding(
                        get: { drafts.indices.contains(index) ? drafts[index] : word["w"]?.stringValue ?? "" },
                        set: { if drafts.indices.contains(index) { drafts[index] = $0 } }
                    ))
                    .textFieldStyle(.roundedBorder)
                    .font(EditorType.caption)
                }
            }
            Button("Correct words") { applyCorrections() }
                .controlSize(.small)
                .disabled(drafts.isEmpty || !words.enumerated().contains { index, item in drafts.indices.contains(index) && drafts[index] != (item["w"]?.stringValue ?? "") })
        }
        .padding(8)
        .background(EditorPalette.raised, in: RoundedRectangle(cornerRadius: 5))
        .onAppear {
            drafts = words.map { $0["w"]?.stringValue ?? "" }
        }
        .onChange(of: words) { _, newWords in drafts = newWords.map { $0["w"]?.stringValue ?? "" } }
    }

    private func applyCorrections() {
        let corrections = words.enumerated().compactMap { index, word -> JSONValue? in
            guard drafts.indices.contains(index), drafts[index] != (word["w"]?.stringValue ?? "") else { return nil }
            return .object(["index": .number(Double(index)), "word": .string(drafts[index])])
        }
        store.correctInspectorWords(documentID: documentID, nodeID: node.id, corrections: corrections)
    }
}

private func humanize(_ key: String) -> String {
    let spaced = key
        .replacingOccurrences(of: "([a-z0-9])([A-Z])", with: "$1 $2", options: .regularExpression)
        .replacingOccurrences(of: "_", with: " ")
        .replacingOccurrences(of: "-", with: " ")
    return spaced.prefix(1).uppercased() + spaced.dropFirst()
}

private func laneColor(_ lane: String) -> Color {
    let value = lane.lowercased()
    if value.contains("audio") || value.contains("sound") || value.contains("music") { return EditorPalette.audio }
    if value.contains("overlay") || value.contains("text") || value.contains("caption") { return EditorPalette.overlay }
    if value.contains("cutaway") || value.contains("broll") { return EditorPalette.cutaway }
    return EditorPalette.video
}
