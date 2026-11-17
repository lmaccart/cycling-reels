import XCTest
@testable import ReelsEditor

final class SourcePreparationTests: XCTestCase {
    func testEventsMapToOutcomes() {
        XCTAssertEqual(SourcePreparation.outcome(event: "source.ready", state: nil), .ready)
        XCTAssertEqual(SourcePreparation.outcome(event: "source.unavailable", state: nil), .unavailable)
        XCTAssertEqual(SourcePreparation.outcome(event: "source.error", state: "failed"), .failed)
        XCTAssertEqual(SourcePreparation.outcome(event: "source.error", state: "cancelled"), .cancelled)
        XCTAssertEqual(SourcePreparation.outcome(event: "source.error", state: "timed_out"), .timedOut)
        XCTAssertNil(SourcePreparation.outcome(event: "sphere.ready", state: nil))
    }

    func testFailuresRetryWithGrowingDelaysThenGiveUp() {
        var prep = SourcePreparation()
        var delays: [TimeInterval] = []
        for attempt in 1...SourcePreparation.maxAttempts {
            XCTAssertEqual(prep.requested("a"), attempt)
            XCTAssertEqual(prep.accepted("a", attempt: attempt, jobID: "job-\(attempt)"), .waiting)
            switch prep.finished("a", attempt == 2 ? .lost : .failed) {
            case .retry(let delay): delays.append(delay)
            case .giveUp: XCTAssertEqual(attempt, SourcePreparation.maxAttempts)
            default: XCTFail("a failed job either retries or gives up")
            }
        }
        XCTAssertEqual(delays.count, SourcePreparation.maxAttempts - 1)
        XCTAssertEqual(delays, delays.sorted())
        XCTAssertFalse(prep.isWaiting("a"))
        // Giving up forgets the count: asking again later starts over.
        XCTAssertEqual(prep.requested("a"), 1)
    }

    func testCancelResetsAndReadyClearsTheCount() {
        var prep = SourcePreparation()
        _ = prep.requested("a")
        XCTAssertEqual(prep.finished("a", .failed), .retry(after: 2))
        XCTAssertEqual(prep.requested("a"), 2)
        XCTAssertEqual(prep.finished("a", .cancelled), .reset)
        XCTAssertNil(prep.attempts["a"])
        XCTAssertFalse(prep.isWaiting("a"))

        _ = prep.requested("b")
        XCTAssertEqual(prep.finished("b", .failed), .retry(after: 2))
        _ = prep.requested("b")
        XCTAssertEqual(prep.finished("b", .ready), .show)
        XCTAssertNil(prep.attempts["b"])
    }

    func testTimeoutsAndUnavailableSourcesAreNotRetried() {
        var prep = SourcePreparation()
        _ = prep.requested("a")
        XCTAssertEqual(prep.finished("a", .timedOut), .giveUp)
        _ = prep.requested("b")
        XCTAssertEqual(prep.finished("b", .unavailable), .giveUp)
    }

    func testAnEventThatBeatsTheReplyEndsTheRequest() {
        var prep = SourcePreparation()
        let attempt = prep.requested("a")
        XCTAssertTrue(prep.admit("a", jobID: "job-1", .ready, message: nil))
        XCTAssertEqual(prep.finished("a", .ready), .show)
        // The late reply must not start a wait for a job that is already done.
        XCTAssertEqual(prep.accepted("a", attempt: attempt, jobID: "job-1"), .stale)
        XCTAssertNil(prep.job(for: "a"))
        XCTAssertNil(prep.asset(forJob: "job-1"))
    }

    func testAStaleFailureBeforeTheReplyDoesNotEndTheNewRequest() {
        var prep = SourcePreparation()
        _ = prep.requested("a")
        XCTAssertEqual(prep.accepted("a", attempt: 1, jobID: "job-1"), .waiting)
        XCTAssertEqual(prep.finished("a", .failed), .retry(after: 2))
        let attempt = prep.requested("a")
        // job-1's failure is delivered again late, after the retry was sent but before its reply.
        XCTAssertFalse(prep.admit("a", jobID: "job-1", .failed, message: "old"))
        XCTAssertTrue(prep.isWaiting("a"))
        XCTAssertEqual(prep.accepted("a", attempt: attempt, jobID: "job-2"), .waiting)
        XCTAssertEqual(prep.job(for: "a"), "job-2")
        XCTAssertFalse(prep.admit("a", jobID: "job-1", .failed, message: "old"))
        XCTAssertTrue(prep.admit("a", jobID: "job-2", .failed, message: "new"))
    }

    func testTheCurrentJobsFailureBeforeTheReplyIsHeldForIt() {
        var prep = SourcePreparation()
        let attempt = prep.requested("a")
        XCTAssertFalse(prep.admit("a", jobID: "job-1", .failed, message: "sandbox hiccup"))
        XCTAssertEqual(prep.accepted("a", attempt: attempt, jobID: "job-1"),
                       .ended(.failed, message: "sandbox hiccup"))
        XCTAssertEqual(prep.finished("a", .failed), .retry(after: 2))
    }

    func testOnlyTheCurrentJobsFailureCounts() {
        var prep = SourcePreparation()
        let attempt = prep.requested("a")
        XCTAssertEqual(prep.accepted("a", attempt: attempt, jobID: "job-2"), .waiting)
        XCTAssertEqual(prep.asset(forJob: "job-2"), "a")
        XCTAssertFalse(prep.admit("a", jobID: "job-1", .failed, message: nil))
        XCTAssertTrue(prep.admit("a", jobID: "job-2", .cancelled, message: nil))
        XCTAssertTrue(prep.admit("a", jobID: "job-1", .ready, message: nil))
        // Nothing awaited: a stray failure changes nothing.
        XCTAssertFalse(prep.admit("other", jobID: "job-9", .failed, message: nil))
    }

