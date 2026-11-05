import AgendaSyncCore
import SwiftUI
import UniformTypeIdentifiers
#if DEBUG
import os

private let agendaDetailPerformanceLog = OSLog(
    subsystem: "com.willschulz.Agenda",
    category: "TypingPerformance"
)
#endif

enum AgendaDisplayMode: String, CaseIterable, Identifiable {
    case board
    case overview

    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var icon: String { self == .board ? "rectangle.3.group" : "square.grid.2x2" }
}

enum AgendaDensity: String, CaseIterable, Identifiable {
    case comfortable
    case compact

    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var cardWidth: CGFloat { self == .comfortable ? 286 : 246 }
    var cardTodoLimit: Int { self == .comfortable ? 8 : 5 }
    var overviewTodoLimit: Int { self == .comfortable ? 3 : 2 }
    var overviewMinimumWidth: CGFloat { self == .comfortable ? 168 : 145 }
}

private struct AgendaPointsEnabledKey: EnvironmentKey {
    static let defaultValue = true
}

extension EnvironmentValues {
    /// Whether effort points are shown (the Mac's Settings → Effort points).
    var agendaPointsEnabled: Bool {
        get { self[AgendaPointsEnabledKey.self] }
        set { self[AgendaPointsEnabledKey.self] = newValue }
    }
}

struct ContentView: View {
    @EnvironmentObject private var sync: MobileSyncController
    @AppStorage("agenda.mobile.displayMode") private var displayModeRaw =
        AgendaDisplayMode.board.rawValue
    @AppStorage("agenda.mobile.density") private var densityRaw =
        AgendaDensity.comfortable.rawValue

    private var displayMode: AgendaDisplayMode {
        AgendaDisplayMode(rawValue: displayModeRaw) ?? .board
    }

    private var density: AgendaDensity {
        AgendaDensity(rawValue: densityRaw) ?? .comfortable
    }

    var body: some View {
        let presentation = AgendaBoardPresentation(
            projects: sync.store.projects,
            todos: sync.store.todos,
            boardOrderRows: sync.store.boardOrderRows,
            rowDefinitions: sync.store.rowDefinitions
        )

        NavigationStack {
            ZStack {
                AgendaPalette.background.ignoresSafeArea()
                if sync.store.projects.isEmpty {
                    ContentUnavailableView(
                        "No cached Agenda data",
                        systemImage: "icloud",
                        description: Text(
                            "Connect to iCloud once to download your private Agenda snapshot."
                        )
                    )
                    .foregroundStyle(AgendaPalette.text)
                } else if displayMode == .board {
                    AgendaBoardView(
                        presentation: presentation,
                        density: density,
                        status: sync.status,
                        error: sync.store.lastError
                    )
                } else {
                    AgendaOverviewView(
                        presentation: presentation,
                        density: density,
                        status: sync.status,
                        error: sync.store.lastError
                    )
                }
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Picker("View", selection: $displayModeRaw) {
                        ForEach(AgendaDisplayMode.allCases) { mode in
                            Label(mode.title, systemImage: mode.icon)
                                .tag(mode.rawValue)
                        }
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 205)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button("Sync Now", systemImage: "arrow.clockwise") {
                            sync.syncNow()
                        }
                        Picker("Density", selection: $densityRaw) {
                            ForEach(AgendaDensity.allCases) { density in
                                Text(density.title).tag(density.rawValue)
                            }
                        }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                    }
                    .accessibilityLabel("Agenda options")
                }
            }
            .navigationDestination(for: AgendaProjectCardModel.self) { card in
                AgendaProjectDetailView(initialCard: card)
            }
        }
        .tint(AgendaPalette.accent)
        .environment(\.agendaPointsEnabled, sync.store.pointsEnabled)
    }
}

struct AgendaBoardView: View {
    let presentation: AgendaBoardPresentation
    let density: AgendaDensity
    let status: String
    let error: String?

    var body: some View {
        ScrollView(.vertical) {
            LazyVStack(alignment: .leading, spacing: density == .compact ? 18 : 25) {
                AgendaSyncBanner(status: status, error: error)
                    .padding(.horizontal, 16)

                ForEach(presentation.sections) { section in
                    AgendaCategoryRow(section: section, density: density)
                }
            }
            .padding(.vertical, 12)
        }
        .scrollIndicators(.hidden)
    }
}

struct AgendaCategoryRow: View {
    let section: AgendaBoardSection
    let density: AgendaDensity

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(section.title)
                    .font(.system(.headline, design: .serif, weight: .semibold))
                    .foregroundStyle(AgendaPalette.text)
                Text("\(section.projects.count)")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(AgendaPalette.faint)
            }
            .padding(.horizontal, 16)

            ScrollView(.horizontal) {
                LazyHStack(alignment: .top, spacing: density == .compact ? 10 : 14) {
                    ForEach(section.projects) { card in
                        NavigationLink(value: card) {
                            AgendaProjectCardView(card: card, density: density)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .scrollTargetLayout()
                .padding(.horizontal, 16)
                .padding(.vertical, 4)
            }
            .scrollTargetBehavior(.viewAligned)
            .scrollIndicators(.hidden)
        }
    }
}

