import json
import unittest
from pathlib import Path

from sync_contract import (
    CanonicalState,
    ContractDecision,
    ContractOutcome,
    EntityKind,
    MutationOperation,
    MutationProposal,
    MutationResultRecord,
    PriorMutation,
    canonical_semantic_fingerprint,
    evaluate_deletion_batch,
    evaluate_mutation,
    fingerprint_mutation,
)


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "apple"
    / "AgendaSyncCore"
    / "Tests"
    / "AgendaSyncCoreTests"
    / "Fixtures"
    / "sync_contract_v2_cases.json"
)


class SyncContractFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(FIXTURE_PATH.read_text())

    def test_mutation_decisions_match_shared_fixtures(self):
        for case in self.fixtures["mutation_cases"]:
            with self.subTest(case=case["name"]):
                canonical = CanonicalState(**case["canonical"])
                mutation_data = dict(case["mutation"])
                mutation_data["operation"] = MutationOperation(
                    mutation_data["operation"]
                )
                if mutation_data.get("entity_kind"):
                    mutation_data["entity_kind"] = EntityKind(
                        mutation_data["entity_kind"]
                    )
                mutation = MutationProposal(**mutation_data)

                decision = evaluate_mutation(
                    canonical,
                    mutation,
                    prior_mutation=PriorMutation(case["prior_mutation"]),
                )

                self.assertEqual(_decision_dict(decision), case["expected"])

    def test_deletion_guards_match_shared_fixtures(self):
        for case in self.fixtures["deletion_cases"]:
            with self.subTest(case=case["name"]):
                decision = evaluate_deletion_batch(
                    entity_kind=EntityKind(case["entity_kind"]),
                    baseline_count=case["baseline_count"],
                    deletion_count=case["deletion_count"],
                    zone_deleted=case["zone_deleted"],
                )

                self.assertEqual(_decision_dict(decision), case["expected"])

    def test_semantic_fingerprints_match_shared_fixtures(self):
        for case in self.fixtures["fingerprint_cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    canonical_semantic_fingerprint(case["fields"]),
                    case["expected"],
                )

        for case in self.fixtures["mutation_fingerprint_cases"]:
            with self.subTest(case=case["name"]):
                mutation_data = dict(case["mutation"])
                mutation_data["operation"] = MutationOperation(
                    mutation_data["operation"]
                )
                self.assertEqual(
                    fingerprint_mutation(MutationProposal(**mutation_data)),
                    case["expected"],
                )

    def test_result_records_match_shared_fixtures(self):
        for case in self.fixtures["result_cases"]:
            with self.subTest(case=case["name"]):
                result_data = dict(case["result"])
                result_data["outcome"] = ContractOutcome(
                    result_data["outcome"]
                )
                result = MutationResultRecord(**result_data)
                self.assertEqual(result.record_name, case["record_name"])


def _decision_dict(decision: ContractDecision) -> dict:
    result = {
        "outcome": decision.outcome.value,
        "reason": decision.reason,
    }
    if decision.next_revision is not None:
        result["next_revision"] = decision.next_revision
    if decision.next_structure_revision is not None:
        result["next_structure_revision"] = decision.next_structure_revision
    return result


if __name__ == "__main__":
    unittest.main()
