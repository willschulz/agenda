import AgendaSyncCore
import CloudKit
import Foundation
import SwiftData
#if DEBUG
import os

private let agendaPersistencePerformanceLog = OSLog(
    subsystem: "com.willschulz.Agenda",
    category: "TypingPerformance"
)
#endif

enum MobileAgendaStoreError: LocalizedError {
    case structureMutationPending

    var errorDescription: String? {
        switch self {
        case .structureMutationPending:
            "Wait for the current structure change to finish."
        }
    }
}

struct AgendaTodoConflict: Identifiable, Hashable {
    let mutationID: String
    let todoID: String
    let projectID: String?
    let macValue: String
    let proposedValue: String
    let isStructural: Bool
    let reason: String

    var id: String { mutationID }

    var reasonSummary: String {
        switch reason {
        case "stale_structure_revision":
            "The Mac structure changed before this move arrived."
        case "stale_revision":
            "The Mac project changed before this edit arrived."
        case "invalid_payload":
            "The proposed structure could not be validated."
        case "entity_deleted":
            "The Mac deleted this todo before the edit arrived."
        default:
            "The Mac could not apply this edit (\(reason))."
        }
    }
}

struct AgendaPendingMutationSummary: Equatable {
    let count: Int
    let stalledCount: Int
    let oldestObservedAt: Date?
}

/// Mac deletions that tripped the collapse guard, held for human review
/// instead of failing every sync cycle. Approving applies exactly this set;
/// only the count cap is bypassed, and the explicit-descendant checks still run.
struct AgendaHeldDeletionBatch: Codable, Equatable {
    let tombstones: [AgendaTombstoneRecord]
    let projectCount: Int
    let todoCount: Int
    let heldAt: Date

    var fingerprint: String {
        tombstones.map(\.id).sorted().joined(separator: "|")
    }
}

struct AgendaHeldDeletionItem: Identifiable, Equatable {
    let id: String
    let kind: AgendaContractEntityKind
    let title: String
    let projectName: String?
}

@Model
final class CachedProject {
    @Attribute(.unique) var stableID: String
    var name: String
    var category: String
    var displayOrder: Int
    var archived: Bool
    var payload: Data
    var systemFields: Data?
    var syncRevision: Int = 0
    var structureRevision: Int = 0
    var deletedAt: String?

    init(value: AgendaProjectRecord, payload: Data, systemFields: Data?) {
        stableID = value.stableID
        name = value.name
        category = value.category
        displayOrder = value.displayOrder
        archived = value.archived
        self.payload = payload
        self.systemFields = systemFields
        syncRevision = 0
        structureRevision = value.structureRevision ?? 0
        deletedAt = nil
    }
}

@Model
final class CachedTodo {
    @Attribute(.unique) var stableID: String
    var projectStableID: String
    var parentStableID: String?
    var text: String
    var displayOrder: Int
    var completed: Bool
    var updatedAt: String
    var payload: Data
    var systemFields: Data?
    var syncRevision: Int = 0
    var deletedAt: String?

    init(value: AgendaTodoRecord, payload: Data, systemFields: Data?) {
        stableID = value.stableID
        projectStableID = value.projectStableID
        parentStableID = value.parentStableID
        text = value.text
        displayOrder = value.displayOrder
        completed = value.completed
        updatedAt = value.updatedAt
        self.payload = payload
        self.systemFields = systemFields
        syncRevision = 0
        deletedAt = nil
    }
}

@Model
final class SyncMetadata {
    @Attribute(.unique) var key: String
    var value: Data

    init(key: String, value: Data) {
        self.key = key
        self.value = value
    }
}

@Model
final class CachedTombstone {
    @Attribute(.unique) var recordID: String
    var entityKind: String
    var stableID: String
    var projectStableID: String?
    var parentStableID: String?
    var revision: Int
    var deletedAt: String

    init(value: AgendaTombstoneRecord) {
        recordID = value.id
        entityKind = value.entityKind.rawValue
        stableID = value.stableID
        projectStableID = value.projectStableID
        parentStableID = value.parentStableID
        revision = value.revision
        deletedAt = value.deletedAt
    }
}

@MainActor
final class MobileAgendaStore: ObservableObject, AgendaCloudSyncStore, @unchecked Sendable {
    @Published private(set) var projects: [ProjectViewValue] = []
    @Published private(set) var todos: [TodoViewValue] = []
    @Published private(set) var lastError: String?
    @Published private(set) var pendingTodoIDs = Set<String>()
    @Published private(set) var pendingCreateTodoIDs = Set<String>()
    @Published private(set) var pendingProjectIDs = Set<String>()
    @Published private(set) var pendingMutationCount = 0
    @Published private(set) var stalledPendingMutationCount = 0
    @Published private(set) var oldestPendingMutationAt: Date?
    @Published private(set) var conflicts: [AgendaTodoConflict] = []
    @Published private(set) var boardOrderRows: [String: [String]] = [:]
    /// The user's rows from the Mac (nil until a snapshot carrying them arrives).
    @Published private(set) var rowDefinitions: [AgendaRowDefinition]?
    @Published private(set) var heldDeletionBatch: AgendaHeldDeletionBatch?

    private let container: ModelContainer
    private var context: ModelContext { container.mainContext }
    private var tombstoneRevisions: [String: Int] = [:]
    static let pendingStallInterval: TimeInterval = 120

    init(inMemory: Bool = false) {
        do {
            let configuration = ModelConfiguration(
                "AgendaOffline",
                isStoredInMemoryOnly: inMemory,
                cloudKitDatabase: .none
            )
            container = try ModelContainer(
                for: CachedProject.self,
                CachedTodo.self,
                CachedTombstone.self,
                SyncMetadata.self,
                configurations: configuration
            )
        } catch {
            fatalError("Could not open Agenda's offline store: \(error)")
        }
        reloadTombstoneIndex()
        reloadBoardOrder()
        heldDeletionBatch = metadataValue(key: Self.heldDeletionKey).flatMap {
            try? JSONDecoder().decode(AgendaHeldDeletionBatch.self, from: $0)
        }
        reloadPublishedValues()
#if DEBUG
        if ProcessInfo.processInfo.environment["AGENDA_PREVIEW_DATA"] == "1" {
            projects = AgendaPreviewData.projects
            todos = AgendaPreviewData.todos
        }
#endif
    }

    func loadEngineState() async -> CKSyncEngine.State.Serialization? {
        let key = "cksyncengine-state"
        var descriptor = FetchDescriptor<SyncMetadata>(
            predicate: #Predicate { $0.key == key }
        )
        descriptor.fetchLimit = 1
        guard
            let data = try? context.fetch(descriptor).first?.value
        else { return nil }
        return try? JSONDecoder().decode(
            CKSyncEngine.State.Serialization.self,
            from: data
        )
    }

    func applyBoardOrder(_ snapshot: AgendaBoardOrderSnapshot) {
        guard snapshot.schemaVersion == agendaContractSchemaVersion else { return }
        let key = "board-order-v2"
        if let existing = metadataValue(key: key),
           let cached = try? JSONDecoder().decode(
               AgendaBoardOrderSnapshot.self,
               from: existing
           ),
           cached.generatedAt > snapshot.generatedAt {
            return
        }
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        upsertMetadata(key: key, value: data)
        if boardOrderRows != snapshot.rows {
            boardOrderRows = snapshot.rows
        }
        if rowDefinitions != snapshot.rowDefinitions {
            rowDefinitions = snapshot.rowDefinitions
        }
        try? context.save()
    }

    private func reloadBoardOrder() {
        guard
            let data = metadataValue(key: "board-order-v2"),
            let snapshot = try? JSONDecoder().decode(
                AgendaBoardOrderSnapshot.self,
                from: data
            ),
            snapshot.schemaVersion == agendaContractSchemaVersion
        else { return }
        if boardOrderRows != snapshot.rows {
            boardOrderRows = snapshot.rows
        }
        if rowDefinitions != snapshot.rowDefinitions {
            rowDefinitions = snapshot.rowDefinitions
        }
    }

    func saveEngineState(
        _ state: CKSyncEngine.State.Serialization
    ) async {
        guard let data = try? JSONEncoder().encode(state) else { return }
        upsertMetadata(key: "cksyncengine-state", value: data)
        try? context.save()
    }