struct AgendaProjectCardView: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let card: AgendaProjectCardModel
    let density: AgendaDensity

    private var cardWidth: CGFloat {
        dynamicTypeSize.isAccessibilitySize ? max(density.cardWidth, 326) : density.cardWidth
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 7) {
                HStack(alignment: .firstTextBaseline) {
                    Text(card.project.name)
                        .font(.system(.title3, design: .serif, weight: .semibold))
                        .foregroundStyle(AgendaPalette.text)
                        .multilineTextAlignment(.leading)
                    Spacer(minLength: 6)
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(AgendaPalette.faint)
                }

                AgendaProjectMetrics(card: card)
            }
            .padding(density == .compact ? 12 : 15)
            .background(AgendaPalette.elevated)

            Divider().overlay(AgendaPalette.softBorder)

            VStack(alignment: .leading, spacing: 0) {
                let visible = Array(
                    card.flattenedTodos.prefix(density.cardTodoLimit)
                )
                if visible.isEmpty {
                    Text("No open todos")
                        .font(.subheadline)
                        .foregroundStyle(AgendaPalette.faint)
                        .padding(density == .compact ? 12 : 15)
                } else {
                    ForEach(visible) { placement in
                        AgendaTodoRow(
                            placement: placement,
                            compact: density == .compact
                        )
                    }

                    let hidden = card.openCount - visible.count
                    if hidden > 0 {
                        Text("+ \(hidden) more")
                            .font(.caption.weight(.medium))
                            .foregroundStyle(AgendaPalette.accent)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 10)
                    }
                }
            }
        }
        .frame(width: cardWidth, alignment: .topLeading)
        .background(AgendaPalette.elevated)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(AgendaPalette.softBorder, lineWidth: 1)
        }
        .shadow(color: .black.opacity(0.06), radius: 8, y: 3)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(card.project.name), \(card.openCount) open todos")
    }
}

struct AgendaOverviewView: View {
    let presentation: AgendaBoardPresentation
    let density: AgendaDensity
    let status: String
    let error: String?

    var columns: [GridItem] {
        [
            GridItem(
                .adaptive(minimum: density.overviewMinimumWidth),
                spacing: 10,
                alignment: .top
            )
        ]
    }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 20) {
                AgendaSyncBanner(status: status, error: error)

                ForEach(presentation.sections) { section in
                    VStack(alignment: .leading, spacing: 9) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(section.title)
                                .font(.system(.headline, design: .serif, weight: .semibold))
                            Spacer()
                            Text(
                                "\(section.projects.count) "
                                    + (section.projects.count == 1 ? "project" : "projects")
                            )
                                .font(.caption)
                                .foregroundStyle(AgendaPalette.faint)
                        }

                        LazyVGrid(columns: columns, alignment: .leading, spacing: 10) {
                            ForEach(section.projects) { card in
                                NavigationLink(value: card) {
                                    AgendaOverviewTile(
                                        card: card,
                                        todoLimit: density.overviewTodoLimit
                                    )
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                }
            }
            .padding(14)
        }
        .scrollIndicators(.hidden)
        .foregroundStyle(AgendaPalette.text)
    }
}

struct AgendaOverviewTile: View {
    let card: AgendaProjectCardModel
    let todoLimit: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top) {
                Text(card.project.name)
                    .font(.system(.subheadline, design: .serif, weight: .semibold))
                    .lineLimit(2)
                Spacer(minLength: 5)
                Text("\(card.openCount)")
                    .font(.caption.monospacedDigit().weight(.semibold))
                    .foregroundStyle(AgendaPalette.muted)
            }

            AgendaProjectMetrics(card: card, condensed: true)

            ForEach(card.flattenedTodos.prefix(todoLimit)) { placement in
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Circle()
                        .fill(AgendaStatus.color(for: placement.todo))
                        .frame(width: 4, height: 4)
                    Text(placement.todo.text)
                        .font(.caption2)
                        .foregroundStyle(AgendaPalette.muted)
                        .lineLimit(1)
                }
                .padding(.leading, CGFloat(min(placement.depth, 2)) * 6)
            }

            if card.openCount == 0 {
                Text("No open todos")
                    .font(.caption2)
                    .foregroundStyle(AgendaPalette.faint)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 104, alignment: .topLeading)
        .padding(11)
        .background(AgendaPalette.elevated)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(AgendaPalette.softBorder, lineWidth: 1)
        }
    }
}

struct AgendaNewTodoComposer: View {
    @State private var text: String
    @State private var reportedHasDraft: Bool
    @FocusState private var isFocused: Bool
    let buffer: AgendaTodoTextBuffer
    let add: (String) -> Void
    let draftAvailabilityChanged: (Bool) -> Void
    let typingChanged: (Bool) -> Void

