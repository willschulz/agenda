import AgendaSyncCore
import CloudKit
import Foundation
import XCTest
@testable import Agenda

@available(macOS 14.0, *)
private actor MockAgendaCloudDatabase: AgendaCloudDatabaseAccess {
    var fetched: AgendaRecordResults = [:]
    var failMissingSaves = false
    var savedRecordNames: [String] = []

    func records(for ids: [CKRecord.ID]) async throws -> AgendaRecordResults {
        Dictionary(
            uniqueKeysWithValues: ids.compactMap { id in
                fetched[id].map { (id, $0) }
            }
        )
    }

    func saveZone() async throws {
        savedRecordNames.append("zone:\(AgendaCloudRecord.zoneID.zoneName)")
    }

    func save(_ record: CKRecord) async throws -> CKRecord {
        savedRecordNames.append(record.recordID.recordName)
        return record
    }

    func saveMissing(_ records: [CKRecord]) async throws -> AgendaRecordResults {
        Dictionary(
            uniqueKeysWithValues: records.map { record in
                savedRecordNames.append(record.recordID.recordName)
                if failMissingSaves {
                    return (
                        record.recordID,
                        .failure(URLError(.networkConnectionLost))
                    )
                }
                return (record.recordID, .success(record))
            }
        )
    }

    func setFetched(_ record: CKRecord, for id: CKRecord.ID) {
        fetched[id] = .success(record)
    }

    func setFailure(_ error: Error, for id: CKRecord.ID) {
        fetched[id] = .failure(error)
    }

    func savedNames() -> [String] {
        savedRecordNames
    }
}

@available(macOS 14.0, *)
private actor MockPendingRecordDiscarder: AgendaPendingRecordDiscarding {
    private(set) var discarded: [CKRecord.ID] = []

    func discardPendingRecordIDs(_ recordIDs: [CKRecord.ID]) async {
        discarded.append(contentsOf: recordIDs)
    }
}

@available(macOS 14.0, *)
@MainActor
final class CloudKitQueueReconciliationTests: XCTestCase {
    private var temporaryDirectories: [URL] = []