    func prepareEngineState(version: Int) {
        cleanupOrphanedConflictMetadata()
        let key = "cksyncengine-state-version"
        let current = metadataValue(key: key).flatMap {
            try? JSONDecoder().decode(Int.self, from: $0)
        } ?? 0
        guard current < version else { return }
        deleteMetadata(key: "cksyncengine-state")
        if let data = try? JSONEncoder().encode(version) {
            upsertMetadata(key: key, value: data)
        }
        try? context.save()
    }

    private func cleanupOrphanedConflictMetadata() {
        let all = (try? context.fetch(FetchDescriptor<SyncMetadata>())) ?? []
        let proposalIDs = Set(
            all.compactMap {
                $0.key.hasPrefix("conflict-proposal-")
                    ? String($0.key.dropFirst("conflict-proposal-".count))
                    : nil
            }
        )
        for metadata in all where metadata.key.hasPrefix("mutation-conflict-") {
            let mutationID = String(
                metadata.key.dropFirst("mutation-conflict-".count)
            )
            if !proposalIDs.contains(mutationID) {
                context.delete(metadata)
            }
        }
        try? context.save()
    }

    @discardableResult
    func applyFetchedCycle(_ cycle: AgendaCloudFetchCycle) async -> Bool {
        if metadataBool(key: "account-quarantined") {
            lastError = "iCloud account changed; remote work is quarantined."
            return false
        }
        var tombstones: [AgendaTombstoneRecord] = []
        var unreadableTombstone = false
        for record in cycle.records where record.recordType == AgendaCloudRecord.tombstoneType {
            do {
                tombstones.append(try AgendaCloudRecord.decodeTombstone(record))
            } catch {
                unreadableTombstone = true
            }
        }

        applyCanonicalV2Records(cycle.records)
        applyResultRecords(cycle.records)
        applyCanonicalRecords(cycle.records)

        var cycleAccepted = false
        if cycle.zoneDeleted {
            lastError = "CloudKit zone deletion quarantined; cached data was preserved."
        } else if !cycle.physicalDeletions.isEmpty {
            lastError = "Physical CloudKit deletions quarantined; cached data was preserved."
        } else if unreadableTombstone {
            lastError = "A newer or unreadable tombstone was quarantined."
        } else {
            cycleAccepted = applyGuardedTombstones(tombstones)
        }
        if cycleAccepted {
            updateLiveCountBaselines()
        }
        try? context.save()
        reloadPublishedValues()
        return cycleAccepted
    }

    private func applyCanonicalRecords(_ records: [CKRecord]) {
        for record in records {
            do {
                let systemFields = try AgendaCloudRecord.archiveSystemFields(record)
                switch record.recordType {
                case AgendaCloudRecord.projectType:
                    let value = try AgendaCloudRecord.decodeProject(record)
                    try upsert(project: value, systemFields: systemFields)
                case AgendaCloudRecord.todoType:
                    let value = try AgendaCloudRecord.decodeTodo(record)
                    try upsert(todo: value, systemFields: systemFields)
                default:
                    continue
                }
            } catch {
                lastError = "Skipped an unreadable CloudKit record."
            }
        }
    }

    private func applyCanonicalV2Records(_ records: [CKRecord]) {
        for record in records where record.recordType == AgendaCloudRecord.canonicalType {
            do {
                let canonical = try AgendaCloudRecord.decodeCanonical(record)
                let priorTombstone = tombstoneRevision(
                    kind: canonical.entityKind,
                    stableID: canonical.stableID
                ) ?? -1
                let priorLive = liveRevision(
                    kind: canonical.entityKind,
                    stableID: canonical.stableID
                ) ?? -1
                guard
                    canonical.revision > priorTombstone,
                    canonical.revision >= priorLive
                else { continue }
                clearTombstones(
                    kind: canonical.entityKind,
                    stableID: canonical.stableID
                )
                let systemFields = try AgendaCloudRecord.archiveSystemFields(record)
                let payload = Data(canonical.payloadJSON.utf8)
                if canonical.entityKind == .project {
                    let value = try JSONDecoder().decode(
                        AgendaProjectRecord.self, from: payload
                    )
                    try upsert(project: value, systemFields: systemFields)
                    try applyCanonicalStructure(
                        payload: canonical.payloadJSON,
                        projectID: canonical.stableID
                    )
                    setRevision(
                        kind: .project,
                        stableID: canonical.stableID,
                        revision: canonical.revision
                    )
                    setStructureRevision(
                        stableID: canonical.stableID,
                        revision: canonical.structureRevision
                            ?? value.structureRevision
                            ?? 0
                    )
                } else {
                    let value = try JSONDecoder().decode(
                        AgendaTodoRecord.self, from: payload
                    )
                    try upsert(todo: value, systemFields: systemFields)
                    setRevision(
                        kind: .todo,
                        stableID: canonical.stableID,
                        revision: canonical.revision
                    )
                }
                reconcileStoredResults(entityID: canonical.stableID)
            } catch {
                lastError = "A schema-2 canonical record was quarantined."
            }
        }
    }

    private func applyResultRecords(_ records: [CKRecord]) {
        for record in records where record.recordType == AgendaCloudRecord.resultType {
            do {
                let result = try AgendaCloudRecord.decodeResult(record)
                let resultData = try JSONEncoder().encode(result)
                upsertMetadata(
                    key: "mutation-result-\(result.mutationID)",
                    value: resultData
                )
                if let pending = metadataValue(
                    key: "pending-mutation-\(result.mutationID)"
                ),
                   let proposal = try? JSONDecoder().decode(
                       AgendaMutationProposal.self, from: pending
                   ),
                   AgendaSyncContract.fingerprint(mutation: proposal)
                    == result.mutationFingerprint {
                    if result.outcome == .conflict {
                        upsertMetadata(
                            key: "conflict-proposal-\(result.mutationID)",
                            value: pending
                        )
                        clearPendingMutationMetadata(
                            mutationID: result.mutationID,
                            clearStructureScope: false
                        )
                    } else if result.outcome == .quarantine {
                        lastError = "A pending edit was quarantined: \(result.reason)."
                    }
                }
                if result.outcome == .conflict {
                    upsertMetadata(
                        key: "mutation-conflict-\(result.mutationID)",
                        value: resultData
                    )
                }
                reconcilePending(result: result)
            } catch {
                lastError = "An unreadable mutation result was quarantined."
            }
        }
    }

    func recordSavesSucceeded(_ records: [CKRecord]) async {
        applyCanonicalRecords(records)
        try? context.save()
        reloadPublishedValues()
    }

    func loadPendingRecords() async throws -> [CKRecord] {
        guard let data = metadataValue(key: "pending-records") else {
            return []
        }
        let archived = try JSONDecoder().decode([String: Data].self, from: data)
        let records = archived.values.compactMap(
            AgendaCloudRecord.unarchiveRecord
        )
        guard records.count == archived.count else {
            throw CocoaError(.fileReadCorruptFile)
        }
        return records
    }

    func persistPendingRecords(_ records: [CKRecord]) async throws {
        var archived: [String: Data] = [:]
        if let data = metadataValue(key: "pending-records") {
            archived = try JSONDecoder().decode([String: Data].self, from: data)
        }
        for record in records {
            archived[record.recordID.recordName] = try AgendaCloudRecord.archiveRecord(
                record
            )
        }
        upsertMetadata(
            key: "pending-records",
            value: try JSONEncoder().encode(archived)
        )
        try context.save()
    }

    func removePendingRecordIDs(_ recordIDs: [CKRecord.ID]) async {
        guard let data = metadataValue(key: "pending-records") else { return }
        guard var archived = try? JSONDecoder().decode(
            [String: Data].self,
            from: data
        ) else {
            lastError = "The durable CloudKit queue is unreadable and was preserved."
            return
        }
        for recordID in recordIDs {
            archived[recordID.recordName] = nil
        }
        if let value = try? JSONEncoder().encode(archived) {
            upsertMetadata(key: "pending-records", value: value)
            try? context.save()
        }
    }

    func accountChanged() async {
        setMetadataBool(key: "account-quarantined", value: true)
        try? context.save()
    }

    func accountAvailable(accountMarker: String?) async {
        guard let accountMarker else { return }
        let prior: String?
        if let data = metadataValue(key: "account-marker") {
            prior = try? JSONDecoder().decode(String.self, from: data)
        } else {
            prior = nil
        }
        if prior == nil,
           let data = try? JSONEncoder().encode(accountMarker) {
            upsertMetadata(key: "account-marker", value: data)
        }
        if prior == nil || prior == accountMarker {
            setMetadataBool(key: "account-quarantined", value: false)
        }
        try? context.save()
    }

