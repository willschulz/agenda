import AgendaSyncCore
import CloudKit
import Foundation

private struct AgendaResultsSnapshot: Decodable {
    let schemaVersion: Int
    let results: [AgendaMutationResultRecord]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case results
    }
}

@available(macOS 14.0, *)
actor MacAgendaCloudStore: AgendaCloudSyncStore {
    private let stateURL: URL
    private let pendingURL: URL
    private let quarantineURL: URL
    private let canonicalQuarantineURL: URL
    private let accountMarkerURL: URL
    private let localAPI: URL
    private var testTodoIDs = Set<String>()
    private var exportedTodos: [String: AgendaTodoRecord] = [:]
    private var publishedTombstones = Set<String>()
    private var publishedSchema2 = Set<String>()
    private var quarantinedCanonicalNames = Set<String>()
    private var processedMutationIDs = Set<String>()
    private var zoneRecreationNeeded = false

    init(localAPI: URL, cloudDirectory: URL? = nil) {
        self.localAPI = localAPI
        let directory: URL
        if let cloudDirectory {
            directory = cloudDirectory
        } else {
            let support = FileManager.default.urls(
                for: .applicationSupportDirectory,
                in: .userDomainMask
            ).first!
            directory = support
                .appendingPathComponent("Agenda", isDirectory: true)
                .appendingPathComponent("CloudKit", isDirectory: true)
        }
        try? FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        stateURL = directory.appendingPathComponent("engine-state.json")
        pendingURL = directory.appendingPathComponent("pending-records.json")
        quarantineURL = directory.appendingPathComponent("account-quarantined")
        canonicalQuarantineURL = directory.appendingPathComponent(
            "canonical-quarantine.json"
        )
        accountMarkerURL = directory.appendingPathComponent("account-marker")
        if let data = try? Data(contentsOf: canonicalQuarantineURL),
           let entries = try? JSONDecoder().decode(
               [String: AgendaCanonicalQuarantineEntry].self,
               from: data
           ) {
            quarantinedCanonicalNames = Set(entries.keys)
        }
    }

    func register(snapshot: AgendaSnapshot) {
        let testProjects = Set(
            snapshot.projects
                .filter { $0.name == agendaSyncTestProjectName }
                .map(\.stableID)
        )
        testTodoIDs = Set(
            snapshot.todos
                .filter { testProjects.contains($0.projectStableID) }
                .map(\.stableID)
        )
        exportedTodos = Dictionary(
            uniqueKeysWithValues: snapshot.todos.map { ($0.stableID, $0) }
        )
    }

    func loadEngineState() async -> CKSyncEngine.State.Serialization? {
        guard let data = try? Data(contentsOf: stateURL) else { return nil }
        return try? JSONDecoder().decode(
            CKSyncEngine.State.Serialization.self,
            from: data
        )
    }

    func saveEngineState(
        _ state: CKSyncEngine.State.Serialization
    ) async {
        guard let data = try? JSONEncoder().encode(state) else { return }
        try? data.write(to: stateURL, options: .atomic)
    }

    @discardableResult
    func applyFetchedCycle(_ cycle: AgendaCloudFetchCycle) async -> Bool {
        guard !FileManager.default.fileExists(atPath: quarantineURL.path) else {
            await reportSyncError(
                "CloudKit account identity is quarantined; remote work was preserved."
            )
            return false
        }
        if cycle.zoneDeleted {
            publishedTombstones.removeAll()
            publishedSchema2.removeAll()
            zoneRecreationNeeded = true
            await reportSyncError(
                "Remote zone deletion ignored; local data was preserved."
            )
        }
        if !cycle.physicalDeletions.isEmpty {
            for recordID in cycle.physicalDeletions {
                publishedSchema2.remove(recordID.recordName)
                if recordID.recordName.hasPrefix("tombstone-") {
                    publishedTombstones.remove(
                        String(recordID.recordName.dropFirst("tombstone-".count))
                    )
                }
            }
            await reportSyncError(
                "Ignored \(cycle.physicalDeletions.count) physical CloudKit deletion(s)."
            )
        }
        let records = cycle.records
        if !records.isEmpty {
            NSLog("Agenda CloudKit: fetched %d record(s)", records.count)
        }
        for record in records where record.recordType == AgendaCloudRecord.tombstoneType {
            if let tombstone = try? AgendaCloudRecord.decodeTombstone(record) {
                publishedTombstones.insert(tombstone.id)
            }
        }
        for record in records where
            record.recordType == AgendaCloudRecord.resultType {
            publishedSchema2.insert(record.recordID.recordName)
        }
        await removePendingRecordIDs(
            records.compactMap {
                $0.recordType == AgendaCloudRecord.canonicalType
                    ? nil : $0.recordID
            }
        )
        for record in records where record.recordType == AgendaCloudRecord.mutationType {
            guard let mutation = try? AgendaCloudRecord.decodeMutation(record) else {
                continue
            }
            guard !processedMutationIDs.contains(mutation.mutationID) else {
                continue
            }
            do {
                try await apply(mutation: mutation)
                processedMutationIDs.insert(mutation.mutationID)
            } catch {
                await reportSyncError(
                    "Mutation \(mutation.mutationID) was preserved but not evaluated: \(error.localizedDescription)"
                )
            }
        }
        for record in records where record.recordType == AgendaCloudRecord.todoType {
            guard
                let todo = try? AgendaCloudRecord.decodeTodo(record),
                testTodoIDs.contains(todo.stableID),
                let exported = exportedTodos[todo.stableID],
                todo.text != exported.text
            else { continue }
            do {
                try await apply(todo: todo, baseUpdatedAt: exported.updatedAt)
                exportedTodos[todo.stableID] = todo
                NSLog("Agenda CloudKit: applied guarded test todo %@", todo.stableID)
            } catch {
                await reportSyncError(
                    "Inbound test todo was not applied: \(error.localizedDescription)"
                )
            }
        }
        return !cycle.zoneDeleted && cycle.physicalDeletions.isEmpty
    }

    func needsZoneRecreation() -> Bool {
        zoneRecreationNeeded
    }

    func markZoneRecreated() {
        zoneRecreationNeeded = false
    }

    func recordSavesSucceeded(_ records: [CKRecord]) async {
        for record in records where record.recordType == AgendaCloudRecord.tombstoneType {
            if let tombstone = try? AgendaCloudRecord.decodeTombstone(record) {
                publishedTombstones.insert(tombstone.id)
            }
        }
        for record in records where
            record.recordType == AgendaCloudRecord.canonicalType
                || record.recordType == AgendaCloudRecord.resultType {
            publishedSchema2.insert(record.recordID.recordName)
        }
    }

    func loadPendingRecords() async throws -> [CKRecord] {
        let archived = try pendingArchive()
        let records = archived.values.compactMap(
            AgendaCloudRecord.unarchiveRecord
        )
        guard records.count == archived.count else {
            throw CocoaError(.fileReadCorruptFile)
        }
        return records
    }

    func persistPendingRecords(_ records: [CKRecord]) async throws {
        var archived = try pendingArchive()
        for record in records {
            archived[record.recordID.recordName] = try AgendaCloudRecord.archiveRecord(
                record
            )
        }
        try JSONEncoder().encode(archived).write(to: pendingURL, options: .atomic)
    }

    func removePendingRecordIDs(_ recordIDs: [CKRecord.ID]) async {
        do {
            try removePendingRecordsAtomically(recordIDs)
        } catch {
            await reportSyncError(
                "Could not persist CloudKit queue acknowledgement: "
                    + error.localizedDescription
            )
        }
    }

    func pendingCanonicalRecords() throws -> [CKRecord] {
        let archived = try pendingArchive()
        let records = archived.values
            .compactMap(AgendaCloudRecord.unarchiveRecord)
        guard records.count == archived.count else {
            throw CocoaError(.fileReadCorruptFile)
        }
        return records.filter {
            $0.recordType == AgendaCloudRecord.canonicalType
        }
    }

    func removePendingRecordsAtomically(_ recordIDs: [CKRecord.ID]) throws {
        guard !recordIDs.isEmpty else { return }
        var archived = try pendingArchive()
        for recordID in recordIDs {
            archived[recordID.recordName] = nil
        }
        try JSONEncoder().encode(archived).write(to: pendingURL, options: .atomic)
    }

    func quarantineCanonical(
        local: CKRecord,
        server: CKRecord?,
        currentExport: CKRecord? = nil,
        reason: String
    ) throws {
        var entries: [String: AgendaCanonicalQuarantineEntry] = [:]
        if let data = try? Data(contentsOf: canonicalQuarantineURL) {
            entries = try JSONDecoder().decode(
                [String: AgendaCanonicalQuarantineEntry].self,
                from: data
            )
        }
        let entry = try AgendaCanonicalQuarantineEntry.make(
            local: local,
            server: server,
            currentExport: currentExport,
            reason: reason
        )
        entries[entry.recordName] = entry
        try JSONEncoder().encode(entries).write(
            to: canonicalQuarantineURL,
            options: .atomic
        )
        quarantinedCanonicalNames.insert(entry.recordName)
    }

    func isCanonicalQuarantined(_ recordName: String) -> Bool {
        quarantinedCanonicalNames.contains(recordName)
    }

    func accountChanged() async {
        FileManager.default.createFile(
            atPath: quarantineURL.path,
            contents: Data()
        )
    }

    func accountAvailable(accountMarker: String?) async {
        guard let accountMarker else { return }
        let previous = try? String(
            contentsOf: accountMarkerURL,
            encoding: .utf8
        )
        if previous == nil {
            try? accountMarker.write(
                to: accountMarkerURL,
                atomically: true,
                encoding: .utf8
            )
        }
        if previous == nil || previous == accountMarker {
            try? FileManager.default.removeItem(at: quarantineURL)
        }
    }

    func unpublishedTombstoneRecords(
        _ values: [AgendaTombstoneRecord]
    ) -> [CKRecord] {
        values.compactMap { value in
            guard !publishedTombstones.contains(value.id) else { return nil }
            return try? AgendaCloudRecord.tombstone(value)
        }
    }

    func unpublishedSchema2Records(
        canonicals: [AgendaCanonicalRecordV2],
        results: [AgendaMutationResultRecord]
    ) -> [CKRecord] {
        let records =
            canonicals.compactMap { try? AgendaCloudRecord.canonical($0) }
            + results.compactMap { try? AgendaCloudRecord.result($0) }
        return records.filter {
            !publishedSchema2.contains($0.recordID.recordName)
                && !quarantinedCanonicalNames.contains($0.recordID.recordName)
        }
    }

    func unpublishedResultRecords(
        _ results: [AgendaMutationResultRecord]
    ) -> [CKRecord] {
        results
            .compactMap { try? AgendaCloudRecord.result($0) }
            .filter {
                !publishedSchema2.contains($0.recordID.recordName)
            }
    }

    func unpublishedCanonicalRecords(
        _ canonicals: [AgendaCanonicalRecordV2]
    ) throws -> [CKRecord] {
        let pendingNames = Set(try pendingArchive().keys)
        return canonicals
            .compactMap { try? AgendaCloudRecord.canonical($0) }
            .filter {
                !quarantinedCanonicalNames.contains($0.recordID.recordName)
                    && (
                        !publishedSchema2.contains($0.recordID.recordName)
                            || pendingNames.contains($0.recordID.recordName)
                    )
            }
    }

    func pendingCanonicalRecordIDs() throws -> [CKRecord.ID] {
        let archived = try pendingArchive()
        let records = archived.values
            .compactMap(AgendaCloudRecord.unarchiveRecord)
        guard records.count == archived.count else {
            throw CocoaError(.fileReadCorruptFile)
        }
        return records
            .filter { $0.recordType == AgendaCloudRecord.canonicalType }
            .map(\.recordID)
    }

    func reportSyncError(_ message: String) async {
        NSLog("Agenda CloudKit: %@", message)
    }

    private func pendingArchive() throws -> [String: Data] {
        guard FileManager.default.fileExists(atPath: pendingURL.path) else {
            return [:]
        }
        let data = try Data(contentsOf: pendingURL)
        return try JSONDecoder().decode([String: Data].self, from: data)
    }

    private func apply(
        todo: AgendaTodoRecord,
        baseUpdatedAt: String
    ) async throws {
        let endpoint = localAPI.appendingPathComponent("api/sync/test-todo")
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "schema_version": agendaWireSchemaVersion,
            "stable_id": todo.stableID,
            "text": todo.text,
            "base_updated_at": baseUpdatedAt,
        ])
        let (_, response) = try await URLSession.shared.data(for: request)
        guard
            let http = response as? HTTPURLResponse,
            (200..<300).contains(http.statusCode)
        else {
            throw URLError(.cannotParseResponse)
        }
    }

    private func apply(mutation: AgendaMutationProposal) async throws {
        let endpoint = localAPI.appendingPathComponent("api/sync/mutations")
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(mutation)
        let (_, response) = try await URLSession.shared.data(for: request)
        guard
            let http = response as? HTTPURLResponse,
            (200..<300).contains(http.statusCode)
        else {
            throw URLError(.cannotParseResponse)
        }
    }
}

