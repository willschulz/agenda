import CloudKit
import Foundation

@available(macOS 14.0, iOS 17.0, *)
public struct AgendaCloudFetchCycle: @unchecked Sendable {
    public let records: [CKRecord]
    public let physicalDeletions: [CKRecord.ID]
    public let zoneDeleted: Bool

    public init(
        records: [CKRecord],
        physicalDeletions: [CKRecord.ID],
        zoneDeleted: Bool
    ) {
        self.records = records
        self.physicalDeletions = physicalDeletions
        self.zoneDeleted = zoneDeleted
    }
}

@available(macOS 14.0, iOS 17.0, *)
public protocol AgendaCloudSyncStore: Sendable {
    func loadEngineState() async -> CKSyncEngine.State.Serialization?
    func saveEngineState(_ state: CKSyncEngine.State.Serialization) async
    @discardableResult
    func applyFetchedCycle(_ cycle: AgendaCloudFetchCycle) async -> Bool
    func recordSavesSucceeded(_ records: [CKRecord]) async
    func loadPendingRecords() async throws -> [CKRecord]
    func persistPendingRecords(_ records: [CKRecord]) async throws
    func removePendingRecordIDs(_ recordIDs: [CKRecord.ID]) async
    func accountChanged() async
    func accountAvailable(accountMarker: String?) async
    func reportSyncError(_ message: String) async
}