    func reportSyncError(_ message: String) async {
        if lastError != message {
            lastError = message
        }
        print("AgendaMobile: \(message)")
    }

    func clearSyncError() {
        if lastError != nil {
            lastError = nil
        }
    }

    func cachedTombstoneState(
        entityKind: AgendaContractEntityKind,
        stableID: String
    ) -> (deletedAt: String?, payload: Data)? {
        let id = stableID
        if entityKind == .project {
            var descriptor = FetchDescriptor<CachedProject>(
                predicate: #Predicate { $0.stableID == id }
            )
            descriptor.fetchLimit = 1
            guard let cached = try? context.fetch(descriptor).first else {
                return nil
            }
            return (cached.deletedAt, cached.payload)
        }
        var descriptor = FetchDescriptor<CachedTodo>(
            predicate: #Predicate { $0.stableID == id }
        )
        descriptor.fetchLimit = 1
        guard let cached = try? context.fetch(descriptor).first else {
            return nil
        }
        return (cached.deletedAt, cached.payload)
    }

    func hasTombstoneSuppression(
        entityKind: AgendaContractEntityKind,
        stableID: String
    ) -> Bool {
        tombstoneRevision(kind: entityKind, stableID: stableID) != nil
    }

    func cachePendingMutation(
        _ mutation: AgendaMutationProposal,
        pendingTodoIDs: Set<String> = []
    ) throws {
        upsertMetadata(
            key: "pending-mutation-\(mutation.mutationID)",
            value: try JSONEncoder().encode(mutation)
        )
        let observedAtKey = "pending-mutation-observed-at-\(mutation.mutationID)"
        if metadataValue(key: observedAtKey) == nil {
            upsertMetadata(
                key: observedAtKey,
                value: try JSONEncoder().encode(Date())
            )
        }
        if !pendingTodoIDs.isEmpty {
            upsertMetadata(
                key: "pending-structure-scope-\(mutation.mutationID)",
                value: try JSONEncoder().encode(pendingTodoIDs.sorted())
            )
        }
        try context.save()
        reloadPublishedValues()
    }

    func isMutationPending(_ mutationID: String) -> Bool {
        metadataValue(key: "pending-mutation-\(mutationID)") != nil
    }

    func hasPendingCreate(todoID: String) -> Bool {
        pendingCreateTodoIDs.contains(todoID)
    }

    func pendingMutationRecords() -> [CKRecord] {
        pendingMutations().compactMap { try? AgendaCloudRecord.mutation($0) }
    }

    func replacePendingCreateText(
        todoID: String,
        text: String
    ) throws -> (record: CKRecord, replacedRecordID: CKRecord.ID)? {
        guard
            let prior = pendingMutations().first(where: {
                $0.entityID == todoID && $0.operation == .create
            }),
            let pendingData = metadataValue(key: "pending-records"),
            var archived = try? JSONDecoder().decode(
                [String: Data].self,
                from: pendingData
            ),
            archived["mutation-\(prior.mutationID)"] != nil,
            let raw = prior.proposedPayload?.data(using: .utf8),
            let payload = try? JSONSerialization.jsonObject(with: raw)
                as? [String: Any],
            let projectID = payload["project_stable_id"] as? String
        else { return nil }

        let replacement = AgendaMutationProposal(
            schemaVersion: agendaContractSchemaVersion,
            mutationID: UUID().uuidString.lowercased(),
            deviceID: prior.deviceID,
            entityID: prior.entityID,
            operation: .create,
            baseRevision: 0,
            proposedPayload: try canonicalJSON([
                "project_stable_id": projectID,
                "text": text.trimmingCharacters(in: .whitespacesAndNewlines),
            ]),
            entityKind: .todo
        )
        let oldRecord = try AgendaCloudRecord.mutation(prior)
        let newRecord = try AgendaCloudRecord.mutation(replacement)
        archived[oldRecord.recordID.recordName] = nil
        archived[newRecord.recordID.recordName] = try AgendaCloudRecord.archiveRecord(
            newRecord
        )
        clearPendingMutationMetadata(mutationID: prior.mutationID)
        upsertMetadata(
            key: "pending-mutation-\(replacement.mutationID)",
            value: try JSONEncoder().encode(replacement)
        )
        upsertMetadata(
            key: "pending-mutation-observed-at-\(replacement.mutationID)",
            value: try JSONEncoder().encode(Date())
        )
        upsertMetadata(
            key: "pending-records",
            value: try JSONEncoder().encode(archived)
        )
        try context.save()
        reloadPublishedValues()
        return (newRecord, oldRecord.recordID)
    }

    func canonicalRecordsNeeded(
        _ entries: [AgendaCanonicalManifestEntry]
    ) -> [AgendaCanonicalManifestEntry] {
        let projects = (
            try? context.fetch(FetchDescriptor<CachedProject>())
        ) ?? []
        let todos = (
            try? context.fetch(FetchDescriptor<CachedTodo>())
        ) ?? []
        let projectRevisions = Dictionary(
            uniqueKeysWithValues: projects.map {
                (
                    $0.stableID,
                    (scalar: $0.syncRevision, structure: $0.structureRevision)
                )
            }
        )
        let todoRevisions = Dictionary(
            uniqueKeysWithValues: todos.map {
                ($0.stableID, $0.syncRevision)
            }
        )
        return entries.filter { entry in
            if entry.deleted {
                if let revision = tombstoneRevision(
                    kind: entry.entityKind,
                    stableID: entry.stableID
                ), revision >= entry.revision {
                    return false
                }
                let liveRevision: Int?
                if entry.entityKind == .project {
                    liveRevision = projectRevisions[entry.stableID]?.scalar
                } else {
                    liveRevision = todoRevisions[entry.stableID]
                }
                return (liveRevision ?? -1) <= entry.revision
            }
            if entry.entityKind == .project {
                guard let local = projectRevisions[entry.stableID] else {
                    return true
                }
                return local.scalar < entry.revision
                    || local.structure < (entry.structureRevision ?? 0)
            }
            guard let local = todoRevisions[entry.stableID] else { return true }
            return local < entry.revision
        }
    }

    func makeCreateTodoMutation(
        projectID: String,
        text: String
    ) throws -> CKRecord {
        let payload = try canonicalJSON([
            "project_stable_id": projectID,
            "text": text.trimmingCharacters(in: .whitespacesAndNewlines),
        ])
        return try prepareMutation(
            entityID: UUID().uuidString.lowercased(),
            operation: .create,
            baseRevision: 0,
            proposedPayload: payload
        )
    }

    func makeEditTodoMutation(todoID: String, text: String) throws -> CKRecord {
        try prepareMutation(
            entityID: todoID,
            operation: .scalarEdit,
            baseRevision: try todoRevision(todoID),
            proposedPayload: canonicalJSON([
                "text": text.trimmingCharacters(in: .whitespacesAndNewlines),
            ])
        )
    }

    func makeCompletionMutation(
        todoID: String,
        completed: Bool
    ) throws -> CKRecord {
        try prepareMutation(
            entityID: todoID,
            operation: .scalarEdit,
            baseRevision: try todoRevision(todoID),
            proposedPayload: canonicalJSON(["completed": completed])
        )
    }

    func makeTodoUpdateMutation(
        todoID: String,
        values: AgendaTodoEditValues
    ) throws -> CKRecord {
        try prepareMutation(
            entityID: todoID,
            operation: .scalarEdit,
            baseRevision: try todoRevision(todoID),
            proposedPayload: canonicalJSON([
                "text": values.text.trimmingCharacters(
                    in: .whitespacesAndNewlines
                ),
                "is_blocker": values.isBlocker,
                "for_later": values.forLater,
                "in_progress": values.completed ? false : values.inProgress,
                "flagged": values.completed ? false : values.flagged,
                "awaiting_review": values.completed
                    ? false : values.awaitingReview,
                "completed": values.completed,
                "effort": values.effort ?? NSNull(),
                "deadline": values.deadline ?? NSNull(),
            ])
        )
    }

    func makeStructureMutation(
        projectID: String,
        updates: [AgendaStructureUpdate],
        pendingTodoIDs: Set<String>
    ) throws -> CKRecord {
        guard !pendingProjectIDs.contains(projectID) else {
            throw MobileAgendaStoreError.structureMutationPending
        }
        let revisions = try projectRevisions(projectID)
        return try prepareMutation(
            entityID: projectID,
            operation: .structureEdit,
            baseRevision: revisions.scalar,
            baseStructureRevision: revisions.structure,
            proposedPayload: try structurePayload(updates),
            entityKind: .project,
            pendingTodoIDs: pendingTodoIDs
        )
    }