@available(macOS 14.0, *)
@MainActor
final class CloudKitCoordinator: ObservableObject {
    @Published private(set) var status = "CloudKit not started"

    private var engine: AgendaSyncEngine?
    private var store: MacAgendaCloudStore?
    private var pollTask: Task<Void, Never>?
    private var localAPI: URL?
    private var syncInProgress = false
    private let database: any AgendaCloudDatabaseAccess

    init(database: (any AgendaCloudDatabaseAccess)? = nil) {
        self.database = database ?? LiveAgendaCloudDatabase(
            database: CKContainer(
                identifier: agendaCloudContainerIdentifier
            ).privateCloudDatabase
        )
    }

    func start(localAPI: URL) {
        guard engine == nil else { return }
        self.localAPI = localAPI
        status = "Connecting to private iCloud…"
        let store = MacAgendaCloudStore(localAPI: localAPI)
        let engine = AgendaSyncEngine(store: store)
        self.store = store
        self.engine = engine
        syncInProgress = true
        Task {
            defer {
                syncInProgress = false
                startPolling()
            }
            do {
                await engine.start(automaticallySync: false)
                let initialSnapshot = try await fetchSnapshot(localAPI: localAPI)
                await store.register(snapshot: initialSnapshot)
                let account = try await engine.accountStatus()
                guard account == .available else {
                    status = "iCloud account unavailable; local Agenda is unaffected"
                    return
                }
                // Pull before pushing so a Mac relaunch cannot overwrite a
                // remote test edit with its stale pre-launch snapshot.
                try await engine.fetchNow()
                try await recreateZoneIfNeeded(store: store)
                let snapshot = try await fetchSnapshot(localAPI: localAPI)
                await store.register(snapshot: snapshot)
                let tombstones = try await fetchTombstones(localAPI: localAPI)
                let canonicals = try await fetchCanonicals(localAPI: localAPI)
                let results = try await fetchResults(localAPI: localAPI)
                let outboundCanonicals = canonicalsEligibleForPublishing(
                    canonicals,
                    results: results
                )
                try await discardIneligibleCanonicalQueue(
                    canonicals,
                    eligible: outboundCanonicals,
                    store: store,
                    engine: engine
                )
                await publishResultsDirectly(
                    results,
                    into: store,
                    engine: engine
                )
                try await publishChangedCanonicalsDirectly(
                    canonicals,
                    results: results,
                    into: store,
                    engine: engine
                )
                try await publishTombstonesDirectly(
                    tombstones,
                    into: store,
                    engine: engine
                )
                await publishCanonicalManifest(
                    canonicals,
                    tombstones: tombstones,
                    results: results,
                    store: store
                )
                let boardOrder = try await fetchBoardOrder(localAPI: localAPI)
                await publishBoardOrder(boardOrder)
                let schema2Records = await store.unpublishedSchema2Records(
                    canonicals: outboundCanonicals,
                    results: results
                )
                let records = schema2Records
                try await engine.stage(records)
                do {
                    try await engine.sendNow()
                    status = "CloudKit Development snapshot synced"
                } catch {
                    status = "CloudKit snapshot partially synced; local Agenda is unaffected"
                    NSLog(
                        "Agenda CloudKit snapshot send returned: %@",
                        error.localizedDescription
                    )
                }
            } catch {
                status = "CloudKit unavailable; local Agenda is unaffected"
                NSLog("Agenda CloudKit startup failed: %@", error.localizedDescription)
            }
        }
    }

