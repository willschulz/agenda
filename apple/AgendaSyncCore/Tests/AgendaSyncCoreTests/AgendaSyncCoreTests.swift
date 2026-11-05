import CloudKit
import XCTest
@testable import AgendaSyncCore

final class AgendaSyncCoreTests: XCTestCase {
    func testSnapshotDecodesPythonWireKeys() throws {
        let data = """
        {
          "schema_version": 1,
          "generated_at": "2026-08-09T00:00:00+00:00",
          "projects": [{
            "stable_id": "11111111-1111-4111-8111-111111111111",
            "name": "CloudKit Sync Test",
            "category": "side_project",
            "display_order": 0,
            "notes": [],
            "northstars": [],
            "archived": false,
            "created_at": "2026-08-09T00:00:00+00:00",
            "updated_at": "2026-08-09T00:00:00+00:00"
          }],
          "todos": []
        }
        """.data(using: .utf8)!

        let snapshot = try JSONDecoder().decode(AgendaSnapshot.self, from: data)

        XCTAssertEqual(snapshot.schemaVersion, 1)
        XCTAssertEqual(snapshot.projects.first?.name, agendaSyncTestProjectName)
    }

    func testTodoDecodesForLaterAndLegacyAlias() throws {
        func decode(_ placementField: String) throws -> AgendaTodoRecord {
            let data = """
            {
              "stable_id": "22222222-2222-4222-8222-222222222222",
              "project_stable_id": "11111111-1111-4111-8111-111111111111",
              "parent_stable_id": null,
              "text": "Parked idea",
              "is_blocker": false,
              \(placementField),
              "in_progress": false,
              "flagged": false,
              "awaiting_review": false,
              "display_order": 0,
              "completed": false,
              "completed_at": null,
              "notes": [],
              "effort": null,
              "deadline": null,
              "created_at": "2026-08-09T00:00:00+00:00",
              "updated_at": "2026-08-09T00:00:00+00:00"
            }
            """.data(using: .utf8)!
            return try JSONDecoder().decode(AgendaTodoRecord.self, from: data)
        }

        XCTAssertTrue(try decode("\"for_later\": true").forLater)
        XCTAssertTrue(try decode("\"is_followup\": true").forLater)
    }

    func testCloudRecordRoundTrip() throws {
        let project = AgendaProjectRecord(
            stableID: "11111111-1111-4111-8111-111111111111",
            name: "CloudKit Sync Test",
            category: "side_project",
            displayOrder: 0,
            notes: [],
            northstars: [],
            archived: false,
            createdAt: "2026-08-09T00:00:00+00:00",
            updatedAt: "2026-08-09T00:00:00+00:00"
        )

        let record = try AgendaCloudRecord.project(project)
        let decoded = try AgendaCloudRecord.decodeProject(record)

        XCTAssertEqual(decoded, project)
        XCTAssertEqual(record.recordID.zoneID.zoneName, agendaCloudZoneName)
    }

    func testTombstoneSnapshotAndCloudRecordRoundTrip() throws {
        let data = """
        {
          "schema_version": 2,
          "generated_at": "2026-08-09T20:00:00+00:00",
          "tombstones": [{
            "schema_version": 2,
            "entity_kind": "todo",
            "stable_id": "22222222-2222-4222-8222-222222222222",
            "project_stable_id": "11111111-1111-4111-8111-111111111111",
            "parent_stable_id": null,
            "revision": 1,
            "deleted_at": "2026-08-09T20:00:00+00:00",
            "payload": {"ignored_by_transport": true}
          }]
        }
        """.data(using: .utf8)!
        let snapshot = try JSONDecoder().decode(
            AgendaTombstoneSnapshot.self,
            from: data
        )
        let tombstone = try XCTUnwrap(snapshot.tombstones.first)
        let record = try AgendaCloudRecord.tombstone(tombstone)
        let decoded = try AgendaCloudRecord.decodeTombstone(record)

        XCTAssertEqual(snapshot.schemaVersion, agendaContractSchemaVersion)
        XCTAssertEqual(decoded, tombstone)
        XCTAssertEqual(record.recordType, AgendaCloudRecord.tombstoneType)
        XCTAssertTrue(record.recordID.recordName.hasSuffix("-r1"))
    }