    override func tearDownWithError() throws {
        for directory in temporaryDirectories {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: directory.path
            )
            try? FileManager.default.removeItem(at: directory)
        }
        temporaryDirectories = []
    }

    func testBoardOrderPublishesReplaceableFixedRecord() async throws {
        let fixture = try makeFixture()
        let snapshot = AgendaBoardOrderSnapshot(
            generatedAt: "2026-08-14T03:30:00+00:00",
            rows: ["in_development": ["project-a", "project-b"]]
        )

        await fixture.coordinator.publishBoardOrder(snapshot)

        let savedNames = await fixture.database.savedNames()
        XCTAssertEqual(
            savedNames,
            [AgendaCloudRecord.boardOrderID.recordName]
        )
    }

    func testBoardOrderSkipsUnchangedAndOlderSnapshots() async throws {
        let fixture = try makeFixture()
        let current = AgendaBoardOrderSnapshot(
            generatedAt: "2026-08-14T12:00:00+00:00",
            rows: ["in_development": ["project-a"]]
        )
        await fixture.database.setFetched(
            try AgendaCloudRecord.boardOrder(current),
            for: AgendaCloudRecord.boardOrderID
        )

        await fixture.coordinator.publishBoardOrder(
            AgendaBoardOrderSnapshot(
                generatedAt: "2026-08-14T12:01:00+00:00",
                rows: current.rows
            )
        )
        await fixture.coordinator.publishBoardOrder(
            AgendaBoardOrderSnapshot(
                generatedAt: "2026-08-14T11:59:00+00:00",
                rows: ["in_development": ["project-b"]]
            )
        )

        let savedNames = await fixture.database.savedNames()
        XCTAssertTrue(savedNames.isEmpty)
    }

    func testTombstoneIsSavedBeforeItCanBeManifested() async throws {
        let fixture = try makeFixture()
        let tombstone = AgendaTombstoneRecord(
            entityKind: .todo,
            stableID: "deleted-todo",
            revision: 2,
            deletedAt: "2026-08-14T12:00:00+00:00"
        )
        let record = try AgendaCloudRecord.tombstone(tombstone)
        await fixture.database.setFailure(
            cloudError(.unknownItem),
            for: record.recordID
        )

        try await fixture.coordinator.publishTombstonesDirectly(
            [tombstone],
            into: fixture.store,
            engine: fixture.engine
        )

        let savedNames = await fixture.database.savedNames()
        XCTAssertEqual(savedNames, [record.recordID.recordName])
    }

    func testPhysicalTombstoneDeletionInvalidatesPublishedCache() async throws {
        let fixture = try makeFixture()
        let tombstone = AgendaTombstoneRecord(
            entityKind: .todo,
            stableID: "deleted-todo",
            revision: 2,
            deletedAt: "2026-08-14T12:00:00+00:00"
        )
        let record = try AgendaCloudRecord.tombstone(tombstone)
        await fixture.store.recordSavesSucceeded([record])
        let initiallyUnpublished = await fixture.store
            .unpublishedTombstoneRecords([tombstone])
        XCTAssertTrue(initiallyUnpublished.isEmpty)

        await fixture.store.applyFetchedCycle(
            AgendaCloudFetchCycle(
                records: [],
                physicalDeletions: [record.recordID],
                zoneDeleted: false
            )
        )

        let unpublished = await fixture.store.unpublishedTombstoneRecords([
            tombstone,
        ])
        XCTAssertEqual(unpublished.map(\.recordID), [record.recordID])
    }

    func testZoneDeletionRequiresRecreationAndInvalidatesCaches() async throws {
        let fixture = try makeFixture()
        let tombstone = AgendaTombstoneRecord(
            entityKind: .todo,
            stableID: "deleted-todo",
            revision: 2,
            deletedAt: "2026-08-14T12:00:00+00:00"
        )
        let record = try AgendaCloudRecord.tombstone(tombstone)
        await fixture.store.recordSavesSucceeded([record])

        await fixture.store.applyFetchedCycle(
            AgendaCloudFetchCycle(
                records: [],
                physicalDeletions: [],
                zoneDeleted: true
            )
        )

        let needsRecreation = await fixture.store.needsZoneRecreation()
        XCTAssertTrue(needsRecreation)
        let unpublished = await fixture.store.unpublishedTombstoneRecords([
            tombstone,
        ])
        XCTAssertEqual(unpublished.map(\.recordID), [record.recordID])
        try await fixture.coordinator.recreateZoneIfNeeded(
            store: fixture.store
        )
        let recreated = await fixture.store.needsZoneRecreation()
        XCTAssertFalse(recreated)
        let savedNames = await fixture.database.savedNames()
        XCTAssertEqual(
            savedNames,
            ["zone:\(AgendaCloudRecord.zoneID.zoneName)"]
        )
    }

    func testSemanticReplayAcknowledgesDurableRecord() async throws {
        let fixture = try makeFixture()
        try await fixture.store.persistPendingRecords([fixture.record])
        await fixture.database.setFetched(
            fixture.record,
            for: fixture.record.recordID
        )

        try await fixture.coordinator.publishChangedCanonicalsDirectly(
            [fixture.canonical],
            results: [],
            into: fixture.store,
            engine: fixture.engine
        )

        let pending = try await fixture.store.pendingCanonicalRecords()
        XCTAssertTrue(pending.isEmpty)
    }

    func testMissingRecordPublishesAndAcknowledges() async throws {
        let fixture = try makeFixture()
        try await fixture.store.persistPendingRecords([fixture.record])
        await fixture.database.setFailure(
            cloudError(.unknownItem),
            for: fixture.record.recordID
        )

        try await fixture.coordinator.publishChangedCanonicalsDirectly(
            [fixture.canonical],
            results: [],
            into: fixture.store,
            engine: fixture.engine
        )

        let pending = try await fixture.store.pendingCanonicalRecords()
        let savedNames = await fixture.database.savedNames()
        XCTAssertTrue(pending.isEmpty)
        XCTAssertEqual(savedNames, [fixture.record.recordID.recordName])
    }

    func testObsoleteRecordIsRemovedWithoutServerWrite() async throws {
        let fixture = try makeFixture()
        try await fixture.store.persistPendingRecords([fixture.record])

        try await fixture.coordinator.publishChangedCanonicalsDirectly(
            [],
            results: [],
            into: fixture.store,
            engine: fixture.engine
        )

        let pending = try await fixture.store.pendingCanonicalRecords()
        let savedNames = await fixture.database.savedNames()
        XCTAssertTrue(pending.isEmpty)
        XCTAssertTrue(savedNames.isEmpty)
    }

    func testSemanticMismatchIsDurablyQuarantined() async throws {
        let fixture = try makeFixture()
        try await fixture.store.persistPendingRecords([fixture.record])
        let server = try AgendaCloudRecord.canonical(
            canonical(id: "todo-1", revision: 1, text: "server")
        )
        await fixture.database.setFetched(server, for: fixture.record.recordID)

        try await fixture.coordinator.publishChangedCanonicalsDirectly(
            [fixture.canonical],
            results: [],
            into: fixture.store,
            engine: fixture.engine
        )

        let pending = try await fixture.store.pendingCanonicalRecords()
        let quarantined = await fixture.store.isCanonicalQuarantined(
            fixture.record.recordID.recordName
        )
        XCTAssertTrue(pending.isEmpty)
        XCTAssertTrue(quarantined)
        let data = try Data(
            contentsOf: fixture.directory.appendingPathComponent(
                "canonical-quarantine.json"
            )
        )
        let entries = try JSONDecoder().decode(
            [String: AgendaCanonicalQuarantineEntry].self,
            from: data
        )
        XCTAssertEqual(entries.count, 1)
        XCTAssertNotEqual(
            entries.values.first?.localPayloadSHA256,
            entries.values.first?.serverPayloadSHA256
        )
    }

    func testUpdatedAtOnlyDriftIsAcknowledgedNotQuarantined() async throws {
        let fixture = try makeFixture()
        try await fixture.store.persistPendingRecords([fixture.record])
        let server = try AgendaCloudRecord.canonical(
            canonical(
                id: "todo-1",
                revision: 1,
                text: "local",
                updatedAt: "2026-08-11T16:22:24+00:00"
            )
        )
        let local = canonical(
            id: "todo-1",
            revision: 1,
            text: "local",
            updatedAt: "2026-09-22T15:51:03+00:00"
        )
        await fixture.database.setFetched(server, for: fixture.record.recordID)

        try await fixture.coordinator.publishChangedCanonicalsDirectly(
            [local],
            results: [],
            into: fixture.store,
            engine: fixture.engine
        )

        let pending = try await fixture.store.pendingCanonicalRecords()
        let quarantined = await fixture.store.isCanonicalQuarantined(
            fixture.record.recordID.recordName
        )
        let savedNames = await fixture.database.savedNames()
        XCTAssertTrue(pending.isEmpty)
        XCTAssertFalse(quarantined)
        XCTAssertTrue(savedNames.isEmpty)
    }

    func testSemanticEqualityIgnoresOnlyVolatilePayloadKeys() {
        let base = canonical(
            id: "todo-1", revision: 2, text: "same", updatedAt: "2026-08-01"
        )
        XCTAssertTrue(agendaCanonicalsSemanticallyEqual(
            base,
            canonical(
                id: "todo-1", revision: 2, text: "same",
                updatedAt: "2026-10-01"
            )
        ))
        XCTAssertFalse(agendaCanonicalsSemanticallyEqual(
            base,
            canonical(
                id: "todo-1", revision: 2, text: "changed",
                updatedAt: "2026-10-01"
            )
        ))
        XCTAssertFalse(agendaCanonicalsSemanticallyEqual(
            base,
            canonical(
                id: "todo-1", revision: 3, text: "same",
                updatedAt: "2026-08-01"
            )
        ))
        XCTAssertFalse(agendaCanonicalsSemanticallyEqual(
            AgendaCanonicalRecordV2(
                entityKind: .todo, stableID: "todo-1", revision: 2,
                payloadJSON: "not json"
            ),
            AgendaCanonicalRecordV2(
                entityKind: .todo, stableID: "todo-1", revision: 2,
                payloadJSON: "not json either"
            )
        ))
    }

    func testManifestOmitsUnresolvedQuarantinedCanonical() async throws {
        let fixture = try makeFixture()
        try await fixture.store.quarantineCanonical(
            local: fixture.record,
            server: fixture.record,
            reason: "review-test"
        )

        await fixture.coordinator.publishCanonicalManifest(
            [fixture.canonical],
            tombstones: [],
            results: [],
            store: fixture.store
        )

        let savedNames = await fixture.database.savedNames()
        XCTAssertEqual(
            savedNames,
            [AgendaCloudRecord.canonicalManifestID.recordName]
        )
    }

    func testInboundCanonicalFetchPreservesDurableBodyForReconciliation() async throws {
        let fixture = try makeFixture()
        try await fixture.store.persistPendingRecords([fixture.record])
        let conflicting = try AgendaCloudRecord.canonical(
            canonical(id: "todo-1", revision: 1, text: "server")
        )

        await fixture.store.applyFetchedCycle(
            AgendaCloudFetchCycle(
                records: [conflicting],
                physicalDeletions: [],
                zoneDeleted: false
            )
        )

        let pending = try await fixture.store.pendingCanonicalRecords()
        let unpublished = try await fixture.store.unpublishedCanonicalRecords([
            fixture.canonical,
        ])
        XCTAssertEqual(pending.map(\.recordID), [fixture.record.recordID])
        XCTAssertEqual(unpublished.map(\.recordID), [fixture.record.recordID])
    }

    func testChangedDurableBodyIsQuarantinedWithoutReplacement() async throws {
        let fixture = try makeFixture()
        let queued = try AgendaCloudRecord.canonical(
            canonical(id: "todo-1", revision: 1, text: "queued-old-body")
        )
        try await fixture.store.persistPendingRecords([queued])
        let server = try AgendaCloudRecord.canonical(
            canonical(id: "todo-1", revision: 1, text: "server-third-body")
        )
        await fixture.database.setFetched(
            server,
            for: fixture.record.recordID
        )

        try await fixture.coordinator.publishChangedCanonicalsDirectly(
            [fixture.canonical],
            results: [],
            into: fixture.store,
            engine: fixture.engine
        )

        let pending = try await fixture.store.pendingCanonicalRecords()
        XCTAssertTrue(pending.isEmpty)
        let data = try Data(
            contentsOf: fixture.directory.appendingPathComponent(
                "canonical-quarantine.json"
            )
        )
        let entries = try JSONDecoder().decode(
            [String: AgendaCanonicalQuarantineEntry].self,
            from: data
        )
        XCTAssertEqual(
            entries.values.first?.reason,
            "durable_body_current_export_mismatch"
        )
        XCTAssertEqual(
            AgendaCloudRecord.unarchiveRecord(
                try XCTUnwrap(entries.values.first?.localRecord)
            ).flatMap { try? AgendaCloudRecord.decodeCanonical($0) },
            try AgendaCloudRecord.decodeCanonical(queued)
        )
        XCTAssertEqual(
            AgendaCloudRecord.unarchiveRecord(
                try XCTUnwrap(entries.values.first?.serverRecord)
            ).flatMap { try? AgendaCloudRecord.decodeCanonical($0) },
            try AgendaCloudRecord.decodeCanonical(server)
        )
        XCTAssertEqual(
            AgendaCloudRecord.unarchiveRecord(
                try XCTUnwrap(entries.values.first?.currentExportRecord)
            ).flatMap { try? AgendaCloudRecord.decodeCanonical($0) },
            fixture.canonical
        )
    }

    func testRestartFinishesPreviouslyQuarantinedCleanup() async throws {
        let fixture = try makeFixture()
        let server = try AgendaCloudRecord.canonical(
            canonical(id: "todo-1", revision: 1, text: "server")
        )
        try await fixture.store.persistPendingRecords([fixture.record])
        try await fixture.store.quarantineCanonical(
            local: fixture.record,
            server: server,
            reason: "same_record_id_semantic_mismatch"
        )

        let restartedStore = MacAgendaCloudStore(
            localAPI: URL(string: "http://127.0.0.1:1")!,
            cloudDirectory: fixture.directory
        )
        try await fixture.coordinator.publishChangedCanonicalsDirectly(
            [fixture.canonical],
            results: [],
            into: restartedStore,
            engine: fixture.engine
        )

        let pending = try await restartedStore.pendingCanonicalRecords()
        XCTAssertTrue(pending.isEmpty)
    }

    func testTransientFailurePreservesOriginalDurableBody() async throws {
        let fixture = try makeFixture()
        try await fixture.store.persistPendingRecords([fixture.record])
        let before = try Data(
            contentsOf: fixture.directory.appendingPathComponent(
                "pending-records.json"
            )
        )
        await fixture.database.setFailure(
            cloudError(.networkFailure),
            for: fixture.record.recordID
        )

        try await fixture.coordinator.publishChangedCanonicalsDirectly(
            [fixture.canonical],
            results: [],
            into: fixture.store,
            engine: fixture.engine
        )

        let after = try Data(
            contentsOf: fixture.directory.appendingPathComponent(
                "pending-records.json"
            )
        )
        XCTAssertEqual(before, after)
        let pending = try await fixture.store.pendingCanonicalRecords()
        XCTAssertEqual(pending.count, 1)
    }

    func testPartialFailureAcknowledgesOnlySuccessfulRecord() async throws {
        let fixture = try makeFixture()
        let secondCanonical = canonical(
            id: "todo-2",
            revision: 1,
            text: "second"
        )
        let secondRecord = try AgendaCloudRecord.canonical(secondCanonical)
        try await fixture.store.persistPendingRecords([
            fixture.record,
            secondRecord,
        ])
        await fixture.database.setFetched(
            fixture.record,
            for: fixture.record.recordID
        )
        await fixture.database.setFailure(
            cloudError(.networkFailure),
            for: secondRecord.recordID
        )

        try await fixture.coordinator.publishChangedCanonicalsDirectly(
            [fixture.canonical, secondCanonical],
            results: [],
            into: fixture.store,
            engine: fixture.engine
        )

        let pending = try await fixture.store.pendingCanonicalRecords()
        XCTAssertEqual(pending.map(\.recordID.recordName), [
            secondRecord.recordID.recordName,
        ])
    }

    func testPendingRewriteFailureIsSurfacedAndPreservesBody() async throws {
        let fixture = try makeFixture()
        try await fixture.store.persistPendingRecords([fixture.record])
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o500],
            ofItemAtPath: fixture.directory.path
        )
        defer {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: fixture.directory.path
            )
        }

        do {
            try await fixture.store.removePendingRecordsAtomically([
                fixture.record.recordID,
            ])
            XCTFail("Expected the atomic pending rewrite to fail")
        } catch {
            XCTAssertFalse(error.localizedDescription.isEmpty)
        }

        try FileManager.default.setAttributes(
            [.posixPermissions: 0o700],
            ofItemAtPath: fixture.directory.path
        )
        let pending = try await fixture.store.pendingCanonicalRecords()
        XCTAssertEqual(pending.map(\.recordID.recordName), [
            fixture.record.recordID.recordName,
        ])
    }

    func testUnreadableDurableQueueIsNotReportedAsEmpty() async throws {
        let fixture = try makeFixture()
        try Data("not-json".utf8).write(
            to: fixture.directory.appendingPathComponent(
                "pending-records.json"
            )
        )

        do {
            _ = try await fixture.store.loadPendingRecords()
            XCTFail("Expected corrupt durable queue to throw")
        } catch {
            XCTAssertFalse(error.localizedDescription.isEmpty)
        }
    }

    func testStagingCannotOverwriteQueueCorruptedAfterStartup() async throws {
        let fixture = try makeFixture()
        let pendingURL = fixture.directory.appendingPathComponent(
            "pending-records.json"
        )
        let corrupt = Data("not-json".utf8)
        try corrupt.write(to: pendingURL)

        do {
            try await fixture.store.persistPendingRecords([fixture.record])
            XCTFail("Expected corrupt durable queue to block staging")
        } catch {
            XCTAssertEqual(try Data(contentsOf: pendingURL), corrupt)
        }
    }

    func testRestartAfterAcknowledgementIsIdempotent() async throws {
        let fixture = try makeFixture()
        try await fixture.store.persistPendingRecords([fixture.record])
        await fixture.database.setFetched(
            fixture.record,
            for: fixture.record.recordID
        )
        try await fixture.coordinator.publishChangedCanonicalsDirectly(
            [fixture.canonical],
            results: [],
            into: fixture.store,
            engine: fixture.engine
        )

        let restartedStore = MacAgendaCloudStore(
            localAPI: URL(string: "http://127.0.0.1:1")!,
            cloudDirectory: fixture.directory
        )
        let restartedEngine = MockPendingRecordDiscarder()
        try await fixture.coordinator.publishChangedCanonicalsDirectly(
            [fixture.canonical],
            results: [],
            into: restartedStore,
            engine: restartedEngine
        )

        let pending = try await restartedStore.pendingCanonicalRecords()
        XCTAssertTrue(pending.isEmpty)
    }

    private func makeFixture() throws -> (
        directory: URL,
        canonical: AgendaCanonicalRecordV2,
        record: CKRecord,
        store: MacAgendaCloudStore,
        engine: MockPendingRecordDiscarder,
        database: MockAgendaCloudDatabase,
        coordinator: CloudKitCoordinator
    ) {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        temporaryDirectories.append(directory)
        let canonical = canonical(id: "todo-1", revision: 1, text: "local")
        let record = try AgendaCloudRecord.canonical(canonical)
        let store = MacAgendaCloudStore(
            localAPI: URL(string: "http://127.0.0.1:1")!,
            cloudDirectory: directory
        )
        let engine = MockPendingRecordDiscarder()
        let database = MockAgendaCloudDatabase()
        return (
            directory,
            canonical,
            record,
            store,
            engine,
            database,
            CloudKitCoordinator(database: database)
        )
    }

    private func canonical(
        id: String,
        revision: Int,
        text: String,
        updatedAt: String? = nil
    ) -> AgendaCanonicalRecordV2 {
        let stamp = updatedAt.map { #","updated_at":"\#($0)""# } ?? ""
        return AgendaCanonicalRecordV2(
            entityKind: .todo,
            stableID: id,
            revision: revision,
            payloadJSON: #"{"id":"\#(id)","revision":\#(revision),"text":"\#(text)"\#(stamp)}"#
        )
    }

    private func cloudError(_ code: CKError.Code) -> Error {
        NSError(
            domain: CKError.errorDomain,
            code: code.rawValue,
            userInfo: nil
        )
    }
}