    func syncNow() {
        guard
            let engine, let store, let localAPI,
            !syncInProgress
        else { return }
        syncInProgress = true
        NSLog("Agenda CloudKit: checking for remote changes")
        Task {
            defer { syncInProgress = false }
            do {
                try await engine.fetchNow()
                try await recreateZoneIfNeeded(store: store)
                let tombstones = try await fetchTombstones(localAPI: localAPI)
                let canonicals = try await fetchCanonicals(localAPI: localAPI)
                let results = try await fetchResults(localAPI: localAPI)
                let outboundCanonicals = canonicalsEligibleForPublishing(
                    canonicals,
                    results: results
                )
                try await discardIneligibleCanonicalQueue(
                    canonicals,
                    eligible: outboundCanonicals,
                    store: store,
                    engine: engine
                )
                await publishResultsDirectly(
                    results,
                    into: store,
                    engine: engine
                )
                try await publishChangedCanonicalsDirectly(
                    canonicals,
                    results: results,
                    into: store,
                    engine: engine
                )
                try await publishTombstonesDirectly(
                    tombstones,
                    into: store,
                    engine: engine
                )
                await publishCanonicalManifest(
                    canonicals,
                    tombstones: tombstones,
                    results: results,
                    store: store
                )
                let boardOrder = try await fetchBoardOrder(localAPI: localAPI)
                await publishBoardOrder(boardOrder)
                let schema2Records = await store.unpublishedSchema2Records(
                    canonicals: outboundCanonicals,
                    results: results
                )
                let records = schema2Records
                if !records.isEmpty {
                    try await engine.stage(records)
                    try await engine.sendNow()
                }
                status = "CloudKit changes checked"
            } catch {
                status = "CloudKit check failed; local Agenda is unaffected"
            }
        }
    }

