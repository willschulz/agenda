import Foundation

struct AgendaStructureUpdate: Codable, Hashable {
    let stableID: String
    let parentStableID: String?
    let displayOrder: Int
    let forLater: Bool

    enum CodingKeys: String, CodingKey {
        case stableID = "stable_id"
        case parentStableID = "parent_stable_id"
        case displayOrder = "display_order"
        case forLater = "for_later"
    }
}

struct AgendaStructurePlan: Hashable {
    let updates: [AgendaStructureUpdate]
    let pendingTodoIDs: Set<String>
}

enum AgendaStructureDropIntent: Hashable {
    case before(String)
    case childOf(String)
    case root(forLater: Bool)
}

enum AgendaStructurePlannerError: LocalizedError, Equatable {
    case missingTodo
    case crossProject
    case selfParent
    case cycle
    case noChange

    var errorDescription: String? {
        switch self {
        case .missingTodo: "The dragged todo no longer exists."
        case .crossProject: "Cross-project moves are not enabled."
        case .selfParent: "A todo cannot be its own parent."
        case .cycle: "A todo cannot be moved below one of its descendants."
        case .noChange: "The todo is already in that position."
        }
    }
}

enum AgendaStructurePlanner {
    private enum Group: Hashable {
        case roots(forLater: Bool)
        case children(parentID: String)
    }

    private struct State {
        let id: String
        let projectID: String
        var parentID: String?
        var displayOrder: Int
        var forLater: Bool
    }

    static func plan(
        todos: [TodoViewValue],
        movedID: String,
        intent: AgendaStructureDropIntent
    ) throws -> AgendaStructurePlan {
        let openIDs = Set(todos.filter { !$0.completed }.map(\.id))
        guard
            openIDs.contains(movedID),
            let moved = todos.first(where: { $0.id == movedID })
        else {
            throw AgendaStructurePlannerError.missingTodo
        }
        var states = Dictionary(
            uniqueKeysWithValues: todos.map {
                (
                    $0.id,
                    State(
                        id: $0.id,
                        projectID: $0.projectID,
                        parentID: $0.parentID,
                        displayOrder: $0.displayOrder,
                        forLater: $0.forLater
                    )
                )
            }
        )
        let initialStates = states
        let oldGroup = group(for: moved)
        let newParent: String?
        let insertionTarget: String?
        let newRootLater: Bool?
        switch intent {
        case let .before(targetID):
            guard targetID != movedID else {
                throw AgendaStructurePlannerError.noChange
            }
            guard openIDs.contains(targetID), let target = states[targetID] else {
                throw AgendaStructurePlannerError.missingTodo
            }
            guard target.projectID == moved.projectID else {
                throw AgendaStructurePlannerError.crossProject
            }
            newParent = target.parentID
            insertionTarget = targetID
            newRootLater = target.parentID == nil ? target.forLater : nil
        case let .childOf(parentID):
            guard openIDs.contains(parentID), let parent = states[parentID] else {
                throw AgendaStructurePlannerError.missingTodo
            }
            guard parent.projectID == moved.projectID else {
                throw AgendaStructurePlannerError.crossProject
            }
            guard parentID != movedID else {
                throw AgendaStructurePlannerError.selfParent
            }
            newParent = parentID
            insertionTarget = nil
            newRootLater = nil
        case let .root(forLater):
            newParent = nil
            insertionTarget = nil
            newRootLater = forLater
        }

        states[movedID]?.parentID = newParent
        if let newRootLater {
            states[movedID]?.forLater = newRootLater
        }
        try validateAcyclic(states)

        guard let projectedMoved = states[movedID] else {
            throw AgendaStructurePlannerError.missingTodo
        }
        let newGroup = group(for: projectedMoved)
        let touchedGroups = Set([oldGroup, newGroup])
        var structureChanged = oldGroup != newGroup
        for siblingGroup in touchedGroups {
            let originalSiblings = initialStates.values
                .filter {
                    openIDs.contains($0.id)
                        && $0.projectID == moved.projectID
                        && group(for: $0) == siblingGroup
                }
                .sorted(by: stateOrder)
                .map(\.id)
            var siblings = states.values
                .filter {
                    openIDs.contains($0.id)
                        && $0.projectID == moved.projectID
                        && group(for: $0) == siblingGroup
                }
                .sorted(by: stateOrder)
                .map(\.id)
            siblings.removeAll(where: { $0 == movedID })
            if siblingGroup == newGroup {
                if let insertionTarget,
                   let index = siblings.firstIndex(of: insertionTarget) {
                    siblings.insert(movedID, at: index)
                } else {
                    siblings.append(movedID)
                }
            }
            structureChanged = structureChanged || siblings != originalSiblings
            for (index, id) in siblings.enumerated() {
                states[id]?.displayOrder = index * 10
            }
        }
        guard structureChanged else {
            throw AgendaStructurePlannerError.noChange
        }

        let rootLater = try rootLaterValues(states)
        for id in states.keys {
            states[id]?.forLater = rootLater[id] ?? false
        }

        let requiredIDs = states.values
            .filter {
                openIDs.contains($0.id)
                    && $0.projectID == moved.projectID
                    && touchedGroups.contains(group(for: $0))
            }
            .map(\.id)
        let updates: [AgendaStructureUpdate] = requiredIDs.compactMap { id in
            guard let state = states[id] else { return nil }
            return AgendaStructureUpdate(
                stableID: state.id,
                parentStableID: state.parentID,
                displayOrder: state.displayOrder,
                forLater: state.forLater
            )
        }.sorted {
            if $0.parentStableID != $1.parentStableID {
                return ($0.parentStableID ?? "") < ($1.parentStableID ?? "")
            }
            if $0.displayOrder != $1.displayOrder {
                return $0.displayOrder < $1.displayOrder
            }
            return $0.stableID < $1.stableID
        }
        return AgendaStructurePlan(
            updates: updates,
            pendingTodoIDs: descendantIDs(of: movedID, states: states)
        )
    }

