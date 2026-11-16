import Foundation
import Observation

/// One scheduling session: what the sheet shows and how far the background job has got.
/// Lives outside EditorStore so the store's stored properties stay untouched.
@MainActor
@Observable
final class ScheduleModel {
    static let shared = ScheduleModel()

    enum StepState: Equatable { case pending, running, done, skipped, failed }
    enum Phase: Equatable { case editing, running, finished, failed }

    struct Step: Identifiable, Equatable {
        let id: String
        var title: String
        var detail: String
        var state: StepState = .pending
    }

    var isPresented = false
    var isLoading = false
    var phase: Phase = .editing
    var slug = ""
    var caption = ""
    var hashtags: [String] = []
    var networks: [String] = []
    var coverPath: String?
    var timeZoneID = "America/Chicago"
    var when = Date()
    var existingID: String?
    var needsRender = true
    var errorText: String?
    var resultID: String?
    var steps: [Step] = []
    var jobID: String?

    var timeZone: TimeZone { TimeZone(identifier: timeZoneID) ?? .current }
    var minimumDate: Date { Date().addingTimeInterval(15 * 60) }
    var isUpdate: Bool { existingID != nil }

    static let networkTitles = ["instagram": "Instagram Reel", "tiktok": "TikTok", "youtube": "YouTube Short"]

    func formatted(_ date: Date) -> String {
        let f = DateFormatter()
        f.timeZone = timeZone
        f.dateFormat = "EEE MMM d, h:mm a"
        return f.string(from: date) + " " + (timeZone.abbreviation(for: date) ?? timeZoneID)
    }

    var confirmation: String {
        let names = networks.compactMap { Self.networkTitles[$0] }
        let list = names.count > 1 ? names.dropLast().joined(separator: ", ") + " and " + names.last! : names.first ?? "your accounts"
        return "This will post publicly on \(formatted(when)) to \(list)."
    }

    func configure(from value: JSONValue) {
        slug = value["slug"]?.stringValue ?? ""
        caption = value["caption"]?.stringValue ?? ""
        hashtags = value["hashtags"]?.arrayValue?.compactMap(\.stringValue) ?? []
        networks = value["networks"]?.arrayValue?.compactMap(\.stringValue) ?? ["instagram", "tiktok", "youtube"]
        coverPath = value["coverPath"]?.stringValue
        timeZoneID = value["timezone"]?.stringValue ?? "America/Chicago"
        existingID = value["metricoolId"]?.stringValue
        needsRender = value["needsRender"]?.boolValue ?? true
        if let text = value["defaultSlot"]?.stringValue {
            let f = DateFormatter()
            f.timeZone = timeZone
            f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
            when = f.date(from: text) ?? minimumDate
        }
        phase = .editing
        errorText = nil
        resultID = nil
        jobID = nil
        steps = [
            Step(id: "render", title: "Render", detail: needsRender ? "The reel changed, so it is rendered again" : "Already up to date"),
            Step(id: "upload", title: "Upload to R2", detail: "Public link for Metricool, this reel only"),
            Step(id: "send", title: "Send to Metricool", detail: isUpdate ? "Updates the post that is already scheduled" : "Creates one post for all three networks"),
            Step(id: "mark", title: "Marked scheduled", detail: "Recorded in out/\(slug)/post.json")
        ]
    }

    private func setStep(_ id: String, _ state: StepState) {
        guard let i = steps.firstIndex(where: { $0.id == id }) else { return }
        steps[i].state = state
    }

    /// `job.changed` events with kind "schedule". The service logs `STEP <name> <running|done>` as it goes.
    func handleJob(_ payload: JSONValue) {
        if let id = payload["id"]?.stringValue, let jobID, id != jobID { return }
        if let log = payload["log"]?.stringValue, log.hasPrefix("STEP ") {
            let parts = log.dropFirst(5).split(separator: " ", maxSplits: 1).map(String.init)
            if parts.count == 2, parts[0] != "failed" {
                if parts[0] == "render", parts[1] == "done", !needsRender { setStep("render", .skipped) }
                else { setStep(parts[0], parts[1] == "done" ? .done : .running) }
            }
        } else if let log = payload["log"]?.stringValue, let i = steps.firstIndex(where: { $0.state == .running }) {
            steps[i].detail = log
        }
        switch payload["state"]?.stringValue {
        case "completed":
            phase = .finished
            resultID = payload["result"]?["metricoolId"]?.stringValue
            for i in steps.indices where steps[i].state != .skipped { steps[i].state = .done }
        case "failed", "interrupted", "cancelled":
            phase = .failed
            errorText = payload["error"]?["message"]?.stringValue ?? "Scheduling stopped before it finished."
            if let i = steps.firstIndex(where: { $0.state == .running }) { steps[i].state = .failed }
        default: break
        }
    }
}

extension EditorStore {
    var schedule: ScheduleModel { ScheduleModel.shared }

    /// Toolbar "Schedule…": ask the service for the caption, cover and next open slot, then show the sheet.
    func beginSchedule() {
        guard hasDocument else { return }
        let model = schedule
        model.isLoading = true
        model.isPresented = true
        Task {
            do {
                let prep = try await service.call("schedule.prepare")
                model.configure(from: prep)
            } catch {
                model.isPresented = false
                errorMessage = error.localizedDescription
            }
            model.isLoading = false
        }
    }

    /// The Schedule button. Saves the caption into the plan first so the post, the render and the plan agree.
    func submitSchedule() {
        let model = schedule
        guard model.phase == .editing || model.phase == .failed else { return }
        model.phase = .running
        model.errorText = nil
        for i in model.steps.indices { model.steps[i].state = .pending }
        Task {
            do {
                let caption = model.caption.trimmingCharacters(in: .whitespacesAndNewlines)
                let saved = snapshot?.plan["post"]?["caption"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                if caption != saved {
                    await apply([.object(["op": .string("set_field"), "id": .string("document"),
                                          "path": .string("/post/caption"), "value": .string(caption)])],
                                reason: "Edit caption")
                }
                let f = DateFormatter()
                f.timeZone = model.timeZone
                f.locale = Locale(identifier: "en_US_POSIX")
                f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
                let job = try await service.call("schedule.submit", params: .object([
                    "when": .string(f.string(from: model.when)),
                    "caption": .string(caption)
                ]))
                model.jobID = job["id"]?.stringValue
                if job["state"]?.stringValue == "completed" || job["state"]?.stringValue == "failed" { model.handleJob(job) }
            } catch {
                model.phase = .failed
                model.errorText = error.localizedDescription
            }
        }
    }

    func finishSchedule() {
        schedule.isPresented = false
        Task { await refreshPlans() }
    }
}
