import AgendaSyncCore
import CloudKit
import XCTest
@testable import Agenda

@MainActor
final class TombstonePersistenceTests: XCTestCase {
    func testTodoTombstoneIsSoftAndReplayIsIdempotent() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let records = try (0..<4).map { try AgendaCloudRecord.project(project($0)) }
            + (0..<4).map { try AgendaCloudRecord.todo(todo($0, project: $0)) }
        await store.applyFetchedCycle(cycle(records))
        XCTAssertEqual(store.todos.count, 4)

        let tombstone = try AgendaCloudRecord.tombstone(
            tombstone(.todo, id: "todo-0", projectID: "project-0")
        )
        await store.applyFetchedCycle(cycle([tombstone]))
        XCTAssertEqual(store.todos.count, 3)
        let state = try XCTUnwrap(
            store.cachedTombstoneState(entityKind: .todo, stableID: "todo-0")
        )
        XCTAssertNotNil(state.deletedAt)
        XCTAssertFalse(state.payload.isEmpty)

        await store.applyFetchedCycle(cycle([tombstone]))
        XCTAssertEqual(store.todos.count, 3)
        XCTAssertNil(store.lastError)
    }

    func testProjectRequiresExplicitChildTombstones() async throws {
        let store = MobileAgendaStore(inMemory: true)
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.project(project(0)),
                try AgendaCloudRecord.todo(todo(0, project: 0)),
                try AgendaCloudRecord.project(project(1)),
                try AgendaCloudRecord.project(project(2)),
                try AgendaCloudRecord.project(project(3)),
            ])
        )
        let accepted = await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.tombstone(
                    tombstone(.project, id: "project-0")
                )
            ])
        )
        XCTAssertFalse(accepted)
        XCTAssertEqual(store.projects.count, 4)
        XCTAssertTrue(store.lastError?.contains("explicit child") == true)
    }

    func testExactStateValidatorRejectsMismatchedPayloadIdentity() throws {
        let record = try AgendaCloudRecord.tombstone(
            tombstone(.todo, id: "actual", projectID: "project-0")
        )
        let entry = AgendaCanonicalManifestEntry(
            entityKind: .todo,
            stableID: "expected",
            revision: 1,
            deleted: true
        )

        XCTAssertThrowsError(
            try AgendaExactStateValidator.validate(record, for: entry)
        )

        let deletedCanonical = try AgendaCloudRecord.canonical(
            AgendaCanonicalRecordV2(
                entityKind: .todo,
                stableID: "expected",
                revision: 1,
                deleted: true,
                payloadJSON: "{}"
            )
        )
        XCTAssertThrowsError(
            try AgendaExactStateValidator.validate(
                deletedCanonical,
                for: AgendaCanonicalManifestEntry(
                    entityKind: .todo,
                    stableID: "expected",
                    revision: 1
                )
            )
        )
    }

    func testExactCapAcceptedAndOverCapRejectedAtomically() async throws {
        let acceptedStore = MobileAgendaStore(inMemory: true)
        let projectRecords = try (0..<20).map {
            try AgendaCloudRecord.project(project($0))
        }
        await acceptedStore.applyFetchedCycle(cycle(projectRecords))
        let exactCap = try (0..<5).map {
            try AgendaCloudRecord.tombstone(
                tombstone(.project, id: "project-\($0)")
            )
        }
        await acceptedStore.applyFetchedCycle(cycle(exactCap))
        XCTAssertEqual(acceptedStore.projects.count, 15)

        let rejectedStore = MobileAgendaStore(inMemory: true)
        await rejectedStore.applyFetchedCycle(cycle(projectRecords))
        let overCap = try (0..<6).map {
            try AgendaCloudRecord.tombstone(
                tombstone(.project, id: "project-\($0)")
            )
        }
        await rejectedStore.applyFetchedCycle(cycle(overCap))
        XCTAssertEqual(rejectedStore.projects.count, 20)
        XCTAssertTrue(rejectedStore.lastError?.contains("collapse_guard") == true)
    }

    func testOverCapTodoDeletionsAreHeldForReviewThenApplied() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let projectRecord = try AgendaCloudRecord.project(project(0))
        let todoRecords = try (0..<200).map {
            try AgendaCloudRecord.todo(todo($0, project: 0))
        }
        await store.applyFetchedCycle(cycle([projectRecord] + todoRecords))
        XCTAssertEqual(store.todos.count, 200)

        let deletions = try (0..<60).map {
            try AgendaCloudRecord.tombstone(
                tombstone(.todo, id: "todo-\($0)", projectID: "project-0")
            )
        }
        let accepted = await store.applyFetchedCycle(cycle(deletions))
        XCTAssertFalse(accepted)
        XCTAssertEqual(store.todos.count, 200)
        let held = try XCTUnwrap(store.heldDeletionBatch)
        XCTAssertEqual(held.todoCount, 60)
        XCTAssertEqual(held.projectCount, 0)
        XCTAssertTrue(store.lastError?.contains("collapse_guard") == true)
        let items = store.heldDeletionItems()
        XCTAssertEqual(items.count, 60)
        XCTAssertEqual(Set(items.map(\.projectName)), ["Project 0"])

        // Replaying the same cycle keeps the original hold.
        await store.applyFetchedCycle(cycle(deletions))
        XCTAssertEqual(store.heldDeletionBatch?.heldAt, held.heldAt)

        XCTAssertFalse(store.approveHeldDeletions(fingerprint: "stale"))
        XCTAssertEqual(store.todos.count, 200)

        XCTAssertTrue(store.approveHeldDeletions(fingerprint: held.fingerprint))
        XCTAssertEqual(store.todos.count, 140)
        XCTAssertNil(store.heldDeletionBatch)
        XCTAssertNil(store.lastError)

        // A later replay of the applied batch is idempotent.
        let replayed = await store.applyFetchedCycle(cycle(deletions))
        XCTAssertTrue(replayed)
        XCTAssertEqual(store.todos.count, 140)
    }

    func testMissingBaselineAndPhysicalDeletionFailClosed() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let projectRecord = try AgendaCloudRecord.project(project(0))
        await store.recordSavesSucceeded([projectRecord])
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.tombstone(
                    tombstone(.project, id: "project-0")
                )
            ])
        )
        XCTAssertEqual(store.projects.count, 1)
        XCTAssertTrue(store.lastError?.contains("baseline_required") == true)
        XCTAssertNil(store.heldDeletionBatch)

        await store.applyFetchedCycle(cycle([projectRecord]))
        await store.applyFetchedCycle(
            AgendaCloudFetchCycle(
                records: [
                    try AgendaCloudRecord.tombstone(
                        tombstone(.project, id: "project-0")
                    )
                ],
                physicalDeletions: [
                    CKRecord.ID(
                        recordName: "project-0",
                        zoneID: AgendaCloudRecord.zoneID
                    )
                ],
                zoneDeleted: false
            )
        )
        XCTAssertEqual(store.projects.count, 1)
        XCTAssertTrue(store.lastError?.contains("Physical") == true)
    }

    func testZoneDeletionAndUnknownTombstoneSchemaFailClosed() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let projectRecord = try AgendaCloudRecord.project(project(0))
        await store.applyFetchedCycle(cycle([projectRecord]))
        await store.applyFetchedCycle(
            AgendaCloudFetchCycle(
                records: [],
                physicalDeletions: [],
                zoneDeleted: true
            )
        )
        XCTAssertEqual(store.projects.count, 1)
        XCTAssertTrue(store.lastError?.contains("zone deletion") == true)

        let id = CKRecord.ID(
            recordName: "future-tombstone",
            zoneID: AgendaCloudRecord.zoneID
        )
        let future = CKRecord(
            recordType: AgendaCloudRecord.tombstoneType,
            recordID: id
        )
        future[AgendaCloudRecord.schemaVersionKey] = 3 as CKRecordValue
        future[AgendaCloudRecord.payloadKey] = Data("{}".utf8) as CKRecordValue
        await store.applyFetchedCycle(cycle([future]))
        XCTAssertEqual(store.projects.count, 1)
        XCTAssertTrue(store.lastError?.contains("newer or unreadable") == true)
    }

    func testTombstoneForAbsentEntitySuppressesLaterSchemaOneReplay() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let tombstoneRecord = try AgendaCloudRecord.tombstone(
            tombstone(.project, id: "project-0")
        )
        await store.applyFetchedCycle(cycle([tombstoneRecord]))
        XCTAssertEqual(store.projects.count, 0)
        XCTAssertNil(store.lastError)
        XCTAssertTrue(
            store.hasTombstoneSuppression(
                entityKind: .project,
                stableID: "project-0"
            )
        )

        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.project(project(0))])
        )
        XCTAssertEqual(store.projects.count, 0)
    }

    func testPendingMutationSurvivesSendAndResultUntilCanonicalArrives() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let mutation = AgendaMutationProposal(
            schemaVersion: 2,
            mutationID: "mutation-1",
            deviceID: "device-1",
            entityID: "todo-1",
            operation: .scalarEdit,
            baseRevision: 0,
            proposedPayload: #"{"text":"After"}"#
        )
        try store.cachePendingMutation(mutation)
        let mutationRecord = try AgendaCloudRecord.mutation(mutation)
        try await store.persistPendingRecords([mutationRecord])
        let pendingBeforeAck = try await store.loadPendingRecords()
        XCTAssertEqual(pendingBeforeAck.count, 1)
        await store.removePendingRecordIDs([mutationRecord.recordID])
        let pendingAfterAck = try await store.loadPendingRecords()
        XCTAssertEqual(pendingAfterAck.count, 0)
        XCTAssertTrue(store.isMutationPending("mutation-1"))

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
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.result(result)])
        )
        XCTAssertTrue(store.isMutationPending("mutation-1"))
    }

    func testBodylessProposalReconstructsOriginalMutationForReplay() throws {
        let store = MobileAgendaStore(inMemory: true)
        let mutation = AgendaMutationProposal(
            schemaVersion: 2,
            mutationID: "mutation-bodyless",
            deviceID: "device-1",
            entityID: "todo-bodyless",
            operation: .create,
            baseRevision: 0,
            proposedPayload:
                #"{"project_stable_id":"project-0","text":"Replay exactly"}"#,
            entityKind: .todo
        )
        try store.cachePendingMutation(mutation)

        let replay = try XCTUnwrap(store.pendingMutationRecords().first)

        XCTAssertEqual(try AgendaCloudRecord.decodeMutation(replay), mutation)
        XCTAssertEqual(
            AgendaSyncContract.fingerprint(
                mutation: try AgendaCloudRecord.decodeMutation(replay)
            ),
            AgendaSyncContract.fingerprint(mutation: mutation)
        )
    }

    func testFingerprintMismatchCannotClearPendingProposal() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let mutation = AgendaMutationProposal(
            schemaVersion: 2,
            mutationID: "mutation-mismatch",
            deviceID: "device-1",
            entityID: "todo-mismatch",
            operation: .create,
            baseRevision: 0,
            proposedPayload: #"{"text":"Keep pending"}"#,
            entityKind: .todo
        )
        try store.cachePendingMutation(mutation)
        let mismatched = AgendaMutationResultRecord(
            schemaVersion: 2,
            mutationID: mutation.mutationID,
            mutationFingerprint: "not-the-proposal-fingerprint",
            deviceID: mutation.deviceID,
            entityID: mutation.entityID,
            outcome: .accept,
            reason: "accepted",
            canonicalRevision: 0,
            canonicalStructureRevision: 0
        )

        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.result(mismatched)])
        )

        XCTAssertTrue(store.isMutationPending(mutation.mutationID))
        XCTAssertEqual(store.pendingMutationCount, 1)
    }

    func testOfflinePendingCreateEditsReplaceDurableEnvelope() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let originalRecord = try store.makeCreateTodoMutation(
            projectID: "project-1",
            text: "Original"
        )
        let original = try AgendaCloudRecord.decodeMutation(originalRecord)
        try await store.persistPendingRecords([originalRecord])

        let intermediate = try XCTUnwrap(
            store.replacePendingCreateText(
                todoID: original.entityID,
                text: "Intermediate"
            )
        )
        XCTAssertEqual(intermediate.replacedRecordID, originalRecord.recordID)
        XCTAssertFalse(store.isMutationPending(original.mutationID))

        let final = try XCTUnwrap(
            store.replacePendingCreateText(
                todoID: original.entityID,
                text: "Final"
            )
        )
        let finalMutation = try AgendaCloudRecord.decodeMutation(final.record)
        let durable = try await store.loadPendingRecords()
        XCTAssertEqual(durable.count, 1)
        XCTAssertEqual(durable.first?.recordID, final.record.recordID)
        XCTAssertTrue(store.isMutationPending(finalMutation.mutationID))
        XCTAssertEqual(store.todos.first?.text, "Final")
    }

    func testAccountChangeQuarantinesUntilIdentityIsReestablished() async throws {
        let store = MobileAgendaStore(inMemory: true)
        await store.accountAvailable(accountMarker: "account-a")
        await store.accountChanged()
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.project(project(0))])
        )
        XCTAssertEqual(store.projects.count, 0)
        await store.accountAvailable(accountMarker: "account-b")
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.project(project(0))])
        )
        XCTAssertEqual(store.projects.count, 0)
        await store.accountAvailable(accountMarker: "account-a")
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.project(project(0))])
        )
        XCTAssertEqual(store.projects.count, 1)
    }

    func testSchemaTwoCanonicalResurrectionSupersedesTombstone() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let projectRecords = try (0..<4).map {
            try AgendaCloudRecord.project(project($0))
        }
        await store.applyFetchedCycle(cycle(projectRecords))
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.tombstone(
                    tombstone(.project, id: "project-0")
                )
            ])
        )
        XCTAssertEqual(store.projects.count, 3)
        let payload = try JSONEncoder().encode(project(0))
        let canonical = AgendaCanonicalRecordV2(
            entityKind: .project,
            stableID: "project-0",
            revision: 2,
            payloadJSON: String(decoding: payload, as: UTF8.self)
        )
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.canonical(canonical)])
        )
        XCTAssertEqual(store.projects.count, 4)
        XCTAssertFalse(
            store.hasTombstoneSuppression(
                entityKind: .project,
                stableID: "project-0"
            )
        )
    }

    func testOptimisticCreateWaitsForMatchingCanonical() async throws {
        let store = MobileAgendaStore(inMemory: true)
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.project(project(0))])
        )
        let mutationRecord = try store.makeCreateTodoMutation(
            projectID: "project-0",
            text: "Created offline"
        )
        let mutation = try AgendaCloudRecord.decodeMutation(mutationRecord)
        XCTAssertEqual(store.todos.first?.text, "Created offline")
        XCTAssertTrue(store.pendingTodoIDs.contains(mutation.entityID))

        let result = AgendaMutationResultRecord(
            schemaVersion: 2,
            mutationID: mutation.mutationID,
            mutationFingerprint: AgendaSyncContract.fingerprint(
                mutation: mutation
            ),
            deviceID: mutation.deviceID,
            entityID: mutation.entityID,
            outcome: .accept,
            reason: "accepted",
            canonicalRevision: 0,
            canonicalStructureRevision: 0
        )
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.result(result)])
        )
        XCTAssertTrue(store.pendingTodoIDs.contains(mutation.entityID))

        let canonicalTodo = try todoRecord(
            id: mutation.entityID,
            projectID: "project-0",
            text: "Created offline"
        )
        let payload = try JSONEncoder().encode(canonicalTodo)
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.canonical(
                    AgendaCanonicalRecordV2(
                        entityKind: .todo,
                        stableID: mutation.entityID,
                        revision: 0,
                        payloadJSON: String(decoding: payload, as: UTF8.self)
                    )
                )
            ])
        )
        XCTAssertFalse(store.pendingTodoIDs.contains(mutation.entityID))
        XCTAssertEqual(store.todos.count, 1)
        XCTAssertEqual(store.pendingMutationCount, 0)

        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.result(result),
                try AgendaCloudRecord.canonical(
                    AgendaCanonicalRecordV2(
                        entityKind: .todo,
                        stableID: mutation.entityID,
                        revision: 0,
                        payloadJSON: String(decoding: payload, as: UTF8.self)
                    )
                ),
            ])
        )
        XCTAssertFalse(store.isMutationPending(mutation.mutationID))
        XCTAssertEqual(store.todos.count, 1)
    }

    func testFullStatusMutationOverlaysOptimistically() async throws {
        let store = MobileAgendaStore(inMemory: true)
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.project(project(0)),
                try AgendaCloudRecord.todo(todo(0, project: 0)),
            ])
        )
        var values = AgendaTodoEditValues(todo: try XCTUnwrap(store.todos.first))
        values.text = "Status draft"
        values.isBlocker = true
        values.forLater = true
        values.inProgress = true
        values.flagged = true
        values.awaitingReview = true
        values.effort = 8
        values.deadline = "2026-08-31"
        _ = try store.makeTodoUpdateMutation(todoID: "todo-0", values: values)

        let optimistic = try XCTUnwrap(store.todos.first)
        XCTAssertEqual(optimistic.text, "Status draft")
        XCTAssertTrue(optimistic.isBlocker)
        XCTAssertTrue(optimistic.forLater)
        XCTAssertTrue(optimistic.inProgress)
        XCTAssertTrue(optimistic.flagged)
        XCTAssertTrue(optimistic.awaitingReview)
        XCTAssertEqual(optimistic.effort, 8)
        XCTAssertEqual(optimistic.deadline, "2026-08-31")
        XCTAssertTrue(store.pendingTodoIDs.contains("todo-0"))
    }

    func testCompletionOverlayClearsActiveStatusesAndNullableMetadata() async throws {
        let store = MobileAgendaStore(inMemory: true)
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.project(project(0)),
                try AgendaCloudRecord.todo(todo(0, project: 0)),
            ])
        )
        var values = AgendaTodoEditValues(todo: try XCTUnwrap(store.todos.first))
        values.completed = true
        values.inProgress = true
        values.flagged = true
        values.awaitingReview = true
        values.effort = nil
        values.deadline = nil
        let record = try store.makeTodoUpdateMutation(
            todoID: "todo-0",
            values: values
        )
        let mutation = try AgendaCloudRecord.decodeMutation(record)
        let payloadData = try XCTUnwrap(
            mutation.proposedPayload?.data(using: .utf8)
        )
        let payload = try XCTUnwrap(
            JSONSerialization.jsonObject(with: payloadData) as? [String: Any]
        )
        let optimistic = try XCTUnwrap(store.todos.first)

        XCTAssertTrue(optimistic.completed)
        XCTAssertFalse(optimistic.inProgress)
        XCTAssertFalse(optimistic.flagged)
        XCTAssertFalse(optimistic.awaitingReview)
        XCTAssertNil(optimistic.effort)
        XCTAssertNil(optimistic.deadline)
        XCTAssertTrue(payload["effort"] is NSNull)
        XCTAssertTrue(payload["deadline"] is NSNull)
    }

    func testConflictShowsBothValuesAndKeepMacDismisses() async throws {
        let store = MobileAgendaStore(inMemory: true)
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.project(project(0)),
                try AgendaCloudRecord.todo(todo(0, project: 0)),
            ])
        )
        let record = try store.makeEditTodoMutation(
            todoID: "todo-0",
            text: "Mine"
        )
        let mutation = try AgendaCloudRecord.decodeMutation(record)
        let result = AgendaMutationResultRecord(
            schemaVersion: 2,
            mutationID: mutation.mutationID,
            mutationFingerprint: AgendaSyncContract.fingerprint(
                mutation: mutation
            ),
            deviceID: mutation.deviceID,
            entityID: mutation.entityID,
            outcome: .conflict,
            reason: "stale_revision",
            canonicalRevision: 1,
            canonicalStructureRevision: 0
        )
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.result(result)])
        )
        let conflict = try XCTUnwrap(store.conflicts.first)
        XCTAssertEqual(conflict.macValue, "Todo 0")
        XCTAssertEqual(conflict.proposedValue, "Mine")
        store.keepMacValue(conflictID: conflict.id)
        XCTAssertTrue(store.conflicts.isEmpty)
    }

    func testDeletedTodoConflictRetainsOwningProject() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let records = try (0..<4).map {
            try AgendaCloudRecord.project(project($0))
        } + (0..<4).map {
            try AgendaCloudRecord.todo(todo($0, project: $0))
        }
        await store.applyFetchedCycle(cycle(records))
        let record = try store.makeEditTodoMutation(
            todoID: "todo-0",
            text: "Mine"
        )
        let mutation = try AgendaCloudRecord.decodeMutation(record)
        let result = AgendaMutationResultRecord(
            schemaVersion: 2,
            mutationID: mutation.mutationID,
            mutationFingerprint: AgendaSyncContract.fingerprint(
                mutation: mutation
            ),
            deviceID: mutation.deviceID,
            entityID: mutation.entityID,
            outcome: .conflict,
            reason: "entity_deleted",
            canonicalRevision: 1,
            canonicalStructureRevision: 0
        )
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.result(result),
                try AgendaCloudRecord.tombstone(
                    tombstone(.todo, id: "todo-0", projectID: "project-0")
                ),
            ])
        )

        XCTAssertFalse(store.todos.contains { $0.id == "todo-0" })
        let conflict = try XCTUnwrap(store.conflicts.first)
        XCTAssertEqual(conflict.projectID, "project-0")
        XCTAssertEqual(
            conflict.reasonSummary,
            "The Mac deleted this todo before the edit arrived."
        )
    }

    func testStatusConflictShowsFieldLevelMacAndProposedValues() async throws {
        let store = MobileAgendaStore(inMemory: true)
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.project(project(0)),
                try AgendaCloudRecord.todo(todo(0, project: 0)),
            ])
        )
        var values = AgendaTodoEditValues(todo: try XCTUnwrap(store.todos.first))
        values.flagged = true
        values.effort = 13
        values.deadline = "2026-09-01"
        let record = try store.makeTodoUpdateMutation(
            todoID: "todo-0",
            values: values
        )
        let mutation = try AgendaCloudRecord.decodeMutation(record)
        let result = AgendaMutationResultRecord(
            schemaVersion: 2,
            mutationID: mutation.mutationID,
            mutationFingerprint: AgendaSyncContract.fingerprint(
                mutation: mutation
            ),
            deviceID: mutation.deviceID,
            entityID: mutation.entityID,
            outcome: .conflict,
            reason: "stale_revision",
            canonicalRevision: 1,
            canonicalStructureRevision: 0
        )
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.result(result)])
        )
        let conflict = try XCTUnwrap(store.conflicts.first)
        XCTAssertTrue(conflict.macValue.contains("Flagged: No"))
        XCTAssertTrue(conflict.proposedValue.contains("Flagged: Yes"))
        XCTAssertTrue(conflict.macValue.contains("Effort: None"))
        XCTAssertTrue(conflict.proposedValue.contains("Effort: 13"))
        XCTAssertTrue(conflict.proposedValue.contains("Deadline: 2026-09-01"))
    }

    func testStructureMutationWaitsForProjectCanonicalAndKeepsOverlay() async throws {
        let store = MobileAgendaStore(inMemory: true)
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.project(project(0)),
                try AgendaCloudRecord.todo(todo(0, project: 0)),
                try AgendaCloudRecord.todo(todo(1, project: 0)),
            ])
        )
        let record = try store.makeStructureMutation(
            projectID: "project-0",
            updates: [
                AgendaStructureUpdate(
                    stableID: "todo-1",
                    parentStableID: nil,
                    displayOrder: 0,
                    forLater: false
                ),
                AgendaStructureUpdate(
                    stableID: "todo-0",
                    parentStableID: nil,
                    displayOrder: 10,
                    forLater: false
                ),
            ],
            pendingTodoIDs: ["todo-1"]
        )
        let mutation = try AgendaCloudRecord.decodeMutation(record)
        XCTAssertEqual(store.todos.sorted { $0.displayOrder < $1.displayOrder }.map(\.id), [
            "todo-1", "todo-0",
        ])
        XCTAssertTrue(store.pendingProjectIDs.contains("project-0"))
        XCTAssertEqual(store.pendingTodoIDs, Set(["todo-1"]))
        XCTAssertThrowsError(
            try store.makeStructureMutation(
                projectID: "project-0",
                updates: [],
                pendingTodoIDs: ["todo-0"]
            )
        )

        let result = AgendaMutationResultRecord(
            schemaVersion: 2,
            mutationID: mutation.mutationID,
            mutationFingerprint: AgendaSyncContract.fingerprint(mutation: mutation),
            deviceID: mutation.deviceID,
            entityID: mutation.entityID,
            outcome: .accept,
            reason: "accepted",
            canonicalRevision: 1,
            canonicalStructureRevision: 1
        )
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.result(result)])
        )
        XCTAssertTrue(store.pendingProjectIDs.contains("project-0"))

        var projectPayload = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: JSONEncoder().encode(project(0))
            ) as? [String: Any]
        )
        projectPayload["structure"] = [
            [
                "stable_id": "todo-1",
                "parent_stable_id": NSNull(),
                "display_order": 0,
                "for_later": false,
            ],
            [
                "stable_id": "todo-0",
                "parent_stable_id": NSNull(),
                "display_order": 10,
                "for_later": false,
            ],
        ]
        let canonical = AgendaCanonicalRecordV2(
            entityKind: .project,
            stableID: "project-0",
            revision: 1,
            structureRevision: 1,
            payloadJSON: String(
                decoding: try JSONSerialization.data(withJSONObject: projectPayload),
                as: UTF8.self
            )
        )
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.canonical(canonical)])
        )
        XCTAssertFalse(store.pendingProjectIDs.contains("project-0"))
        XCTAssertEqual(store.todos.sorted { $0.displayOrder < $1.displayOrder }.map(\.id), [
            "todo-1", "todo-0",
        ])
    }

    func testManifestRefetchesWhenOnlyStructureRevisionLags() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let value = AgendaProjectRecord(
            stableID: "project-0",
            name: "Project 0",
            category: "side_project",
            displayOrder: 0,
            structureRevision: 4,
            notes: [],
            northstars: [],
            archived: false,
            createdAt: "2026-08-09T19:00:00+00:00",
            updatedAt: "2026-08-09T19:00:00+00:00"
        )
        let canonical = AgendaCanonicalRecordV2(
            entityKind: .project,
            stableID: "project-0",
            revision: 5,
            structureRevision: 4,
            payloadJSON: String(
                decoding: try JSONEncoder().encode(value),
                as: UTF8.self
            )
        )
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.canonical(canonical)])
        )

        let needed = store.canonicalRecordsNeeded([
            AgendaCanonicalManifestEntry(
                entityKind: .project,
                stableID: "project-0",
                revision: 5,
                structureRevision: 5
            ),
        ])
        XCTAssertEqual(needed.map(\.stableID), ["project-0"])
    }

    func testManifestRefetchesMissingTombstoneExactlyOnce() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let records = try (0..<4).map {
            try AgendaCloudRecord.project(project($0))
        } + (0..<4).map {
            try AgendaCloudRecord.todo(todo($0, project: $0))
        }
        await store.applyFetchedCycle(cycle(records))
        let entry = AgendaCanonicalManifestEntry(
            entityKind: .todo,
            stableID: "todo-0",
            revision: 1,
            deleted: true
        )

        XCTAssertEqual(
            store.canonicalRecordsNeeded([entry]).map(\.stableID),
            ["todo-0"]
        )
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.tombstone(
                    tombstone(.todo, id: "todo-0", projectID: "project-0")
                )
            ])
        )
        XCTAssertTrue(store.canonicalRecordsNeeded([entry]).isEmpty)
        XCTAssertFalse(store.todos.contains { $0.id == "todo-0" })
    }

    func testStructuralConflictUsesMovedScopeAndReason() async throws {
        let store = MobileAgendaStore(inMemory: true)
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.project(project(0)),
                try AgendaCloudRecord.todo(todo(0, project: 0)),
                try AgendaCloudRecord.todo(todo(1, project: 0)),
            ])
        )
        let record = try store.makeStructureMutation(
            projectID: "project-0",
            updates: [
                AgendaStructureUpdate(
                    stableID: "todo-1",
                    parentStableID: nil,
                    displayOrder: 0,
                    forLater: false
                ),
                AgendaStructureUpdate(
                    stableID: "todo-0",
                    parentStableID: nil,
                    displayOrder: 10,
                    forLater: false
                ),
            ],
            pendingTodoIDs: ["todo-1"]
        )
        let mutation = try AgendaCloudRecord.decodeMutation(record)
        let result = AgendaMutationResultRecord(
            schemaVersion: 2,
            mutationID: mutation.mutationID,
            mutationFingerprint: AgendaSyncContract.fingerprint(
                mutation: mutation
            ),
            deviceID: mutation.deviceID,
            entityID: mutation.entityID,
            outcome: .conflict,
            reason: "invalid_payload",
            canonicalRevision: 0,
            canonicalStructureRevision: 0
        )
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.result(result)])
        )

        let conflict = try XCTUnwrap(store.conflicts.first)
        XCTAssertEqual(conflict.reason, "invalid_payload")
        XCTAssertTrue(conflict.macValue.contains("Todo 1"))
        XCTAssertFalse(conflict.macValue.contains("Todo 0"))
        XCTAssertTrue(conflict.proposedValue.contains("Todo 1"))
        XCTAssertFalse(conflict.proposedValue.contains("Todo 0"))
    }

    func testDeleteOptimisticallySuppressesSubtreeUntilExplicitTombstones() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let records = try (0..<4).map { try AgendaCloudRecord.project(project($0)) }
            + [
                try AgendaCloudRecord.todo(
                    todoRecord(id: "root", projectID: "project-0", text: "Root")
                ),
                try AgendaCloudRecord.todo(
                    todoRecord(
                        id: "child",
                        projectID: "project-0",
                        text: "Child",
                        parentID: "root"
                    )
                ),
            ]
            + (1..<4).map { try AgendaCloudRecord.todo(todo($0, project: $0)) }
        await store.applyFetchedCycle(cycle(records))
        let record = try store.makeDeleteTodoMutation(todoID: "root")
        let mutation = try AgendaCloudRecord.decodeMutation(record)
        XCTAssertFalse(store.todos.contains { $0.id == "root" || $0.id == "child" })

        let result = AgendaMutationResultRecord(
            schemaVersion: 2,
            mutationID: mutation.mutationID,
            mutationFingerprint: AgendaSyncContract.fingerprint(mutation: mutation),
            deviceID: mutation.deviceID,
            entityID: mutation.entityID,
            outcome: .accept,
            reason: "accepted",
            canonicalRevision: 1,
            canonicalStructureRevision: 0
        )
        let rootTombstone = AgendaTombstoneRecord(
            entityKind: .todo,
            stableID: "root",
            projectStableID: "project-0",
            revision: 1,
            deletedAt: "2026-08-13T20:00:00+00:00"
        )
        let childTombstone = AgendaTombstoneRecord(
            entityKind: .todo,
            stableID: "child",
            projectStableID: "project-0",
            parentStableID: "root",
            revision: 1,
            deletedAt: "2026-08-13T20:00:00+00:00"
        )
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.result(result),
                try AgendaCloudRecord.tombstone(rootTombstone),
                try AgendaCloudRecord.tombstone(childTombstone),
            ])
        )
        XCTAssertFalse(store.isMutationPending(mutation.mutationID))
        XCTAssertFalse(store.todos.contains { $0.id == "root" || $0.id == "child" })
    }

    func testParentDeleteTrustIgnoresPreviouslyTombstonedChild() async throws {
        let store = MobileAgendaStore(inMemory: true)
        let records = try (0..<4).map { try AgendaCloudRecord.project(project($0)) }
            + [
                try AgendaCloudRecord.todo(
                    todoRecord(id: "root", projectID: "project-0", text: "Root")
                ),
                try AgendaCloudRecord.todo(
                    todoRecord(
                        id: "child",
                        projectID: "project-0",
                        text: "Child",
                        parentID: "root"
                    )
                ),
                try AgendaCloudRecord.todo(
                    todoRecord(id: "other", projectID: "project-0", text: "Other")
                ),
            ]
        await store.applyFetchedCycle(cycle(records))

        let childRecord = try store.makeDeleteTodoMutation(todoID: "child")
        let childMutation = try AgendaCloudRecord.decodeMutation(childRecord)
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.result(
                    acceptedDeleteResult(childMutation)
                ),
                try AgendaCloudRecord.tombstone(
                    AgendaTombstoneRecord(
                        entityKind: .todo,
                        stableID: "child",
                        projectStableID: "project-0",
                        parentStableID: "root",
                        revision: 1,
                        deletedAt: "2026-08-13T20:00:00+00:00"
                    )
                ),
            ])
        )

        let rootRecord = try store.makeDeleteTodoMutation(todoID: "root")
        let rootMutation = try AgendaCloudRecord.decodeMutation(rootRecord)
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.result(
                    acceptedDeleteResult(rootMutation)
                ),
                try AgendaCloudRecord.tombstone(
                    AgendaTombstoneRecord(
                        entityKind: .todo,
                        stableID: "root",
                        projectStableID: "project-0",
                        revision: 1,
                        deletedAt: "2026-08-13T20:01:00+00:00"
                    )
                ),
            ])
        )

        XCTAssertFalse(store.isMutationPending(rootMutation.mutationID))
        XCTAssertFalse(store.todos.contains { $0.id == "root" })
        XCTAssertNil(store.lastError)
    }

    func testRejectedDeleteRestoresOptimisticSubtreeAndShowsConflict() async throws {
        let store = MobileAgendaStore(inMemory: true)
        await store.applyFetchedCycle(
            cycle([
                try AgendaCloudRecord.project(project(0)),
                try AgendaCloudRecord.todo(todo(0, project: 0)),
            ])
        )
        let record = try store.makeDeleteTodoMutation(todoID: "todo-0")
        let mutation = try AgendaCloudRecord.decodeMutation(record)
        XCTAssertTrue(store.todos.isEmpty)

        let result = AgendaMutationResultRecord(
            schemaVersion: 2,
            mutationID: mutation.mutationID,
            mutationFingerprint: AgendaSyncContract.fingerprint(mutation: mutation),
            deviceID: mutation.deviceID,
            entityID: mutation.entityID,
            outcome: .conflict,
            reason: "stale_revision",
            canonicalRevision: 1,
            canonicalStructureRevision: 0
        )
        await store.applyFetchedCycle(
            cycle([try AgendaCloudRecord.result(result)])
        )
        XCTAssertEqual(store.todos.map(\.id), ["todo-0"])
        XCTAssertTrue(store.conflicts.first?.proposedValue.contains("Delete") == true)
    }

    private func cycle(_ records: [CKRecord]) -> AgendaCloudFetchCycle {
        AgendaCloudFetchCycle(
            records: records,
            physicalDeletions: [],
            zoneDeleted: false
        )
    }

    private func acceptedDeleteResult(
        _ mutation: AgendaMutationProposal
    ) -> AgendaMutationResultRecord {
        AgendaMutationResultRecord(
            schemaVersion: 2,
            mutationID: mutation.mutationID,
            mutationFingerprint: AgendaSyncContract.fingerprint(mutation: mutation),
            deviceID: mutation.deviceID,
            entityID: mutation.entityID,
            outcome: .accept,
            reason: "accepted",
            canonicalRevision: 1,
            canonicalStructureRevision: 0
        )
    }

    private func project(_ index: Int) throws -> AgendaProjectRecord {
        let data = """
        {
          "stable_id":"project-\(index)","name":"Project \(index)",
          "category":"side_project","display_order":\(index),
          "notes":[],"northstars":[],"archived":false,
          "created_at":"2026-08-09T19:00:00+00:00",
          "updated_at":"2026-08-09T19:00:00+00:00"
        }
        """.data(using: .utf8)!
        return try JSONDecoder().decode(AgendaProjectRecord.self, from: data)
    }

    private func todo(_ index: Int, project: Int) throws -> AgendaTodoRecord {
        let data = """
        {
          "stable_id":"todo-\(index)","project_stable_id":"project-\(project)",
          "parent_stable_id":null,"text":"Todo \(index)","is_blocker":false,
          "for_later":false,"in_progress":false,"flagged":false,
          "awaiting_review":false,"display_order":\(index),"completed":false,
          "completed_at":null,"notes":[],"effort":null,"deadline":null,
          "created_at":"2026-08-09T19:00:00+00:00",
          "updated_at":"2026-08-09T19:00:00+00:00"
        }
        """.data(using: .utf8)!
        return try JSONDecoder().decode(AgendaTodoRecord.self, from: data)
    }

    private func todoRecord(
        id: String,
        projectID: String,
        text: String,
        parentID: String? = nil
    ) throws -> AgendaTodoRecord {
        let value: [String: Any] = [
            "stable_id": id,
            "project_stable_id": projectID,
            "parent_stable_id": parentID ?? NSNull(),
            "text": text,
            "is_blocker": false,
            "for_later": false,
            "in_progress": false,
            "flagged": false,
            "awaiting_review": false,
            "display_order": 0,
            "completed": false,
            "completed_at": NSNull(),
            "notes": [],
            "effort": NSNull(),
            "deadline": NSNull(),
            "created_at": "2026-08-09T19:00:00+00:00",
            "updated_at": "2026-08-09T19:00:00+00:00",
        ]
        return try JSONDecoder().decode(
            AgendaTodoRecord.self,
            from: JSONSerialization.data(withJSONObject: value)
        )
    }

    private func tombstone(
        _ kind: AgendaContractEntityKind,
        id: String,
        projectID: String? = nil
    ) -> AgendaTombstoneRecord {
        AgendaTombstoneRecord(
            entityKind: kind,
            stableID: id,
            projectStableID: projectID,
            revision: 1,
            deletedAt: "2026-08-09T20:00:00+00:00"
        )
    }
}