@available(macOS 14.0, iOS 17.0, *)
public final actor AgendaSyncEngine: CKSyncEngineDelegate {
    private let store: any AgendaCloudSyncStore
    private let container: CKContainer
    private var engine: CKSyncEngine?
    private var pendingRecords: [CKRecord.ID: CKRecord] = [:]
    private var fetchedRecords: [CKRecord] = []
    private var fetchedPhysicalDeletions: [CKRecord.ID] = []
    private var fetchedZoneDeleted = false
    private var durableQueueAvailable = true

    public init(
        store: any AgendaCloudSyncStore,
        containerIdentifier: String = agendaCloudContainerIdentifier
    ) {
        self.store = store
        self.container = CKContainer(identifier: containerIdentifier)
    }

    public func start(automaticallySync: Bool = true) async {
        guard engine == nil else { return }
        let state = await store.loadEngineState()
        var durableRecords: [CKRecord] = []
        do {
            durableRecords = try await store.loadPendingRecords()
        } catch {
            durableQueueAvailable = false
            await store.reportSyncError(
                "Could not read the durable CloudKit queue; serialized "
                    + "pending state was preserved."
            )
        }
        var configuration = CKSyncEngine.Configuration(
            database: container.privateCloudDatabase,
            stateSerialization: state,
            delegate: self
        )
        configuration.automaticallySync =
            automaticallySync && durableQueueAvailable
        engine = CKSyncEngine(configuration)
        if durableQueueAvailable {
            let durableIDs = Set(durableRecords.map(\.recordID))
            let bodylessIDs = Self.bodylessPendingSaveIDs(
                engine?.state.pendingRecordZoneChanges ?? [],
                durableIDs: durableIDs
            )
            if !bodylessIDs.isEmpty {
                engine?.state.remove(
                    pendingRecordZoneChanges: bodylessIDs.map {
                        .saveRecord($0)
                    }
                )
            }
        }
        pendingRecords.merge(
            Self.restoredPendingRecords(durableRecords),
            uniquingKeysWith: { _, restored in restored }
        )
        if !durableRecords.isEmpty {
            engine?.state.add(
                pendingDatabaseChanges: [
                    .saveZone(CKRecordZone(zoneID: AgendaCloudRecord.zoneID)),
                ]
            )
            engine?.state.add(
                pendingRecordZoneChanges: durableRecords.map {
                    .saveRecord($0.recordID)
                }
            )
        }
    }

    public func isDurableQueueAvailable() -> Bool {
        durableQueueAvailable
    }

    static func restoredPendingRecords(
        _ records: [CKRecord]
    ) -> [CKRecord.ID: CKRecord] {
        Dictionary(
            records.map { ($0.recordID, $0) },
            uniquingKeysWith: { _, latest in latest }
        )
    }

    static func bodylessPendingSaveIDs(
        _ changes: [CKSyncEngine.PendingRecordZoneChange],
        durableIDs: Set<CKRecord.ID>
    ) -> [CKRecord.ID] {
        changes.compactMap { change in
            guard case .saveRecord(let recordID) = change,
                  !durableIDs.contains(recordID)
            else { return nil }
            return recordID
        }
    }

    public func accountStatus() async throws -> CKAccountStatus {
        let status = try await container.accountStatus()
        if status == .available {
            let marker = try? await container.userRecordID().recordName
            await store.accountAvailable(accountMarker: marker)
        }
        return status
    }

    public func stage(_ records: [CKRecord]) async throws {
        await start()
        guard durableQueueAvailable else {
            throw CocoaError(.fileReadCorruptFile)
        }
        guard let engine else { return }
        try await store.persistPendingRecords(records)
        for record in records {
            pendingRecords[record.recordID] = record
        }
        engine.state.add(
            pendingDatabaseChanges: [
                .saveZone(CKRecordZone(zoneID: AgendaCloudRecord.zoneID)),
            ]
        )
        engine.state.add(
            pendingRecordZoneChanges: records.map {
                .saveRecord($0.recordID)
            }
        )
    }

    public func replaceStagedRecord(
        _ oldRecordID: CKRecord.ID,
        with record: CKRecord
    ) async {
        await start()
        guard durableQueueAvailable, let engine else { return }
        pendingRecords[oldRecordID] = nil
        pendingRecords[record.recordID] = record
        engine.state.remove(
            pendingRecordZoneChanges: [.saveRecord(oldRecordID)]
        )
        engine.state.add(
            pendingRecordZoneChanges: [.saveRecord(record.recordID)]
        )
    }

    public func discardPendingRecordIDs(
        _ recordIDs: [CKRecord.ID]
    ) async {
        await start()
        guard
            durableQueueAvailable,
            let engine,
            !recordIDs.isEmpty
        else { return }
        for recordID in recordIDs {
            pendingRecords[recordID] = nil
        }
        engine.state.remove(
            pendingRecordZoneChanges: recordIDs.map {
                .saveRecord($0)
            }
        )
        await store.removePendingRecordIDs(recordIDs)
    }

    public func sendNow() async throws {
        await start()
        guard durableQueueAvailable else {
            throw CocoaError(.fileReadCorruptFile)
        }
        try await engine?.sendChanges()
    }

    public func fetchNow() async throws {
        await start()
        guard durableQueueAvailable else {
            throw CocoaError(.fileReadCorruptFile)
        }
        try await engine?.fetchChanges()
    }

    public func handleEvent(
        _ event: CKSyncEngine.Event,
        syncEngine: CKSyncEngine
    ) async {
        switch event {
        case .stateUpdate(let update):
            await store.saveEngineState(update.stateSerialization)

        case .willFetchChanges:
            fetchedRecords.removeAll(keepingCapacity: true)
            fetchedPhysicalDeletions.removeAll(keepingCapacity: true)
            fetchedZoneDeleted = false

        case .fetchedRecordZoneChanges(let changes):
            fetchedRecords.append(contentsOf: changes.modifications.map(\.record))
            fetchedPhysicalDeletions.append(
                contentsOf: changes.deletions.map(\.recordID)
            )

        case .fetchedDatabaseChanges(let changes):
            if !changes.deletions.isEmpty {
                fetchedZoneDeleted = true
            }

        case .didFetchChanges:
            await store.applyFetchedCycle(
                AgendaCloudFetchCycle(
                    records: fetchedRecords,
                    physicalDeletions: fetchedPhysicalDeletions,
                    zoneDeleted: fetchedZoneDeleted
                )
            )
            fetchedRecords.removeAll(keepingCapacity: true)
            fetchedPhysicalDeletions.removeAll(keepingCapacity: true)
            fetchedZoneDeleted = false

        case .sentRecordZoneChanges(let changes):
            let savedIDs = changes.savedRecords.map(\.recordID)
            for record in changes.savedRecords {
                pendingRecords[record.recordID] = nil
            }
            await store.recordSavesSucceeded(changes.savedRecords)
            await store.removePendingRecordIDs(savedIDs)
            for failure in changes.failedRecordSaves {
                switch failure.error.code {
                case .zoneNotFound:
                    syncEngine.state.add(
                        pendingDatabaseChanges: [
                            .saveZone(
                                CKRecordZone(
                                    zoneID: failure.record.recordID.zoneID
                                )
                            ),
                        ]
                    )
                    syncEngine.state.add(
                        pendingRecordZoneChanges: [
                            .saveRecord(failure.record.recordID),
                        ]
                    )
                case .serverRecordChanged:
                    let serverRecord = failure.error.userInfo[
                        CKRecordChangedErrorServerRecordKey
                    ] as? CKRecord
                    if let serverRecord,
                       Self.recordsAreSemanticallyIdentical(
                           failure.record,
                           serverRecord
                       ) {
                        if let redelivery = Self.mutationRecordForRedelivery(
                            local: failure.record,
                            server: serverRecord,
                            requestedAt: Date()
                        ) {
                            do {
                                try await store.persistPendingRecords([redelivery])
                                pendingRecords[redelivery.recordID] = redelivery
                                syncEngine.state.add(
                                    pendingRecordZoneChanges: [
                                        .saveRecord(redelivery.recordID),
                                    ]
                                )
                            } catch {
                                await store.reportSyncError(
                                    "Could not preserve mutation redelivery: "
                                        + error.localizedDescription
                                )
                            }
                        } else {
                            pendingRecords[failure.record.recordID] = nil
                            await store.recordSavesSucceeded([serverRecord])
                            await store.removePendingRecordIDs(
                                [failure.record.recordID]
                            )
                        }
                    } else {
                        await store.reportSyncError(
                            "CloudKit conflict for \(failure.record.recordID.recordName); no local data was overwritten."
                        )
                    }
                case .networkFailure, .networkUnavailable, .zoneBusy,
                     .serviceUnavailable, .notAuthenticated,
                     .operationCancelled:
                    await store.reportSyncError(
                        "CloudKit temporarily unavailable: \(failure.error.localizedDescription)"
                    )
                default:
                    await store.reportSyncError(
                        "CloudKit save failed: \(failure.error.localizedDescription)"
                    )
                }
            }

        case .accountChange:
            await store.accountChanged()
            let marker = try? await container.userRecordID().recordName
            await store.accountAvailable(accountMarker: marker)

        case .sentDatabaseChanges,
             .willFetchRecordZoneChanges,
             .didFetchRecordZoneChanges,
             .willSendChanges,
             .didSendChanges:
            break

        @unknown default:
            await store.reportSyncError("Received an unknown CloudKit event.")
        }
    }

    public func nextRecordZoneChangeBatch(
        _ context: CKSyncEngine.SendChangesContext,
        syncEngine: CKSyncEngine
    ) async -> CKSyncEngine.RecordZoneChangeBatch? {
        let changes = syncEngine.state.pendingRecordZoneChanges
            .filter { context.options.scope.contains($0) }
            .sorted {
                pendingPriority($0) > pendingPriority($1)
            }
        return await CKSyncEngine.RecordZoneChangeBatch(
            pendingChanges: changes
        ) { [pendingRecords] recordID in
            pendingRecords[recordID]
        }
    }

    private func pendingPriority(
        _ change: CKSyncEngine.PendingRecordZoneChange
    ) -> Int {
        guard case .saveRecord(let recordID) = change,
              let record = pendingRecords[recordID]
        else { return 0 }
        switch record.recordType {
        case AgendaCloudRecord.mutationType:
            return 5_000_000
        case AgendaCloudRecord.resultType:
            return 4_000_000
        case AgendaCloudRecord.canonicalType:
            let revision = (
                try? AgendaCloudRecord.decodeCanonical(record).revision
            ) ?? 0
            return 3_000_000 + revision
        case AgendaCloudRecord.tombstoneType:
            return 2_000_000
        default:
            return 1_000_000
        }
    }

    static func mutationRecordForRedelivery(
        local: CKRecord,
        server: CKRecord,
        requestedAt: Date
    ) -> CKRecord? {
        guard
            local.recordType == AgendaCloudRecord.mutationType,
            recordsAreSemanticallyIdentical(local, server)
        else { return nil }
        server[AgendaCloudRecord.redeliveryRequestedAtKey] =
            requestedAt as CKRecordValue
        return server
    }

    private static func recordsAreSemanticallyIdentical(
        _ local: CKRecord,
        _ remote: CKRecord
    ) -> Bool {
        guard
            local.recordType == remote.recordType,
            local[AgendaCloudRecord.schemaVersionKey] as? Int
                == remote[AgendaCloudRecord.schemaVersionKey] as? Int,
            local[AgendaCloudRecord.stableIDKey] as? String
                == remote[AgendaCloudRecord.stableIDKey] as? String
        else { return false }
        switch local.recordType {
        case AgendaCloudRecord.mutationType:
            guard
                let lhs = try? AgendaCloudRecord.decodeMutation(local),
                let rhs = try? AgendaCloudRecord.decodeMutation(remote)
            else { return false }
            return lhs.mutationID == rhs.mutationID
                && AgendaSyncContract.fingerprint(mutation: lhs)
                    == AgendaSyncContract.fingerprint(mutation: rhs)
        case AgendaCloudRecord.resultType:
            return (try? AgendaCloudRecord.decodeResult(local))
                == (try? AgendaCloudRecord.decodeResult(remote))
        case AgendaCloudRecord.canonicalType:
            return (try? AgendaCloudRecord.decodeCanonical(local))
                == (try? AgendaCloudRecord.decodeCanonical(remote))
        case AgendaCloudRecord.tombstoneType:
            return (try? AgendaCloudRecord.decodeTombstone(local))
                == (try? AgendaCloudRecord.decodeTombstone(remote))
        case AgendaCloudRecord.projectType:
            return (try? AgendaCloudRecord.decodeProject(local))
                == (try? AgendaCloudRecord.decodeProject(remote))
        case AgendaCloudRecord.todoType:
            return (try? AgendaCloudRecord.decodeTodo(local))
                == (try? AgendaCloudRecord.decodeTodo(remote))
        default:
            return local[AgendaCloudRecord.payloadKey] as? Data
                == remote[AgendaCloudRecord.payloadKey] as? Data
        }
    }
}

