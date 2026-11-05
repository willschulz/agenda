import XCTest
@testable import Agenda

final class StructurePlannerTests: XCTestCase {
    func testReordersCompleteSiblingGroupInTenPointGaps() throws {
        let updates = try AgendaStructurePlanner.updates(
            todos: [
                todo("a", order: 0),
                todo("b", order: 10),
                todo("c", order: 20),
            ],
            movedID: "c",
            intent: .before("a")
        )

        XCTAssertEqual(updates.map(\.stableID), ["c", "a", "b"])
        XCTAssertEqual(updates.map(\.displayOrder), [0, 10, 20])
    }

    func testRejectsIdenticalBeforeDropWithoutRenormalizing() {
        XCTAssertThrowsError(
            try AgendaStructurePlanner.plan(
                todos: [
                    todo("a", order: 0),
                    todo("b", order: 10),
                    todo("c", order: 20),
                ],
                movedID: "b",
                intent: .before("c")
            )
        ) { error in
            XCTAssertEqual(
                error as? AgendaStructurePlannerError,
                .noChange
            )
        }
    }

    func testRejectsDroppingTodoBeforeItself() {
        XCTAssertThrowsError(
            try AgendaStructurePlanner.plan(
                todos: [
                    todo("a", order: 0),
                    todo("b", order: 10),
                    todo("c", order: 20),
                ],
                movedID: "b",
                intent: .before("b")
            )
        ) { error in
            XCTAssertEqual(
                error as? AgendaStructurePlannerError,
                .noChange
            )
        }
    }

    func testReparentsAndIncludesOldAndNewSiblingGroups() throws {
        let updates = try AgendaStructurePlanner.updates(
            todos: [
                todo("a", order: 0),
                todo("b", order: 10),
                todo("child", parentID: "a", order: 0),
            ],
            movedID: "b",
            intent: .childOf("a")
        )

        XCTAssertEqual(Set(updates.map(\.stableID)), Set(["a", "b", "child"]))
        XCTAssertEqual(
            updates.first(where: { $0.stableID == "b" })?.parentStableID,
            "a"
        )
        XCTAssertEqual(
            updates.filter { $0.parentStableID == "a" }.map(\.displayOrder),
            [0, 10]
        )
    }

    func testRootLaterMoveCascadesToDescendants() throws {
        let plan = try AgendaStructurePlanner.plan(
            todos: [
                todo("root", order: 0),
                todo("child", parentID: "root", order: 0),
                todo("later", order: 10, forLater: true),
            ],
            movedID: "root",
            intent: .root(forLater: true)
        )
        let updates = plan.updates

        XCTAssertEqual(
            updates.first(where: { $0.stableID == "root" })?.forLater,
            true
        )
        // The server derives descendant inheritance from the projected root.
        XCTAssertFalse(updates.contains { $0.stableID == "child" })
        XCTAssertEqual(plan.pendingTodoIDs, Set(["root", "child"]))
    }

    func testVisibleActiveRootsExcludeLaterAndCompletedRows() throws {
        let updates = try AgendaStructurePlanner.updates(
            todos: [
                todo("a", order: 0),
                todo("b", order: 10),
                todo("later", order: 0, forLater: true),
                todo("done", order: 0, completed: true),
            ],
            movedID: "b",
            intent: .before("a")
        )

        XCTAssertEqual(updates.map(\.stableID), ["b", "a"])
        XCTAssertEqual(updates.map(\.displayOrder), [0, 10])
    }

    func testOpenChildCanRetainCompletedAncestor() throws {
        let updates = try AgendaStructurePlanner.updates(
            todos: [
                todo("done-parent", order: 0, completed: true),
                todo("child-a", parentID: "done-parent", order: 0),
                todo("child-b", parentID: "done-parent", order: 10),
            ],
            movedID: "child-b",
            intent: .before("child-a")
        )

        XCTAssertEqual(updates.map(\.stableID), ["child-b", "child-a"])
        XCTAssertTrue(updates.allSatisfy { $0.parentStableID == "done-parent" })
    }

