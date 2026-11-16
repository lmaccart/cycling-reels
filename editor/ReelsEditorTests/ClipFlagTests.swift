import XCTest
@testable import ReelsEditor

final class ClipFlagTests: XCTestCase {
    func testParsesFlagsAndSkipsBrokenOnes() {
        let value: JSONValue = .object(["flags": .array([
            .object(["id": .string("f-1"), "asset": .string("a"), "in": .number(12), "out": .number(40), "note": .string("sprint")]),
            .object(["id": .string("f-2"), "asset": .string("pic")]),
            .object(["asset": .string("no-id")]),
        ])])
        let flags = ClipFlag.list(value)
        XCTAssertEqual(flags.map(\.id), ["f-1", "f-2"])
        XCTAssertEqual(flags[0].inPoint, 12)
        XCTAssertEqual(flags[0].note, "sprint")
        XCTAssertNotNil(flags[0].range)
        XCTAssertNil(flags[1].range)
        XCTAssertEqual(ClipFlag.list(nil), [])
    }
}