    func makeDeleteTodoMutation(todoID: String) throws -> CKRecord {
        try prepareMutation(
            entityID: todoID,
            operation: .delete,
            baseRevision: try todoRevision(todoID),
            proposedPayload: nil
        )
    }

    func keepMacValue(conflictID: String) {
        deleteMetadata(key: "conflict-proposal-\(conflictID)")
        deleteMetadata(key: "mutation-conflict-\(conflictID)")
        deleteMetadata(key: "pending-structure-scope-\(conflictID)")
        try? context.save()
        reloadPublishedValues()
    }

    func retryConflict(conflictID: String) throws -> CKRecord {
        guard
            let data = metadataValue(key: "conflict-proposal-\(conflictID)"),
            let prior = try? JSONDecoder().decode(
                AgendaMutationProposal.self, from: data
            )
        else { throw CocoaError(.fileNoSuchFile) }
        if prior.entityKind == .project {
            let revisions = try projectRevisions(prior.entityID)
            let pendingScope = structurePendingScope(
                mutationID: conflictID,
                fallback: []
            )
            var proposedPayload = prior.proposedPayload
            if prior.operation == .structureEdit,
               let raw = prior.proposedPayload?.data(using: .utf8),
               let object = try JSONSerialization.jsonObject(with: raw)
                    as? [String: Any],
               let rawUpdates = object["updates"] {
                let data = try JSONSerialization.data(withJSONObject: rawUpdates)
                let updates = try JSONDecoder().decode(
                    [AgendaStructureUpdate].self,
                    from: data
                )
                proposedPayload = try structurePayload(
                    AgendaStructurePlanner.rebasedUpdates(
                        todos: todos.filter {
                            $0.projectID == prior.entityID
                        },
                        proposed: updates,
                        requiredIDs: pendingScope
                    )
                )
            }
            keepMacValue(conflictID: conflictID)
            return try prepareMutation(
                entityID: prior.entityID,
                operation: prior.operation,
                baseRevision: revisions.scalar,
                baseStructureRevision: revisions.structure,
                proposedPayload: proposedPayload,
                entityKind: .project,
                pendingTodoIDs: pendingScope
            )
        }
        keepMacValue(conflictID: conflictID)
        return try prepareMutation(
            entityID: prior.entityID,
            operation: prior.operation,
            baseRevision: try todoRevision(prior.entityID),
            proposedPayload: prior.proposedPayload
        )
    }

    private func prepareMutation(
        entityID: String,
        operation: AgendaMutationOperation,
        baseRevision: Int,
        baseStructureRevision: Int = 0,
        proposedPayload: String?,
        entityKind: AgendaContractEntityKind = .todo,
        pendingTodoIDs: Set<String> = []
    ) throws -> CKRecord {
        let mutation = AgendaMutationProposal(
            schemaVersion: agendaContractSchemaVersion,
            mutationID: UUID().uuidString.lowercased(),
            deviceID: deviceID(),
            entityID: entityID,
            operation: operation,
            baseRevision: baseRevision,
            baseStructureRevision: baseStructureRevision,
            proposedPayload: proposedPayload,
            entityKind: entityKind
        )
        try cachePendingMutation(mutation, pendingTodoIDs: pendingTodoIDs)
        return try AgendaCloudRecord.mutation(mutation)
    }

    private func todoRevision(_ stableID: String) throws -> Int {
        let id = stableID
        var descriptor = FetchDescriptor<CachedTodo>(
            predicate: #Predicate { $0.stableID == id }
        )
        descriptor.fetchLimit = 1
        guard let cached = try context.fetch(descriptor).first else {
            throw CocoaError(.fileNoSuchFile)
        }
        return cached.syncRevision
    }

    private func deviceID() -> String {
        if let data = metadataValue(key: "device-id"),
           let value = try? JSONDecoder().decode(String.self, from: data) {
            return value
        }
        let value = UUID().uuidString.lowercased()
        if let data = try? JSONEncoder().encode(value) {
            upsertMetadata(key: "device-id", value: data)
            try? context.save()
        }
        return value
    }

    private func canonicalJSON(_ value: Any) throws -> String {
        let data = try JSONSerialization.data(
            withJSONObject: value,
            options: [.sortedKeys]
        )
        return String(decoding: data, as: UTF8.self)
    }

    private func structurePayload(
        _ updates: [AgendaStructureUpdate]
    ) throws -> String {
        let values: [[String: Any]] = updates.map {
            [
                "stable_id": $0.stableID,
                "parent_stable_id": $0.parentStableID ?? NSNull(),
                "display_order": $0.displayOrder,
                "for_later": $0.forLater,
            ]
        }
        return try canonicalJSON(["updates": values])
    }

    func makeEditedTestTodoRecord(
        stableID: String,
        text: String
    ) throws -> CKRecord {
        let id = stableID
        var descriptor = FetchDescriptor<CachedTodo>(
            predicate: #Predicate { $0.stableID == id }
        )
        descriptor.fetchLimit = 1
        guard let cached = try context.fetch(descriptor).first else {
            throw CocoaError(.fileNoSuchFile)
        }
        var value = try JSONDecoder().decode(
            AgendaTodoRecord.self,
            from: cached.payload
        )
        value.text = text
        let recordID = CKRecord.ID(
            recordName: "todo-\(stableID)",
            zoneID: AgendaCloudRecord.zoneID
        )
        let record = AgendaCloudRecord.restoreRecord(
            systemFields: cached.systemFields ?? Data(),
            fallbackType: AgendaCloudRecord.todoType,
            fallbackID: recordID
        )
        try AgendaCloudRecord.update(record, with: value)
        cached.text = text
        cached.payload = try JSONEncoder().encode(value)
        try context.save()
        reloadPublishedValues()
        return record
    }

    private func applyGuardedTombstones(
        _ tombstones: [AgendaTombstoneRecord],
        bypassCollapseGuard: Bool = false
    ) -> Bool {
        let cachedProjects = (try? context.fetch(FetchDescriptor<CachedProject>())) ?? []
        let cachedTodos = (try? context.fetch(FetchDescriptor<CachedTodo>())) ?? []
        let projectByID = Dictionary(
            uniqueKeysWithValues: cachedProjects.map { ($0.stableID, $0) }
        )
        let todoByID = Dictionary(
            uniqueKeysWithValues: cachedTodos.map { ($0.stableID, $0) }
        )
        let projectTombstones = tombstones.filter { $0.entityKind == .project }
        let todoTombstones = tombstones.filter { $0.entityKind == .todo }
        let projectDeletionIDs = Set(projectTombstones.map(\.stableID))
        let todoDeletionIDs = Set(todoTombstones.map(\.stableID))
        var trustedOutboundTodoIDs = Set<String>()
        let pendingDeleteRoots = pendingMutations()
            .filter { $0.operation == .delete }
            .map(\.entityID)
        for rootID in pendingDeleteRoots {
            var expected = Set([rootID])
            var changed = true
            while changed {
                changed = false
                for todo in cachedTodos
                where todo.deletedAt == nil
                    && todo.parentStableID.map(expected.contains) == true {
                    if expected.insert(todo.stableID).inserted {
                        changed = true
                    }
                }
            }
            if expected.isSubset(of: todoDeletionIDs) {
                trustedOutboundTodoIDs.formUnion(expected)
            }
        }

        for projectID in projectDeletionIDs {
            let liveChildren = cachedTodos.filter {
                $0.projectStableID == projectID && $0.deletedAt == nil
            }
            if liveChildren.contains(where: { !todoDeletionIDs.contains($0.stableID) }) {
                lastError = "Project tombstone lacked explicit child tombstones; cycle quarantined."
                return false
            }
        }
        if cachedTodos.contains(where: {
            $0.deletedAt == nil
                && $0.parentStableID.map(todoDeletionIDs.contains) == true
                && !todoDeletionIDs.contains($0.stableID)
        }) {
            lastError = "Todo tombstone lacked explicit descendant tombstones; cycle quarantined."
            return false
        }

        let liveProjectTargets = projectDeletionIDs.filter { id in
            guard let cached = projectByID[id] else { return false }
            return cached.deletedAt == nil
        }.count
        let liveTodoTargets = todoDeletionIDs.filter { id in
            guard !trustedOutboundTodoIDs.contains(id) else { return false }
            guard let cached = todoByID[id] else { return false }
            return cached.deletedAt == nil
        }.count
        let projectDecision = AgendaSyncContract.evaluateDeletionBatch(
            entityKind: .project,
            baselineCount: loadBaseline(kind: "project"),
            deletionCount: liveProjectTargets
        )
        let todoDecision = AgendaSyncContract.evaluateDeletionBatch(
            entityKind: .todo,
            baselineCount: loadBaseline(kind: "todo"),
            deletionCount: liveTodoTargets
        )
        let decisions = [projectDecision, todoDecision]
        let onlyCollapseGuard = decisions.allSatisfy {
            $0.outcome == .accept || $0.reason == "collapse_guard"
        }
        if decisions.contains(where: { $0.outcome != .accept }),
           !(onlyCollapseGuard && bypassCollapseGuard) {
            if onlyCollapseGuard {
                holdDeletionBatch(
                    tombstones,
                    projectCount: liveProjectTargets,
                    todoCount: liveTodoTargets
                )
                lastError = Self.heldDeletionMessage(
                    projectCount: liveProjectTargets,
                    todoCount: liveTodoTargets
                )
                return false
            }
            let reason = projectDecision.outcome != .accept
                ? projectDecision.reason
                : todoDecision.reason
            lastError = "Tombstone cycle quarantined: \(reason)."
            return false
        }

        for tombstone in projectTombstones {
            upsert(tombstone: tombstone)
            guard
                let cached = projectByID[tombstone.stableID],
                tombstone.revision >= cached.syncRevision
            else { continue }
            cached.syncRevision = tombstone.revision
            cached.deletedAt = tombstone.deletedAt
        }
        for tombstone in todoTombstones {
            upsert(tombstone: tombstone)
            guard
                let cached = todoByID[tombstone.stableID],
                tombstone.revision >= cached.syncRevision
            else { continue }
            cached.syncRevision = tombstone.revision
            cached.deletedAt = tombstone.deletedAt
            reconcileStoredResults(entityID: tombstone.stableID)
        }
        if !tombstones.isEmpty {
            lastError = nil
        }
        if let held = heldDeletionBatch,
           Set(held.tombstones.map(\.id))
            .isSubset(of: Set(tombstones.map(\.id))) {
            clearHeldDeletionBatch()
        }
        return true
    }