    deinit {
        pollTask?.cancel()
    }

    func recreateZoneIfNeeded(
        store: MacAgendaCloudStore
    ) async throws {
        guard await store.needsZoneRecreation() else { return }
        try await database.saveZone()
        await store.markZoneRecreated()
    }

    @available(macOS 14.0, *)
    private func fetchSnapshot(localAPI: URL) async throws -> AgendaSnapshot {
        let url = localAPI.appendingPathComponent("api/sync/snapshot")
        let (data, response) = try await URLSession.shared.data(from: url)
        guard
            let http = response as? HTTPURLResponse,
            http.statusCode == 200
        else {
            throw URLError(.badServerResponse)
        }
        let snapshot = try JSONDecoder().decode(AgendaSnapshot.self, from: data)
        guard snapshot.schemaVersion == agendaWireSchemaVersion else {
            throw AgendaCloudRecordError.unsupportedSchema
        }
        return snapshot
    }

    @available(macOS 14.0, *)
    private func fetchTombstones(
        localAPI: URL
    ) async throws -> [AgendaTombstoneRecord] {
        let url = localAPI.appendingPathComponent("api/sync/tombstones")
        let (data, response) = try await URLSession.shared.data(from: url)
        guard
            let http = response as? HTTPURLResponse,
            http.statusCode == 200
        else {
            throw URLError(.badServerResponse)
        }
        let snapshot = try JSONDecoder().decode(
            AgendaTombstoneSnapshot.self,
            from: data
        )
        guard snapshot.schemaVersion == agendaContractSchemaVersion else {
            throw AgendaCloudRecordError.unsupportedSchema
        }
        return snapshot.tombstones
    }

