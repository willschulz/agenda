import CryptoKit
import Foundation

public let agendaContractSchemaVersion = 2

public enum AgendaContractOutcome: String, Codable, Sendable {
    case accept
    case idempotent
    case conflict
    case quarantine
}

public enum AgendaMutationOperation: String, Codable, Sendable {
    case create
    case scalarEdit = "scalar_edit"
    case noteAppend = "note_append"
    case structureEdit = "structure_edit"
    case delete
    case resurrect
}

public enum AgendaContractEntityKind: String, Codable, Sendable {
    case project
    case todo
}

public enum AgendaPriorMutation: String, Codable, Sendable {
    case none
    case same
    case different
}

public struct AgendaCanonicalContractState: Codable, Hashable, Sendable {
    public let schemaVersion: Int
    public let revision: Int
    public let structureRevision: Int
    public let deleted: Bool
    public let notes: [String: String]
    public let exists: Bool?

    public init(
        schemaVersion: Int,
        revision: Int,
        structureRevision: Int = 0,
        deleted: Bool = false,
        notes: [String: String] = [:],
        exists: Bool? = true
    ) {
        self.schemaVersion = schemaVersion
        self.revision = revision
        self.structureRevision = structureRevision
        self.deleted = deleted
        self.notes = notes
        self.exists = exists
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case revision
        case structureRevision = "structure_revision"
        case deleted
        case notes
        case exists
    }
}

public struct AgendaMutationProposal: Codable, Hashable, Sendable {
    public let schemaVersion: Int
    public let mutationID: String
    public let deviceID: String
    public let entityID: String
    public let operation: AgendaMutationOperation
    public let baseRevision: Int
    public let baseStructureRevision: Int?
    public let noteID: String?
    public let notePayload: String?
    public let proposedPayload: String?
    public let entityKind: AgendaContractEntityKind?

    public init(
        schemaVersion: Int,
        mutationID: String,
        deviceID: String,
        entityID: String,
        operation: AgendaMutationOperation,
        baseRevision: Int,
        baseStructureRevision: Int? = nil,
        noteID: String? = nil,
        notePayload: String? = nil,
        proposedPayload: String? = nil,
        entityKind: AgendaContractEntityKind? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.mutationID = mutationID
        self.deviceID = deviceID
        self.entityID = entityID
        self.operation = operation
        self.baseRevision = baseRevision
        self.baseStructureRevision = baseStructureRevision
        self.noteID = noteID
        self.notePayload = notePayload
        self.proposedPayload = proposedPayload
        self.entityKind = entityKind
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case mutationID = "mutation_id"
        case deviceID = "device_id"
        case entityID = "entity_id"
        case operation
        case baseRevision = "base_revision"
        case baseStructureRevision = "base_structure_revision"
        case noteID = "note_id"
        case notePayload = "note_payload"
        case proposedPayload = "proposed_payload"
        case entityKind = "entity_kind"
    }
}

public struct AgendaContractDecision: Codable, Hashable, Sendable {
    public let outcome: AgendaContractOutcome
    public let reason: String
    public let nextRevision: Int?
    public let nextStructureRevision: Int?

    public init(
        outcome: AgendaContractOutcome,
        reason: String,
        nextRevision: Int? = nil,
        nextStructureRevision: Int? = nil
    ) {
        self.outcome = outcome
        self.reason = reason
        self.nextRevision = nextRevision
        self.nextStructureRevision = nextStructureRevision
    }

    enum CodingKeys: String, CodingKey {
        case outcome, reason
        case nextRevision = "next_revision"
        case nextStructureRevision = "next_structure_revision"
    }
}

public struct AgendaMutationResultRecord: Codable, Hashable, Sendable {
    public let schemaVersion: Int
    public let mutationID: String
    public let mutationFingerprint: String
    public let deviceID: String
    public let entityID: String
    public let outcome: AgendaContractOutcome
    public let reason: String
    public let canonicalRevision: Int?
    public let canonicalStructureRevision: Int?

    public var recordName: String { "result-\(mutationID)" }

    public init(
        schemaVersion: Int,
        mutationID: String,
        mutationFingerprint: String,
        deviceID: String,
        entityID: String,
        outcome: AgendaContractOutcome,
        reason: String,
        canonicalRevision: Int? = nil,
        canonicalStructureRevision: Int? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.mutationID = mutationID
        self.mutationFingerprint = mutationFingerprint
        self.deviceID = deviceID
        self.entityID = entityID
        self.outcome = outcome
        self.reason = reason
        self.canonicalRevision = canonicalRevision
        self.canonicalStructureRevision = canonicalStructureRevision
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case mutationID = "mutation_id"
        case mutationFingerprint = "mutation_fingerprint"
        case deviceID = "device_id"
        case entityID = "entity_id"
        case outcome, reason
        case canonicalRevision = "canonical_revision"
        case canonicalStructureRevision = "canonical_structure_revision"
    }
}

public enum AgendaSyncContract {
    public static func fingerprint(
        mutation: AgendaMutationProposal
    ) -> String {
        canonicalSemanticFingerprint(
            fields: [
                "schema_version": String(mutation.schemaVersion),
                "device_id": mutation.deviceID,
                "entity_id": mutation.entityID,
                "operation": mutation.operation.rawValue,
                "base_revision": String(mutation.baseRevision),
                "base_structure_revision": mutation.baseStructureRevision.map(String.init),
                "note_id": mutation.noteID,
                "note_payload": mutation.notePayload,
                "proposed_payload": mutation.proposedPayload,
                "entity_kind": mutation.entityKind?.rawValue,
            ]
        )
    }

