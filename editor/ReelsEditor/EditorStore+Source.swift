import Foundation

/// Where each asset's `source.prepare` job stands (the 360 front/back views and compatibility
/// proxies) and what happens when it ends. The service answers every job with source.ready,
/// source.unavailable or source.error; this turns that into showing it, retrying a couple of
/// times or giving up, so the Source monitor never waits forever.
struct SourcePreparation {
    enum Outcome: Equatable {
        case ready
        /// The service's answer is final: offline, unsupported, or the request was refused.
        case unavailable
        case failed
        case timedOut
        case cancelled
        /// No word about the job for longer than the service's own deadline plus `grace`.
        case lost
    }

    enum Next: Equatable {
        case show
        case retry(after: TimeInterval)
        /// Back to never asked: the next time the asset is needed it is requested again.
        case reset
        case giveUp
    }

    static let maxAttempts = 3
    /// The service's source job deadline (service.py SOURCE_JOB_TIMEOUT) when a reply doesn't carry one.
    static let defaultServerTimeout: TimeInterval = 1800
    /// Extra wait past that deadline, so the app never gives up on a job the service can still finish.
    static let grace: TimeInterval = 120

    /// What the reply to a request means once it names the job (`accepted`).
    enum Acceptance: Equatable {
        /// The request is no longer awaited: its job already ended or a newer request replaced it.
        case stale
        /// Wait for the job's event.
        case waiting
        /// The job's own event came before the reply and was held until now.
        case ended(Outcome, message: String?)
    }

    private struct HeldEvent {
        let job: String
        let outcome: Outcome
        let message: String?
    }

    private(set) var attempts: [String: Int] = [:]
    /// The request each asset is waiting on: its attempt number, once the reply is in the job ID,
    /// and until then the failures that arrived with no way to tell whose job they are about.
    private var awaiting: [String: (attempt: Int, job: String?, held: [HeldEvent])] = [:]

    static func outcome(event: String, state: String?) -> Outcome? {
        switch event {
        case "source.ready": return .ready
        case "source.unavailable": return .unavailable
        case "source.error":
            switch state {
            case "cancelled": return .cancelled
            case "timed_out": return .timedOut
            default: return .failed
            }
        default: return nil
        }
    }

    /// How long to wait for a job's event, counted from the reply or its latest job.changed.
    static func patience(serverTimeout: Double?) -> TimeInterval {
        let server = serverTimeout.flatMap { $0 > 0 ? $0 : nil } ?? defaultServerTimeout
        return server + grace
    }

    /// A new request for the asset; returns its attempt number.
    mutating func requested(_ assetID: String) -> Int {
        let attempt = attempts[assetID, default: 0] + 1
        attempts[assetID] = attempt
        awaiting[assetID] = (attempt, nil, [])
        return attempt
    }

    /// The service took attempt `attempt` on as `jobID`. A failure held for that job ends the
    /// request now; failures held for other (older) jobs are dropped.
    mutating func accepted(_ assetID: String, attempt: Int, jobID: String) -> Acceptance {
        guard let waiting = awaiting[assetID], waiting.attempt == attempt else { return .stale }
        awaiting[assetID] = (attempt, jobID, [])
        if let event = waiting.held.first(where: { $0.job == jobID }) {
            return .ended(event.outcome, message: event.message)
        }
        return .waiting
    }

    func isWaiting(_ assetID: String) -> Bool { awaiting[assetID] != nil }
    func job(for assetID: String) -> String? { awaiting[assetID]?.job }
    func asset(forJob jobID: String) -> String? { awaiting.first { $0.value.job == jobID }?.key }

    /// Whether to act on an event now. A ready source is always welcome. A failure counts only for
    /// the job the asset is waiting on; one that arrives before the reply names that job is held
    /// for `accepted`, since it may be a late failure of an earlier job.
    mutating func admit(_ assetID: String, jobID: String?, _ outcome: Outcome, message: String?) -> Bool {
        if outcome == .ready { return true }
        guard let waiting = awaiting[assetID] else { return false }
        guard let jobID else { return true }  // nothing to match it against
        guard let current = waiting.job else {
            awaiting[assetID]?.held.append(HeldEvent(job: jobID, outcome: outcome, message: message))
            return false
        }
        return current == jobID
    }

