import XCTest
@testable import Agenda

final class BoardPresentationTests: XCTestCase {
    func testCanonicalCategoryOrderAndProjectOrder() {
        let presentation = AgendaBoardPresentation(
            projects: [
                project("future", category: "future_ideas", order: 0),
                project("development-b", category: "in_development", order: 20),
                project("top", category: "top_of_mind", order: 0),
                project("development-a", category: "in_development", order: 10),
            ],
            todos: []
        )

        XCTAssertEqual(
            presentation.sections.map(\.category),
            [.topOfMind, .inDevelopment, .futureIdeas]
        )
        XCTAssertEqual(
            presentation.sections[1].projects.map(\.id),
            ["development-a", "development-b"]
        )
    }

    func testSnapshotRanksPrecedeFallbackAndPartialRowsStayDeterministic() {
        let presentation = AgendaBoardPresentation(
            projects: [
                project("manual-first", category: "in_development", order: 0),
                project("ranked-second", category: "in_development", order: 10),
                project("ranked-first", category: "in_development", order: 20),
                project("manual-last", category: "in_development", order: 30),
            ],
            todos: [],
            boardOrderRows: [
                "in_development": ["ranked-first", "ranked-second"],
            ]
        )

        XCTAssertEqual(
            presentation.sections[0].projects.map(\.id),
            [
                "ranked-first",
                "ranked-second",
                "manual-first",
                "manual-last",
            ]
        )
    }

    func testBuildsHierarchyAndPromotesMissingParentToRoot() {
        let presentation = AgendaBoardPresentation(
            projects: [project("p", category: "top_of_mind")],
            todos: [
                todo("child", projectID: "p", parentID: "parent", order: 20),
                todo("orphan", projectID: "p", parentID: "missing", order: 10),
                todo("parent", projectID: "p", order: 0),
            ]
        )
        let card = presentation.sections[0].projects[0]

        XCTAssertEqual(card.rootTodos.map(\.id), ["parent", "orphan"])
        XCTAssertEqual(card.rootTodos[0].children.map(\.id), ["child"])
        XCTAssertEqual(
            card.flattenedTodos.map { "\($0.depth):\($0.todo.id)" },
            ["0:parent", "1:child", "0:orphan"]
        )
    }

    func testFiltersCompletedAndDerivesStatusSummary() {
        let presentation = AgendaBoardPresentation(
            projects: [project("p", category: "top_of_mind")],
            todos: [
                todo(
                    "active",
                    projectID: "p",
                    isBlocker: true,
                    inProgress: true,
                    flagged: true,
                    awaitingReview: true,
                    effort: 5
                ),
                todo("plain", projectID: "p"),
                todo("done", projectID: "p", completed: true, effort: 13),
            ]
        )
        let card = presentation.sections[0].projects[0]

        XCTAssertEqual(card.openCount, 2)
        XCTAssertEqual(card.inProgressCount, 1)
        XCTAssertEqual(card.blockerCount, 1)
        XCTAssertEqual(card.flaggedCount, 1)
        XCTAssertEqual(card.reviewCount, 1)
        XCTAssertEqual(card.effort, 6)
        XCTAssertFalse(card.openTodos.contains { $0.id == "done" })
    }

    func testOmitsEmptyUnknownCategories() {
        let presentation = AgendaBoardPresentation(
            projects: [project("unknown", category: "not_a_category")],
            todos: []
        )

        XCTAssertTrue(presentation.sections.isEmpty)
    }

    private func project(
        _ id: String,
        category: String,
        order: Int = 0
    ) -> ProjectViewValue {
        ProjectViewValue(
            id: id,
            name: id,
            category: category,
            displayOrder: order,
            northstars: []
        )
    }

    private func todo(
        _ id: String,
        projectID: String,
        parentID: String? = nil,
        order: Int = 0,
        isBlocker: Bool = false,
        inProgress: Bool = false,
        flagged: Bool = false,
        awaitingReview: Bool = false,
        completed: Bool = false,
        effort: Int? = nil
    ) -> TodoViewValue {
        TodoViewValue(
            id: id,
            projectID: projectID,
            parentID: parentID,
            text: id,
            isBlocker: isBlocker,
            forLater: false,
            inProgress: inProgress,
            flagged: flagged,
            awaitingReview: awaitingReview,
            displayOrder: order,
            completed: completed,
            effort: effort,
            deadline: nil
        )
    }
}