    static let heldDeletionKey = "held-deletion-batch"

    static func heldDeletionMessage(projectCount: Int, todoCount: Int) -> String {
        var parts: [String] = []
        if projectCount > 0 {
            parts.append("\(projectCount) project" + (projectCount == 1 ? "" : "s"))
        }
        if todoCount > 0 {
            parts.append("\(todoCount) todo" + (todoCount == 1 ? "" : "s"))
        }
        let what = parts.isEmpty ? "items" : parts.joined(separator: " and ")
        return "The Mac deleted \(what) while this device was away. "
            + "Review before they are removed here (collapse_guard)."
    }

    private func holdDeletionBatch(
        _ tombstones: [AgendaTombstoneRecord],
        projectCount: Int,
        todoCount: Int
    ) {
        var batch = AgendaHeldDeletionBatch(
            tombstones: tombstones,
            projectCount: projectCount,
            todoCount: todoCount,
            heldAt: Date()
        )
        if let existing = heldDeletionBatch {
            if existing.fingerprint == batch.fingerprint,
               existing.projectCount == projectCount,
               existing.todoCount == todoCount {
                return
            }
            if existing.fingerprint == batch.fingerprint {
                batch = AgendaHeldDeletionBatch(
                    tombstones: tombstones,
                    projectCount: projectCount,
                    todoCount: todoCount,
                    heldAt: existing.heldAt
                )
            }
        }
        guard let data = try? JSONEncoder().encode(batch) else { return }
        upsertMetadata(key: Self.heldDeletionKey, value: data)
        heldDeletionBatch = batch
    }

    private func clearHeldDeletionBatch() {
        deleteMetadata(key: Self.heldDeletionKey)
        if heldDeletionBatch != nil {
            heldDeletionBatch = nil
        }
    }

    /// Human-approved application of the held batch. The fingerprint must
    /// match what the reviewer saw, so a batch that changed underneath the
    /// review sheet is never applied.
    @discardableResult
    func approveHeldDeletions(fingerprint: String) -> Bool {
        guard
            let batch = heldDeletionBatch,
            batch.fingerprint == fingerprint
        else { return false }
        let applied = applyGuardedTombstones(
            batch.tombstones,
            bypassCollapseGuard: true
        )
        if applied {
            clearHeldDeletionBatch()
            updateLiveCountBaselines()
            lastError = nil
        }
        try? context.save()
        reloadPublishedValues()
        return applied
    }

    func heldDeletionItems() -> [AgendaHeldDeletionItem] {
        guard let batch = heldDeletionBatch else { return [] }
        let cachedProjects = (try? context.fetch(FetchDescriptor<CachedProject>())) ?? []
        let cachedTodos = (try? context.fetch(FetchDescriptor<CachedTodo>())) ?? []
        let projectByID = Dictionary(
            cachedProjects.map { ($0.stableID, $0) },
            uniquingKeysWith: { first, _ in first }
        )
        let todoByID = Dictionary(
            cachedTodos.map { ($0.stableID, $0) },
            uniquingKeysWith: { first, _ in first }
        )
        return batch.tombstones.compactMap { tombstone in
            switch tombstone.entityKind {
            case .project:
                guard let project = projectByID[tombstone.stableID],
                      project.deletedAt == nil
                else { return nil }
                return AgendaHeldDeletionItem(
                    id: tombstone.id,
                    kind: .project,
                    title: project.name,
                    projectName: nil
                )
            case .todo:
                guard let todo = todoByID[tombstone.stableID],
                      todo.deletedAt == nil
                else { return nil }
                return AgendaHeldDeletionItem(
                    id: tombstone.id,
                    kind: .todo,
                    title: todo.text,
                    projectName: projectByID[todo.projectStableID]?.name
                )
            }
        }
        .sorted {
            ($0.projectName ?? "", $0.title) < ($1.projectName ?? "", $1.title)
        }
    }

    private func loadBaseline(kind: String) -> Int? {
        let key = "live-\(kind)-baseline"
        var descriptor = FetchDescriptor<SyncMetadata>(
            predicate: #Predicate { $0.key == key }
        )
        descriptor.fetchLimit = 1
        guard
            let data = try? context.fetch(descriptor).first?.value
        else { return nil }
        return try? JSONDecoder().decode(Int.self, from: data)
    }

    private func metadataValue(key: String) -> Data? {
        let lookup = key
        var descriptor = FetchDescriptor<SyncMetadata>(
            predicate: #Predicate { $0.key == lookup }
        )
        descriptor.fetchLimit = 1
        return try? context.fetch(descriptor).first?.value
    }

    private func deleteMetadata(key: String) {
        let lookup = key
        var descriptor = FetchDescriptor<SyncMetadata>(
            predicate: #Predicate { $0.key == lookup }
        )
        descriptor.fetchLimit = 1
        if let existing = try? context.fetch(descriptor).first {
            context.delete(existing)
        }
    }

    private func pendingMutations() -> [AgendaMutationProposal] {
        let all = (try? context.fetch(FetchDescriptor<SyncMetadata>())) ?? []
        return all
            .filter { $0.key.hasPrefix("pending-mutation-") }
            .compactMap {
                try? JSONDecoder().decode(
                    AgendaMutationProposal.self, from: $0.value
                )
            }
    }

    static func pendingSummary(
        mutations: [AgendaMutationProposal],
        observedAt: [String: Date],
        now: Date
    ) -> AgendaPendingMutationSummary {
        let dates = mutations.compactMap { observedAt[$0.mutationID] }
        let stalled = mutations.reduce(into: 0) { count, mutation in
            guard let date = observedAt[mutation.mutationID] else {
                count += 1
                return
            }
            if now.timeIntervalSince(date) >= pendingStallInterval {
                count += 1
            }
        }
        return AgendaPendingMutationSummary(
            count: mutations.count,
            stalledCount: stalled,
            oldestObservedAt: dates.min()
        )
    }

    private func structurePendingScope(
        mutationID: String,
        fallback: Set<String>
    ) -> Set<String> {
        guard
            let data = metadataValue(
                key: "pending-structure-scope-\(mutationID)"
            ),
            let ids = try? JSONDecoder().decode([String].self, from: data)
        else { return fallback }
        return Set(ids)
    }

