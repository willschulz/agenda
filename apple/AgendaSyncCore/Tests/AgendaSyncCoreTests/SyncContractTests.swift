import Foundation
import XCTest
@testable import AgendaSyncCore

final class SyncContractTests: XCTestCase {
    func testMutationDecisionsMatchSharedFixtures() throws {
        let fixtures = try loadFixtures()

        for fixture in fixtures.mutationCases {
            let actual = AgendaSyncContract.evaluate(
                canonical: fixture.canonical,
                mutation: fixture.mutation,
                priorMutation: fixture.priorMutation
            )
            XCTAssertEqual(actual, fixture.expected, fixture.name)
        }
    }

    func testDeletionGuardsMatchSharedFixtures() throws {
        let fixtures = try loadFixtures()

        for fixture in fixtures.deletionCases {
            let actual = AgendaSyncContract.evaluateDeletionBatch(
                entityKind: fixture.entityKind,
                baselineCount: fixture.baselineCount,
                deletionCount: fixture.deletionCount,
                zoneDeleted: fixture.zoneDeleted
            )
            XCTAssertEqual(actual, fixture.expected, fixture.name)
        }
    }

    func testSemanticFingerprintsMatchSharedFixtures() throws {
        let fixtures = try loadFixtures()

        for fixture in fixtures.fingerprintCases {
            XCTAssertEqual(
                AgendaSyncContract.canonicalSemanticFingerprint(
                    fields: fixture.fields
                ),
                fixture.expected,
                fixture.name
            )
        }

        for fixture in fixtures.mutationFingerprintCases {
            XCTAssertEqual(
                AgendaSyncContract.fingerprint(mutation: fixture.mutation),
                fixture.expected,
                fixture.name
            )
        }
    }

    func testResultRecordsMatchSharedFixtures() throws {
        let fixtures = try loadFixtures()

        for fixture in fixtures.resultCases {
            XCTAssertEqual(
                fixture.result.recordName,
                fixture.recordName,
                fixture.name
            )
        }
    }

    private func loadFixtures() throws -> ContractFixtures {
        let url = try XCTUnwrap(
            Bundle.module.url(
                forResource: "sync_contract_v2_cases",
                withExtension: "json"
            )
        )
        return try JSONDecoder().decode(
            ContractFixtures.self,
            from: Data(contentsOf: url)
        )
    }
}

private struct ContractFixtures: Decodable {
    let mutationCases: [MutationFixture]
    let deletionCases: [DeletionFixture]
    let fingerprintCases: [FingerprintFixture]
    let mutationFingerprintCases: [MutationFingerprintFixture]
    let resultCases: [ResultFixture]

    enum CodingKeys: String, CodingKey {
        case mutationCases = "mutation_cases"
        case deletionCases = "deletion_cases"
        case fingerprintCases = "fingerprint_cases"
        case mutationFingerprintCases = "mutation_fingerprint_cases"
        case resultCases = "result_cases"
    }
}

private struct FingerprintFixture: Decodable {
    let name: String
    let fields: [String: String?]
    let expected: String
}

private struct MutationFingerprintFixture: Decodable {
    let name: String
    let mutation: AgendaMutationProposal
    let expected: String
}

private struct ResultFixture: Decodable {
    let name: String
    let result: AgendaMutationResultRecord
    let recordName: String

    enum CodingKeys: String, CodingKey {
        case name, result
        case recordName = "record_name"
    }
}

private struct MutationFixture: Decodable {
    let name: String
    let canonical: AgendaCanonicalContractState
    let mutation: AgendaMutationProposal
    let priorMutation: AgendaPriorMutation
    let expected: AgendaContractDecision

    enum CodingKeys: String, CodingKey {
        case name, canonical, mutation, expected
        case priorMutation = "prior_mutation"
    }
}

private struct DeletionFixture: Decodable {
    let name: String
    let entityKind: AgendaContractEntityKind
    let baselineCount: Int?
    let deletionCount: Int
    let zoneDeleted: Bool
    let expected: AgendaContractDecision

    enum CodingKeys: String, CodingKey {
        case name, expected
        case entityKind = "entity_kind"
        case baselineCount = "baseline_count"
        case deletionCount = "deletion_count"
        case zoneDeleted = "zone_deleted"
    }
}
