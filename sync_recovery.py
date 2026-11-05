"""Durable schema-2 mutation evaluation for the Mac-canonical Agenda store."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

import sync_tombstones
import todo_structure
from sync_contract import (
    CanonicalState,
    ContractDecision,
    ContractOutcome,
    MutationOperation,
    MutationProposal,
    MutationResultRecord,
    EntityKind,
    PriorMutation,
    evaluate_mutation,
    fingerprint_mutation,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def proposal_from_dict(payload: dict[str, Any]) -> MutationProposal:
    return MutationProposal(
        schema_version=int(payload["schema_version"]),
        mutation_id=str(payload["mutation_id"]),
        device_id=str(payload["device_id"]),
        entity_id=str(payload["entity_id"]),
        operation=MutationOperation(payload["operation"]),
        base_revision=int(payload["base_revision"]),
        base_structure_revision=payload.get("base_structure_revision"),
        note_id=payload.get("note_id"),
        note_payload=payload.get("note_payload"),
        proposed_payload=payload.get("proposed_payload"),
        entity_kind=EntityKind(payload["entity_kind"])
        if payload.get("entity_kind") else None,
    )


def apply_mutation(
    conn: sqlite3.Connection,
    *,
    entity_kind: str,
    mutation: MutationProposal,
) -> dict[str, Any]:
    """Evaluate and durably record exactly one outcome in the caller transaction."""
    fingerprint = fingerprint_mutation(mutation)
    prior = conn.execute(
        "SELECT fingerprint FROM sync_mutations WHERE mutation_id = ?",
        (mutation.mutation_id,),
    ).fetchone()
    if prior is not None:
        if prior["fingerprint"] == fingerprint:
            result = conn.execute(
                "SELECT result_json FROM sync_results WHERE mutation_id = ?",
                (mutation.mutation_id,),
            ).fetchone()
            if result is None:
                raise sqlite3.IntegrityError("mutation exists without durable result")
            return json.loads(result["result_json"])
        canonical, canonical_payload = _canonical(
            conn, entity_kind, mutation.entity_id
        )
        decision = ContractDecision(
            ContractOutcome.CONFLICT,
            "mutation_id_reuse",
            canonical.revision if canonical else None,
            canonical.structure_revision if canonical else None,
        )
        _persist_conflict(
            conn,
            mutation,
            entity_kind,
            decision.reason,
            canonical_payload,
        )
        return _result(mutation, fingerprint, decision)

    canonical, canonical_payload = _canonical(
        conn, entity_kind, mutation.entity_id
    )
    decision = evaluate_mutation(
        canonical, mutation, prior_mutation=PriorMutation.NONE
    )

    if decision.outcome is ContractOutcome.ACCEPT:
        decision = _apply_accepted(
            conn, entity_kind, mutation, decision, canonical_payload
        )
    result = _result(mutation, fingerprint, decision)
    now = _now()
    conn.execute(
        """
        INSERT INTO sync_mutations (
            mutation_id, fingerprint, device_id, entity_kind, entity_uuid,
            operation, payload_json, received_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mutation.mutation_id,
            fingerprint,
            mutation.device_id,
            entity_kind,
            mutation.entity_id,
            mutation.operation.value,
            json.dumps(mutation.__dict__, default=_json_default, sort_keys=True),
            now,
        ),
    )
    if decision.outcome is ContractOutcome.CONFLICT:
        _persist_conflict(
            conn,
            mutation,
            entity_kind,
            decision.reason,
            canonical_payload,
        )
    conn.execute(
        """
        INSERT INTO sync_results (
            mutation_id, fingerprint, result_json, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (mutation.mutation_id, fingerprint, json.dumps(result, sort_keys=True), now),
    )
    return result


def list_results(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT result_json FROM sync_results ORDER BY created_at, mutation_id"
    ).fetchall()
    return [json.loads(row["result_json"]) for row in rows]


def list_conflicts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, mutation_id, entity_kind, entity_uuid, reason,
               canonical_payload_json, proposed_payload_json,
               created_at, resolved_at
        FROM sync_conflicts ORDER BY id DESC
        """
    ).fetchall()
    return [
        {
            **dict(row),
            "canonical_payload": json.loads(row["canonical_payload_json"])
            if row["canonical_payload_json"] else None,
            "proposed_payload": json.loads(row["proposed_payload_json"])
            if row["proposed_payload_json"] else None,
        }
        for row in rows
    ]


