import CloudKit
import Foundation

public enum AgendaCloudRecord {
    public static let projectType = "AgendaProjectV1"
    public static let todoType = "AgendaTodoV1"
    public static let tombstoneType = "AgendaTombstoneV2"
    public static let canonicalType = "AgendaCanonicalV2"
    public static let canonicalManifestType = "AgendaCanonicalManifestV2"
    public static let boardOrderType = "AgendaBoardOrderV2"
    public static let mutationType = "AgendaMutationV2"
    public static let resultType = "AgendaResultV2"
    public static let payloadKey = "payload"
    public static let schemaVersionKey = "schemaVersion"
    public static let stableIDKey = "stableID"
    public static let redeliveryRequestedAtKey = "redeliveryRequestedAt"

    public static var zoneID: CKRecordZone.ID {
        CKRecordZone.ID(zoneName: agendaCloudZoneName)
    }

    public static var canonicalManifestID: CKRecord.ID {
        CKRecord.ID(
            recordName: "canonical-manifest-v2",
            zoneID: zoneID
        )
    }

    public static var boardOrderID: CKRecord.ID {
        CKRecord.ID(recordName: "board-order-v2", zoneID: zoneID)
    }

    public static func stateRecordID(
        for entry: AgendaCanonicalManifestEntry
    ) -> CKRecord.ID {
        let prefix = entry.deleted ? "tombstone" : "canonical"
        return CKRecord.ID(
            recordName:
                "\(prefix)-\(entry.entityKind.rawValue)-"
                + "\(entry.stableID)-r\(entry.revision)",
            zoneID: zoneID
        )
    }

    public static func project(_ value: AgendaProjectRecord) throws -> CKRecord {
        let id = CKRecord.ID(
            recordName: "project-\(value.stableID)",
            zoneID: zoneID
        )
        let record = CKRecord(recordType: projectType, recordID: id)
        try populate(
            record,
            with: value,
            stableID: value.stableID,
            schemaVersion: agendaWireSchemaVersion
        )
        return record
    }

    public static func todo(_ value: AgendaTodoRecord) throws -> CKRecord {
        let id = CKRecord.ID(
            recordName: "todo-\(value.stableID)",
            zoneID: zoneID
        )
        let record = CKRecord(recordType: todoType, recordID: id)
        try populate(
            record,
            with: value,
            stableID: value.stableID,
            schemaVersion: agendaWireSchemaVersion
        )
        return record
    }

    public static func tombstone(_ value: AgendaTombstoneRecord) throws -> CKRecord {
        let id = CKRecord.ID(
            recordName: "tombstone-\(value.id)",
            zoneID: zoneID
        )
        let record = CKRecord(recordType: tombstoneType, recordID: id)
        try populate(
            record,
            with: value,
            stableID: value.stableID,
            schemaVersion: agendaContractSchemaVersion
        )
        return record
    }

    public static func canonical(
        _ value: AgendaCanonicalRecordV2
    ) throws -> CKRecord {
        let id = CKRecord.ID(
            recordName: "canonical-\(value.id)",
            zoneID: zoneID
        )
        let record = CKRecord(recordType: canonicalType, recordID: id)
        try populate(
            record,
            with: value,
            stableID: value.stableID,
            schemaVersion: agendaContractSchemaVersion
        )
        return record
    }

    public static func canonicalManifest(
        _ entries: [AgendaCanonicalManifestEntry],
        existingRecord: CKRecord? = nil
    ) throws -> CKRecord {
        let record = existingRecord ?? CKRecord(
            recordType: canonicalManifestType,
            recordID: canonicalManifestID
        )
        try populate(
            record,
            with: entries,
            stableID: "canonical-manifest-v2",
            schemaVersion: agendaContractSchemaVersion
        )
        return record
    }

    public static func decodeCanonicalManifest(
        _ record: CKRecord
    ) throws -> [AgendaCanonicalManifestEntry] {
        guard record.recordType == canonicalManifestType else {
            throw AgendaCloudRecordError.unsupportedSchema
        }
        return try decode(
            record,
            as: [AgendaCanonicalManifestEntry].self,
            schemaVersion: agendaContractSchemaVersion
        )
    }

    public static func boardOrder(
        _ value: AgendaBoardOrderSnapshot,
        existingRecord: CKRecord? = nil
    ) throws -> CKRecord {
        let record = existingRecord ?? CKRecord(
            recordType: boardOrderType,
            recordID: boardOrderID
        )
        try populate(
            record,
            with: value,
            stableID: "board-order-v2",
            schemaVersion: agendaContractSchemaVersion
        )
        return record
    }

    public static func decodeBoardOrder(
        _ record: CKRecord
    ) throws -> AgendaBoardOrderSnapshot {
        guard record.recordType == boardOrderType else {
            throw AgendaCloudRecordError.unsupportedSchema
        }
        return try decode(
            record,
            as: AgendaBoardOrderSnapshot.self,
            schemaVersion: agendaContractSchemaVersion
        )
    }

