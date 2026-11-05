import AgendaSyncCore
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
            presentation.sections.map(\.key),
            ["top_of_mind", "in_development", "future_ideas"]
        )
        XCTAssertEqual(
            presentation.sections.map(\.title),
            ["Top of Mind", "In Development", "Future Ideas"]
        )
        XCTAssertEqual(
            presentation.sections[1].projects.map(\.id),
            ["development-a", "development-b"]
        )
    }

    func testSyncedRowDefinitionsSetOrderAndNames() {
        let presentation = AgendaBoardPresentation(
            projects: [
                project("dev", category: "in_development", order: 0),
                project("ongoing", category: "ongoing", order: 0),
                project("top", category: "top_of_mind", order: 0),
            ],
            todos: [],
            rowDefinitions: [
                AgendaRowDefinition(key: "top_of_mind", label: "Now"),
                AgendaRowDefinition(key: "ongoing", label: "Ongoing"),
                AgendaRowDefinition(key: "in_development", label: "Building"),
            ]
        )
        XCTAssertEqual(presentation.sections.map(\.key), ["top_of_mind", "ongoing", "in_development"])
        XCTAssertEqual(presentation.sections.map(\.title), ["Now", "Ongoing", "Building"])
    }

    func testProjectsInUnknownRowsAreShownNotHidden() {
        // An older snapshot (no definitions) and a project in a newer row.
        let presentation = AgendaBoardPresentation(
            projects: [
                project("top", category: "top_of_mind", order: 0),
                project("new-row", category: "lab_admin", order: 0),
            ],
            todos: []
        )
        XCTAssertEqual(presentation.sections.map(\.key), ["top_of_mind", "lab_admin"])
        XCTAssertEqual(presentation.sections.last?.title, "Lab Admin")
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

    func testOmitsRowsWithoutProjects() {
        // Rows with no projects (default or user-defined) aren't shown.
        // Projects in unknown rows are no longer hidden; see
        // testProjectsInUnknownRowsAreShownNotHidden.
        let presentation = AgendaBoardPresentation(
            projects: [project("top", category: "top_of_mind")],
            todos: [],
            rowDefinitions: [
                AgendaRowDefinition(key: "top_of_mind", label: "Top of Mind"),
                AgendaRowDefinition(key: "ongoing", label: "Ongoing"),
            ]
        )

        XCTAssertEqual(presentation.sections.map(\.key), ["top_of_mind"])
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