    private func clearPendingStructureScope(mutationID: String) {
        deleteMetadata(key: "pending-structure-scope-\(mutationID)")
    }

    private func clearPendingMutationMetadata(
        mutationID: String,
        clearStructureScope: Bool = true
    ) {
        deleteMetadata(key: "pending-mutation-\(mutationID)")
        deleteMetadata(key: "pending-mutation-observed-at-\(mutationID)")
        if clearStructureScope {
            clearPendingStructureScope(mutationID: mutationID)
        }
    }

    private func reconcilePending(result: AgendaMutationResultRecord) {
        guard
            result.outcome == .accept || result.outcome == .idempotent,
            let requiredRevision = result.canonicalRevision,
            let proposalData = metadataValue(
                key: "pending-mutation-\(result.mutationID)"
            ),
            let proposal = try? JSONDecoder().decode(
                AgendaMutationProposal.self,
                from: proposalData
            )
        else { return }
        if proposal.operation == .delete {
            guard
                let revision = tombstoneRevision(
                    kind: .todo,
                    stableID: result.entityID
                ),
                revision >= requiredRevision
            else { return }
            clearPendingMutationMetadata(mutationID: result.mutationID)
            return
        }
        guard
            let revision = liveRevision(
                kind: proposal.entityKind ?? .todo,
                stableID: result.entityID
            ),
            revision >= requiredRevision
        else { return }
        if proposal.operation == .structureEdit {
            guard
                let requiredStructureRevision =
                    result.canonicalStructureRevision,
                let local = try? projectRevisions(result.entityID),
                local.structure >= requiredStructureRevision
            else { return }
        }
        clearPendingMutationMetadata(mutationID: result.mutationID)
    }

    private func reconcileStoredResults(entityID: String) {
        let all = (try? context.fetch(FetchDescriptor<SyncMetadata>())) ?? []
        for metadata in all where metadata.key.hasPrefix("mutation-result-") {
            if let result = try? JSONDecoder().decode(
                AgendaMutationResultRecord.self, from: metadata.value
            ), result.entityID == entityID {
                reconcilePending(result: result)
            }
        }
    }

    private func metadataBool(key: String) -> Bool {
        guard let data = metadataValue(key: key) else { return false }
        return (try? JSONDecoder().decode(Bool.self, from: data)) ?? false
    }

    private func setMetadataBool(key: String, value: Bool) {
        guard let data = try? JSONEncoder().encode(value) else { return }
        upsertMetadata(key: key, value: data)
    }

    private func updateLiveCountBaselines() {
        let projectCount = (
            (try? context.fetch(FetchDescriptor<CachedProject>())) ?? []
        ).filter { $0.deletedAt == nil }.count
        let todoCount = (
            (try? context.fetch(FetchDescriptor<CachedTodo>())) ?? []
        ).filter { $0.deletedAt == nil }.count
        if let data = try? JSONEncoder().encode(projectCount) {
            upsertMetadata(key: "live-project-baseline", value: data)
        }
        if let data = try? JSONEncoder().encode(todoCount) {
            upsertMetadata(key: "live-todo-baseline", value: data)
        }
    }

    private func upsert(
        project: AgendaProjectRecord,
        systemFields: Data
    ) throws {
        guard tombstoneRevision(kind: .project, stableID: project.stableID) == nil else {
            return
        }
        let id = project.stableID
        var descriptor = FetchDescriptor<CachedProject>(
            predicate: #Predicate { $0.stableID == id }
        )
        descriptor.fetchLimit = 1
        let payload = try JSONEncoder().encode(project)
        if let cached = try context.fetch(descriptor).first {
            guard cached.deletedAt == nil else { return }
            cached.name = project.name
            cached.category = project.category
            cached.displayOrder = project.displayOrder
            cached.archived = project.archived
            if let revision = project.structureRevision {
                cached.structureRevision = revision
            }
            cached.payload = payload
            cached.systemFields = systemFields
        } else {
            context.insert(
                CachedProject(
                    value: project,
                    payload: payload,
                    systemFields: systemFields
                )
            )
        }
    }

    private func upsert(todo: AgendaTodoRecord, systemFields: Data) throws {
        guard tombstoneRevision(kind: .todo, stableID: todo.stableID) == nil else {
            return
        }
        let id = todo.stableID
        var descriptor = FetchDescriptor<CachedTodo>(
            predicate: #Predicate { $0.stableID == id }
        )
        descriptor.fetchLimit = 1
        let payload = try JSONEncoder().encode(todo)
        if let cached = try context.fetch(descriptor).first {
            guard cached.deletedAt == nil else { return }
            cached.projectStableID = todo.projectStableID
            cached.parentStableID = todo.parentStableID
            cached.text = todo.text
            cached.displayOrder = todo.displayOrder
            cached.completed = todo.completed
            cached.updatedAt = todo.updatedAt
            cached.payload = payload
            cached.systemFields = systemFields
        } else {
            context.insert(
                CachedTodo(
                    value: todo,
                    payload: payload,
                    systemFields: systemFields
                )
            )
        }
    }

    private func applyCanonicalStructure(
        payload: String,
        projectID: String
    ) throws {
        guard
            let data = payload.data(using: .utf8),
            let object = try JSONSerialization.jsonObject(with: data)
                as? [String: Any],
            let updates = object["structure"] as? [[String: Any]]
        else { return }
        let cachedTodos = (
            try? context.fetch(FetchDescriptor<CachedTodo>())
        ) ?? []
        let byID = Dictionary(
            uniqueKeysWithValues: cachedTodos
                .filter { $0.projectStableID == projectID }
                .map { ($0.stableID, $0) }
        )
        for update in updates {
            guard
                let stableID = update["stable_id"] as? String,
                let cached = byID[stableID],
                let displayOrder = update["display_order"] as? Int,
                let forLater = update["for_later"] as? Bool,
                var todoPayload = try JSONSerialization.jsonObject(
                    with: cached.payload
                ) as? [String: Any]
            else { continue }
            let parent = update["parent_stable_id"]
            cached.parentStableID = parent as? String
            cached.displayOrder = displayOrder
            todoPayload["parent_stable_id"] = parent ?? NSNull()
            todoPayload["display_order"] = displayOrder
            todoPayload["for_later"] = forLater
            cached.payload = try JSONSerialization.data(
                withJSONObject: todoPayload,
                options: [.sortedKeys]
            )
        }
    }

    private func upsertMetadata(key: String, value: Data) {
        var descriptor = FetchDescriptor<SyncMetadata>(
            predicate: #Predicate { $0.key == key }
        )
        descriptor.fetchLimit = 1
        if let existing = try? context.fetch(descriptor).first {
            existing.value = value
        } else {
            context.insert(SyncMetadata(key: key, value: value))
        }
    }

    private func tombstoneRevision(
        kind: AgendaContractEntityKind,
        stableID: String
    ) -> Int? {
        tombstoneRevisions["\(kind.rawValue):\(stableID)"]
    }

    private func clearTombstones(
        kind: AgendaContractEntityKind,
        stableID: String
    ) {
        let key = "\(kind.rawValue):\(stableID)"
        tombstoneRevisions[key] = nil
        let all = (try? context.fetch(FetchDescriptor<CachedTombstone>())) ?? []
        for tombstone in all where
            tombstone.entityKind == kind.rawValue
                && tombstone.stableID == stableID {
            context.delete(tombstone)
        }
        if kind == .project {
            let allProjects = (
                try? context.fetch(FetchDescriptor<CachedProject>())
            ) ?? []
            for cached in allProjects where cached.stableID == stableID {
                cached.deletedAt = nil
            }
        } else {
            let allTodos = (try? context.fetch(FetchDescriptor<CachedTodo>())) ?? []
            for cached in allTodos where cached.stableID == stableID {
                cached.deletedAt = nil
            }
        }
    }

    private func setRevision(
        kind: AgendaContractEntityKind,
        stableID: String,
        revision: Int
    ) {
        if kind == .project {
            let all = (try? context.fetch(FetchDescriptor<CachedProject>())) ?? []
            all.first(where: { $0.stableID == stableID })?.syncRevision = revision
        } else {
            let all = (try? context.fetch(FetchDescriptor<CachedTodo>())) ?? []
            all.first(where: { $0.stableID == stableID })?.syncRevision = revision
        }
    }