    static func updates(
        todos: [TodoViewValue],
        movedID: String,
        intent: AgendaStructureDropIntent
    ) throws -> [AgendaStructureUpdate] {
        try plan(todos: todos, movedID: movedID, intent: intent).updates
    }

    static func rebasedUpdates(
        todos: [TodoViewValue],
        proposed: [AgendaStructureUpdate],
        requiredIDs: Set<String> = []
    ) throws -> [AgendaStructureUpdate] {
        let openIDs = Set(todos.filter { !$0.completed }.map(\.id))
        guard requiredIDs.isSubset(of: openIDs) else {
            throw AgendaStructurePlannerError.missingTodo
        }
        let projectIDs = Set(todos.map(\.projectID))
        guard projectIDs.count <= 1 else {
            throw AgendaStructurePlannerError.crossProject
        }
        var states = Dictionary(
            uniqueKeysWithValues: todos.map {
                (
                    $0.id,
                    State(
                        id: $0.id,
                        projectID: $0.projectID,
                        parentID: $0.parentID,
                        displayOrder: $0.displayOrder,
                        forLater: $0.forLater
                    )
                )
            }
        )
        var touchedGroups = Set<Group>()
        for update in proposed {
            guard openIDs.contains(update.stableID) else {
                if requiredIDs.contains(update.stableID) {
                    throw AgendaStructurePlannerError.missingTodo
                }
                continue
            }
            if let current = states[update.stableID] {
                touchedGroups.insert(group(for: current))
            }
            if let parentID = update.parentStableID {
                guard states[parentID] != nil else {
                    throw AgendaStructurePlannerError.missingTodo
                }
                guard
                    openIDs.contains(parentID)
                        || states[update.stableID]?.parentID == parentID
                else {
                    throw AgendaStructurePlannerError.missingTodo
                }
                guard parentID != update.stableID else {
                    throw AgendaStructurePlannerError.selfParent
                }
            }
            states[update.stableID]?.parentID = update.parentStableID
            states[update.stableID]?.displayOrder = update.displayOrder
            states[update.stableID]?.forLater = update.forLater
            if let projected = states[update.stableID] {
                touchedGroups.insert(group(for: projected))
            }
        }
        try validateAcyclic(states)
        for siblingGroup in touchedGroups {
            let siblings = states.values
                .filter {
                    openIDs.contains($0.id) && group(for: $0) == siblingGroup
                }
                .sorted(by: stateOrder)
            for (index, sibling) in siblings.enumerated() {
                states[sibling.id]?.displayOrder = index * 10
            }
        }
        let rootLater = try rootLaterValues(states)
        for id in states.keys {
            states[id]?.forLater = rootLater[id] ?? false
        }
        return states.values.filter {
            openIDs.contains($0.id)
                && touchedGroups.contains(group(for: $0))
        }.map {
            AgendaStructureUpdate(
                stableID: $0.id,
                parentStableID: $0.parentID,
                displayOrder: $0.displayOrder,
                forLater: $0.forLater
            )
        }.sorted {
            if $0.parentStableID != $1.parentStableID {
                return ($0.parentStableID ?? "") < ($1.parentStableID ?? "")
            }
            if $0.displayOrder != $1.displayOrder {
                return $0.displayOrder < $1.displayOrder
            }
            return $0.stableID < $1.stableID
        }
    }

    private static func validateAcyclic(_ states: [String: State]) throws {
        for start in states.keys {
            var seen = Set<String>()
            var current: String? = start
            while let id = current {
                guard seen.insert(id).inserted else {
                    throw AgendaStructurePlannerError.cycle
                }
                current = states[id]?.parentID
            }
        }
    }

    private static func rootLaterValues(
        _ states: [String: State]
    ) throws -> [String: Bool] {
        var result: [String: Bool] = [:]
        for start in states.keys {
            var seen = Set<String>()
            var current = start
            while let parent = states[current]?.parentID {
                guard seen.insert(current).inserted else {
                    throw AgendaStructurePlannerError.cycle
                }
                current = parent
            }
            result[start] = states[current]?.forLater ?? false
        }
        return result
    }

    private static func stateOrder(_ lhs: State, _ rhs: State) -> Bool {
        if lhs.displayOrder != rhs.displayOrder {
            return lhs.displayOrder < rhs.displayOrder
        }
        return lhs.id < rhs.id
    }

    private static func group(for state: State) -> Group {
        if let parentID = state.parentID {
            return .children(parentID: parentID)
        }
        return .roots(forLater: state.forLater)
    }

    private static func group(for todo: TodoViewValue) -> Group {
        if let parentID = todo.parentID {
            return .children(parentID: parentID)
        }
        return .roots(forLater: todo.forLater)
    }

    private static func descendantIDs(
        of rootID: String,
        states: [String: State]
    ) -> Set<String> {
        var result = Set([rootID])
        var changed = true
        while changed {
            changed = false
            for state in states.values
            where state.parentID.map(result.contains) == true {
                if result.insert(state.id).inserted {
                    changed = true
                }
            }
        }
        return result
    }
}
