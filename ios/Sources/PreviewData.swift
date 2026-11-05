#if DEBUG
import Foundation

enum AgendaPreviewData {
    static let projects: [ProjectViewValue] = [
        project(
            "agenda",
            "Agenda",
            category: "top_of_mind",
            order: 0,
            northstars: ["Make active work visible without adding friction."]
        ),
        project("paper", "Survey Methods Paper", category: "top_of_mind", order: 10),
        project("site", "Lab Website Refresh", category: "in_development", order: 0),
        project("tool", "Annotation Tool", category: "in_development", order: 10),
        project("grant", "Grant Renewal", category: "near_publication", order: 0),
        project("garden", "Research Garden", category: "early_stage", order: 0),
        project("reading", "Reading Group", category: "side_project", order: 0),
        project("future", "Future Experiments", category: "future_ideas", order: 0),
    ]

    static let todos: [TodoViewValue] = [
        todo("agenda-1", "agenda", "Review iOS board on physical iPhone", review: true, effort: 3),
        todo("agenda-2", "agenda", "Harden broad CloudKit editing", blocker: true, effort: 8),
        todo("agenda-3", "agenda", "Conflict and deletion safety", parent: "agenda-2", effort: 5),
        todo("paper-1", "paper", "Revise theory section", inProgress: true, effort: 5),
        todo("paper-2", "paper", "Check robustness appendix", flagged: true, effort: 3),
        todo("site-1", "site", "Polish publications page", inProgress: true, effort: 5),
        todo("site-2", "site", "Parked dark-mode idea", forLater: true, effort: 2),
        todo("tool-1", "tool", "Review label export", effort: 2),
        todo("grant-1", "grant", "Incorporate reviewer comments", inProgress: true, effort: 8),
        todo("garden-1", "garden", "Scope next analysis", effort: 3),
        todo("reading-1", "reading", "Pick next month's paper", flagged: true, effort: 2),
        todo("future-1", "future", "Capture mobile research ideas", effort: 1),
    ]

    private static func project(
        _ id: String,
        _ name: String,
        category: String,
        order: Int,
        northstars: [String] = []
    ) -> ProjectViewValue {
        ProjectViewValue(
            id: id,
            name: name,
            category: category,
            displayOrder: order,
            northstars: northstars
        )
    }

    private static func todo(
        _ id: String,
        _ project: String,
        _ text: String,
        parent: String? = nil,
        blocker: Bool = false,
        forLater: Bool = false,
        inProgress: Bool = false,
        flagged: Bool = false,
        review: Bool = false,
        effort: Int? = nil
    ) -> TodoViewValue {
        TodoViewValue(
            id: id,
            projectID: project,
            parentID: parent,
            text: text,
            isBlocker: blocker,
            forLater: forLater,
            inProgress: inProgress,
            flagged: flagged,
            awaitingReview: review,
            displayOrder: 0,
            completed: false,
            effort: effort,
            deadline: nil
        )
    }
}
#endif