    @available(macOS 14.0, *)
    private func fetchCanonicals(
        localAPI: URL
    ) async throws -> [AgendaCanonicalRecordV2] {
        let url = localAPI.appendingPathComponent("api/sync/canonical-v2")
        let (data, response) = try await URLSession.shared.data(from: url)
        guard
            let http = response as? HTTPURLResponse,
            http.statusCode == 200
        else {
            throw URLError(.badServerResponse)
        }
        let snapshot = try JSONDecoder().decode(
            AgendaCanonicalSnapshotV2.self,
            from: data
        )
        guard snapshot.schemaVersion == agendaContractSchemaVersion else {
            throw AgendaCloudRecordError.unsupportedSchema
        }
        return snapshot.records
    }

    @available(macOS 14.0, *)
    private func fetchBoardOrder(
        localAPI: URL
    ) async throws -> AgendaBoardOrderSnapshot {
        let url = localAPI.appendingPathComponent("api/sync/board-order")
        let (data, response) = try await URLSession.shared.data(from: url)
        guard
            let http = response as? HTTPURLResponse,
            http.statusCode == 200
        else {
            throw URLError(.badServerResponse)
        }
        let snapshot = try JSONDecoder().decode(
            AgendaBoardOrderSnapshot.self,
            from: data
        )
        guard snapshot.schemaVersion == agendaContractSchemaVersion else {
            throw AgendaCloudRecordError.unsupportedSchema
        }
        return snapshot
    }

    @available(macOS 14.0, *)
    private func fetchResults(
        localAPI: URL
    ) async throws -> [AgendaMutationResultRecord] {
        let url = localAPI.appendingPathComponent("api/sync/results")
        let (data, response) = try await URLSession.shared.data(from: url)
        guard
            let http = response as? HTTPURLResponse,
            http.statusCode == 200
        else {
            throw URLError(.badServerResponse)
        }
        let snapshot = try JSONDecoder().decode(
            AgendaResultsSnapshot.self,
            from: data
        )
        guard snapshot.schemaVersion == agendaContractSchemaVersion else {
            throw AgendaCloudRecordError.unsupportedSchema
        }
        return snapshot.results
    }