    private func setStructureRevision(stableID: String, revision: Int) {
        let all = (try? context.fetch(FetchDescriptor<CachedProject>())) ?? []
        all.first(where: { $0.stableID == stableID })?.structureRevision = revision
    }

    private func projectRevisions(_ stableID: String) throws -> (
        scalar: Int,
        structure: Int
    ) {
        let id = stableID
        var descriptor = FetchDescriptor<CachedProject>(
            predicate: #Predicate { $0.stableID == id }
        )
        descriptor.fetchLimit = 1
        guard let cached = try context.fetch(descriptor).first else {
            throw CocoaError(.fileNoSuchFile)
        }
        return (cached.syncRevision, cached.structureRevision)
    }

    private func liveRevision(
        kind: AgendaContractEntityKind,
        stableID: String
    ) -> Int? {
        if kind == .project {
            let all = (try? context.fetch(FetchDescriptor<CachedProject>())) ?? []
            return all.first(where: { $0.stableID == stableID })?.syncRevision
        }
        let all = (try? context.fetch(FetchDescriptor<CachedTodo>())) ?? []
        return all.first(where: { $0.stableID == stableID })?.syncRevision
    }

    private func reloadTombstoneIndex() {
        let all = (try? context.fetch(FetchDescriptor<CachedTombstone>())) ?? []
        tombstoneRevisions = all.reduce(into: [:]) { result, tombstone in
            let key = "\(tombstone.entityKind):\(tombstone.stableID)"
            result[key] = max(result[key] ?? 0, tombstone.revision)
        }
    }

    private func upsert(tombstone: AgendaTombstoneRecord) {
        let key = "\(tombstone.entityKind.rawValue):\(tombstone.stableID)"
        tombstoneRevisions[key] = max(
            tombstoneRevisions[key] ?? 0,
            tombstone.revision
        )
        let all = (try? context.fetch(FetchDescriptor<CachedTombstone>())) ?? []
        if let existing = all.first(where: { $0.recordID == tombstone.id }) {
            existing.deletedAt = tombstone.deletedAt
            return
        }
        context.insert(CachedTombstone(value: tombstone))
    }

    private func reloadPublishedValues() {
#if DEBUG
        let signpostID = OSSignpostID(log: agendaPersistencePerformanceLog)
        os_signpost(
            .begin,
            log: agendaPersistencePerformanceLog,
            name: "ReloadPublishedValues",
            signpostID: signpostID
        )
        defer {
            os_signpost(
                .end,
                log: agendaPersistencePerformanceLog,
                name: "ReloadPublishedValues",
                signpostID: signpostID
            )
        }
#endif
        let projectDescriptor = FetchDescriptor<CachedProject>(
            sortBy: [
                SortDescriptor(\.displayOrder),
                SortDescriptor(\.name),
            ]
        )
        let todoDescriptor = FetchDescriptor<CachedTodo>(
            sortBy: [SortDescriptor(\.displayOrder)]
        )
        let nextProjects = (try? context.fetch(projectDescriptor))?.compactMap {
            cached -> ProjectViewValue? in
            guard !cached.archived, cached.deletedAt == nil else { return nil }
            if let value = try? JSONDecoder().decode(
                AgendaProjectRecord.self,
                from: cached.payload
            ) {
                return ProjectViewValue(
                    id: value.stableID,
                    name: value.name,
                    category: value.category,
                    displayOrder: value.displayOrder,
                    northstars: value.northstars,
                    scalarRevision: cached.syncRevision,
                    structureRevision: cached.structureRevision
                )
            }
            return ProjectViewValue(
                id: cached.stableID,
                name: cached.name,
                category: cached.category,
                displayOrder: cached.displayOrder,
                northstars: [],
                scalarRevision: cached.syncRevision,
                structureRevision: cached.structureRevision
            )
        } ?? []
        var visibleTodos: [TodoViewValue] = (
            try? context.fetch(todoDescriptor)
        )?.compactMap { cached -> TodoViewValue? in
            guard cached.deletedAt == nil else { return nil }
            if let value = try? JSONDecoder().decode(
                AgendaTodoRecord.self,
                from: cached.payload
            ) {
                return TodoViewValue(
                    id: value.stableID,
                    projectID: value.projectStableID,
                    parentID: value.parentStableID,
                    text: value.text,
                    isBlocker: value.isBlocker,
                    forLater: value.forLater,
                    inProgress: value.inProgress,
                    flagged: value.flagged,
                    awaitingReview: value.awaitingReview,
                    displayOrder: value.displayOrder,
                    completed: value.completed,
                    effort: value.effort,
                    deadline: value.deadline
                )
            }
            return TodoViewValue(
                id: cached.stableID,
                projectID: cached.projectStableID,
                parentID: cached.parentStableID,
                text: cached.text,
                isBlocker: false,
                forLater: false,
                inProgress: false,
                flagged: false,
                awaitingReview: false,
                displayOrder: cached.displayOrder,
                completed: cached.completed,
                effort: nil,
                deadline: nil
            )
        } ?? []

        let pending = pendingMutations()
        let observedAt = Dictionary(
            uniqueKeysWithValues: pending.compactMap { mutation in
                metadataValue(
                    key: "pending-mutation-observed-at-\(mutation.mutationID)"
                ).flatMap {
                    try? JSONDecoder().decode(Date.self, from: $0)
                }.map { (mutation.mutationID, $0) }
            }
        )
        let pendingSummary = Self.pendingSummary(
            mutations: pending,
            observedAt: observedAt,
            now: Date()
        )
        let nextPendingProjectIDs = Set(
            pending.filter { $0.entityKind == .project }.map(\.entityID)
        )
        var nextPendingTodoIDs = Set(
            pending.filter { $0.entityKind != .project }.map(\.entityID)
        )
        let nextPendingCreateTodoIDs = Set(
            pending.filter {
                $0.entityKind == .todo && $0.operation == .create
            }.map(\.entityID)
        )
        for mutation in pending {
            if mutation.operation == .delete {
                var suppressed = Set([mutation.entityID])
                var changed = true
                while changed {
                    changed = false
                    for todo in visibleTodos
                    where todo.parentID.map(suppressed.contains) == true {
                        if suppressed.insert(todo.id).inserted {
                            changed = true
                        }
                    }
                }
                nextPendingTodoIDs.formUnion(suppressed)
                visibleTodos.removeAll { suppressed.contains($0.id) }
                continue
            }
            guard
                let raw = mutation.proposedPayload?.data(using: .utf8),
                let payload = try? JSONSerialization.jsonObject(with: raw)
                    as? [String: Any]
            else { continue }
            if mutation.operation == .structureEdit,
               let updates = payload["updates"] as? [[String: Any]] {
                let updateIDs = Set(updates.compactMap {
                    $0["stable_id"] as? String
                })
                nextPendingTodoIDs.formUnion(
                    structurePendingScope(
                        mutationID: mutation.mutationID,
                        fallback: updateIDs
                    )
                )
                for update in updates {
                    guard
                        let stableID = update["stable_id"] as? String,
                        let index = visibleTodos.firstIndex(where: {
                            $0.id == stableID
                        }),
                        let displayOrder = update["display_order"] as? Int,
                        let forLater = update["for_later"] as? Bool
                    else { continue }
                    let current = visibleTodos[index]
                    visibleTodos[index] = TodoViewValue(
                        id: current.id,
                        projectID: current.projectID,
                        parentID: update["parent_stable_id"] as? String,
                        text: current.text,
                        isBlocker: current.isBlocker,
                        forLater: forLater,
                        inProgress: current.inProgress,
                        flagged: current.flagged,
                        awaitingReview: current.awaitingReview,
                        displayOrder: displayOrder,
                        completed: current.completed,
                        effort: current.effort,
                        deadline: current.deadline
                    )
                }
            } else if mutation.operation == .create,
               !visibleTodos.contains(where: { $0.id == mutation.entityID }),
               let projectID = payload["project_stable_id"] as? String,
               let text = payload["text"] as? String {
                visibleTodos.append(
                    TodoViewValue(
                        id: mutation.entityID,
                        projectID: projectID,
                        parentID: nil,
                        text: text,
                        isBlocker: false,
                        forLater: false,
                        inProgress: false,
                        flagged: false,
                        awaitingReview: false,
                        displayOrder: Int.min,
                        completed: false,
                        effort: nil,
                        deadline: nil
                    )
                )
            } else if mutation.operation == .scalarEdit,
                      let index = visibleTodos.firstIndex(where: {
                          $0.id == mutation.entityID
                      }) {
                let current = visibleTodos[index]
                let completed = payload["completed"] as? Bool
                    ?? current.completed
                let inProgress = completed
                    ? false
                    : (payload["in_progress"] as? Bool ?? current.inProgress)
                let flagged = completed
                    ? false
                    : (payload["flagged"] as? Bool ?? current.flagged)
                let awaitingReview = completed
                    ? false
                    : (payload["awaiting_review"] as? Bool
                        ?? current.awaitingReview)
                visibleTodos[index] = TodoViewValue(
                    id: current.id,
                    projectID: current.projectID,
                    parentID: current.parentID,
                    text: payload["text"] as? String ?? current.text,
                    isBlocker: payload["is_blocker"] as? Bool
                        ?? current.isBlocker,
                    forLater: payload["for_later"] as? Bool
                        ?? current.forLater,
                    inProgress: inProgress,
                    flagged: flagged,
                    awaitingReview: awaitingReview,
                    displayOrder: current.displayOrder,
                    completed: completed,
                    effort: payload.keys.contains("effort")
                        ? payload["effort"] as? Int : current.effort,
                    deadline: payload.keys.contains("deadline")
                        ? payload["deadline"] as? String : current.deadline
                )
            }
        }
        if projects != nextProjects {
            projects = nextProjects
        }
        if pendingProjectIDs != nextPendingProjectIDs {
            pendingProjectIDs = nextPendingProjectIDs
        }
        if pendingTodoIDs != nextPendingTodoIDs {
            pendingTodoIDs = nextPendingTodoIDs
        }
        if pendingCreateTodoIDs != nextPendingCreateTodoIDs {
            pendingCreateTodoIDs = nextPendingCreateTodoIDs
        }
        if pendingMutationCount != pendingSummary.count {
            pendingMutationCount = pendingSummary.count
        }
        if stalledPendingMutationCount != pendingSummary.stalledCount {
            stalledPendingMutationCount = pendingSummary.stalledCount
        }
        if oldestPendingMutationAt != pendingSummary.oldestObservedAt {
            oldestPendingMutationAt = pendingSummary.oldestObservedAt
        }
        if todos != visibleTodos {
            todos = visibleTodos
        }
        reloadConflicts()
    }