    init(
        buffer: AgendaTodoTextBuffer,
        add: @escaping (String) -> Void,
        draftAvailabilityChanged: @escaping (Bool) -> Void,
        typingChanged: @escaping (Bool) -> Void
    ) {
        self.buffer = buffer
        self.add = add
        self.draftAvailabilityChanged = draftAvailabilityChanged
        self.typingChanged = typingChanged
        let hasDraft = !buffer.text.trimmingCharacters(
            in: .whitespacesAndNewlines
        ).isEmpty
        _text = State(initialValue: buffer.text)
        _reportedHasDraft = State(initialValue: hasDraft)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("New Todo")
                .font(.system(.headline, design: .serif))
            HStack {
                TextField("Top-level todo", text: $text)
                    .textFieldStyle(.roundedBorder)
                    .focused($isFocused)
                Button("Add") {
                    let submitted = text
                    text = ""
                    buffer.text = ""
                    reportDraftAvailability(false)
                    if isFocused {
                        isFocused = false
                        typingChanged(false)
                    }
                    add(submitted)
                }
                .buttonStyle(.borderedProminent)
                .disabled(trimmedText.isEmpty)
            }
            Text("Creation is limited to top-level todos during the pilot.")
                .font(.caption2)
                .foregroundStyle(AgendaPalette.faint)
        }
        .agendaDetailSection()
        .onChange(of: isFocused) { _, focused in
            typingChanged(focused)
        }
        .onChange(of: text) { _, value in
            buffer.text = value
            reportDraftAvailability(
                !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            )
        }
        .onDisappear {
            if isFocused {
                typingChanged(false)
            }
        }
    }

    private var trimmedText: String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func reportDraftAvailability(_ hasDraft: Bool) {
        guard reportedHasDraft != hasDraft else { return }
        reportedHasDraft = hasDraft
        draftAvailabilityChanged(hasDraft)
    }
}

struct AgendaProjectDetailView: View {
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject private var sync: MobileSyncController
    let initialCard: AgendaProjectCardModel
    @State private var newTodoBuffer = AgendaTodoTextBuffer(text: "")
    @State private var hasUnsavedNewTodo = false
    @State private var showingUnsavedNewTodoPrompt = false
    @State private var editDraft: AgendaTodoEditDraft?
    @State private var deleteDraft: AgendaTodoDeleteDraft?
    @State private var selectedConflict: AgendaTodoConflict?
    @State private var showingCompleted = false

    private var card: AgendaProjectCardModel {
#if DEBUG
        let signpostID = OSSignpostID(log: agendaDetailPerformanceLog)
        os_signpost(
            .begin,
            log: agendaDetailPerformanceLog,
            name: "ProjectDetailEvaluation",
            signpostID: signpostID
        )
        defer {
            os_signpost(
                .end,
                log: agendaDetailPerformanceLog,
                name: "ProjectDetailEvaluation",
                signpostID: signpostID
            )
        }
#endif
        return AgendaBoardPresentation(
            projects: sync.store.projects.filter {
                $0.id == initialCard.project.id
            },
            todos: sync.store.todos.filter {
                $0.projectID == initialCard.project.id
            }
        ).sections
            .flatMap(\.projects)
            .first(where: { $0.id == initialCard.id }) ?? initialCard
    }

    private var projectConflicts: [AgendaTodoConflict] {
        return sync.store.conflicts.filter {
            $0.projectID == card.project.id
        }
    }

