import Foundation

public let agendaWireSchemaVersion = 1
public let agendaCloudContainerIdentifier = "iCloud.com.willschulz.Agenda"
public let agendaCloudZoneName = "AgendaV1"
public let agendaSyncTestProjectName = "CloudKit Sync Test"

public struct AgendaNote: Codable, Hashable, Sendable {
    public let ts: String?
    public let author: String?
    public let text: String

    public init(ts: String?, author: String?, text: String) {
        self.ts = ts
        self.author = author
        self.text = text
    }
}

public struct AgendaProjectRecord: Codable, Hashable, Identifiable, Sendable {
    public let stableID: String
    public let name: String
    public let category: String
    public let displayOrder: Int
    public let structureRevision: Int?
    public let notes: [AgendaNote]
    public let northstars: [String]
    public let archived: Bool
    public let createdAt: String
    public let updatedAt: String

    public var id: String { stableID }

    public init(
        stableID: String,
        name: String,
        category: String,
        displayOrder: Int,
        structureRevision: Int? = nil,
        notes: [AgendaNote],
        northstars: [String],
        archived: Bool,
        createdAt: String,
        updatedAt: String
    ) {
        self.stableID = stableID
        self.name = name
        self.category = category
        self.displayOrder = displayOrder
        self.structureRevision = structureRevision
        self.notes = notes
        self.northstars = northstars
        self.archived = archived
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }

    enum CodingKeys: String, CodingKey {
        case stableID = "stable_id"
        case name, category
        case displayOrder = "display_order"
        case structureRevision = "structure_revision"
        case notes, northstars, archived
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

public struct AgendaTodoRecord: Codable, Hashable, Identifiable, Sendable {
    public let stableID: String
    public let projectStableID: String
    public let parentStableID: String?
    public var text: String
    public let isBlocker: Bool
    public let forLater: Bool
    public let inProgress: Bool
    public let flagged: Bool
    public let awaitingReview: Bool
    public let displayOrder: Int
    public let completed: Bool
    public let completedAt: String?
    public let notes: [AgendaNote]
    public let effort: Int?
    public let deadline: String?
    public let createdAt: String
    public let updatedAt: String

    public var id: String { stableID }

    enum CodingKeys: String, CodingKey {
        case stableID = "stable_id"
        case projectStableID = "project_stable_id"
        case parentStableID = "parent_stable_id"
        case text
        case isBlocker = "is_blocker"
        case forLater = "for_later"
        case inProgress = "in_progress"
        case flagged
        case awaitingReview = "awaiting_review"
        case displayOrder = "display_order"
        case completed
        case completedAt = "completed_at"
        case notes, effort, deadline
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    private enum LegacyCodingKeys: String, CodingKey {
        case isFollowup = "is_followup"
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let legacy = try decoder.container(keyedBy: LegacyCodingKeys.self)
        stableID = try values.decode(String.self, forKey: .stableID)
        projectStableID = try values.decode(String.self, forKey: .projectStableID)
        parentStableID = try values.decodeIfPresent(String.self, forKey: .parentStableID)
        text = try values.decode(String.self, forKey: .text)
        isBlocker = try values.decode(Bool.self, forKey: .isBlocker)
        forLater = try values.decodeIfPresent(Bool.self, forKey: .forLater)
            ?? legacy.decodeIfPresent(Bool.self, forKey: .isFollowup)
            ?? false
        inProgress = try values.decode(Bool.self, forKey: .inProgress)
        flagged = try values.decode(Bool.self, forKey: .flagged)
        awaitingReview = try values.decode(Bool.self, forKey: .awaitingReview)
        displayOrder = try values.decode(Int.self, forKey: .displayOrder)
        completed = try values.decode(Bool.self, forKey: .completed)
        completedAt = try values.decodeIfPresent(String.self, forKey: .completedAt)
        notes = try values.decode([AgendaNote].self, forKey: .notes)
        effort = try values.decodeIfPresent(Int.self, forKey: .effort)
        deadline = try values.decodeIfPresent(String.self, forKey: .deadline)
        createdAt = try values.decode(String.self, forKey: .createdAt)
        updatedAt = try values.decode(String.self, forKey: .updatedAt)
    }
}

public struct AgendaSnapshot: Codable, Hashable, Sendable {
    public let schemaVersion: Int
    public let generatedAt: String
    public let projects: [AgendaProjectRecord]
    public let todos: [AgendaTodoRecord]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case generatedAt = "generated_at"
        case projects, todos
    }

    public init(
        schemaVersion: Int,
        generatedAt: String,
        projects: [AgendaProjectRecord],
        todos: [AgendaTodoRecord]
    ) {
        self.schemaVersion = schemaVersion
        self.generatedAt = generatedAt
        self.projects = projects
        self.todos = todos
    }
}

public struct AgendaTombstoneRecord: Codable, Hashable, Identifiable, Sendable {
    public let schemaVersion: Int
    public let entityKind: AgendaContractEntityKind
    public let stableID: String
    public let projectStableID: String?
    public let parentStableID: String?
    public let revision: Int
    public let deletedAt: String

    public var id: String {
        "\(entityKind.rawValue)-\(stableID)-r\(revision)"
    }

