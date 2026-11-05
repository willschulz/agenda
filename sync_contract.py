"""Pure reference model for Agenda CloudKit wire-schema 2.

This module is deliberately not imported by the runtime.  It makes the Phase
6A conflict/deletion contract executable before later phases migrate storage or
widen the sync write surface.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


SUPPORTED_SCHEMA_VERSION = 2


class ContractOutcome(str, Enum):
    ACCEPT = "accept"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    QUARANTINE = "quarantine"


class MutationOperation(str, Enum):
    CREATE = "create"
    SCALAR_EDIT = "scalar_edit"
    NOTE_APPEND = "note_append"
    STRUCTURE_EDIT = "structure_edit"
    DELETE = "delete"
    RESURRECT = "resurrect"


class EntityKind(str, Enum):
    PROJECT = "project"
    TODO = "todo"


class PriorMutation(str, Enum):
    NONE = "none"
    SAME = "same"
    DIFFERENT = "different"


@dataclass(frozen=True)
class CanonicalState:
    schema_version: int
    revision: int
    structure_revision: int = 0
    deleted: bool = False
    notes: dict[str, str] = field(default_factory=dict)
    exists: bool = True


@dataclass(frozen=True)
class MutationProposal:
    schema_version: int
    mutation_id: str
    device_id: str
    entity_id: str
    operation: MutationOperation
    base_revision: int
    base_structure_revision: int | None = None
    note_id: str | None = None
    note_payload: str | None = None
    proposed_payload: str | None = None
    entity_kind: EntityKind | None = None


@dataclass(frozen=True)
class ContractDecision:
    outcome: ContractOutcome
    reason: str
    next_revision: int | None = None
    next_structure_revision: int | None = None


@dataclass(frozen=True)
class MutationResultRecord:
    schema_version: int
    mutation_id: str
    mutation_fingerprint: str
    device_id: str
    entity_id: str
    outcome: ContractOutcome
    reason: str
    canonical_revision: int | None = None
    canonical_structure_revision: int | None = None

    @property
    def record_name(self) -> str:
        return f"result-{self.mutation_id}"


def fingerprint_mutation(mutation: MutationProposal) -> str:
    """Hash semantic fields, excluding mutation ID and transport timestamps."""
    return canonical_semantic_fingerprint(
        {
            "schema_version": mutation.schema_version,
            "device_id": mutation.device_id,
            "entity_id": mutation.entity_id,
            "operation": mutation.operation,
            "base_revision": mutation.base_revision,
            "base_structure_revision": mutation.base_structure_revision,
            "note_id": mutation.note_id,
            "note_payload": mutation.note_payload,
            "proposed_payload": mutation.proposed_payload,
            "entity_kind": mutation.entity_kind,
        }
    )


def canonical_semantic_fingerprint(
    fields: Mapping[str, object | None],
) -> str:
    """Cross-language SHA-256 over a sorted UTF-8 length-prefixed field map."""
    digest = hashlib.sha256()
    for key in sorted(fields):
        raw_value = fields[key]
        if raw_value is None:
            continue
        value = _canonical_scalar(raw_value)
        key_bytes = key.encode("utf-8")
        value_bytes = value.encode("utf-8")
        digest.update(str(len(key_bytes)).encode("ascii"))
        digest.update(b":")
        digest.update(key_bytes)
        digest.update(str(len(value_bytes)).encode("ascii"))
        digest.update(b":")
        digest.update(value_bytes)
        digest.update(b";")
    return digest.hexdigest()


def evaluate_mutation(
    canonical: CanonicalState,
    mutation: MutationProposal,
    *,
    prior_mutation: PriorMutation = PriorMutation.NONE,
) -> ContractDecision:
    """Evaluate one proposal without mutating canonical state."""
    if canonical.schema_version > SUPPORTED_SCHEMA_VERSION:
        return _quarantine("newer_canonical_schema")
    if mutation.schema_version > SUPPORTED_SCHEMA_VERSION:
        return _quarantine("newer_mutation_schema")
    if mutation.schema_version < canonical.schema_version:
        return _quarantine("schema_downgrade")
    if canonical.schema_version < SUPPORTED_SCHEMA_VERSION:
        return _quarantine("canonical_upgrade_required")

    if prior_mutation is PriorMutation.SAME:
        return ContractDecision(
            ContractOutcome.IDEMPOTENT,
            "mutation_replay",
            canonical.revision,
            canonical.structure_revision,
        )
    if prior_mutation is PriorMutation.DIFFERENT:
        return _conflict(canonical, "mutation_id_reuse")

    if mutation.operation is MutationOperation.CREATE:
        if mutation.entity_kind is not EntityKind.TODO:
            return _conflict(canonical, "create_kind_not_enabled")
        if canonical.exists:
            return _conflict(canonical, "entity_exists")
        if mutation.base_revision != 0:
            return _conflict(canonical, "invalid_create_revision")
        return _accept(canonical, increment_structure=False)

    if not canonical.exists:
        return _conflict(canonical, "entity_missing")

    if mutation.base_revision > canonical.revision:
        return _conflict(canonical, "future_revision")

    if canonical.deleted:
        if (
            mutation.operation is MutationOperation.RESURRECT
            and mutation.base_revision == canonical.revision
        ):
            return _accept(canonical, increment_structure=False)
        return _conflict(canonical, "entity_deleted")

    if mutation.operation is MutationOperation.RESURRECT:
        return _conflict(canonical, "entity_not_deleted")

    if mutation.operation is MutationOperation.NOTE_APPEND:
        if not mutation.note_id or mutation.note_payload is None:
            return _conflict(canonical, "invalid_note_append")
        existing = canonical.notes.get(mutation.note_id)
        if existing is not None:
            if existing == mutation.note_payload:
                return ContractDecision(
                    ContractOutcome.IDEMPOTENT,
                    "note_replay",
                    canonical.revision,
                    canonical.structure_revision,
                )
            return _conflict(canonical, "note_id_reuse")
        return _accept(canonical, increment_structure=False)

    if mutation.base_revision != canonical.revision:
        return _conflict(canonical, "stale_revision")

    if mutation.operation is MutationOperation.STRUCTURE_EDIT:
        if mutation.entity_kind is not EntityKind.PROJECT:
            return _conflict(canonical, "structure_kind_not_enabled")
        if mutation.base_structure_revision != canonical.structure_revision:
            return _conflict(canonical, "stale_structure_revision")
        return _accept(canonical, increment_structure=True)

    if mutation.operation in (
        MutationOperation.SCALAR_EDIT,
        MutationOperation.DELETE,
    ):
        return _accept(canonical, increment_structure=False)

    return _conflict(canonical, "unsupported_operation")


def evaluate_deletion_batch(
    *,
    entity_kind: EntityKind,
    baseline_count: int | None,
    deletion_count: int,
    zone_deleted: bool = False,
) -> ContractDecision:
    """Apply the atomic inbound-deletion collapse guard."""
    if deletion_count < 0 or (baseline_count is not None and baseline_count < 0):
        return _quarantine("invalid_count")
    if zone_deleted:
        return _quarantine("zone_deletion")
    if deletion_count == 0:
        return ContractDecision(ContractOutcome.ACCEPT, "empty_batch")
    if baseline_count is None:
        return _quarantine("baseline_required")

    absolute_cap = 5 if entity_kind is EntityKind.PROJECT else 50
    relative_cap = baseline_count // 4
    effective_cap = min(absolute_cap, relative_cap)
    if deletion_count > effective_cap:
        return _quarantine("collapse_guard")
    return ContractDecision(ContractOutcome.ACCEPT, "within_guard")


def _accept(
    canonical: CanonicalState,
    *,
    increment_structure: bool,
) -> ContractDecision:
    return ContractDecision(
        ContractOutcome.ACCEPT,
        "accepted",
        canonical.revision + 1,
        canonical.structure_revision + (1 if increment_structure else 0),
    )


def _conflict(canonical: CanonicalState, reason: str) -> ContractDecision:
    return ContractDecision(
        ContractOutcome.CONFLICT,
        reason,
        canonical.revision,
        canonical.structure_revision,
    )


def _quarantine(reason: str) -> ContractDecision:
    return ContractDecision(ContractOutcome.QUARANTINE, reason)


def _canonical_scalar(value: object) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int)):
        return str(value)
    raise TypeError(
        "semantic fingerprint values must be strings, integers, booleans, "
        "enums, or None"
    )
