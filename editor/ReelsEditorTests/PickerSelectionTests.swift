import XCTest
@testable import ReelsEditor

final class PickerSelectionTests: XCTestCase {
    private let ids = ["a", "b", "c"]

    func testMovesAndClampsAtEnds() {
        XCTAssertEqual(PickerSelection.move(from: "a", in: ids, by: 1), "b")
        XCTAssertEqual(PickerSelection.move(from: "c", in: ids, by: 1), "c")
        XCTAssertEqual(PickerSelection.move(from: "a", in: ids, by: -1), "a")
    }

    func testNoSelectionOrFilteredOutSelectionStartsAtEdge() {
        XCTAssertEqual(PickerSelection.move(from: nil, in: ids, by: 1), "a")
        XCTAssertEqual(PickerSelection.move(from: nil, in: ids, by: -1), "c")
        XCTAssertEqual(PickerSelection.move(from: "gone", in: ids, by: 1), "a")
    }

    func testEmptyListHasNoSelection() {
        XCTAssertNil(PickerSelection.move(from: "a", in: [], by: 1))
    }
}
