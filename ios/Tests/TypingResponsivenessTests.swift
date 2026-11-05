import AgendaSyncCore
import Combine
import XCTest
@testable import Agenda

final class TypingInvalidationGateTests: XCTestCase {
    func testInvalidationsCoalesceUntilLastTypingSessionEnds() {
        var gate = AgendaTypingInvalidationGate()

        gate.begin(sessionID: "new-todo")
        XCTAssertFalse(gate.receiveInvalidation())
        XCTAssertFalse(gate.receiveInvalidation())
        gate.begin(sessionID: "edit-todo")
        XCTAssertFalse(gate.end(sessionID: "new-todo"))
        XCTAssertTrue(gate.end(sessionID: "edit-todo"))
        XCTAssertFalse(gate.end(sessionID: "edit-todo"))
        XCTAssertTrue(gate.receiveInvalidation())
    }

    func testEndingCleanSessionDoesNotInventRefresh() {
        var gate = AgendaTypingInvalidationGate()

        gate.begin(sessionID: "new-todo")

        XCTAssertFalse(gate.end(sessionID: "new-todo"))
        XCTAssertFalse(gate.hasDeferredInvalidation)
    }
}

@MainActor
final class TypingPersistenceTests: XCTestCase {
    func testPendingCreateMembershipIsPublishedWithStoreSnapshot() throws {
        let store = MobileAgendaStore(inMemory: true)
        let todoID = "pending-create"
        try store.cachePendingMutation(
            AgendaMutationProposal(
                schemaVersion: agendaContractSchemaVersion,
                mutationID: "mutation-1",
                deviceID: "device",
                entityID: todoID,
                operation: .create,
                baseRevision: 0,
                proposedPayload: "{\"text\":\"Draft\"}",
                entityKind: .todo
            )
        )

        XCTAssertTrue(store.pendingCreateTodoIDs.contains(todoID))
        XCTAssertTrue(store.hasPendingCreate(todoID: todoID))
        XCTAssertFalse(store.hasPendingCreate(todoID: "other"))
        XCTAssertEqual(store.pendingMutationCount, 1)
        XCTAssertEqual(store.stalledPendingMutationCount, 0)
        XCTAssertNotNil(store.oldestPendingMutationAt)
    }

    func testUnknownAndOldPendingMutationsAreStalled() {
        let now = Date(timeIntervalSince1970: 1_786_888_800)
        let mutations = [
            proposal(id: "unknown"),
            proposal(id: "old"),
            proposal(id: "fresh"),
        ]

        let summary = MobileAgendaStore.pendingSummary(
            mutations: mutations,
            observedAt: [
                "old": now.addingTimeInterval(
                    -MobileAgendaStore.pendingStallInterval
                ),
                "fresh": now.addingTimeInterval(-30),
            ],
            now: now
        )

        XCTAssertEqual(summary.count, 3)
        XCTAssertEqual(summary.stalledCount, 2)
        XCTAssertEqual(
            summary.oldestObservedAt,
            now.addingTimeInterval(-MobileAgendaStore.pendingStallInterval)
        )
    }

    func testDraftBufferTypingDoesNotCreatePersistenceWork() {
        let store = MobileAgendaStore(inMemory: true)
        let buffer = AgendaTodoTextBuffer(text: "")

        for character in "A locally isolated draft" {
            buffer.text.append(character)
        }

        XCTAssertEqual(buffer.text, "A locally isolated draft")
        XCTAssertTrue(store.pendingTodoIDs.isEmpty)
        XCTAssertTrue(store.pendingCreateTodoIDs.isEmpty)
    }

    func testUnchangedBoardOrderDoesNotPublishAgain() {
        let store = MobileAgendaStore(inMemory: true)
        var publications = 0
        let observation = store.objectWillChange.sink {
            publications += 1
        }
        store.applyBoardOrder(
            AgendaBoardOrderSnapshot(
                generatedAt: "2026-08-15T12:00:00-04:00",
                rows: ["top_of_mind": ["project-a"]]
            )
        )
        store.applyBoardOrder(
            AgendaBoardOrderSnapshot(
                generatedAt: "2026-08-15T12:01:00-04:00",
                rows: ["top_of_mind": ["project-a"]]
            )
        )

        XCTAssertEqual(publications, 1)
        withExtendedLifetime(observation) {}
    }

    private func proposal(id: String) -> AgendaMutationProposal {
        AgendaMutationProposal(
            schemaVersion: agendaContractSchemaVersion,
            mutationID: id,
            deviceID: "device",
            entityID: "todo-\(id)",
            operation: .create,
            baseRevision: 0,
            proposedPayload: "{\"text\":\"Draft\"}",
            entityKind: .todo
        )
    }
}

final class BoardPresentationPerformanceTests: XCTestCase {
    func testRealisticCachedBoardBuildBenchmark() {
        let projectCount = 40
        let projects = (0..<projectCount).map { index in
            ProjectViewValue(
                id: "project-\(index)",
                name: "Project \(index)",
                category: AgendaCategory.allCases[
                    index % AgendaCategory.allCases.count
                ].rawValue,
                displayOrder: index * 10,
                northstars: []
            )
        }
        let todos = (0..<1_200).map { index in
            TodoViewValue(
                id: "todo-\(index)",
                projectID: "project-\(index % projectCount)",
                parentID: nil,
                text: "Todo \(index)",
                isBlocker: index.isMultiple(of: 17),
                forLater: index.isMultiple(of: 11),
                inProgress: index.isMultiple(of: 7),
                flagged: index.isMultiple(of: 13),
                awaitingReview: index.isMultiple(of: 19),
                displayOrder: index * 10,
                completed: index.isMultiple(of: 5),
                effort: index % 6,
                deadline: nil
            )
        }

        measure {
            let presentation = AgendaBoardPresentation(
                projects: projects,
                todos: todos
            )
            XCTAssertEqual(presentation.sections.count, AgendaCategory.allCases.count)
        }
    }
}
