import AgendaSyncCore
import CloudKit
import Combine
import Foundation
#if DEBUG
import os

private let agendaInvalidationPerformanceLog = OSLog(
    subsystem: "com.willschulz.Agenda",
    category: "TypingPerformance"
)
#endif

enum AgendaExactStateValidationError: Error {
    case mismatch
}

enum AgendaExactStateValidator {
    static func validate(
        _ record: CKRecord,
        for entry: AgendaCanonicalManifestEntry
    ) throws -> CKRecord {
        if entry.deleted {
            guard record.recordType == AgendaCloudRecord.tombstoneType else {
                throw AgendaExactStateValidationError.mismatch
            }
            let tombstone = try AgendaCloudRecord.decodeTombstone(record)
            guard
                tombstone.entityKind == entry.entityKind,
                tombstone.stableID == entry.stableID,
                tombstone.revision == entry.revision
            else { throw AgendaExactStateValidationError.mismatch }
        } else {
            guard record.recordType == AgendaCloudRecord.canonicalType else {
                throw AgendaExactStateValidationError.mismatch
            }
            let canonical = try AgendaCloudRecord.decodeCanonical(record)
            guard
                !canonical.deleted,
                canonical.entityKind == entry.entityKind,
                canonical.stableID == entry.stableID,
                canonical.revision == entry.revision,
                entry.structureRevision == nil
                    || canonical.structureRevision == entry.structureRevision
            else { throw AgendaExactStateValidationError.mismatch }
        }
        return record
    }
}

@MainActor
final class MobileSyncController: ObservableObject {
    private(set) var status = "Starting offline store…" {
        willSet {
            guard newValue != status else { return }
            requestUIInvalidation()
        }
    }
    private(set) var lastSuccessfulSyncAt: Date? {
        willSet {
            guard newValue != lastSuccessfulSyncAt else { return }
            requestUIInvalidation()
        }
    }
    @Published var draftText: [String: String] = [:]

    let store = MobileAgendaStore()
    private var engine: AgendaSyncEngine?
    private var storeObservation: AnyCancellable?
    private var pollTask: Task<Void, Never>?
    private var typingInvalidationGate = AgendaTypingInvalidationGate()
    private lazy var database = CKContainer(
        identifier: agendaCloudContainerIdentifier
    ).privateCloudDatabase

    init() {
        storeObservation = store.objectWillChange.sink { [weak self] _ in
            self?.requestUIInvalidation()
        }
    }

    func beginTyping(sessionID: String) {
        typingInvalidationGate.begin(sessionID: sessionID)
    }

    func endTyping(sessionID: String) {
        if typingInvalidationGate.end(sessionID: sessionID) {
            objectWillChange.send()
        }
    }

    private func requestUIInvalidation() {
        let forwarded = typingInvalidationGate.receiveInvalidation()
#if DEBUG
        os_signpost(
            .event,
            log: agendaInvalidationPerformanceLog,
            name: "UIInvalidation",
            "forwarded=%{public}s",
            forwarded ? "yes" : "deferred"
        )
#endif
        if forwarded {
            objectWillChange.send()
        }
    }

    func start() {
        guard engine == nil else { return }
#if DEBUG
        if ProcessInfo.processInfo.environment["AGENDA_PREVIEW_DATA"] == "1" {
            status = "Offline preview — private iCloud is not connected"
            return
        }
#endif
        if ProcessInfo.processInfo.environment["AGENDA_DISABLE_CLOUDKIT"] == "1" {
            status = "Offline test — showing only cached Agenda data"
            return
        }
        status = store.projects.isEmpty
            ? "Connecting to private iCloud…"
            : "Showing offline data; checking iCloud…"
        store.prepareEngineState(version: 4)
        let engine = AgendaSyncEngine(store: store)
        self.engine = engine
        startPolling()
        Task {
            do {
                await engine.start()
                let account = try await engine.accountStatus()
                guard account == .available else {
                    status = "iCloud unavailable — offline data remains available"
                    return
                }
                try await runSyncCycle(engine: engine)
                print("AgendaMobile: CloudKit fetch completed")
                try await runConfiguredTestEditIfNeeded(engine: engine)
            } catch {
                status = "Offline — cached Agenda data remains available"
                print("AgendaMobile: sync failed: \(error)")
            }
        }
    }