    func testMovingRootBetweenSectionsNormalizesEachSectionIndependently() throws {
        let updates = try AgendaStructurePlanner.updates(
            todos: [
                todo("active-a", order: 0),
                todo("active-b", order: 10),
                todo("later-a", order: 0, forLater: true),
            ],
            movedID: "active-a",
            intent: .root(forLater: true)
        )

        XCTAssertEqual(
            updates.filter { !$0.forLater }.map(\.stableID),
            ["active-b"]
        )
        XCTAssertEqual(
            updates.filter(\.forLater).map(\.stableID),
            ["later-a", "active-a"]
        )
        XCTAssertEqual(
            updates.filter(\.forLater).map(\.displayOrder),
            [0, 10]
        )
    }

    func testRejectsCycleAndCrossProject() {
        let values = [
            todo("root"),
            todo("child", parentID: "root"),
            todo("other", projectID: "other"),
        ]
        XCTAssertThrowsError(
            try AgendaStructurePlanner.updates(
                todos: values,
                movedID: "root",
                intent: .childOf("child")
            )
        )
        XCTAssertThrowsError(
            try AgendaStructurePlanner.updates(
                todos: values,
                movedID: "root",
                intent: .before("other")
            )
        )
    }

    func testRetryRebasesMineOverMacAddedSibling() throws {
        let rebased = try AgendaStructurePlanner.rebasedUpdates(
            todos: [
                todo("a", order: 0),
                todo("new", order: 10),
                todo("b", order: 20),
            ],
            proposed: [
                AgendaStructureUpdate(
                    stableID: "b",
                    parentStableID: nil,
                    displayOrder: 0,
                    forLater: false
                ),
                AgendaStructureUpdate(
                    stableID: "a",
                    parentStableID: nil,
                    displayOrder: 10,
                    forLater: false
                ),
            ]
        )

        XCTAssertEqual(rebased.map(\.stableID), ["b", "a", "new"])
        XCTAssertEqual(rebased.map(\.displayOrder), [0, 10, 20])
    }

    func testRetryRejectsMissingMovedScopeButSkipsMissingOldSibling() throws {
        let proposed = [
            AgendaStructureUpdate(
                stableID: "b",
                parentStableID: nil,
                displayOrder: 0,
                forLater: false
            ),
            AgendaStructureUpdate(
                stableID: "a",
                parentStableID: nil,
                displayOrder: 10,
                forLater: false
            ),
        ]
        XCTAssertThrowsError(
            try AgendaStructurePlanner.rebasedUpdates(
                todos: [todo("a", order: 0)],
                proposed: proposed,
                requiredIDs: ["b"]
            )
        ) { error in
            XCTAssertEqual(
                error as? AgendaStructurePlannerError,
                .missingTodo
            )
        }
        XCTAssertThrowsError(
            try AgendaStructurePlanner.rebasedUpdates(
                todos: [
                    todo("a", order: 0),
                    todo("b", order: 10),
                ],
                proposed: proposed,
                requiredIDs: ["b", "missing-descendant"]
            )
        ) { error in
            XCTAssertEqual(
                error as? AgendaStructurePlannerError,
                .missingTodo
            )
        }

        let rebased = try AgendaStructurePlanner.rebasedUpdates(
            todos: [todo("b", order: 0)],
            proposed: proposed,
            requiredIDs: ["b"]
        )
        XCTAssertEqual(rebased.map(\.stableID), ["b"])
    }

    private func todo(
        _ id: String,
        projectID: String = "p",
        parentID: String? = nil,
        order: Int = 0,
        forLater: Bool = false,
        completed: Bool = false
    ) -> TodoViewValue {
        TodoViewValue(
            id: id,
            projectID: projectID,
            parentID: parentID,
            text: id,
            isBlocker: false,
            forLater: forLater,
            inProgress: false,
            flagged: false,
            awaitingReview: false,
            displayOrder: order,
            completed: completed,
            effort: nil,
            deadline: nil
        )
    }
}