    mutating func finished(_ assetID: String, _ outcome: Outcome) -> Next {
        awaiting[assetID] = nil
        switch outcome {
        case .ready:
            attempts[assetID] = nil
            return .show
        case .cancelled:
            attempts[assetID] = nil
            return .reset
        case .unavailable, .timedOut:
            // A job that used its whole deadline would only use it again.
            attempts[assetID] = nil
            return .giveUp
        case .failed, .lost:
            let made = attempts[assetID, default: 0]
            guard made < Self.maxAttempts else {
                attempts[assetID] = nil
                return .giveUp
            }
            return .retry(after: 2 * Double(made * made))
        }
    }
}

extension EditorStore {
    /// Asks for the asset's playable source unless it is showing or already on its way.
    func ensureSource(_ assetID: String) async {
        if timelineSourceURLs[assetID] != nil {
            sourceStates[assetID] = "ready"
            return
        }
        guard sourceStates[assetID] != "pending" else { return }
        await prepareSource(assetID)
    }

    /// One `source.prepare` request. The service runs one job per asset (a repeat joins it) and
    /// answers with a source.* event, handled by `receiveSource`.
    func prepareSource(_ assetID: String) async {
        let attempt = sourcePreparation.requested(assetID)
        sourceStates[assetID] = "pending"
        do {
            let job = try await service.call("source.prepare", params: .object(["id": .string(assetID)]))
            guard let jobID = job["id"]?.stringValue else { return }
            switch sourcePreparation.accepted(assetID, attempt: attempt, jobID: jobID) {
            case .stale:
                return
            case .waiting:
                armSourceWatchdog(assetID, jobID: jobID,
                                  after: SourcePreparation.patience(serverTimeout: job["timeoutSeconds"]?.numberValue))
            case .ended(let outcome, let message):
                finishSource(assetID, outcome, message: message)
            }
        } catch {
            finishSource(assetID, .unavailable, message: error.localizedDescription)
        }
    }

    /// source.ready, source.unavailable or source.error.
    func receiveSource(name: String, payload: JSONValue, url: URL?) {
        let message = payload["message"]?.stringValue ?? payload["reason"]?.stringValue
        guard let assetID = payload["assetId"]?.stringValue ?? payload["id"]?.stringValue,
              var outcome = SourcePreparation.outcome(event: name, state: payload["state"]?.stringValue),
              sourcePreparation.admit(assetID, jobID: payload["jobId"]?.stringValue, outcome, message: message)
        else { return }
        if outcome == .ready {
            if let layout = payload["layout"]?.stringValue { sourceLayouts[assetID] = layout }
            if let url { timelineSourceURLs[assetID] = url } else { outcome = .unavailable }
            if assetID == selectedAssetID, let asset = payload["asset"] { selectedAsset = asset }
        }
        finishSource(assetID, outcome, message: message)
    }

    /// job.changed for a source job: the service is still on it, so the wait starts over.
    func sourceJobChanged(_ payload: JSONValue) {
        guard let jobID = payload["id"]?.stringValue,
              let assetID = sourcePreparation.asset(forJob: jobID),
              ["queued", "running"].contains(payload["state"]?.stringValue ?? "") else { return }
        armSourceWatchdog(assetID, jobID: jobID,
                          after: SourcePreparation.patience(serverTimeout: payload["timeoutSeconds"]?.numberValue))
    }

    private func finishSource(_ assetID: String, _ outcome: SourcePreparation.Outcome, message: String?) {
        sourceWatchdogs.removeValue(forKey: assetID)?.cancel()
        switch sourcePreparation.finished(assetID, outcome) {
        case .show:
            sourceStates[assetID] = "ready"
        case .reset:
            sourceStates[assetID] = nil
        case .giveUp:
            sourceStates[assetID] = "error"
            notice = message ?? "Source proxy is unavailable"
        case .retry(let delay):
            sourceStates[assetID] = "pending"
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(delay))
                guard let self, self.sourceStates[assetID] == "pending",
                      !self.sourcePreparation.isWaiting(assetID) else { return }
                await self.prepareSource(assetID)
            }
        }
    }

    private func armSourceWatchdog(_ assetID: String, jobID: String, after seconds: TimeInterval) {
        sourceWatchdogs[assetID]?.cancel()
        sourceWatchdogs[assetID] = Task { [weak self] in
            try? await Task.sleep(for: .seconds(seconds))
            guard !Task.isCancelled, let self, self.sourcePreparation.job(for: assetID) == jobID else { return }
            self.finishSource(assetID, .lost, message: "The editing service stopped answering about this source.")
        }
    }
}