    func syncNow() {
        guard let engine else {
            start()
            return
        }
        status = "Checking private iCloud…"
        Task {
            do {
                try await runSyncCycle(engine: engine)
            } catch {
                status = "Offline — cached Agenda data remains available"
            }
        }
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(30))
                guard !Task.isCancelled, let self, let engine = self.engine else {
                    return
                }
                do {
                    try await self.runSyncCycle(engine: engine)
                } catch {
                    self.status = "Offline — cached Agenda data remains available"
                }
            }
        }
    }

    /// One sync pass. Network and CloudKit failures propagate and show as
    /// Offline. A store safety guard rejecting the manifest cycle does not:
    /// the remaining steps still run, so mutation results and board order
    /// keep arriving while held work waits, and the status names the reason.
    private func runSyncCycle(engine: AgendaSyncEngine) async throws {
        try await flushPending(engine: engine)
        try await engine.fetchNow()
        var rejection: AgendaSyncCycleRejected?
        do {
            try await fetchCanonicalManifest()
        } catch let error as AgendaSyncCycleRejected {
            rejection = error
        }
        try await fetchBoardOrder()
        try await fetchPendingResults()
        if let rejection {
            markPaused(reason: rejection.reason)
        } else {
            markSynced()
        }
    }

    func approveHeldDeletions(fingerprint: String) {
        guard store.approveHeldDeletions(fingerprint: fingerprint) else { return }
        syncNow()
    }

    private func fetchCanonicalManifest() async throws {
        let manifestResult = try await database.records(
            for: [AgendaCloudRecord.canonicalManifestID]
        )[AgendaCloudRecord.canonicalManifestID]
        guard let manifest = try manifestResult?.get() else { return }
        let entries = try AgendaCloudRecord.decodeCanonicalManifest(manifest)
        let needed = store.canonicalRecordsNeeded(entries)
        let ids = needed.map(AgendaCloudRecord.stateRecordID)
        guard !ids.isEmpty else { return }
        let fetched = try await database.agendaRecords(for: ids)
        let records = try needed.map { entry in
            let id = AgendaCloudRecord.stateRecordID(for: entry)
            guard let result = fetched[id] else {
                throw URLError(.badServerResponse)
            }
            let record = try result.get()
            return try AgendaExactStateValidator.validate(record, for: entry)
        }
        let accepted = await store.applyFetchedCycle(
            AgendaCloudFetchCycle(
                records: records,
                physicalDeletions: [],
                zoneDeleted: false
            )
        )
        guard accepted else {
            throw AgendaSyncCycleRejected(
                reason: store.lastError ?? "A sync safety check held this cycle."
            )
        }
    }

    private func fetchBoardOrder() async throws {
        do {
            guard
                let result = try await database.records(
                for: [AgendaCloudRecord.boardOrderID]
                )[AgendaCloudRecord.boardOrderID]
            else { return }
            let record = try result.get()
            store.applyBoardOrder(
                try AgendaCloudRecord.decodeBoardOrder(record)
            )
        } catch let error as CKError where error.code == .unknownItem {
            // Older Mac builds do not publish this optional presentation hint.
            return
        } catch {
            // Ordering is an optional presentation hint. Preserve the last
            // good cached order without blocking mutation/result sync.
            print("AgendaMobile: ignored board-order record: \(error)")
        }
    }

    private func fetchPendingResults() async throws {
        let ids: [CKRecord.ID] = store.pendingMutationRecords().compactMap {
            record -> CKRecord.ID? in
            guard
                let mutation = try? AgendaCloudRecord.decodeMutation(record)
            else { return nil }
            return CKRecord.ID(
                recordName: "result-\(mutation.mutationID)",
                zoneID: AgendaCloudRecord.zoneID
            )
        }
        guard !ids.isEmpty else { return }
        let fetched = try await database.agendaRecords(for: ids)
        let records = fetched.values.compactMap { try? $0.get() }
        guard !records.isEmpty else { return }
        await store.applyFetchedCycle(
            AgendaCloudFetchCycle(
                records: records,
                physicalDeletions: [],
                zoneDeleted: false
            )
        )
    }

    private func markPaused(reason: String) {
        let time = Date().formatted(date: .omitted, time: .shortened)
        status = store.heldDeletionBatch != nil
            ? "Checked \(time) · Mac deletions need review"
            : "Checked \(time) · sync paused"
        print("AgendaMobile: sync paused: \(reason)")
    }

    private func markSynced() {
        let now = Date()
        store.clearSyncError()
        lastSuccessfulSyncAt = now
        let time = now.formatted(date: .omitted, time: .shortened)
        if store.pendingMutationCount > 0 {
            let waiting = store.stalledPendingMutationCount > 0
                ? "\(store.stalledPendingMutationCount) stalled"
                : "\(store.pendingMutationCount) waiting for Mac"
            status = "Checked \(time) · \(waiting)"
        } else {
            status = "Last synced \(time)"
        }
    }

    func createTodo(projectID: String, text: String) {
        submit(statusText: "Creating todo…") {
            try self.store.makeCreateTodoMutation(
                projectID: projectID,
                text: text
            )
        }
    }

    func editTodo(todoID: String, text: String) {
        do {
            if let replacement = try store.replacePendingCreateText(
                todoID: todoID,
                text: text
            ) {
                submitReplacement(
                    statusText: "Updating offline create…",
                    replacement: replacement
                )
                return
            }
        } catch {
            status = "Edit could not be queued: \(error.localizedDescription)"
            return
        }
        submit(statusText: "Saving edit…") {
            try self.store.makeEditTodoMutation(todoID: todoID, text: text)
        }
    }

    func saveTodo(todoID: String, values: AgendaTodoEditValues) {
        do {
            if let replacement = try store.replacePendingCreateText(
                todoID: todoID,
                text: values.text
            ) {
                submitReplacement(
                    statusText: "Updating offline create…",
                    replacement: replacement
                )
                return
            }
        } catch {
            status = "Edit could not be queued: \(error.localizedDescription)"
            return
        }
        submit(statusText: "Saving todo…") {
            try self.store.makeTodoUpdateMutation(
                todoID: todoID,
                values: values
            )
        }
    }

    func setCompleted(todoID: String, completed: Bool) {
        submit(statusText: completed ? "Completing todo…" : "Reopening todo…") {
            try self.store.makeCompletionMutation(
                todoID: todoID,
                completed: completed
            )
        }
    }

    func moveTodo(
        projectID: String,
        todoID: String,
        intent: AgendaStructureDropIntent
    ) {
        submit(statusText: "Saving structure…") {
            let plan = try AgendaStructurePlanner.plan(
                todos: self.store.todos.filter {
                    $0.projectID == projectID
                },
                movedID: todoID,
                intent: intent
            )
            return try self.store.makeStructureMutation(
                projectID: projectID,
                updates: plan.updates,
                pendingTodoIDs: plan.pendingTodoIDs
            )
        }
    }

    func deleteTodo(todoID: String) {
        submit(statusText: "Deleting todo…") {
            try self.store.makeDeleteTodoMutation(todoID: todoID)
        }
    }

    func keepMacValue(conflictID: String) {
        store.keepMacValue(conflictID: conflictID)
        status = "Kept the Mac value"
    }

    func retryMine(conflictID: String) {
        submit(statusText: "Retrying against the latest Mac value…") {
            try self.store.retryConflict(conflictID: conflictID)
        }
    }

    private func submit(
        statusText: String,
        makeRecord: @escaping @MainActor () throws -> CKRecord
    ) {
        status = statusText
        let record: CKRecord
        do {
            // Cache the optimistic mutation before yielding so overlapping
            // SwiftUI drop destinations cannot submit against the same base.
            record = try makeRecord()
        } catch {
            status = "Edit could not be queued: \(error.localizedDescription)"
            return
        }
        Task {
            if engine == nil {
                start()
            }
            guard let engine else {
                status = "Saved offline — will send when iCloud reconnects"
                return
            }
            do {
                try await engine.stage([record])
                try await engine.sendNow()
                status = "Sent — waiting for Mac confirmation"
            } catch {
                status = "Saved offline — will retry automatically"
            }
        }
    }

    private func submitReplacement(
        statusText: String,
        replacement: (record: CKRecord, replacedRecordID: CKRecord.ID)
    ) {
        status = statusText
        Task {
            if engine == nil {
                start()
            }
            guard let engine else {
                status = "Saved offline — will send when iCloud reconnects"
                return
            }
            await engine.replaceStagedRecord(
                replacement.replacedRecordID,
                with: replacement.record
            )
            do {
                try await engine.sendNow()
                status = "Sent — waiting for Mac confirmation"
            } catch {
                status = "Saved offline — will retry automatically"
            }
        }
    }

    private func flushPending(engine: AgendaSyncEngine) async throws {
        let records = store.pendingMutationRecords()
        guard !records.isEmpty else { return }
        try await engine.stage(records)
        do {
            try await engine.sendNow()
        } catch {
            // CKSyncEngine can report serverRecordChanged even after the
            // delegate recognizes a semantic replay and clears the durable
            // outbound body. Fetch authoritative results regardless.
            status = "Checking Mac confirmation after retry…"
        }
    }

    func saveTestTodo(_ todo: TodoViewValue) {
        guard let engine else { return }
        let text = (draftText[todo.id] ?? todo.text)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        status = "Sending guarded test edit…"
        Task {
            do {
                let record = try store.makeEditedTestTodoRecord(
                    stableID: todo.id,
                    text: text
                )
                try await engine.stage([record])
                try await engine.sendNow()
                status = "Test edit sent; Mac will conflict-check it"
            } catch {
                status = "Test edit was not sent: \(error.localizedDescription)"
            }
        }
    }

    private func runConfiguredTestEditIfNeeded(
        engine: AgendaSyncEngine
    ) async throws {
        guard
            let text = ProcessInfo.processInfo.environment["AGENDA_TEST_EDIT_TEXT"],
            let project = store.projects.first(where: {
                $0.name == agendaSyncTestProjectName
            }),
            let todo = store.todos.first(where: {
                $0.projectID == project.id
            })
        else { return }
        let record = try store.makeEditedTestTodoRecord(
            stableID: todo.id,
            text: text
        )
        try await engine.stage([record])
        print("AgendaMobile: staged test edit \(todo.id)")
        try await engine.sendNow()
        status = "On-device test edit sent to CloudKit"
        print("AgendaMobile: test edit send completed")
    }
}

struct AgendaSyncCycleRejected: Error {
    let reason: String
}
