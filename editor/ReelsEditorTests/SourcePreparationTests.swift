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
            XCTAssertTrue(prep.accepted("a", attempt: attempt, jobID: "job-\(attempt)"))
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
        XCTAssertTrue(prep.concerns("a", jobID: "job-1", .ready))
        XCTAssertEqual(prep.finished("a", .ready), .show)
        // The late reply must not start a wait for a job that is already done.
        XCTAssertFalse(prep.accepted("a", attempt: attempt, jobID: "job-1"))
        XCTAssertNil(prep.job(for: "a"))
        XCTAssertNil(prep.asset(forJob: "job-1"))
    }

    func testOnlyTheCurrentJobsFailureCounts() {
        var prep = SourcePreparation()
        let attempt = prep.requested("a")
        XCTAssertTrue(prep.concerns("a", jobID: "job-1", .failed))  // before the reply names the job
        XCTAssertTrue(prep.accepted("a", attempt: attempt, jobID: "job-2"))
        XCTAssertEqual(prep.asset(forJob: "job-2"), "a")
        XCTAssertFalse(prep.concerns("a", jobID: "job-1", .failed))
        XCTAssertTrue(prep.concerns("a", jobID: "job-2", .cancelled))
        XCTAssertTrue(prep.concerns("a", jobID: "job-1", .ready))
        // Nothing awaited: a stray failure changes nothing.
        XCTAssertFalse(prep.concerns("other", jobID: "job-9", .failed))
    }

    func testTheAppWaitsLongerThanTheServiceDeadline() {
        XCTAssertGreaterThan(SourcePreparation.patience(serverTimeout: 1800), 1800)
        XCTAssertGreaterThan(SourcePreparation.patience(serverTimeout: 3600), 3600)
        XCTAssertGreaterThan(SourcePreparation.patience(serverTimeout: nil), SourcePreparation.defaultServerTimeout)
        XCTAssertEqual(SourcePreparation.patience(serverTimeout: 0), SourcePreparation.patience(serverTimeout: nil))
    }
}
