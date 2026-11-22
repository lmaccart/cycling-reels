import AppKit
import XCTest
@testable import flibrary

final class FlibraryTests: XCTestCase {
    static let json = """
    {"root":"/r","exportsDir":"/r/flibrary-exports","freeBytes":100,"assets":[
     {"id":"a1","kind":"video","media":"/m/a1.360","originalName":"GS01.360","created":"2026-11-21T07:06:14","duration":185.5,
      "is360":true,"projection":"gopro-eac","thumb":null,
      "sheets":["/s/a1.jpg"],"sheetGrid":{"cols":4,"rows":3,"times":[0,1,2,3,4,5,6,7,8,9,10,11]},
      "tags":{"type":"solo-ride","shot":"pov","quality":3,"summary":"Elevator then campus","hooks":["the commute"],"location":"Houston","notes":"bar mount"},
      "exports":[{"kind":"equirect","path":"/e/a1-equirect.mp4","bytes":5,"label":"equirect"}]},
     {"id":"b2","kind":"video","media":"/m/b2.mov","created":"2026-11-20T10:00:00","duration":30,"is360":false,
      "tags":{"type":"group-ride","quality":5,"summary":"Sprint"},"exports":[]},
     {"id":"c3","kind":"photo","media":"/m/c3.heic","created":"2026-11-19T09:00:00","is360":false,"tags":null},
     {"id":"d4","kind":"video","media":"/m/d4.360","created":"2026-11-18T09:00:00","is360":true,"exports":[]}]}
    """

    func list() throws -> [Asset] { try JSONDecoder().decode(LibraryList.self, from: Data(Self.json.utf8)).assets }

    func testDecodeAndDate() throws {
        let a = try list()
        XCTAssertEqual(a.count, 4)
        XCTAssertEqual(Calendar.current.component(.hour, from: try XCTUnwrap(a[0].date)), 7)
        XCTAssertEqual(a[0].equirectExport?.path, "/e/a1-equirect.mp4")
    }

    func testFilters() throws {
        let a = try list()
        var f = LibraryFilter()
        XCTAssertEqual(f.apply(a).map(\.id), ["a1", "b2", "c3", "d4"])
        f.kind = .threeSixty; XCTAssertEqual(f.apply(a).map(\.id), ["a1", "d4"])
        f.kind = .flat; XCTAssertEqual(f.apply(a).map(\.id), ["b2", "c3"])
        f.kind = .all; f.minQuality = 4; XCTAssertEqual(f.apply(a).map(\.id), ["b2"])
        f.minQuality = 0; f.query = "houston elevator"; XCTAssertEqual(f.apply(a).map(\.id), ["a1"])
        f.query = "COMMUTE"; XCTAssertEqual(f.apply(a).map(\.id), ["a1"])
        f.query = "gs01"; XCTAssertEqual(f.apply(a).map(\.id), ["a1"])
        f.query = ""; f.type = "group-ride"; XCTAssertEqual(f.apply(a).map(\.id), ["b2"])
        f.type = nil; f.date = .day; f.day = try XCTUnwrap(a[1].date); XCTAssertEqual(f.apply(a).map(\.id), ["b2"])
        f.date = .week; f.day = Date()
        let now = try XCTUnwrap(a[0].date)
        XCTAssertEqual(f.apply(a, now: now).map(\.id), ["a1", "b2", "c3", "d4"])
        f.date = .week; XCTAssertEqual(f.apply(a, now: now.addingTimeInterval(86400 * 6)).map(\.id), ["a1", "b2"])
    }

    func testDragPlan() throws {
        let a = try list()
        let plan = DragPlanner.plan(a, exists: { _ in true })
        XCTAssertEqual(plan.urls.map(\.path), ["/e/a1-equirect.mp4", "/m/b2.mov", "/m/c3.heic"])
        XCTAssertEqual(plan.needsEquirect.map(\.id), ["d4"])
        // An export that is missing on disk counts as no export; originals missing are skipped silently.
        let missing = DragPlanner.plan(a, exists: { !$0.hasPrefix("/e/") })
        XCTAssertEqual(missing.needsEquirect.map(\.id), ["a1", "d4"])
    }

    func testPasteboardCarriesFileURLs() throws {
        let urls = [URL(fileURLWithPath: "/m/b2.mov"), URL(fileURLWithPath: "/e/a1-equirect.mp4")]
        let pb = NSPasteboard(name: NSPasteboard.Name("flibrary-test-\(UUID().uuidString)"))
        pb.clearContents()
        XCTAssertTrue(pb.writeObjects(DragPlanner.writers(urls)))
        XCTAssertEqual(pb.pasteboardItems?.count, 2)
        XCTAssertTrue(pb.pasteboardItems?.allSatisfy { $0.types.contains(NSPasteboard.PasteboardType("public.file-url")) } ?? false)
        let read = pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]
        XCTAssertEqual(read?.map(\.path), urls.map(\.path))
    }

    func testSheetTile() {
        let g = SheetGrid(cols: 4, rows: 3, times: Array(repeating: 0, count: 24), perSheet: nil, tileW: nil, tileH: nil, gutter: nil, header: nil, sheetWidth: nil)
        let t = Media.sheetTile(grid: g, sheetCount: 2, fraction: 0.99, imageSize: CGSize(width: 400, height: 300))
        XCTAssertEqual(t?.sheet, 1)
        XCTAssertEqual(t?.rect, CGRect(x: 300, y: 200, width: 100, height: 100))
        XCTAssertEqual(Media.sheetTile(grid: g, sheetCount: 2, fraction: 0, imageSize: CGSize(width: 400, height: 300))?.rect.origin, .zero)
    }

    func testSheetTileExactGeometry() {
        let g = SheetGrid(cols: 3, rows: 4, times: Array(repeating: 0, count: 14), perSheet: 12, tileW: 520, tileH: 260, gutter: 10, header: 72, sheetWidth: 1600)
        let t = Media.sheetTile(grid: g, sheetCount: 2, fraction: 0.99, imageSize: CGSize(width: 1600, height: 1162))
        XCTAssertEqual(t?.sheet, 1)
        XCTAssertEqual(t?.rect, CGRect(x: 540, y: 72, width: 520, height: 260))  // tile 13 = slot 1 on sheet 2
        XCTAssertEqual(Media.sheetTile(grid: g, sheetCount: 2, fraction: 0, imageSize: CGSize(width: 1600, height: 1162))?.rect, CGRect(x: 10, y: 72, width: 520, height: 260))
    }

    func testRepositoryCandidate() {
        XCTAssertEqual(RepositoryLocator.candidate(arguments: ["x", "--repo", "/a"], environment: ["FLIBRARY_REPO_ROOT": "/b"], saved: "/c"), "/a")
        XCTAssertEqual(RepositoryLocator.candidate(arguments: [], environment: ["FLIBRARY_REPO_ROOT": "/b"], saved: "/c"), "/b")
        XCTAssertEqual(RepositoryLocator.candidate(arguments: [], environment: [:], saved: "/c"), "/c")
        XCTAssertNil(RepositoryLocator.candidate(arguments: [], environment: [:], saved: nil))
    }
}