    private var completedTodos: [TodoViewValue] {
        sync.store.todos
            .filter {
                $0.projectID == card.project.id && $0.completed
            }
            .sorted {
                $0.text.localizedCaseInsensitiveCompare($1.text)
                    == .orderedAscending
            }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                AgendaSyncBanner(
                    status: sync.status,
                    error: sync.store.lastError
                )
                AgendaProjectMetrics(card: card)

                AgendaNewTodoComposer(
                    buffer: newTodoBuffer,
                    add: {
                        sync.createTodo(projectID: card.project.id, text: $0)
                    },
                    draftAvailabilityChanged: {
                        hasUnsavedNewTodo = $0
                    },
                    typingChanged: {
                        let sessionID = "new-todo-\(card.project.id)"
                        if $0 {
                            sync.beginTyping(sessionID: sessionID)
                        } else {
                            sync.endTyping(sessionID: sessionID)
                        }
                    }
                )

                if !projectConflicts.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Needs Review")
                            .font(.system(.headline, design: .serif))
                        ForEach(projectConflicts) { conflict in
                            Button {
                                selectedConflict = conflict
                            } label: {
                                Label(
                                    "Review conflicting edit",
                                    systemImage: "exclamationmark.triangle.fill"
                                )
                            }
                            .buttonStyle(.bordered)
                        }
                    }
                    .agendaDetailSection()
                }

                if !card.project.northstars.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Northstars")
                            .font(.system(.headline, design: .serif))
                        ForEach(card.project.northstars, id: \.self) { northstar in
                            Label(northstar, systemImage: "sparkle")
                                .font(.subheadline)
                                .foregroundStyle(AgendaPalette.muted)
                        }
                    }
                    .agendaDetailSection()
                }

                AgendaStructuralTodoSection(
                    title: "Active",
                    emptyText: "No active todos",
                    projectID: card.project.id,
                    placements: card.activePlacements,
                    forLater: false,
                    requestDelete: requestDelete
                ) { placement, pending, pendingCreate, structureLocked in
                    AgendaTodoRow(
                        placement: placement,
                        compact: false,
                        pending: pending,
                        onToggleCompleted: pending || structureLocked ? nil : {
                            sync.setCompleted(
                                todoID: placement.todo.id,
                                completed: true
                            )
                        },
                        onEdit: pending && !pendingCreate ? nil : {
                            editDraft = AgendaTodoEditDraft(
                                todoID: placement.todo.id,
                                values: AgendaTodoEditValues(
                                    todo: placement.todo
                                ),
                                statusEditingDisabled: pendingCreate
                                    || structureLocked
                            )
                        }
                    )
                }

                AgendaStructuralTodoSection(
                    title: "Later",
                    emptyText: "Drop a todo here for Later",
                    projectID: card.project.id,
                    placements: card.laterPlacements,
                    forLater: true,
                    requestDelete: requestDelete
                ) { placement, pending, pendingCreate, structureLocked in
                    AgendaTodoRow(
                        placement: placement,
                        compact: false,
                        pending: pending,
                        onToggleCompleted: pending || structureLocked ? nil : {
                            sync.setCompleted(
                                todoID: placement.todo.id,
                                completed: true
                            )
                        },
                        onEdit: pending && !pendingCreate ? nil : {
                            editDraft = AgendaTodoEditDraft(
                                todoID: placement.todo.id,
                                values: AgendaTodoEditValues(
                                    todo: placement.todo
                                ),
                                statusEditingDisabled: pendingCreate
                                    || structureLocked
                            )
                        }
                    )
                }

                if !completedTodos.isEmpty {
                    VStack(alignment: .leading, spacing: 0) {
                        DisclosureGroup(
                            isExpanded: $showingCompleted
                        ) {
                            ForEach(completedTodos) { todo in
                                let pending = sync.store.pendingTodoIDs.contains(
                                    todo.id
                                )
                                AgendaTodoRow(
                                    placement: AgendaTodoPlacement(
                                        todo: todo,
                                        depth: 0
                                    ),
                                    compact: false,
                                    pending: pending,
                                    onToggleCompleted: pending ? nil : {
                                        sync.setCompleted(
                                            todoID: todo.id,
                                            completed: false
                                        )
                                    },
                                    onEdit: pending ? nil : {
                                        editDraft = AgendaTodoEditDraft(
                                            todoID: todo.id,
                                            values: AgendaTodoEditValues(
                                                todo: todo
                                            ),
                                            statusEditingDisabled: false
                                        )
                                    }
                                )
                            }
                        } label: {
                            Text("Completed Todos (\(completedTodos.count))")
                                .font(.system(.headline, design: .serif))
                                .padding(.horizontal, 14)
                                .padding(.vertical, 13)
                        }
                        .tint(AgendaPalette.muted)
                    }
                    .agendaDetailSection(padded: false)
                }

            }
            .padding(16)
        }
        .background(AgendaPalette.background)
        .navigationTitle(card.project.name)
        .navigationBarTitleDisplayMode(.inline)
        .navigationBarBackButtonHidden(hasUnsavedNewTodo)
        .toolbar {
            if hasUnsavedNewTodo {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        showingUnsavedNewTodoPrompt = true
                    } label: {
                        Label("Back", systemImage: "chevron.left")
                    }
                }
            }
        }
        .sheet(item: $editDraft) { draft in
            AgendaTodoEditSheet(
                draft: draft,
                save: {
                    sync.saveTodo(todoID: draft.todoID, values: $0)
                },
                typingChanged: {
                    let sessionID = "edit-todo-\(draft.todoID)"
                    if $0 {
                        sync.beginTyping(sessionID: sessionID)
                    } else {
                        sync.endTyping(sessionID: sessionID)
                    }
                }
            )
        }
        .sheet(item: $selectedConflict) { conflict in
            AgendaConflictSheet(conflict: conflict) {
                sync.keepMacValue(conflictID: conflict.id)
            } retryMine: {
                sync.retryMine(conflictID: conflict.id)
            }
        }
        .alert(item: $deleteDraft) { draft in
            Alert(
                title: Text("Delete “\(draft.text)” ?"),
                message: Text(
                    draft.descendantCount == 0
                        ? "The Mac will create a recoverable tombstone."
                        : "This also deletes \(draft.descendantCount) "
                            + (draft.descendantCount == 1
                                ? "descendant." : "descendants.")
                ),
                primaryButton: .destructive(Text("Delete")) {
                    draft.closeSwipe()
                    sync.deleteTodo(todoID: draft.todoID)
                },
                secondaryButton: .cancel(Text("Cancel")) {
                    draft.closeSwipe()
                }
            )
        }
        .alert(
            "Unsaved New Todo",
            isPresented: $showingUnsavedNewTodoPrompt
        ) {
            Button("Save & Exit") {
                let text = newTodoBuffer.text
                newTodoBuffer.text = ""
                hasUnsavedNewTodo = false
                sync.createTodo(projectID: card.project.id, text: text)
                dismiss()
            }
            Button("Discard", role: .destructive) {
                newTodoBuffer.text = ""
                hasUnsavedNewTodo = false
                dismiss()
            }
            Button("Keep Editing", role: .cancel) {}
        } message: {
            Text("Save this new todo before leaving the project?")
        }
    }

    private func requestDelete(
        _ todo: TodoViewValue,
        closeSwipe: @escaping () -> Void
    ) {
        var selected = Set([todo.id])
        var changed = true
        while changed {
            changed = false
            for candidate in sync.store.todos
            where candidate.projectID == todo.projectID
                && candidate.parentID.map(selected.contains) == true {
                if selected.insert(candidate.id).inserted {
                    changed = true
                }
            }
        }
        deleteDraft = AgendaTodoDeleteDraft(
            todoID: todo.id,
            text: todo.text,
            descendantCount: max(0, selected.count - 1),
            closeSwipe: closeSwipe
        )
    }
}