    func publishChangedCanonicalsDirectly(
        _ canonicals: [AgendaCanonicalRecordV2],
        results: [AgendaMutationResultRecord],
        into store: MacAgendaCloudStore,
        engine: any AgendaPendingRecordDiscarding
    ) async throws {
        let createdIDs = Set(
            results.compactMap {
                $0.canonicalRevision == 0 ? $0.entityID : nil
            }
        )
        let eligible = canonicals.filter {
            shouldPublishCanonical($0, createdIDs: createdIDs)
        }
        let currentRecords = eligible.compactMap {
            try? AgendaCloudRecord.canonical($0)
        }
        let currentByID = Dictionary(
            uniqueKeysWithValues: currentRecords.map {
                ($0.recordID, $0)
            }
        )
        let pending = try await store.pendingCanonicalRecords()
        let pendingByID = Dictionary(
            uniqueKeysWithValues: pending.map {
                ($0.recordID, $0)
            }
        )
        var quarantinedPendingIDs: [CKRecord.ID] = []
        for record in pending where await store.isCanonicalQuarantined(
            record.recordID.recordName
        ) {
            quarantinedPendingIDs.append(record.recordID)
        }
        var report = AgendaCanonicalReconciliationReport()
        if !quarantinedPendingIDs.isEmpty {
            do {
                try await discardPendingCanonicalIDs(
                    quarantinedPendingIDs,
                    from: store,
                    engine: engine
                )
            } catch {
                report.retrying += quarantinedPendingIDs.count
                await store.reportSyncError(
                    "Could not finish quarantined canonical cleanup: "
                        + error.localizedDescription
                )
            }
        }
        let quarantinedNames = Set(
            quarantinedPendingIDs.map(\.recordName)
        )
        let obsoleteIDs = pending.compactMap { record -> CKRecord.ID? in
            guard !quarantinedNames.contains(record.recordID.recordName) else {
                return nil
            }
            return currentByID[record.recordID] == nil ? record.recordID : nil
        }
        if !obsoleteIDs.isEmpty {
            do {
                try await discardPendingCanonicalIDs(
                    obsoleteIDs,
                    from: store,
                    engine: engine
                )
                report.obsolete += obsoleteIDs.count
            } catch {
                report.retrying += obsoleteIDs.count
                await store.reportSyncError(
                    "Could not discard obsolete canonical queue entries: "
                        + error.localizedDescription
                )
            }
        }

        let records = try await store.unpublishedCanonicalRecords(eligible)
        guard !records.isEmpty else {
            if report.obsolete > 0 {
                NSLog("Agenda CloudKit canonical reconciliation: %@", report.summary)
            }
            return
        }
        do {
            var missing: [CKRecord] = []
            let fetched = try await database.records(
                for: records.map(\.recordID)
            )
            for local in records {
                guard let result = fetched[local.recordID] else {
                    report.retrying += 1
                    continue
                }
                if let queued = pendingByID[local.recordID],
                   !agendaCanonicalsSemanticallyEqual(queued, local) {
                    do {
                        let server: CKRecord?
                        do {
                            server = try result.get()
                        } catch let error as CKError
                            where error.code == .unknownItem {
                            server = nil
                        }
                        try await store.quarantineCanonical(
                            local: queued,
                            server: server,
                            currentExport: local,
                            reason: "durable_body_current_export_mismatch"
                        )
                        try await discardPendingCanonicalIDs(
                            [local.recordID],
                            from: store,
                            engine: engine
                        )
                        report.quarantined += 1
                    } catch {
                        report.retrying += 1
                        await store.reportSyncError(
                            "Could not quarantine changed durable canonical "
                                + "\(local.recordID.recordName): "
                                + error.localizedDescription
                        )
                    }
                    continue
                }
                do {
                    let server = try result.get()
                    guard agendaCanonicalsSemanticallyEqual(server, local) else {
                        do {
                            try await store.quarantineCanonical(
                                local: local,
                                server: server,
                                reason: "same_record_id_semantic_mismatch"
                            )
                            try await discardPendingCanonicalIDs(
                                [local.recordID],
                                from: store,
                                engine: engine
                            )
                            report.quarantined += 1
                        } catch {
                            report.retrying += 1
                            await store.reportSyncError(
                                "Could not quarantine canonical conflict "
                                    + "\(local.recordID.recordName): "
                                    + error.localizedDescription
                            )
                        }
                        continue
                    }
                    do {
                        try await discardPendingCanonicalIDs(
                            [server.recordID],
                            from: store,
                            engine: engine
                        )
                        await store.recordSavesSucceeded([server])
                        report.acknowledged += 1
                    } catch {
                        report.retrying += 1
                        await store.reportSyncError(
                            "Could not acknowledge canonical "
                                + "\(local.recordID.recordName): "
                                + error.localizedDescription
                        )
                    }
                } catch let error as CKError where error.code == .unknownItem {
                    missing.append(local)
                } catch {
                    report.retrying += 1
                    NSLog(
                        "Agenda CloudKit direct canonical fetch failed for %@: %@",
                        local.recordID.recordName,
                        error.localizedDescription
                    )
                }
            }
            if !missing.isEmpty {
                let response = try await database.saveMissing(missing)
                for local in missing {
                    guard let result = response[local.recordID] else {
                        report.retrying += 1
                        continue
                    }
                    do {
                        let saved = try result.get()
                        try await discardPendingCanonicalIDs(
                            [saved.recordID],
                            from: store,
                            engine: engine
                        )
                        await store.recordSavesSucceeded([saved])
                        report.published += 1
                    } catch {
                        report.retrying += 1
                        NSLog(
                            "Agenda CloudKit direct canonical publish failed for %@: %@",
                            local.recordID.recordName,
                            error.localizedDescription
                        )
                    }
                }
            }
            NSLog("Agenda CloudKit canonical reconciliation: %@", report.summary)
        } catch {
            report.retrying += records.count
            NSLog(
                "Agenda CloudKit canonical batch publish failed: %@",
                error.localizedDescription
            )
            NSLog("Agenda CloudKit canonical reconciliation: %@", report.summary)
        }
    }

