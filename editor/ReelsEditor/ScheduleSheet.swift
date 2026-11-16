import AppKit
import SwiftUI

/// Schedule a reel straight to Metricool: caption, networks, time, then a live checklist.
struct ScheduleSheet: View {
    @Bindable var store: EditorStore

    private var model: ScheduleModel { store.schedule }
    private var locked: Bool { model.phase == .running || model.phase == .finished }

    var body: some View {
        @Bindable var model = store.schedule
        VStack(spacing: 0) {
            HStack {
                PanelTitle("Schedule")
                Spacer()
                if model.phase != .running {
                    Button { model.isPresented = false } label: { Image(systemName: "xmark") }
                        .buttonStyle(.plain).foregroundStyle(.secondary)
                        .accessibilityLabel("Close")
                        .keyboardShortcut(.cancelAction)
                }
            }
            .padding(.horizontal, 20).padding(.vertical, 12)
            Divider()
            if model.isLoading {
                ProgressView("Checking the next open slot…").padding(60)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        HStack(alignment: .top, spacing: 16) {
                            cover(model)
                            VStack(alignment: .leading, spacing: 8) {
                                stepTitle("1", "Caption")
                                TextEditor(text: $model.caption)
                                    .font(EditorType.body)
                                    .frame(height: 84)
                                    .scrollContentBackground(.hidden)
                                    .padding(6)
                                    .background(EditorPalette.raised, in: RoundedRectangle(cornerRadius: 6))
                                    .disabled(locked)
                                if !model.hashtags.isEmpty {
                                    Text(model.hashtags.joined(separator: " "))
                                        .font(EditorType.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                        VStack(alignment: .leading, spacing: 8) {
                            stepTitle("2", "Posts to")
                            HStack(spacing: 8) {
                                ForEach(model.networks, id: \.self) { network in
                                    Label(ScheduleModel.networkTitles[network] ?? network, systemImage: "checkmark.circle.fill")
                                        .font(EditorType.bodyEmphasis)
                                        .padding(.horizontal, 10).padding(.vertical, 5)
                                        .background(EditorPalette.accent.opacity(0.18), in: Capsule())
                                }
                            }
                        }
                        VStack(alignment: .leading, spacing: 8) {
                            stepTitle("3", "When")
                            HStack {
                                DatePicker("", selection: $model.when, in: model.minimumDate..., displayedComponents: [.date, .hourAndMinute])
                                    .labelsHidden()
                                    .environment(\.timeZone, model.timeZone)
                                    .disabled(locked)
                                Text(model.timeZoneID.replacingOccurrences(of: "_", with: " "))
                                    .font(EditorType.caption).foregroundStyle(.secondary)
                            }
                            Text("Starts at the next open daily slot. At least 15 minutes from now.")
                                .font(EditorType.caption).foregroundStyle(.tertiary)
                        }
                        VStack(alignment: .leading, spacing: 8) {
                            stepTitle("4", "Progress")
                            ForEach(model.steps) { step in stepRow(step) }
                        }
                        if let error = model.errorText {
                            Text(error)
                                .font(EditorType.caption).textSelection(.enabled)
                                .foregroundStyle(.red)
                                .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                                .background(Color.red.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
                        }
                    }
                    .padding(20)
                }
                Divider()
                footer(model)
            }
        }
        .frame(width: 560, height: 640)
        .background(EditorPalette.panel)
    }

    @ViewBuilder private func cover(_ model: ScheduleModel) -> some View {
        Group {
            if let path = model.coverPath, let image = NSImage(contentsOfFile: path) {
                Image(nsImage: image).resizable().scaledToFill()
            } else {
                ZStack {
                    EditorPalette.raised
                    Image(systemName: "film").font(.title).foregroundStyle(.tertiary)
                }
            }
        }
        .frame(width: 90, height: 160)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(alignment: .bottom) {
            Text(model.slug).font(EditorType.caption).lineLimit(1)
                .padding(3).frame(maxWidth: .infinity).background(.black.opacity(0.55))
                .clipShape(RoundedRectangle(cornerRadius: 8))
        }
    }

    private func stepTitle(_ number: String, _ title: String) -> some View {
        HStack(spacing: 6) {
            Text(number).font(EditorType.caption.weight(.bold)).foregroundStyle(.white)
                .frame(width: 16, height: 16).background(EditorPalette.accent, in: Circle())
            Text(title).font(EditorType.section)
        }
    }

    private func stepRow(_ step: ScheduleModel.Step) -> some View {
        HStack(spacing: 10) {
            Group {
                switch step.state {
                case .pending: Image(systemName: "circle").foregroundStyle(.tertiary)
                case .running: ProgressView().controlSize(.small)
                case .done: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                case .skipped: Image(systemName: "checkmark.circle").foregroundStyle(.secondary)
                case .failed: Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                }
            }
            .frame(width: 18)
            VStack(alignment: .leading, spacing: 1) {
                Text(step.title).font(EditorType.bodyEmphasis)
                Text(step.detail).font(EditorType.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer()
        }
    }

    @ViewBuilder private func footer(_ model: ScheduleModel) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if model.phase == .finished {
                Label("Scheduled for \(model.formatted(model.when)). Metricool post \(model.resultID ?? "").", systemImage: "checkmark.seal.fill")
                    .font(EditorType.bodyEmphasis).foregroundStyle(.green)
            } else {
                Text(model.confirmation).font(EditorType.caption).foregroundStyle(.secondary)
            }
            HStack {
                Spacer()
                if model.phase == .finished {
                    Button("Done") { store.finishSchedule() }
                        .keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent)
                } else {
                    Button("Cancel") { model.isPresented = false }.disabled(model.phase == .running)
                    Button(model.phase == .failed ? "Try again" : (model.isUpdate ? "Update scheduled post" : "Schedule")) {
                        store.submitSchedule()
                    }
                    .keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent)
                    .disabled(model.phase == .running || model.caption.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .padding(.horizontal, 20).padding(.vertical, 12)
    }
}