struct AgendaSwipeDeleteRow<Content: View>: View {
    let disabled: Bool
    let delete: (@escaping () -> Void) -> Void
    @ViewBuilder let content: () -> Content
    @State private var revealed = false
    @GestureState private var translation: CGFloat = 0

    private var offset: CGFloat {
        guard !disabled else { return 0 }
        return min(0, max(-92, (revealed ? -92 : 0) + translation))
    }

    var body: some View {
        ZStack(alignment: .trailing) {
            Button(role: .destructive) {
                delete { revealed = false }
            } label: {
                Label("Delete", systemImage: "trash")
                    .labelStyle(.iconOnly)
                    .frame(width: 92)
                    .frame(maxHeight: .infinity)
                    .background(.red)
                    .foregroundStyle(.white)
            }
            .disabled(disabled)

            content()
                .background(AgendaPalette.elevated)
                .offset(x: offset)
                .highPriorityGesture(
                    DragGesture(minimumDistance: 14)
                        .updating($translation) { value, state, _ in
                            guard !disabled else { return }
                            state = value.translation.width
                        }
                        .onEnded { value in
                            guard !disabled else {
                                revealed = false
                                return
                            }
                            if value.translation.width < -36 {
                                revealed = true
                            } else if value.translation.width > 24 {
                                revealed = false
                            }
                        }
                )
                .accessibilityAction(named: "Delete") {
                    guard !disabled else { return }
                    delete { revealed = false }
                }
        }
        .clipped()
    }
}

struct AgendaStructuralTodoSection<RowContent: View>: View {
    @EnvironmentObject private var sync: MobileSyncController
    let title: String
    let emptyText: String
    let projectID: String
    let placements: [AgendaTodoPlacement]
    let forLater: Bool
    let requestDelete: (TodoViewValue, @escaping () -> Void) -> Void
    @ViewBuilder let row: (
        AgendaTodoPlacement,
        Bool,
        Bool,
        Bool
    ) -> RowContent

    private var projectPending: Bool {
        sync.store.pendingProjectIDs.contains(projectID)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(title)
                    .font(.system(.headline, design: .serif))
                Spacer()
                if projectPending {
                    Label("Structure pending", systemImage: "clock.arrow.circlepath")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(AgendaPalette.accent)
                } else {
                    Label("Drop here", systemImage: "arrow.down.to.line")
                        .font(.caption2)
                        .foregroundStyle(AgendaPalette.faint)
                }
            }
            .padding(.horizontal, 14)
            .padding(.top, 13)
            .padding(.bottom, 6)
            .contentShape(Rectangle())
            .dropDestination(for: String.self) { values, _ in
                submit(values, intent: .root(forLater: forLater))
            }

            if placements.isEmpty {
                Text(emptyText)
                    .font(.subheadline)
                    .foregroundStyle(AgendaPalette.faint)
                    .padding(14)
                    .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
                    .contentShape(Rectangle())
                    .dropDestination(for: String.self) { values, _ in
                        submit(values, intent: .root(forLater: forLater))
                    }
            } else {
                ForEach(placements) { placement in
                    let pending = sync.store.pendingTodoIDs.contains(
                        placement.todo.id
                    )
                    let pendingCreate = sync.store.pendingCreateTodoIDs.contains(
                        placement.todo.id
                    )
                    HStack(spacing: 0) {
                        AgendaSwipeDeleteRow(
                            disabled: pending || projectPending,
                            delete: { closeSwipe in
                                requestDelete(placement.todo, closeSwipe)
                            }
                        ) {
                            row(placement, pending, pendingCreate, projectPending)
                        }
                        .contentShape(Rectangle())
                        .draggable(placement.todo.id)
                        .dropDestination(for: String.self) { values, _ in
                            submit(values, intent: .before(placement.todo.id))
                        }
                        .frame(maxWidth: .infinity)

                        Image(systemName: "arrow.turn.down.right")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(
                                pending
                                    ? AgendaPalette.faint.opacity(0.35)
                                    : AgendaPalette.accent
                            )
                            .frame(width: 50)
                            .frame(minHeight: 52)
                            .background(AgendaPalette.elevated.opacity(0.92))
                            .contentShape(Rectangle())
                            .dropDestination(for: String.self) { values, _ in
                                submit(
                                    values,
                                    intent: .childOf(placement.todo.id)
                                )
                            }
                            .accessibilityLabel(
                                "Drop to nest under \(placement.todo.text)"
                            )
                    }
                }
            }
        }
        .agendaDetailSection(padded: false)
    }

    private func submit(
        _ values: [String],
        intent: AgendaStructureDropIntent
    ) -> Bool {
        guard
            !projectPending,
            let movedID = values.first,
            !movedID.isEmpty,
            !sync.store.pendingTodoIDs.contains(movedID)
        else { return false }
        sync.moveTodo(
            projectID: projectID,
            todoID: movedID,
            intent: intent
        )
        return true
    }
}