    public static func canonicalSemanticFingerprint(
        fields: [String: String?]
    ) -> String {
        var bytes = Data()
        for key in fields.keys.sorted() {
            guard let value = fields[key] ?? nil else { continue }
            let keyData = Data(key.utf8)
            let valueData = Data(value.utf8)
            bytes.append(Data(String(keyData.count).utf8))
            bytes.append(0x3A)
            bytes.append(keyData)
            bytes.append(Data(String(valueData.count).utf8))
            bytes.append(0x3A)
            bytes.append(valueData)
            bytes.append(0x3B)
        }
        return SHA256.hash(data: bytes)
            .map { String(format: "%02x", $0) }
            .joined()
    }

    public static func evaluate(
        canonical: AgendaCanonicalContractState,
        mutation: AgendaMutationProposal,
        priorMutation: AgendaPriorMutation = .none
    ) -> AgendaContractDecision {
        if canonical.schemaVersion > agendaContractSchemaVersion {
            return quarantine("newer_canonical_schema")
        }
        if mutation.schemaVersion > agendaContractSchemaVersion {
            return quarantine("newer_mutation_schema")
        }
        if mutation.schemaVersion < canonical.schemaVersion {
            return quarantine("schema_downgrade")
        }
        if canonical.schemaVersion < agendaContractSchemaVersion {
            return quarantine("canonical_upgrade_required")
        }

        switch priorMutation {
        case .same:
            return AgendaContractDecision(
                outcome: .idempotent,
                reason: "mutation_replay",
                nextRevision: canonical.revision,
                nextStructureRevision: canonical.structureRevision
            )
        case .different:
            return conflict(canonical, "mutation_id_reuse")
        case .none:
            break
        }

        if mutation.operation == .create {
            if mutation.entityKind != .todo {
                return conflict(canonical, "create_kind_not_enabled")
            }
            if canonical.exists ?? true {
                return conflict(canonical, "entity_exists")
            }
            if mutation.baseRevision != 0 {
                return conflict(canonical, "invalid_create_revision")
            }
            return accept(canonical, incrementStructure: false)
        }

        if canonical.exists == false {
            return conflict(canonical, "entity_missing")
        }

        if mutation.baseRevision > canonical.revision {
            return conflict(canonical, "future_revision")
        }

        if canonical.deleted {
            if mutation.operation == .resurrect,
               mutation.baseRevision == canonical.revision {
                return accept(canonical, incrementStructure: false)
            }
            return conflict(canonical, "entity_deleted")
        }

        if mutation.operation == .resurrect {
            return conflict(canonical, "entity_not_deleted")
        }

        if mutation.operation == .noteAppend {
            guard
                let noteID = mutation.noteID,
                let notePayload = mutation.notePayload
            else {
                return conflict(canonical, "invalid_note_append")
            }
            if let existing = canonical.notes[noteID] {
                if existing == notePayload {
                    return AgendaContractDecision(
                        outcome: .idempotent,
                        reason: "note_replay",
                        nextRevision: canonical.revision,
                        nextStructureRevision: canonical.structureRevision
                    )
                }
                return conflict(canonical, "note_id_reuse")
            }
            return accept(canonical, incrementStructure: false)
        }

        if mutation.baseRevision != canonical.revision {
            return conflict(canonical, "stale_revision")
        }

        switch mutation.operation {
        case .structureEdit:
            if mutation.entityKind != .project {
                return conflict(canonical, "structure_kind_not_enabled")
            }
            if mutation.baseStructureRevision != canonical.structureRevision {
                return conflict(canonical, "stale_structure_revision")
            }
            return accept(canonical, incrementStructure: true)
        case .scalarEdit, .delete:
            return accept(canonical, incrementStructure: false)
        case .create, .noteAppend, .resurrect:
            return conflict(canonical, "unsupported_operation")
        }
    }

    public static func evaluateDeletionBatch(
        entityKind: AgendaContractEntityKind,
        baselineCount: Int?,
        deletionCount: Int,
        zoneDeleted: Bool = false
    ) -> AgendaContractDecision {
        if deletionCount < 0 {
            return quarantine("invalid_count")
        }
        if let baselineCount, baselineCount < 0 {
            return quarantine("invalid_count")
        }
        if zoneDeleted {
            return quarantine("zone_deletion")
        }
        if deletionCount == 0 {
            return AgendaContractDecision(outcome: .accept, reason: "empty_batch")
        }
        guard let baselineCount else {
            return quarantine("baseline_required")
        }

        let absoluteCap = entityKind == .project ? 5 : 50
        let relativeCap = baselineCount / 4
        let effectiveCap = min(absoluteCap, relativeCap)
        if deletionCount > effectiveCap {
            return quarantine("collapse_guard")
        }
        return AgendaContractDecision(outcome: .accept, reason: "within_guard")
    }

    private static func accept(
        _ canonical: AgendaCanonicalContractState,
        incrementStructure: Bool
    ) -> AgendaContractDecision {
        AgendaContractDecision(
            outcome: .accept,
            reason: "accepted",
            nextRevision: canonical.revision + 1,
            nextStructureRevision: canonical.structureRevision
                + (incrementStructure ? 1 : 0)
        )
    }

    private static func conflict(
        _ canonical: AgendaCanonicalContractState,
        _ reason: String
    ) -> AgendaContractDecision {
        AgendaContractDecision(
            outcome: .conflict,
            reason: reason,
            nextRevision: canonical.revision,
            nextStructureRevision: canonical.structureRevision
        )
    }

    private static func quarantine(_ reason: String) -> AgendaContractDecision {
        AgendaContractDecision(outcome: .quarantine, reason: reason)
    }
}