    func testSchemaTwoMutationResultCanonicalAndArchiveRoundTrip() throws {
        let mutation = AgendaMutationProposal(
            schemaVersion: 2,
            mutationID: "mutation-1",
            deviceID: "device-1",
            entityID: "todo-1",
            operation: .scalarEdit,
            baseRevision: 0,
            proposedPayload: #"{"text":"After"}"#
        )
        let result = AgendaMutationResultRecord(
            schemaVersion: 2,
            mutationID: "mutation-1",
            mutationFingerprint: AgendaSyncContract.fingerprint(
                mutation: mutation
            ),
            deviceID: "device-1",
            entityID: "todo-1",
            outcome: .accept,
            reason: "accepted",
            canonicalRevision: 1,
            canonicalStructureRevision: 0
        )
        let canonical = AgendaCanonicalRecordV2(
            entityKind: .todo,
            stableID: "todo-1",
            revision: 1,
            payloadJSON: #"{"stable_id":"todo-1"}"#
        )

        XCTAssertEqual(
            try AgendaCloudRecord.decodeMutation(
                AgendaCloudRecord.mutation(mutation)
            ),
            mutation
        )
        XCTAssertEqual(
            try AgendaCloudRecord.decodeResult(
                AgendaCloudRecord.result(result)
            ),
            result
        )
        let canonicalRecord = try AgendaCloudRecord.canonical(canonical)
        XCTAssertEqual(
            canonicalRecord.recordID.recordName,
            "canonical-todo-todo-1-r1"
        )
        XCTAssertEqual(
            try AgendaCloudRecord.decodeCanonical(canonicalRecord),
            canonical
        )
        let archived = try AgendaCloudRecord.archiveRecord(canonicalRecord)
        let restored = try XCTUnwrap(
            AgendaCloudRecord.unarchiveRecord(archived)
        )
        XCTAssertEqual(
            try AgendaCloudRecord.decodeCanonical(restored),
            canonical
        )
    }

    func testManualSendStartupRestoresExactQueuedBodies() throws {
        let mutation = AgendaMutationProposal(
            schemaVersion: 2,
            mutationID: "mutation-restored",
            deviceID: "device-1",
            entityID: "todo-1",
            operation: .scalarEdit,
            baseRevision: 1,
            proposedPayload: #"{"text":"queued"}"#
        )
        let tombstone = AgendaTombstoneRecord(
            entityKind: .todo,
            stableID: "todo-deleted",
            revision: 2,
            deletedAt: "2026-08-13T22:00:00-04:00"
        )
        let records = [
            try AgendaCloudRecord.mutation(mutation),
            try AgendaCloudRecord.tombstone(tombstone),
        ]

        let restored = AgendaSyncEngine.restoredPendingRecords(records)

        XCTAssertEqual(restored.count, records.count)
        for record in records {
            let restoredRecord = try XCTUnwrap(restored[record.recordID])
            XCTAssertEqual(
                try AgendaCloudRecord.archiveRecord(restoredRecord),
                try AgendaCloudRecord.archiveRecord(record)
            )
        }
    }

    func testStartupIdentifiesBodylessPendingSaves() {
        let durable = CKRecord.ID(
            recordName: "durable",
            zoneID: AgendaCloudRecord.zoneID
        )
        let bodyless = CKRecord.ID(
            recordName: "bodyless",
            zoneID: AgendaCloudRecord.zoneID
        )

        XCTAssertEqual(
            AgendaSyncEngine.bodylessPendingSaveIDs(
                [
                    .saveRecord(durable),
                    .saveRecord(bodyless),
                    .deleteRecord(bodyless),
                ],
                durableIDs: [durable]
            ),
            [bodyless]
        )
    }

