import AgendaSyncCore
import Foundation
#if DEBUG
import os

private let agendaPresentationLog = OSLog(
    subsystem: "com.willschulz.Agenda",
    category: "TypingPerformance"
)
#endif

enum AgendaCategory: String, CaseIterable, Identifiable {
    case topOfMind = "top_of_mind"
    case nearPublication = "near_publication"
    case inDevelopment = "in_development"
    case earlyStage = "early_stage"
    case sideProject = "side_project"
    case futureIdeas = "future_ideas"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .topOfMind: "Top of Mind"
        case .nearPublication: "Near Publication"
        case .inDevelopment: "In Development"
        case .earlyStage: "Early Stage"
        case .sideProject: "Side Projects"
        case .futureIdeas: "Future Ideas"
        }
    }
}

struct ProjectViewValue: Identifiable, Hashable {
    let id: String
    let name: String
    let category: String
    let displayOrder: Int
    let northstars: [String]
    let scalarRevision: Int
    let structureRevision: Int

    init(
        id: String,
        name: String,
        category: String,
        displayOrder: Int,
        northstars: [String],
        scalarRevision: Int = 0,
        structureRevision: Int = 0
    ) {
        self.id = id
        self.name = name
        self.category = category
        self.displayOrder = displayOrder
        self.northstars = northstars
        self.scalarRevision = scalarRevision
        self.structureRevision = structureRevision
    }
}

struct TodoViewValue: Identifiable, Hashable {
    let id: String
    let projectID: String
    let parentID: String?
    let text: String
    let isBlocker: Bool
    let forLater: Bool
    let inProgress: Bool
    let flagged: Bool
    let awaitingReview: Bool
    let displayOrder: Int
    let completed: Bool
    let effort: Int?
    let deadline: String?
}

struct AgendaTodoEditValues: Hashable {
    var text: String
    var isBlocker: Bool
    var forLater: Bool
    var inProgress: Bool
    var flagged: Bool
    var awaitingReview: Bool
    var completed: Bool
    var effort: Int?
    var deadline: String?

    init(todo: TodoViewValue) {
        text = todo.text
        isBlocker = todo.isBlocker
        forLater = todo.forLater
        inProgress = todo.inProgress
        flagged = todo.flagged
        awaitingReview = todo.awaitingReview
        completed = todo.completed
        effort = todo.effort
        deadline = todo.deadline
    }
}

struct AgendaTodoNode: Identifiable, Hashable {
    let todo: TodoViewValue
    let children: [AgendaTodoNode]

    var id: String { todo.id }

    func flattened(depth: Int = 0) -> [AgendaTodoPlacement] {
        [AgendaTodoPlacement(todo: todo, depth: depth)]
            + children.flatMap { $0.flattened(depth: depth + 1) }
    }
}

struct AgendaTodoPlacement: Identifiable, Hashable {
    let todo: TodoViewValue
    let depth: Int

    var id: String { todo.id }
}

struct AgendaProjectCardModel: Identifiable, Hashable {
    let project: ProjectViewValue
    let rootTodos: [AgendaTodoNode]
    let openTodos: [TodoViewValue]

    var id: String { project.id }
    var openCount: Int { openTodos.count }
    var inProgressCount: Int { openTodos.count(where: \.inProgress) }
    var blockerCount: Int { openTodos.count(where: \.isBlocker) }
    var flaggedCount: Int { openTodos.count(where: \.flagged) }
    var reviewCount: Int { openTodos.count(where: \.awaitingReview) }
    var effort: Int { openTodos.reduce(0) { $0 + ($1.effort ?? 1) } }

    var flattenedTodos: [AgendaTodoPlacement] {
        rootTodos.flatMap { $0.flattened() }
    }

    var activeRootTodos: [AgendaTodoNode] {
        rootTodos.filter { !$0.todo.forLater }
    }

    var laterRootTodos: [AgendaTodoNode] {
        rootTodos.filter(\.todo.forLater)
    }

    var activePlacements: [AgendaTodoPlacement] {
        activeRootTodos.flatMap { $0.flattened() }
    }

    var laterPlacements: [AgendaTodoPlacement] {
        laterRootTodos.flatMap { $0.flattened() }
    }
}

struct AgendaBoardSection: Identifiable, Hashable {
    /// The row key stored in each project's `category`.
    let key: String
    /// The row's display name: the user's label when the Mac has synced one.
    let title: String
    let projects: [AgendaProjectCardModel]

    var id: String { key }
}