def _canonical(
    conn: sqlite3.Connection,
    entity_kind: str,
    stable_uuid: str,
) -> tuple[CanonicalState, dict[str, Any] | None]:
    table = "projects" if entity_kind == "project" else "todos"
    row = conn.execute(
        f"SELECT * FROM {table} WHERE stable_uuid = ?", (stable_uuid,)
    ).fetchone()
    if row is not None:
        payload = dict(row)
        return (
            CanonicalState(
                schema_version=2,
                revision=int(row["sync_revision"]),
                structure_revision=(
                    int(row["sync_structure_revision"])
                    if entity_kind == "project"
                    else 0
                ),
                deleted=False,
            ),
            payload,
        )
    tombstone = conn.execute(
        """
        SELECT revision, payload_json FROM sync_tombstones
        WHERE entity_kind = ? AND stable_uuid = ? AND restored_at IS NULL
        ORDER BY revision DESC LIMIT 1
        """,
        (entity_kind, stable_uuid),
    ).fetchone()
    if tombstone is None:
        return (
            CanonicalState(
                schema_version=2,
                revision=-1,
                exists=False,
            ),
            None,
        )
    return (
        CanonicalState(
            schema_version=2,
            revision=int(tombstone["revision"]),
            deleted=True,
        ),
        json.loads(tombstone["payload_json"]),
    )


def _apply_accepted(
    conn: sqlite3.Connection,
    entity_kind: str,
    mutation: MutationProposal,
    decision: ContractDecision,
    canonical_payload: dict[str, Any] | None,
) -> ContractDecision:
    table = "projects" if entity_kind == "project" else "todos"
    if mutation.operation is MutationOperation.CREATE:
        if entity_kind != "todo" or mutation.proposed_payload is None:
            return _conflict(decision, "runtime_operation_not_enabled")
        try:
            proposed = json.loads(mutation.proposed_payload)
        except (TypeError, json.JSONDecodeError):
            return _conflict(decision, "invalid_payload")
        if set(proposed) != {"project_stable_id", "text"}:
            return _conflict(decision, "invalid_payload")
        text = str(proposed["text"]).strip()
        project = conn.execute(
            "SELECT id FROM projects WHERE stable_uuid = ? AND sync_deleted_at IS NULL",
            (proposed["project_stable_id"],),
        ).fetchone()
        if not text or project is None:
            return _conflict(decision, "invalid_payload")
        order = conn.execute(
            "SELECT COALESCE(MIN(display_order), 10) - 10 FROM todos "
            "WHERE project_id = ? AND parent_id IS NULL AND for_later = 0",
            (project["id"],),
        ).fetchone()[0]
        now = _now()
        conn.execute(
            """
            INSERT INTO todos (
                project_id, parent_id, text, display_order, completed,
                created_at, updated_at, stable_uuid, sync_revision
            ) VALUES (?, NULL, ?, ?, 0, ?, ?, ?, 0)
            """,
            (project["id"], text, order, now, now, mutation.entity_id),
        )
        todo_structure.bump_project_structure_revisions(
            conn,
            [int(project["id"])],
            updated_at=now,
        )
        return decision
    if mutation.operation is MutationOperation.SCALAR_EDIT:
        if entity_kind != "todo" or mutation.proposed_payload is None:
            return _conflict(decision, "runtime_operation_not_enabled")
        try:
            proposed = json.loads(mutation.proposed_payload)
        except (TypeError, json.JSONDecodeError):
            return _conflict(decision, "invalid_payload")
        boolean_fields = {
            "completed",
            "in_progress",
            "flagged",
            "is_blocker",
            "awaiting_review",
            "for_later",
        }
        allowed = boolean_fields | {"text", "effort", "deadline"}
        if not proposed or not set(proposed).issubset(allowed):
            return _conflict(decision, "invalid_payload")
        if any(type(proposed[key]) is not bool for key in boolean_fields & proposed.keys()):
            return _conflict(decision, "invalid_payload")
        if "effort" in proposed and (
            proposed["effort"] is not None
            and (
                type(proposed["effort"]) is not int
                or proposed["effort"] not in {1, 2, 3, 5, 8, 13}
            )
        ):
            return _conflict(decision, "invalid_payload")
        if "deadline" in proposed and proposed["deadline"] is not None:
            deadline = proposed["deadline"]
            if type(deadline) is not str:
                return _conflict(decision, "invalid_payload")
            try:
                if date.fromisoformat(deadline).isoformat() != deadline:
                    return _conflict(decision, "invalid_payload")
            except ValueError:
                return _conflict(decision, "invalid_payload")

        fields: dict[str, Any] = {}
        if "text" in proposed:
            if type(proposed["text"]) is not str:
                return _conflict(decision, "invalid_payload")
            text = proposed["text"].strip()
            if not text:
                return _conflict(decision, "invalid_payload")
            fields["text"] = text
        for key in boolean_fields - {"completed"}:
            if key in proposed:
                fields[key] = int(proposed[key])
        if "effort" in proposed:
            fields["effort"] = proposed["effort"]
        if "deadline" in proposed:
            fields["deadline"] = proposed["deadline"]
        if "completed" in proposed:
            completed = proposed["completed"]
            current = conn.execute(
                "SELECT completed FROM todos WHERE stable_uuid = ?",
                (mutation.entity_id,),
            ).fetchone()
            fields["completed"] = int(completed)
            if completed and current is not None and not current["completed"]:
                fields["completed_at"] = _now()
            elif not completed:
                fields["completed_at"] = None
            if completed:
                fields["in_progress"] = 0
                fields["flagged"] = 0
                fields["running"] = 0
                fields["awaiting_review"] = 0
        fields["sync_revision"] = decision.next_revision
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [*fields.values(), mutation.entity_id]
        conn.execute(
            f"UPDATE todos SET {assignments} WHERE stable_uuid = ?",
            values,
        )
        return decision
    if mutation.operation is MutationOperation.STRUCTURE_EDIT:
        if entity_kind != "project" or mutation.proposed_payload is None:
            return _conflict(
                decision,
                "runtime_operation_not_enabled",
                decrement_structure=True,
            )
        try:
            proposed = json.loads(mutation.proposed_payload)
        except (TypeError, json.JSONDecodeError):
            return _conflict(
                decision,
                "invalid_payload",
                decrement_structure=True,
            )
        if not isinstance(proposed, dict):
            return _conflict(
                decision,
                "invalid_payload",
                decrement_structure=True,
            )
        try:
            todo_structure.apply_project_structure_edit(
                conn,
                project_stable_uuid=mutation.entity_id,
                proposed_payload=proposed,
                next_revision=int(decision.next_revision),
                next_structure_revision=int(decision.next_structure_revision),
                updated_at=_now(),
            )
        except todo_structure.StructureValidationError:
            return _conflict(
                decision,
                "invalid_payload",
                decrement_structure=True,
            )
        return decision
    if mutation.operation is MutationOperation.DELETE:
        if entity_kind != "todo" or canonical_payload is None:
            return _conflict(decision, "runtime_operation_not_enabled")
        row = conn.execute(
            "SELECT id, project_id FROM todos WHERE stable_uuid = ?",
            (mutation.entity_id,),
        ).fetchone()
        if row is None:
            return _conflict(decision, "delete_target_missing")
        deleted = sync_tombstones.delete_todo_tree(conn, int(row["id"]))
        if deleted < 1:
            return _conflict(decision, "delete_target_missing")
        todo_structure.bump_project_structure_revisions(
            conn,
            [int(row["project_id"])],
            updated_at=_now(),
        )
        return decision
    if mutation.operation is MutationOperation.RESURRECT:
        try:
            sync_tombstones.restore(conn, entity_kind, mutation.entity_id)
        except sync_tombstones.TombstoneConflict:
            return _conflict(decision, "resurrection_failed")
        return decision
    return _conflict(decision, "runtime_operation_not_enabled")