    public static func mutation(
        _ value: AgendaMutationProposal
    ) throws -> CKRecord {
        let id = CKRecord.ID(
            recordName: "mutation-\(value.mutationID)",
            zoneID: zoneID
        )
        let record = CKRecord(recordType: mutationType, recordID: id)
        try populate(
            record,
            with: value,
            stableID: value.entityID,
            schemaVersion: agendaContractSchemaVersion
        )
        return record
    }

    public static func result(
        _ value: AgendaMutationResultRecord
    ) throws -> CKRecord {
        let id = CKRecord.ID(
            recordName: value.recordName,
            zoneID: zoneID
        )
        let record = CKRecord(recordType: resultType, recordID: id)
        try populate(
            record,
            with: value,
            stableID: value.entityID,
            schemaVersion: agendaContractSchemaVersion
        )
        return record
    }

    public static func decodeProject(_ record: CKRecord) throws -> AgendaProjectRecord {
        try decode(
            record,
            as: AgendaProjectRecord.self,
            schemaVersion: agendaWireSchemaVersion
        )
    }

    public static func decodeTodo(_ record: CKRecord) throws -> AgendaTodoRecord {
        try decode(
            record,
            as: AgendaTodoRecord.self,
            schemaVersion: agendaWireSchemaVersion
        )
    }

    public static func decodeTombstone(
        _ record: CKRecord
    ) throws -> AgendaTombstoneRecord {
        try decode(
            record,
            as: AgendaTombstoneRecord.self,
            schemaVersion: agendaContractSchemaVersion
        )
    }

    public static func decodeCanonical(
        _ record: CKRecord
    ) throws -> AgendaCanonicalRecordV2 {
        try decode(
            record,
            as: AgendaCanonicalRecordV2.self,
            schemaVersion: agendaContractSchemaVersion
        )
    }

    public static func decodeMutation(
        _ record: CKRecord
    ) throws -> AgendaMutationProposal {
        try decode(
            record,
            as: AgendaMutationProposal.self,
            schemaVersion: agendaContractSchemaVersion
        )
    }

    public static func decodeResult(
        _ record: CKRecord
    ) throws -> AgendaMutationResultRecord {
        try decode(
            record,
            as: AgendaMutationResultRecord.self,
            schemaVersion: agendaContractSchemaVersion
        )
    }

    public static func archiveSystemFields(_ record: CKRecord) throws -> Data {
        let archiver = NSKeyedArchiver(requiringSecureCoding: true)
        record.encodeSystemFields(with: archiver)
        archiver.finishEncoding()
        return archiver.encodedData
    }

    public static func archiveRecord(_ record: CKRecord) throws -> Data {
        try NSKeyedArchiver.archivedData(
            withRootObject: record,
            requiringSecureCoding: true
        )
    }

    public static func unarchiveRecord(_ data: Data) -> CKRecord? {
        try? NSKeyedUnarchiver.unarchivedObject(
            ofClass: CKRecord.self,
            from: data
        )
    }

    public static func restoreRecord(
        systemFields: Data,
        fallbackType: String,
        fallbackID: CKRecord.ID
    ) -> CKRecord {
        do {
            let unarchiver = try NSKeyedUnarchiver(
                forReadingFrom: systemFields
            )
            unarchiver.requiresSecureCoding = true
            defer { unarchiver.finishDecoding() }
            if let record = CKRecord(coder: unarchiver) {
                return record
            }
        } catch {
            // A corrupt cache must not block local use; the next save can start
            // from a fresh record and surface a server conflict if necessary.
        }
        return CKRecord(recordType: fallbackType, recordID: fallbackID)
    }

    public static func update(
        _ record: CKRecord,
        with value: AgendaTodoRecord
    ) throws {
        try populate(
            record,
            with: value,
            stableID: value.stableID,
            schemaVersion: agendaWireSchemaVersion
        )
    }

    private static func populate<T: Encodable>(
        _ record: CKRecord,
        with value: T,
        stableID: String,
        schemaVersion: Int
    ) throws {
        record[payloadKey] = try JSONEncoder().encode(value) as CKRecordValue
        record[schemaVersionKey] = schemaVersion as CKRecordValue
        record[stableIDKey] = stableID as CKRecordValue
    }

    private static func decode<T: Decodable>(
        _ record: CKRecord,
        as type: T.Type,
        schemaVersion: Int
    ) throws -> T {
        guard
            let version = record[schemaVersionKey] as? Int,
            version == schemaVersion,
            let payload = record[payloadKey] as? Data
        else {
            throw AgendaCloudRecordError.unsupportedSchema
        }
        return try JSONDecoder().decode(type, from: payload)
    }
}

public enum AgendaCloudRecordError: Error {
    case unsupportedSchema
}