    func testTheAppWaitsLongerThanTheServiceDeadline() {
        XCTAssertGreaterThan(SourcePreparation.patience(serverTimeout: 1800), 1800)
        XCTAssertGreaterThan(SourcePreparation.patience(serverTimeout: 3600), 3600)
        XCTAssertGreaterThan(SourcePreparation.patience(serverTimeout: nil), SourcePreparation.defaultServerTimeout)
        XCTAssertEqual(SourcePreparation.patience(serverTimeout: 0), SourcePreparation.patience(serverTimeout: nil))
    }

    @MainActor
    func testQueueWaitDoesNotSpendRetriesAndTheSourceStillBecomesReady() {
        let store = EditorStore()
        defer { store.sourceWatchdogs.values.forEach { $0.cancel() } }
        let attempt = store.sourcePreparation.requested("a")
        XCTAssertEqual(store.sourcePreparation.accepted("a", attempt: attempt, jobID: "job-1"), .waiting)
        store.sourceStates["a"] = "pending"
        let queued: JSONValue = .object([
            "id": .string("job-1"), "state": .string("queued"),
            "created": .number(1_000), "timeoutSeconds": .number(20)
        ])
        // Each status check is later than another entire execution budget plus grace.
        // Previously these waits exhausted all three attempts while joining the same queued job.
        for check in 1...SourcePreparation.maxAttempts + 1 {
            let now = 1_000 + Double(check) * SourcePreparation.patience(serverTimeout: 20)
            store.reconcileSourceJob("a", job: queued, now: now)
            XCTAssertEqual(store.sourceStates["a"], "pending")
            XCTAssertEqual(store.sourcePreparation.attempts["a"], 1)
            XCTAssertEqual(store.sourcePreparation.job(for: "a"), "job-1")
        }
        let running: JSONValue = .object([
            "id": .string("job-1"), "state": .string("running"),
            "created": .number(1_000), "started": .number(10_000), "timeoutSeconds": .number(20)
        ])
        store.reconcileSourceJob("a", job: running, now: 10_019)
        XCTAssertEqual(store.sourceStates["a"], "pending")
        XCTAssertEqual(store.sourcePreparation.attempts["a"], 1)
        // Recover the published result even if the source.ready event did not reach the store.
        store.reconcileSourceJob("a", job: .object([
            "id": .string("job-1"), "state": .string("completed"), "result": .object([
                "assetId": .string("a"), "jobId": .string("job-1"), "status": .string("ready"),
                "layout": .string("front-back"), "sourceURL": .string("http://127.0.0.1:9000/source.mp4")
            ])
        ]), now: 10_020)
        XCTAssertEqual(store.sourceStates["a"], "ready")
        XCTAssertEqual(store.timelineSourceURLs["a"], URL(string: "http://127.0.0.1:9000/source.mp4"))
        XCTAssertEqual(store.sourceLayouts["a"], "front-back")
        XCTAssertFalse(store.sourcePreparation.isWaiting("a"))
        XCTAssertNil(store.sourcePreparation.attempts["a"])
        XCTAssertNil(store.sourceWatchdogs["a"])
    }

    func testRunningDeadlineUsesStartedEvenAfterALongQueueOrProgressUpdate() {
        let running: JSONValue = .object([
            "state": .string("running"), "created": .number(1_000),
            "started": .number(10_000), "timeoutSeconds": .number(20)
        ])
        XCTAssertEqual(SourcePreparation.watchdogDelay(for: running, now: 10_000), 20 + SourcePreparation.grace)
        XCTAssertEqual(SourcePreparation.watchdogDelay(for: running, now: 10_019), 1 + SourcePreparation.grace)
        XCTAssertEqual(SourcePreparation.watchdogDelay(for: running, now: 10_020), SourcePreparation.grace)
        XCTAssertEqual(SourcePreparation.watchdogDelay(for: running, now: 10_020 + SourcePreparation.grace), 0)
        XCTAssertEqual(SourcePreparation.watchdogDelay(for: running, now: 20_000), 0)
    }

    @MainActor
    func testStatusCheckHonorsCancellationAndTimeoutWithoutRetrying() {
        for state in ["cancelled", "timed_out"] {
            let store = EditorStore()
            let attempt = store.sourcePreparation.requested("a")
            _ = store.sourcePreparation.accepted("a", attempt: attempt, jobID: "job-1")
            store.sourceStates["a"] = "pending"
            store.reconcileSourceJob("a", job: .object([
                "id": .string("job-1"), "state": .string(state)
            ]))
            XCTAssertEqual(store.sourceStates["a"], state == "cancelled" ? nil : "error")
            XCTAssertFalse(store.sourcePreparation.isWaiting("a"))
            XCTAssertNil(store.sourcePreparation.attempts["a"])
        }
    }

    @MainActor
    func testStatusFromAnOlderJobCannotEndTheCurrentWait() {
        let store = EditorStore()
        let attempt = store.sourcePreparation.requested("a")
        _ = store.sourcePreparation.accepted("a", attempt: attempt, jobID: "job-2")
        store.sourceStates["a"] = "pending"
        store.reconcileSourceJob("a", job: .object([
            "id": .string("job-1"), "state": .string("timed_out")
        ]))
        XCTAssertEqual(store.sourceStates["a"], "pending")
        XCTAssertEqual(store.sourcePreparation.job(for: "a"), "job-2")
        XCTAssertEqual(store.sourcePreparation.attempts["a"], 1)
    }
}