    private func discardPendingCanonicalIDs(
        _ recordIDs: [CKRecord.ID],
        from store: MacAgendaCloudStore,
        engine: any AgendaPendingRecordDiscarding
    ) async throws {
        // Engine state first: a crash before the durable-body removal leaves a
        // replayable body, never a bodyless pending save.
        await engine.discardPendingRecordIDs(recordIDs)
        // Verify the durable rewrite explicitly because the store protocol's
        // acknowledgement is intentionally non-throwing.
        try await store.removePendingRecordsAtomically(recordIDs)
    }

    func publishTombstonesDirectly(
        _ tombstones: [AgendaTombstoneRecord],
        into store: MacAgendaCloudStore,
        engine: any AgendaPendingRecordDiscarding
    ) async throws {
        let records = await store.unpublishedTombstoneRecords(tombstones)
        guard !records.isEmpty else { return }
        let fetched = try await database.records(for: records.map(\.recordID))
        var missing: [CKRecord] = []
        for local in records {
            guard let result = fetched[local.recordID] else {
                throw URLError(.badServerResponse)
            }
            do {
                let server = try result.get()
                guard
                    try AgendaCloudRecord.decodeTombstone(server)
                        == AgendaCloudRecord.decodeTombstone(local)
                else {
                    throw AgendaCloudRecordError.unsupportedSchema
                }
                await store.recordSavesSucceeded([server])
                await engine.discardPendingRecordIDs([server.recordID])
            } catch let error as CKError where error.code == .unknownItem {
                missing.append(local)
            }
        }
        guard !missing.isEmpty else { return }
        let saved = try await database.saveMissing(missing)
        for local in missing {
            guard let result = saved[local.recordID] else {
                throw URLError(.badServerResponse)
            }
            let record = try result.get()
            await store.recordSavesSucceeded([record])
            await engine.discardPendingRecordIDs([record.recordID])
        }
    }

    private func publishResultsDirectly(
        _ results: [AgendaMutationResultRecord],
        into store: MacAgendaCloudStore,
        engine: AgendaSyncEngine
    ) async {
        let records = await store.unpublishedResultRecords(results)
        for local in records {
            do {
                do {
                    let saved = try await database.save(local)
                    await store.recordSavesSucceeded([saved])
                    await engine.discardPendingRecordIDs([saved.recordID])
                } catch let error as CKError
                    where error.code == .serverRecordChanged {
                    guard
                        let server = error.userInfo[
                            CKRecordChangedErrorServerRecordKey
                        ] as? CKRecord,
                        try AgendaCloudRecord.decodeResult(server)
                            == AgendaCloudRecord.decodeResult(local)
                    else {
                        throw error
                    }
                    await store.recordSavesSucceeded([server])
                    await engine.discardPendingRecordIDs([server.recordID])
                }
            } catch {
                NSLog(
                    "Agenda CloudKit direct result publish failed for %@: %@",
                    local.recordID.recordName,
                    error.localizedDescription
                )
            }
        }
    }

    func publishBoardOrder(
        _ snapshot: AgendaBoardOrderSnapshot
    ) async {
        do {
            let existing = try? await database.records(
                for: [AgendaCloudRecord.boardOrderID]
            )[AgendaCloudRecord.boardOrderID]?.get()
            if let existing,
               let current = try? AgendaCloudRecord.decodeBoardOrder(existing) {
                guard current.rows != snapshot.rows
                    || current.rowDefinitions != snapshot.rowDefinitions
                    || current.pointsEnabled != snapshot.pointsEnabled
                else { return }
                guard current.generatedAt < snapshot.generatedAt else { return }
            }
            let record = try AgendaCloudRecord.boardOrder(
                snapshot,
                existingRecord: existing
            )
            do {
                _ = try await database.save(record)
            } catch let error as CKError
                where error.code == .serverRecordChanged {
                guard
                    let server = error.userInfo[
                        CKRecordChangedErrorServerRecordKey
                    ] as? CKRecord
                else { throw error }
                if let current = try? AgendaCloudRecord.decodeBoardOrder(server) {
                    guard current.rows != snapshot.rows
                        || current.rowDefinitions != snapshot.rowDefinitions
                    || current.pointsEnabled != snapshot.pointsEnabled
                    else { return }
                    guard current.generatedAt < snapshot.generatedAt else { return }
                }
                _ = try await database.save(
                    AgendaCloudRecord.boardOrder(
                        snapshot,
                        existingRecord: server
                    )
                )
            }
        } catch {
            NSLog(
                "Agenda CloudKit board-order publish failed: %@",
                error.localizedDescription
            )
        }
    }

