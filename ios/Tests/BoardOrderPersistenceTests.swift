import AgendaSyncCore
import XCTest
@testable import Agenda

@MainActor
final class BoardOrderPersistenceTests: XCTestCase {
    func testAppliesLatestGoodSnapshotAndIgnoresOlderArrival() {
        let store = MobileAgendaStore(inMemory: true)
        store.applyBoardOrder(
            AgendaBoardOrderSnapshot(
                generatedAt: "2026-08-14T03:30:00+00:00",
                rows: ["in_development": ["new-first", "new-second"]]
            )
        )
        store.applyBoardOrder(
            AgendaBoardOrderSnapshot(
                generatedAt: "2026-08-14T03:29:00+00:00",
                rows: ["in_development": ["stale-first"]]
            )
        )

        XCTAssertEqual(
            store.boardOrderRows["in_development"],
            ["new-first", "new-second"]
        )
    }

    func testSnapshotEncodingSupportsOfflineMetadataReload() throws {
        let snapshot = AgendaBoardOrderSnapshot(
            generatedAt: "2026-08-14T03:30:00+00:00",
            rows: ["top_of_mind": ["manual-a", "manual-b"]]
        )

        let persisted = try JSONEncoder().encode(snapshot)
        let restored = try JSONDecoder().decode(
            AgendaBoardOrderSnapshot.self,
            from: persisted
        )

        XCTAssertEqual(restored, snapshot)
    }
}