    public init(
        schemaVersion: Int = agendaContractSchemaVersion,
        entityKind: AgendaContractEntityKind,
        stableID: String,
        projectStableID: String? = nil,
        parentStableID: String? = nil,
        revision: Int,
        deletedAt: String
    ) {
        self.schemaVersion = schemaVersion
        self.entityKind = entityKind
        self.stableID = stableID
        self.projectStableID = projectStableID
        self.parentStableID = parentStableID
        self.revision = revision
        self.deletedAt = deletedAt
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case entityKind = "entity_kind"
        case stableID = "stable_id"
        case projectStableID = "project_stable_id"
        case parentStableID = "parent_stable_id"
        case revision
        case deletedAt = "deleted_at"
    }
}

public struct AgendaTombstoneSnapshot: Codable, Hashable, Sendable {
    public let schemaVersion: Int
    public let generatedAt: String
    public let tombstones: [AgendaTombstoneRecord]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case generatedAt = "generated_at"
        case tombstones
    }
}

public struct AgendaCanonicalRecordV2: Codable, Hashable, Identifiable, Sendable {
    public let schemaVersion: Int
    public let entityKind: AgendaContractEntityKind
    public let stableID: String
    public let revision: Int
    public let structureRevision: Int?
    public let deleted: Bool
    public let payloadJSON: String

    public var id: String {
        "\(entityKind.rawValue)-\(stableID)-r\(revision)"
    }

    public init(
        schemaVersion: Int = agendaContractSchemaVersion,
        entityKind: AgendaContractEntityKind,
        stableID: String,
        revision: Int,
        structureRevision: Int? = nil,
        deleted: Bool = false,
        payloadJSON: String
    ) {
        self.schemaVersion = schemaVersion
        self.entityKind = entityKind
        self.stableID = stableID
        self.revision = revision
        self.structureRevision = structureRevision
        self.deleted = deleted
        self.payloadJSON = payloadJSON
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case entityKind = "entity_kind"
        case stableID = "stable_id"
        case revision, deleted
        case structureRevision = "structure_revision"
        case payloadJSON = "payload_json"
    }
}

public struct AgendaCanonicalManifestEntry: Codable, Hashable, Sendable {
    public let entityKind: AgendaContractEntityKind
    public let stableID: String
    public let revision: Int
    public let structureRevision: Int?
    public let deleted: Bool

    public init(
        entityKind: AgendaContractEntityKind,
        stableID: String,
        revision: Int,
        structureRevision: Int? = nil,
        deleted: Bool = false
    ) {
        self.entityKind = entityKind
        self.stableID = stableID
        self.revision = revision
        self.structureRevision = structureRevision
        self.deleted = deleted
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        entityKind = try values.decode(
            AgendaContractEntityKind.self,
            forKey: .entityKind
        )
        stableID = try values.decode(String.self, forKey: .stableID)
        revision = try values.decode(Int.self, forKey: .revision)
        structureRevision = try values.decodeIfPresent(
            Int.self,
            forKey: .structureRevision
        )
        deleted = try values.decodeIfPresent(Bool.self, forKey: .deleted)
            ?? false
    }

    public static func latestStates(
        canonicals: [AgendaCanonicalRecordV2],
        tombstones: [AgendaTombstoneRecord]
    ) -> [AgendaCanonicalManifestEntry] {
        var entries: [String: AgendaCanonicalManifestEntry] = [:]
        for canonical in canonicals {
            let key = "\(canonical.entityKind.rawValue):\(canonical.stableID)"
            let entry = AgendaCanonicalManifestEntry(
                entityKind: canonical.entityKind,
                stableID: canonical.stableID,
                revision: canonical.revision,
                structureRevision: canonical.structureRevision
            )
            if entry.revision >= (entries[key]?.revision ?? -1) {
                entries[key] = entry
            }
        }
        for tombstone in tombstones {
            let key = "\(tombstone.entityKind.rawValue):\(tombstone.stableID)"
            let entry = AgendaCanonicalManifestEntry(
                entityKind: tombstone.entityKind,
                stableID: tombstone.stableID,
                revision: tombstone.revision,
                deleted: true
            )
            if entry.revision >= (entries[key]?.revision ?? -1) {
                entries[key] = entry
            }
        }
        return entries.values.sorted {
            ($0.entityKind.rawValue, $0.stableID)
                < ($1.entityKind.rawValue, $1.stableID)
        }
    }

    enum CodingKeys: String, CodingKey {
        case entityKind = "entity_kind"
        case stableID = "stable_id"
        case revision
        case structureRevision = "structure_revision"
        case deleted
    }
}

public struct AgendaCanonicalSnapshotV2: Codable, Hashable, Sendable {
    public let schemaVersion: Int
    public let generatedAt: String
    public let records: [AgendaCanonicalRecordV2]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case generatedAt = "generated_at"
        case records
    }
}

public struct AgendaBoardOrderSnapshot: Codable, Hashable, Sendable {
    public let schemaVersion: Int
    public let generatedAt: String
    public let rows: [String: [String]]

    public init(
        schemaVersion: Int = agendaContractSchemaVersion,
        generatedAt: String,
        rows: [String: [String]]
    ) {
        self.schemaVersion = schemaVersion
        self.generatedAt = generatedAt
        self.rows = rows
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case generatedAt = "generated_at"
        case rows
    }
}