struct AgendaTodoRow: View {
    @Environment(\.agendaPointsEnabled) private var pointsEnabled
    let placement: AgendaTodoPlacement
    let compact: Bool
    var pending = false
    var onToggleCompleted: (() -> Void)?
    var onEdit: (() -> Void)?

    var body: some View {
        HStack(alignment: .top, spacing: compact ? 7 : 9) {
            RoundedRectangle(cornerRadius: 2)
                .fill(AgendaStatus.color(for: placement.todo))
                .frame(width: 3)

            if let onToggleCompleted {
                Button(action: onToggleCompleted) {
                    Image(systemName: AgendaStatus.icon(for: placement.todo))
                        .font(.system(size: compact ? 11 : 13, weight: .semibold))
                        .foregroundStyle(AgendaStatus.color(for: placement.todo))
                        .frame(
                            width: compact ? 13 : 32,
                            height: compact ? nil : 32
                        )
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            } else {
                Image(systemName: AgendaStatus.icon(for: placement.todo))
                    .font(.system(size: compact ? 11 : 13, weight: .semibold))
                    .foregroundStyle(AgendaStatus.color(for: placement.todo))
                    .frame(width: 13)
            }

            if let onEdit {
                Button(action: onEdit) {
                    todoText
                }
                .buttonStyle(.plain)
            } else {
                todoText
            }

            if pending {
                Label("Pending", systemImage: "clock.arrow.circlepath")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(AgendaPalette.accent)
                    .accessibilityLabel("Pending Mac confirmation")
            }

            if onEdit != nil {
                Image(systemName: "pencil")
                    .font(.caption)
                    .foregroundStyle(AgendaPalette.faint)
                    .accessibilityLabel("Edit todo")
            }

            if pointsEnabled, let effort = placement.todo.effort {
                Text("\(effort)")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(AgendaPalette.faint)
            }
        }
        .padding(.leading, CGFloat(min(placement.depth, 3)) * (compact ? 10 : 14))
        .padding(.horizontal, compact ? 10 : 13)
        .padding(.vertical, compact ? 7 : 9)
        .background(AgendaPalette.row.opacity(placement.depth > 0 ? 0.7 : 0))
        .overlay(alignment: .bottom) {
            Divider().overlay(AgendaPalette.softBorder)
        }
    }

    private var todoText: some View {
                Text(placement.todo.text)
                    .font(compact ? .caption : .subheadline)
                    .foregroundStyle(AgendaPalette.text)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct AgendaTodoEditDraft: Identifiable {
    let todoID: String
    var values: AgendaTodoEditValues
    let statusEditingDisabled: Bool
    var id: String { todoID }
}

struct AgendaTodoDeleteDraft: Identifiable {
    let todoID: String
    let text: String
    let descendantCount: Int
    let closeSwipe: () -> Void
    var id: String { todoID }
}

struct AgendaTodoEditSheet: View {
    @Environment(\.agendaPointsEnabled) private var pointsEnabled
    @Environment(\.dismiss) private var dismiss
    @State private var values: AgendaTodoEditValues
    @State private var textBuffer: AgendaTodoTextBuffer
    @State private var textIsValid: Bool
    let draft: AgendaTodoEditDraft
    let save: (AgendaTodoEditValues) -> Void
    let typingChanged: (Bool) -> Void

    init(
        draft: AgendaTodoEditDraft,
        save: @escaping (AgendaTodoEditValues) -> Void,
        typingChanged: @escaping (Bool) -> Void = { _ in }
    ) {
        self.draft = draft
        self.save = save
        self.typingChanged = typingChanged
        _values = State(initialValue: draft.values)
        _textBuffer = State(
            initialValue: AgendaTodoTextBuffer(text: draft.values.text)
        )
        _textIsValid = State(
            initialValue: !draft.values.text.trimmingCharacters(
                in: .whitespacesAndNewlines
            ).isEmpty
        )
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Todo") {
                    AgendaTodoTextEditor(
                        initialText: draft.values.text,
                        buffer: textBuffer,
                        isValid: $textIsValid,
                        typingChanged: typingChanged
                    )
                }
                Section("Workflow") {
                    Toggle("In Progress", isOn: $values.inProgress)
                    Toggle("Flagged", isOn: $values.flagged)
                    Toggle("Needs Review", isOn: $values.awaitingReview)
                }
                .disabled(values.completed || draft.statusEditingDisabled)
                Section("Classification") {
                    Toggle("Blocker", isOn: $values.isBlocker)
                }
                .disabled(draft.statusEditingDisabled)
                Section {
                    Label(
                        values.forLater ? "Later" : "Active",
                        systemImage: values.forLater ? "archivebox" : "tray"
                    )
                } footer: {
                    Text(
                        "Move a root todo between Active and Later by dragging it "
                            + "to that section. Descendants inherit the root state."
                    )
                }
                Section("Planning") {
                    if pointsEnabled {
                        Picker("Effort", selection: $values.effort) {
                            Text("None").tag(Int?.none)
                            ForEach([1, 2, 3, 5, 8, 13], id: \.self) { effort in
                                Text("\(effort)").tag(Optional(effort))
                            }
                        }
                    }
                    Toggle("Deadline", isOn: hasDeadline)
                    if values.deadline != nil {
                        DatePicker(
                            "Due",
                            selection: deadlineDate,
                            displayedComponents: .date
                        )
                    }
                }
                .disabled(draft.statusEditingDisabled)
                Section {
                    Toggle("Completed", isOn: $values.completed)
                } footer: {
                    if draft.statusEditingDisabled {
                        Text(
                            "Status fields become editable after the pending "
                                + "offline create is confirmed by the Mac."
                        )
                    } else if values.completed {
                        Text(
                            "Completing clears In Progress, Flagged, and "
                                + "Needs Review."
                        )
                    }
                }
            }
            .navigationTitle("Edit Todo")
            .onChange(of: values.completed) { _, completed in
                if completed {
                    values.inProgress = false
                    values.flagged = false
                    values.awaitingReview = false
                }
            }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        var submitted = values
                        submitted.text = textBuffer.text
                        save(submitted)
                        dismiss()
                    }
                    .disabled(!textIsValid)
                }
            }
        }
    }

    private var hasDeadline: Binding<Bool> {
        Binding(
            get: { values.deadline != nil },
            set: { enabled in
                values.deadline = enabled
                    ? Self.dateString(from: Date()) : nil
            }
        )
    }

    private var deadlineDate: Binding<Date> {
        Binding(
            get: {
                values.deadline.flatMap(Self.date(from:))
                    ?? Calendar.current.startOfDay(for: Date())
            },
            set: { values.deadline = Self.dateString(from: $0) }
        )
    }

    private static func date(from value: String) -> Date? {
        let parts = value.split(separator: "-").compactMap {
            Int($0)
        }
        guard parts.count == 3 else { return nil }
        return Calendar.current.date(
            from: DateComponents(
                year: parts[0],
                month: parts[1],
                day: parts[2]
            )
        )
    }

    private static func dateString(from date: Date) -> String {
        let components = Calendar.current.dateComponents(
            [.year, .month, .day],
            from: date
        )
        return String(
            format: "%04d-%02d-%02d",
            components.year ?? 0,
            components.month ?? 0,
            components.day ?? 0
        )
    }
}