def _conflict(
    prior: ContractDecision,
    reason: str,
    *,
    decrement_structure: bool = False,
) -> ContractDecision:
    return ContractDecision(
        ContractOutcome.CONFLICT,
        reason,
        prior.next_revision - 1 if prior.next_revision is not None else None,
        (
            prior.next_structure_revision - 1
            if decrement_structure
            and prior.next_structure_revision is not None
            else prior.next_structure_revision
        ),
    )


def _persist_conflict(
    conn: sqlite3.Connection,
    mutation: MutationProposal,
    entity_kind: str,
    reason: str,
    canonical_payload: dict[str, Any] | None,
) -> None:
    proposed = None
    if mutation.proposed_payload:
        try:
            proposed = json.loads(mutation.proposed_payload)
        except json.JSONDecodeError:
            proposed = {"raw": mutation.proposed_payload}
    conn.execute(
        """
        INSERT INTO sync_conflicts (
            mutation_id, entity_kind, entity_uuid, reason,
            canonical_payload_json, proposed_payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mutation.mutation_id,
            entity_kind,
            mutation.entity_id,
            reason,
            json.dumps(canonical_payload, sort_keys=True)
            if canonical_payload is not None else None,
            json.dumps(proposed, sort_keys=True) if proposed is not None else None,
            _now(),
        ),
    )


def _result(
    mutation: MutationProposal,
    fingerprint: str,
    decision: ContractDecision,
) -> dict[str, Any]:
    record = MutationResultRecord(
        schema_version=2,
        mutation_id=mutation.mutation_id,
        mutation_fingerprint=fingerprint,
        device_id=mutation.device_id,
        entity_id=mutation.entity_id,
        outcome=decision.outcome,
        reason=decision.reason,
        canonical_revision=decision.next_revision,
        canonical_structure_revision=decision.next_structure_revision,
    )
    return {
        "schema_version": record.schema_version,
        "mutation_id": record.mutation_id,
        "mutation_fingerprint": record.mutation_fingerprint,
        "device_id": record.device_id,
        "entity_id": record.entity_id,
        "outcome": record.outcome.value,
        "reason": record.reason,
        "canonical_revision": record.canonical_revision,
        "canonical_structure_revision": record.canonical_structure_revision,
    }


def _json_default(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)