    private func reloadConflicts() {
        let all = (try? context.fetch(FetchDescriptor<SyncMetadata>())) ?? []
        let cachedTodos = (
            try? context.fetch(FetchDescriptor<CachedTodo>())
        ) ?? []
        let nextConflicts = all
            .filter { $0.key.hasPrefix("conflict-proposal-") }
            .compactMap { metadata -> AgendaTodoConflict? in
                guard
                    let proposal = try? JSONDecoder().decode(
                        AgendaMutationProposal.self, from: metadata.value
                    )
                else { return nil }
                let result = metadataValue(
                    key: "mutation-conflict-\(proposal.mutationID)"
                ).flatMap {
                    try? JSONDecoder().decode(
                        AgendaMutationResultRecord.self,
                        from: $0
                    )
                }
                let reason = result?.reason ?? "conflict"
                if proposal.operation == .delete {
                    let current = todos.first(where: {
                        $0.id == proposal.entityID
                    })
                    return AgendaTodoConflict(
                        mutationID: proposal.mutationID,
                        todoID: proposal.entityID,
                        projectID: cachedTodos.first(where: {
                            $0.stableID == proposal.entityID
                        })?.projectStableID,
                        macValue: current.map {
                            "Keep “\($0.text)” and its current subtree"
                        } ?? "Todo no longer exists on the Mac",
                        proposedValue: "Delete this todo and its descendants",
                        isStructural: true,
                        reason: reason
                    )
                }
                guard
                    let raw = proposal.proposedPayload?.data(using: .utf8),
                    let payload = try? JSONSerialization.jsonObject(with: raw)
                        as? [String: Any]
                else { return nil }
                let current = todos.first(where: {
                    $0.id == proposal.entityID
                })
                let mac: String
                let mine: String
                if proposal.operation == .structureEdit {
                    let projectTodos = todos.filter {
                        $0.projectID == proposal.entityID && !$0.completed
                    }
                    let updateIDs = Set(
                        (payload["updates"] as? [[String: Any]] ?? [])
                            .compactMap { $0["stable_id"] as? String }
                    )
                    let scopedIDs = structurePendingScope(
                        mutationID: proposal.mutationID,
                        fallback: updateIDs
                    )
                    mac = structureSummary(
                        todos: projectTodos.filter {
                            scopedIDs.contains($0.id)
                        }
                    )
                    mine = structureProposalSummary(
                        payload: payload,
                        todos: projectTodos,
                        includedIDs: scopedIDs
                    )
                } else if Set(payload.keys) == Set(["text"]) {
                    mac = current?.text ?? "Todo no longer exists"
                    mine = payload["text"] as? String ?? "Change text"
                } else if Set(payload.keys) == Set(["completed"]) {
                    mac = current?.completed == true
                        ? "Completed" : "Open"
                    mine = (payload["completed"] as? Bool) == true
                        ? "Mark completed" : "Mark open"
                } else {
                    mac = conflictSummary(
                        payload: payload,
                        todo: current,
                        proposed: false
                    )
                    mine = conflictSummary(
                        payload: payload,
                        todo: current,
                        proposed: true
                    )
                }
                return AgendaTodoConflict(
                    mutationID: proposal.mutationID,
                    todoID: proposal.entityID,
                    projectID: proposal.entityKind == .project
                        ? proposal.entityID
                        : (
                            current?.projectID
                                ?? cachedTodos.first(where: {
                                    $0.stableID == proposal.entityID
                                })?.projectStableID
                                ?? payload["project_stable_id"] as? String
                        ),
                    macValue: mac,
                    proposedValue: mine,
                    isStructural: proposal.operation == .structureEdit,
                    reason: reason
                )
            }
        if conflicts != nextConflicts {
            conflicts = nextConflicts
        }
    }

    private func structureSummary(todos: [TodoViewValue]) -> String {
        todos.sorted {
            if $0.parentID != $1.parentID {
                return ($0.parentID ?? "") < ($1.parentID ?? "")
            }
            return $0.displayOrder < $1.displayOrder
        }.map {
            let location = $0.parentID.map { "child of \($0)" }
                ?? ($0.forLater ? "Later root" : "Active root")
            return "\($0.text) — \(location), order \($0.displayOrder)"
        }.joined(separator: "\n")
    }

    private func structureProposalSummary(
        payload: [String: Any],
        todos: [TodoViewValue],
        includedIDs: Set<String>
    ) -> String {
        let names = Dictionary(uniqueKeysWithValues: todos.map { ($0.id, $0.text) })
        guard let updates = payload["updates"] as? [[String: Any]] else {
            return "Unreadable structural proposal"
        }
        return updates.compactMap { update in
            guard
                let id = update["stable_id"] as? String,
                includedIDs.contains(id),
                let order = update["display_order"] as? Int,
                let later = update["for_later"] as? Bool
            else { return nil }
            let parent = update["parent_stable_id"] as? String
            let location = parent.map { "child of \(names[$0] ?? $0)" }
                ?? (later ? "Later root" : "Active root")
            return "\(names[id] ?? id) — \(location), order \(order)"
        }.joined(separator: "\n")
    }

    private func conflictSummary(
        payload: [String: Any],
        todo: TodoViewValue?,
        proposed: Bool
    ) -> String {
        guard let todo else { return "Todo no longer exists" }
        let fields: [(String, String, Any?)] = [
            ("text", "Text", todo.text),
            ("in_progress", "In Progress", todo.inProgress),
            ("flagged", "Flagged", todo.flagged),
            ("is_blocker", "Blocker", todo.isBlocker),
            ("awaiting_review", "Needs Review", todo.awaitingReview),
            ("for_later", "Later", todo.forLater),
            ("effort", "Effort", todo.effort),
            ("deadline", "Deadline", todo.deadline),
            ("completed", "Completed", todo.completed),
        ]
        return fields.compactMap { key, label, macValue in
            guard payload.keys.contains(key) else { return nil }
            let value = proposed ? payload[key] : macValue
            return "\(label): \(conflictValue(value))"
        }.joined(separator: "\n")
    }

    private func conflictValue(_ value: Any?) -> String {
        if value == nil || value is NSNull { return "None" }
        if let value = value as? Bool { return value ? "Yes" : "No" }
        return String(describing: value!)
    }
}

