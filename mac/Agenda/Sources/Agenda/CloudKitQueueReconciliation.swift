import AgendaSyncCore
import CloudKit
import CryptoKit
import Foundation

typealias AgendaRecordResults = [CKRecord.ID: Result<CKRecord, Error>]

protocol AgendaCloudDatabaseAccess: Sendable {
    func records(for ids: [CKRecord.ID]) async throws -> AgendaRecordResults
    func saveZone() async throws
    func save(_ record: CKRecord) async throws -> CKRecord
    func saveMissing(_ records: [CKRecord]) async throws -> AgendaRecordResults
}

protocol AgendaPendingRecordDiscarding: Sendable {
    func discardPendingRecordIDs(_ recordIDs: [CKRecord.ID]) async
}

@available(macOS 14.0, *)
extension AgendaSyncEngine: AgendaPendingRecordDiscarding {}

struct LiveAgendaCloudDatabase: AgendaCloudDatabaseAccess, @unchecked Sendable {
    let database: CKDatabase

    func records(for ids: [CKRecord.ID]) async throws -> AgendaRecordResults {
        try await database.agendaRecords(for: ids)
    }

    func saveZone() async throws {
        _ = try await database.save(
            CKRecordZone(zoneID: AgendaCloudRecord.zoneID)
        )
    }

    func save(_ record: CKRecord) async throws -> CKRecord {
        try await database.save(record)
    }

    func saveMissing(_ records: [CKRecord]) async throws -> AgendaRecordResults {
        try await database.agendaSaveRecords(
            records,
            savePolicy: .ifServerRecordUnchanged
        )
    }
}

struct AgendaCanonicalQuarantineEntry: Codable, Hashable {
    let recordName: String
    let reason: String
    let observedAt: String
    let localRevision: Int?
    let serverRevision: Int?
    let currentExportRevision: Int?
    let localPayloadSHA256: String
    let serverPayloadSHA256: String?
    let currentExportPayloadSHA256: String?
    let localRecord: Data
    let serverRecord: Data?
    let currentExportRecord: Data?

    static func make(
        local: CKRecord,
        server: CKRecord?,
        currentExport: CKRecord? = nil,
        reason: String,
        observedAt: String = ISO8601DateFormatter().string(from: Date())
    ) throws -> AgendaCanonicalQuarantineEntry {
        let localPayload = local["payload"] as? Data ?? Data()
        let serverPayload = server?["payload"] as? Data
        let currentExportPayload = currentExport?["payload"] as? Data
        return AgendaCanonicalQuarantineEntry(
            recordName: local.recordID.recordName,
            reason: reason,
            observedAt: observedAt,
            localRevision: revision(in: localPayload),
            serverRevision: serverPayload.flatMap(revision),
            currentExportRevision: currentExportPayload.flatMap(revision),
            localPayloadSHA256: sha256(localPayload),
            serverPayloadSHA256: serverPayload.map(sha256),
            currentExportPayloadSHA256: currentExportPayload.map(sha256),
            localRecord: try AgendaCloudRecord.archiveRecord(local),
            serverRecord: try server.map(AgendaCloudRecord.archiveRecord),
            currentExportRecord: try currentExport.map(
                AgendaCloudRecord.archiveRecord
            )
        )
    }

    private static func revision(in payload: Data) -> Int? {
        guard
            let object = try? JSONSerialization.jsonObject(with: payload)
                as? [String: Any]
        else { return nil }
        return object["revision"] as? Int
    }

    private static func sha256(_ data: Data) -> String {
        SHA256.hash(data: data)
            .map { String(format: "%02x", $0) }
            .joined()
    }
}

struct AgendaCanonicalReconciliationReport: Equatable {
    var acknowledged = 0
    var published = 0
    var obsolete = 0
    var quarantined = 0
    var retrying = 0

    var summary: String {
        "acknowledged=\(acknowledged) published=\(published) "
            + "obsolete=\(obsolete) quarantined=\(quarantined) "
            + "retrying=\(retrying)"
    }
}

/// Payload keys that may change on the Mac without a canonical revision bump.
/// `updated_at` moves for Mac-local fields (title/suggestion status, `running`)
/// that the revision triggers deliberately ignore, so a same-revision record
/// differing only here carries no synced change and must not be quarantined.
let agendaCanonicalVolatilePayloadKeys: Set<String> = ["updated_at"]

func agendaCanonicalsSemanticallyEqual(
    _ lhs: CKRecord,
    _ rhs: CKRecord
) -> Bool {
    guard
        let left = try? AgendaCloudRecord.decodeCanonical(lhs),
        let right = try? AgendaCloudRecord.decodeCanonical(rhs)
    else { return false }
    return agendaCanonicalsSemanticallyEqual(left, right)
}

func agendaCanonicalsSemanticallyEqual(
    _ left: AgendaCanonicalRecordV2,
    _ right: AgendaCanonicalRecordV2
) -> Bool {
    if left == right { return true }
    guard
        left.schemaVersion == right.schemaVersion,
        left.entityKind == right.entityKind,
        left.stableID == right.stableID,
        left.revision == right.revision,
        left.structureRevision == right.structureRevision,
        left.deleted == right.deleted,
        let leftPayload = agendaStablePayload(left.payloadJSON),
        let rightPayload = agendaStablePayload(right.payloadJSON)
    else { return false }
    return leftPayload == rightPayload
}

private func agendaStablePayload(_ json: String) -> NSDictionary? {
    guard
        let object = try? JSONSerialization.jsonObject(with: Data(json.utf8)),
        var payload = object as? [String: Any]
    else { return nil }
    for key in agendaCanonicalVolatilePayloadKeys {
        payload.removeValue(forKey: key)
    }
    return payload as NSDictionary
}