extension AgendaBoardSection {
    /// Rows in display order: the user's synced rows when available, else the
    /// six defaults. Any category used by a project but missing from that list
    /// (a row created after this snapshot, or an older Mac) is appended so its
    /// projects are never hidden.
    static func rowOrder(
        definitions: [AgendaRowDefinition]?,
        usedCategories: Set<String>
    ) -> [(key: String, title: String)] {
        var rows: [(key: String, title: String)]
        if let definitions, !definitions.isEmpty {
            rows = definitions.map { ($0.key, $0.label) }
        } else {
            rows = AgendaCategory.allCases.map { ($0.rawValue, $0.title) }
        }
        let known = Set(rows.map(\.key))
        for key in usedCategories.subtracting(known).sorted() {
            rows.append((key, fallbackTitle(for: key)))
        }
        return rows
    }

    static func fallbackTitle(for key: String) -> String {
        if let category = AgendaCategory(rawValue: key) { return category.title }
        return key
            .split(separator: "_")
            .map { $0.prefix(1).uppercased() + $0.dropFirst() }
            .joined(separator: " ")
    }
}

struct AgendaBoardPresentation: Hashable {
    let sections: [AgendaBoardSection]

    init(
        projects: [ProjectViewValue],
        todos: [TodoViewValue],
        boardOrderRows: [String: [String]] = [:],
        rowDefinitions: [AgendaRowDefinition]? = nil
    ) {
#if DEBUG
        let signpostID = OSSignpostID(log: agendaPresentationLog)
        os_signpost(
            .begin,
            log: agendaPresentationLog,
            name: "BoardPresentation",
            signpostID: signpostID,
            "projects=%d todos=%d",
            projects.count,
            todos.count
        )
        defer {
            os_signpost(
                .end,
                log: agendaPresentationLog,
                name: "BoardPresentation",
                signpostID: signpostID
            )
        }
#endif
        let todosByProject = Dictionary(grouping: todos.filter { !$0.completed }) {
            $0.projectID
        }
        let cards = projects.map { project in
            let openTodos = (todosByProject[project.id] ?? []).sorted(by: Self.todoOrder)
            return AgendaProjectCardModel(
                project: project,
                rootTodos: Self.makeTree(from: openTodos),
                openTodos: openTodos
            )
        }

        let rowOrder = AgendaBoardSection.rowOrder(
            definitions: rowDefinitions,
            usedCategories: Set(cards.map(\.project.category))
        )
        sections = rowOrder.compactMap { row in
            let ranks = Dictionary(
                (boardOrderRows[row.key] ?? []).enumerated().map { ($0.element, $0.offset) },
                uniquingKeysWith: { first, _ in first }
            )
            let categoryProjects = cards
                .filter { $0.project.category == row.key }
                .sorted {
                    let leftRank = ranks[$0.id]
                    let rightRank = ranks[$1.id]
                    if leftRank != rightRank {
                        if let leftRank, let rightRank {
                            return leftRank < rightRank
                        }
                        return leftRank != nil
                    }
                    if $0.project.displayOrder != $1.project.displayOrder {
                        return $0.project.displayOrder < $1.project.displayOrder
                    }
                    return $0.project.name.localizedCaseInsensitiveCompare(
                        $1.project.name
                    ) == .orderedAscending
                }
            guard !categoryProjects.isEmpty else { return nil }
            return AgendaBoardSection(
                key: row.key,
                title: row.title,
                projects: categoryProjects
            )
        }
    }

    private static func makeTree(from todos: [TodoViewValue]) -> [AgendaTodoNode] {
        let byID = Dictionary(uniqueKeysWithValues: todos.map { ($0.id, $0) })
        let childrenByParent = Dictionary(
            grouping: todos.filter { todo in
                guard let parentID = todo.parentID else { return false }
                return byID[parentID] != nil
            },
            by: { $0.parentID! }
        )

        func node(for todo: TodoViewValue, ancestors: Set<String>) -> AgendaTodoNode {
            guard !ancestors.contains(todo.id) else {
                return AgendaTodoNode(todo: todo, children: [])
            }
            var nextAncestors = ancestors
            nextAncestors.insert(todo.id)
            let children = (childrenByParent[todo.id] ?? [])
                .sorted(by: todoOrder)
                .map { node(for: $0, ancestors: nextAncestors) }
            return AgendaTodoNode(todo: todo, children: children)
        }

        return todos
            .filter { todo in
                guard let parentID = todo.parentID else { return true }
                return byID[parentID] == nil
            }
            .sorted(by: todoOrder)
            .map { node(for: $0, ancestors: []) }
    }

    private static func todoOrder(_ lhs: TodoViewValue, _ rhs: TodoViewValue) -> Bool {
        if lhs.displayOrder != rhs.displayOrder {
            return lhs.displayOrder < rhs.displayOrder
        }
        return lhs.id < rhs.id
    }
}

private extension Collection {
    func count(where predicate: (Element) throws -> Bool) rethrows -> Int {
        try reduce(into: 0) { count, element in
            if try predicate(element) {
                count += 1
            }
        }
    }
}