struct AgendaTodoTextEditor: View {
    @State private var text: String
    @FocusState private var isFocused: Bool
    let buffer: AgendaTodoTextBuffer
    @Binding var isValid: Bool
    let typingChanged: (Bool) -> Void

    init(
        initialText: String,
        buffer: AgendaTodoTextBuffer,
        isValid: Binding<Bool>,
        typingChanged: @escaping (Bool) -> Void
    ) {
        _text = State(initialValue: initialText)
        self.buffer = buffer
        _isValid = isValid
        self.typingChanged = typingChanged
    }

    var body: some View {
        TextField("Todo text", text: $text, axis: .vertical)
            .lineLimit(2...6)
            .focused($isFocused)
            .onChange(of: text) { _, value in
                buffer.text = value
                let valid = !value.trimmingCharacters(
                    in: .whitespacesAndNewlines
                ).isEmpty
                if isValid != valid {
                    isValid = valid
                }
            }
            .onChange(of: isFocused) { _, focused in
                typingChanged(focused)
            }
            .onDisappear {
                if isFocused {
                    typingChanged(false)
                }
            }
    }
}

struct AgendaConflictSheet: View {
    @Environment(\.dismiss) private var dismiss
    let conflict: AgendaTodoConflict
    let keepMac: () -> Void
    let retryMine: () -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text(conflict.reasonSummary)
                }
                Section("Mac kept") {
                    Text(conflict.macValue)
                }
                Section("Your proposed change") {
                    Text(conflict.proposedValue)
                }
                Section {
                    Button("Keep Mac") {
                        keepMac()
                        dismiss()
                    }
                    Button("Retry Mine") {
                        retryMine()
                        dismiss()
                    }
                } footer: {
                    Text(
                        "Retry Mine creates a new mutation against the latest Mac revision."
                    )
                }
            }
            .navigationTitle("Resolve Conflict")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
        }
    }
}

struct AgendaProjectMetrics: View {
    @Environment(\.agendaPointsEnabled) private var pointsEnabled
    let card: AgendaProjectCardModel
    var condensed = false