    func testSemanticMutationConflictRequestsImmutableRedelivery() throws {
        let mutation = AgendaMutationProposal(
            schemaVersion: 2,
            mutationID: "mutation-redelivery",
            deviceID: "device-1",
            entityID: "todo-1",
            operation: .create,
            baseRevision: 0,
            proposedPayload: #"{"text":"Preserve me"}"#,
            entityKind: .todo
        )
        let requestedAt = Date(timeIntervalSince1970: 1_786_888_800)
        let local = try AgendaCloudRecord.mutation(mutation)
        let server = try AgendaCloudRecord.mutation(mutation)

        let redelivery = try XCTUnwrap(
            AgendaSyncEngine.mutationRecordForRedelivery(
                local: local,
                server: server,
                requestedAt: requestedAt
            )
        )

        XCTAssertEqual(try AgendaCloudRecord.decodeMutation(redelivery), mutation)
        XCTAssertEqual(
            redelivery[AgendaCloudRecord.redeliveryRequestedAtKey] as? Date,
            requestedAt
        )
        XCTAssertNil(
            AgendaSyncEngine.mutationRecordForRedelivery(
                local: try AgendaCloudRecord.canonical(
                    AgendaCanonicalRecordV2(
                        entityKind: .todo,
                        stableID: "todo-1",
                        revision: 1,
                        payloadJSON: #"{"stable_id":"todo-1"}"#
                    )
                ),
                server: try AgendaCloudRecord.canonical(
                    AgendaCanonicalRecordV2(
                        entityKind: .todo,
                        stableID: "todo-1",
                        revision: 1,
                        payloadJSON: #"{"stable_id":"todo-1"}"#
                    )
                ),
                requestedAt: requestedAt
            )
        )
    }

    func testBoardOrderCloudRecordRoundTrip() throws {
        let snapshot = AgendaBoardOrderSnapshot(
            generatedAt: "2026-08-14T03:30:00+00:00",
            rows: [
                "top_of_mind": ["project-a", "project-b"],
                "in_development": ["project-c"],
            ]
        )

        let record = try AgendaCloudRecord.boardOrder(snapshot)

        XCTAssertEqual(record.recordID, AgendaCloudRecord.boardOrderID)
        XCTAssertEqual(record.recordType, AgendaCloudRecord.boardOrderType)
        XCTAssertEqual(
            try AgendaCloudRecord.decodeBoardOrder(record),
            snapshot
        )
    }

    func testManifestTracksLatestLiveOrDeletedState() throws {
        let oldManifest = try JSONDecoder().decode(
            AgendaCanonicalManifestEntry.self,
            from: Data(
                #"{"entity_kind":"todo","stable_id":"legacy","revision":1}"#.utf8
            )
        )
        XCTAssertFalse(oldManifest.deleted)

        let entries = AgendaCanonicalManifestEntry.latestStates(
            canonicals: [
                AgendaCanonicalRecordV2(
                    entityKind: .todo,
                    stableID: "deleted",
                    revision: 1,
                    payloadJSON: "{}"
                ),
                AgendaCanonicalRecordV2(
                    entityKind: .todo,
                    stableID: "restored",
                    revision: 3,
                    payloadJSON: "{}"
                ),
                AgendaCanonicalRecordV2(
                    entityKind: .todo,
                    stableID: "equal",
                    revision: 4,
                    payloadJSON: "{}"
                ),
            ],
            tombstones: [
                AgendaTombstoneRecord(
                    entityKind: .todo,
                    stableID: "deleted",
                    revision: 2,
                    deletedAt: "2026-08-14T02:15:42+00:00"
                ),
                AgendaTombstoneRecord(
                    entityKind: .todo,
                    stableID: "restored",
                    revision: 2,
                    deletedAt: "2026-08-14T02:15:42+00:00"
                ),
                AgendaTombstoneRecord(
                    entityKind: .todo,
                    stableID: "equal",
                    revision: 4,
                    deletedAt: "2026-08-14T02:15:42+00:00"
                ),
            ]
        )

        let deleted = try XCTUnwrap(
            entries.first { $0.stableID == "deleted" }
        )
        XCTAssertTrue(deleted.deleted)
        XCTAssertEqual(deleted.revision, 2)
        XCTAssertEqual(
            AgendaCloudRecord.stateRecordID(for: deleted).recordName,
            "tombstone-todo-deleted-r2"
        )
        XCTAssertFalse(
            try XCTUnwrap(entries.first { $0.stableID == "restored" }).deleted
        )
        XCTAssertTrue(
            try XCTUnwrap(entries.first { $0.stableID == "equal" }).deleted
        )
    }
}


final class AgendaCloudKitBatchingTests: XCTestCase {
    func testBatchesRespectCloudKitLimit() {
        let items = Array(0..<822)
        let batches = agendaBatches(items)
        XCTAssertEqual(batches.map(\.count), [400, 400, 22])
        XCTAssertEqual(batches.flatMap { $0 }, items)
    }

    func testBatchesHandleEmptyAndExactMultiple() {
        XCTAssertTrue(agendaBatches([Int]()).isEmpty)
        XCTAssertEqual(agendaBatches(Array(0..<800)).map(\.count), [400, 400])
    }
}