    func publishCanonicalManifest(
        _ canonicals: [AgendaCanonicalRecordV2],
        tombstones: [AgendaTombstoneRecord],
        results: [AgendaMutationResultRecord],
        store: MacAgendaCloudStore
    ) async {
        let createdIDs = Set(
            results.compactMap {
                $0.canonicalRevision == 0 ? $0.entityID : nil
            }
        )
        var eligibleCanonicals: [AgendaCanonicalRecordV2] = []
        for canonical in canonicals
        where shouldPublishCanonical(canonical, createdIDs: createdIDs) {
            guard
                let record = try? AgendaCloudRecord.canonical(canonical),
                !(await store.isCanonicalQuarantined(
                    record.recordID.recordName
                ))
            else { continue }
            eligibleCanonicals.append(canonical)
            }
        let entries = AgendaCanonicalManifestEntry.latestStates(
            canonicals: eligibleCanonicals,
            tombstones: tombstones
        )
        do {
            let existing = try? await database.records(
                for: [AgendaCloudRecord.canonicalManifestID]
            )[AgendaCloudRecord.canonicalManifestID]?.get()
            let record = try AgendaCloudRecord.canonicalManifest(
                entries,
                existingRecord: existing
            )
            do {
                _ = try await database.save(record)
            } catch let error as CKError
                where error.code == .serverRecordChanged {
                guard
                    let server = error.userInfo[
                        CKRecordChangedErrorServerRecordKey
                    ] as? CKRecord
                else { throw error }
                _ = try await database.save(
                    AgendaCloudRecord.canonicalManifest(
                        entries,
                        existingRecord: server
                    )
                )
            }
        } catch {
            NSLog(
                "Agenda CloudKit canonical manifest publish failed: %@",
                error.localizedDescription
            )
        }
    }

    private func shouldPublishCanonical(
        _ canonical: AgendaCanonicalRecordV2,
        createdIDs: Set<String>
    ) -> Bool {
        if canonical.revision > 0 || createdIDs.contains(canonical.stableID) {
            return true
        }
        guard
            let data = canonical.payloadJSON.data(using: .utf8),
            let payload = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any],
            let createdAt = payload["created_at"] as? String
        else { return false }
        return createdAt >= "2026-08-09T00:00:00"
    }

    private func canonicalsEligibleForPublishing(
        _ canonicals: [AgendaCanonicalRecordV2],
        results: [AgendaMutationResultRecord]
    ) -> [AgendaCanonicalRecordV2] {
        let createdIDs = Set(
            results.compactMap {
                $0.canonicalRevision == 0 ? $0.entityID : nil
            }
        )
        return canonicals.filter {
            shouldPublishCanonical($0, createdIDs: createdIDs)
        }
    }

    private func discardIneligibleCanonicalQueue(
        _ canonicals: [AgendaCanonicalRecordV2],
        eligible: [AgendaCanonicalRecordV2],
        store: MacAgendaCloudStore,
        engine: AgendaSyncEngine
    ) async throws {
        let eligibleNames = Set(
            eligible.compactMap {
                try? AgendaCloudRecord.canonical($0).recordID.recordName
            }
        )
        var recordIDs = canonicals.compactMap { canonical -> CKRecord.ID? in
            guard
                let record = try? AgendaCloudRecord.canonical(canonical),
                !eligibleNames.contains(record.recordID.recordName)
            else { return nil }
            return record.recordID
        }
        let pendingIDs = try await store.pendingCanonicalRecordIDs()
        recordIDs.append(
            contentsOf: pendingIDs.filter {
                !eligibleNames.contains($0.recordName)
            }
        )
        do {
            try await discardPendingCanonicalIDs(
                recordIDs,
                from: store,
                engine: engine
            )
        } catch {
            await store.reportSyncError(
                "Could not persist ineligible canonical cleanup: "
                    + error.localizedDescription
            )
        }
    }

    @available(macOS 14.0, *)
    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(30))
                guard !Task.isCancelled else { return }
                self?.syncNow()
            }
        }
    }
}