    var body: some View {
        ViewThatFits(in: .horizontal) {
            metricsRow
            Text(summary)
                .font(condensed ? .caption2 : .caption)
                .foregroundStyle(AgendaPalette.muted)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var metricsRow: some View {
        HStack(spacing: condensed ? 5 : 8) {
            Label("\(card.openCount)", systemImage: "circle")
            if card.inProgressCount > 0 {
                Label("\(card.inProgressCount)", systemImage: "play.fill")
                    .foregroundStyle(AgendaPalette.inProgress)
            }
            if card.blockerCount > 0 {
                Label("\(card.blockerCount)", systemImage: "exclamationmark")
                    .foregroundStyle(AgendaPalette.blocker)
            }
            if card.reviewCount > 0 {
                Label("\(card.reviewCount)", systemImage: "star.fill")
                    .foregroundStyle(AgendaPalette.review)
            }
            if pointsEnabled && card.effort > 0 && !condensed {
                Spacer(minLength: 2)
                Label("\(card.effort)", systemImage: "gauge.with.dots.needle.33percent")
            }
        }
        .font(condensed ? .caption2 : .caption)
        .foregroundStyle(AgendaPalette.muted)
        .labelStyle(.titleAndIcon)
    }

    private var summary: String {
        var parts = ["\(card.openCount) open"]
        if card.inProgressCount > 0 {
            parts.append("\(card.inProgressCount) active")
        }
        if card.blockerCount > 0 {
            parts.append("\(card.blockerCount) blocked")
        }
        if card.reviewCount > 0 {
            parts.append("\(card.reviewCount) review")
        }
        if pointsEnabled && card.effort > 0 && !condensed {
            parts.append("\(card.effort) effort")
        }
        return parts.joined(separator: " · ")
    }
}

struct AgendaSyncBanner: View {
    @EnvironmentObject private var sync: MobileSyncController
    @State private var reviewingDeletions = false
    let status: String
    let error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Label(status, systemImage: "lock.icloud")
                .font(.caption)
                .foregroundStyle(AgendaPalette.muted)
            if let held = sync.store.heldDeletionBatch {
                HStack(spacing: 8) {
                    Text(
                        "The Mac deleted \(held.projectCount + held.todoCount) "
                            + "item\(held.projectCount + held.todoCount == 1 ? "" : "s")"
                            + " while this device was away."
                    )
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    Button("Review") {
                        reviewingDeletions = true
                    }
                    .font(.caption2.weight(.semibold))
                    .buttonStyle(.bordered)
                }
            } else if let error {
                Text(error)
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
            if sync.store.stalledPendingMutationCount > 0 {
                HStack(spacing: 8) {
                    Text(
                        "\(sync.store.stalledPendingMutationCount) change"
                            + (sync.store.stalledPendingMutationCount == 1
                                ? " has" : "s have")
                            + " not reached the Mac."
                    )
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    Button("Retry now") {
                        sync.syncNow()
                    }
                    .font(.caption2.weight(.semibold))
                    .buttonStyle(.bordered)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .sheet(isPresented: $reviewingDeletions) {
            if let held = sync.store.heldDeletionBatch {
                AgendaHeldDeletionReview(
                    fingerprint: held.fingerprint,
                    items: sync.store.heldDeletionItems(),
                    apply: { sync.approveHeldDeletions(fingerprint: $0) }
                )
            }
        }
    }
}

struct AgendaHeldDeletionReview: View {
    @Environment(\.dismiss) private var dismiss
    let fingerprint: String
    let items: [AgendaHeldDeletionItem]
    let apply: (String) -> Void

    private var groups: [(String, [AgendaHeldDeletionItem])] {
        let grouped = Dictionary(grouping: items) {
            $0.kind == .project ? "Projects" : ($0.projectName ?? "Other")
        }
        return grouped.keys.sorted().map { ($0, grouped[$0] ?? []) }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text(
                        "While this device was away, the Mac deleted more items "
                            + "than a single sync may remove automatically. "
                            + "Nothing here has been removed yet."
                    )
                }
                ForEach(groups, id: \.0) { group in
                    Section(group.0) {
                        ForEach(group.1) { item in
                            Text(item.title)
                        }
                    }
                }
                Section {
                    Button("Remove \(items.count) from this device", role: .destructive) {
                        apply(fingerprint)
                        dismiss()
                    }
                    Button("Not now") { dismiss() }
                } footer: {
                    Text(
                        "These items are already deleted on the Mac. If any "
                            + "should not be, restore them on the Mac instead."
                    )
                }
            }
            .navigationTitle("Review Mac Deletions")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
        }
    }
}

enum AgendaStatus {
    static func color(for todo: TodoViewValue) -> Color {
        if todo.completed { return AgendaPalette.muted }
        if todo.awaitingReview { return AgendaPalette.review }
        if todo.inProgress { return AgendaPalette.inProgress }
        if todo.flagged { return AgendaPalette.flagged }
        if todo.isBlocker { return AgendaPalette.blocker }
        if todo.forLater { return AgendaPalette.running }
        return AgendaPalette.border
    }

    static func icon(for todo: TodoViewValue) -> String {
        if todo.completed { return "checkmark.circle.fill" }
        if todo.awaitingReview { return "star.fill" }
        if todo.inProgress { return "play.fill" }
        if todo.flagged { return "flag.fill" }
        if todo.isBlocker { return "exclamationmark" }
        if todo.forLater { return "archivebox" }
        return "circle"
    }
}

private extension View {
    func agendaDetailSection(padded: Bool = true) -> some View {
        Group {
            if padded {
                padding(14)
            } else {
                self
            }
        }
        .background(AgendaPalette.elevated)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(AgendaPalette.softBorder, lineWidth: 1)
        }
    }
}

