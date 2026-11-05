import json
import sqlite3
import unittest

import db
import sync_recovery
from sync_contract import EntityKind, MutationOperation, MutationProposal


class SyncRecoveryScratchTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA_SQL)
        db._migrate_add_sync_identity(self.conn)
        db._migrate_add_sync_tombstones(self.conn)
        db._migrate_add_sync_structure_revision(self.conn)
        db._migrate_add_sync_recovery(self.conn)
        db._migrate_add_project_northstars(self.conn)
        db._migrate_add_todo_flagged(self.conn)
        db._migrate_add_todo_process_statuses(self.conn)
        db._migrate_add_todo_deadline(self.conn)
        db._migrate_add_todo_effort(self.conn)
        db._migrate_add_sync_revision_triggers(self.conn)
        now = "2026-08-09T20:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO projects (
                name, category, created_at, updated_at, stable_uuid
            ) VALUES ('Recovery', 'side_project', ?, ?, 'project-1')
            """,
            (now, now),
        )
        self.conn.execute(
            """
            INSERT INTO todos (
                project_id, text, created_at, updated_at, stable_uuid
            ) VALUES (1, 'Before', ?, ?, 'todo-1')
            """,
            (now, now),
        )

    def tearDown(self):
        self.conn.close()

    def proposal(
        self,
        mutation_id: str,
        *,
        base_revision: int = 0,
        text: str = "After",
        payload=None,
        operation: MutationOperation = MutationOperation.SCALAR_EDIT,
    ) -> MutationProposal:
        return MutationProposal(
            schema_version=2,
            mutation_id=mutation_id,
            device_id="device-a",
            entity_id="todo-1",
            operation=operation,
            base_revision=base_revision,
            proposed_payload=json.dumps(payload or {"text": text}, sort_keys=True)
            if operation is MutationOperation.SCALAR_EDIT else None,
        )

    def apply(self, proposal: MutationProposal):
        self.conn.execute("BEGIN")
        try:
            result = sync_recovery.apply_mutation(
                self.conn,
                entity_kind=(
                    proposal.entity_kind.value
                    if proposal.entity_kind is not None
                    else "todo"
                ),
                mutation=proposal,
            )
            self.conn.execute("COMMIT")
            return result
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def seed_structure_todos(self):
        now = "2026-08-09T20:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO todos (
                project_id, text, display_order, created_at, updated_at,
                stable_uuid
            ) VALUES (1, 'Second root', 10, ?, ?, 'todo-2')
            """,
            (now, now),
        )
        self.conn.execute(
            """
            INSERT INTO todos (
                project_id, parent_id, text, display_order, created_at,
                updated_at, stable_uuid
            ) VALUES (1, 1, 'Child', 0, ?, ?, 'todo-3')
            """,
            (now, now),
        )

    def structure_proposal(
        self,
        mutation_id: str,
        updates: list[dict],
        *,
        base_revision: int = 0,
        base_structure_revision: int = 0,
    ) -> MutationProposal:
        return MutationProposal(
            schema_version=2,
            mutation_id=mutation_id,
            device_id="device-a",
            entity_id="project-1",
            entity_kind=EntityKind.PROJECT,
            operation=MutationOperation.STRUCTURE_EDIT,
            base_revision=base_revision,
            base_structure_revision=base_structure_revision,
            proposed_payload=json.dumps({"updates": updates}, sort_keys=True),
        )

    def test_accept_persists_result_before_idempotent_replay(self):
        proposal = self.proposal("mutation-1")
        first = self.apply(proposal)
        second = self.apply(proposal)
        row = self.conn.execute(
            "SELECT text, sync_revision FROM todos WHERE stable_uuid='todo-1'"
        ).fetchone()
        self.assertEqual(first, second)
        self.assertEqual(first["outcome"], "accept")
        self.assertEqual(row["text"], "After")
        self.assertEqual(row["sync_revision"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sync_results").fetchone()[0], 1
        )

    def test_ordinary_mac_edit_advances_canonical_revision(self):
        self.conn.execute(
            "UPDATE todos SET text='Mac edit', updated_at='later' "
            "WHERE stable_uuid='todo-1'"
        )
        row = self.conn.execute(
            "SELECT sync_revision FROM todos WHERE stable_uuid='todo-1'"
        ).fetchone()
        self.assertEqual(row["sync_revision"], 1)

    def test_full_status_edit_applies_atomically(self):
        result = self.apply(
            self.proposal(
                "mutation-statuses",
                payload={
                    "in_progress": True,
                    "flagged": True,
                    "is_blocker": True,
                    "awaiting_review": True,
                    "for_later": True,
                    "effort": 8,
                    "deadline": "2026-08-31",
                },
            )
        )
        row = self.conn.execute(
            """
            SELECT in_progress, flagged, is_blocker, awaiting_review,
                   for_later, effort, deadline, sync_revision
            FROM todos WHERE stable_uuid='todo-1'
            """
        ).fetchone()
        self.assertEqual(result["outcome"], "accept")
        self.assertEqual(tuple(row), (1, 1, 1, 1, 1, 8, "2026-08-31", 1))

    def test_effort_and_deadline_can_be_cleared_explicitly(self):
        self.conn.execute(
            "UPDATE todos SET effort=5, deadline='2026-08-20' "
            "WHERE stable_uuid='todo-1'"
        )
        revision = self.conn.execute(
            "SELECT sync_revision FROM todos WHERE stable_uuid='todo-1'"
        ).fetchone()["sync_revision"]
        result = self.apply(
            self.proposal(
                "mutation-clear-metadata",
                base_revision=revision,
                payload={"effort": None, "deadline": None},
            )
        )
        row = self.conn.execute(
            "SELECT effort, deadline FROM todos WHERE stable_uuid='todo-1'"
        ).fetchone()
        self.assertEqual(result["outcome"], "accept")
        self.assertEqual(tuple(row), (None, None))

    def test_completion_matches_api_side_effects(self):
        self.conn.execute(
            """
            UPDATE todos
            SET in_progress=1, flagged=1, running=1, awaiting_review=1,
                is_blocker=1, for_later=1
            WHERE stable_uuid='todo-1'
            """
        )
        revision = self.conn.execute(
            "SELECT sync_revision FROM todos WHERE stable_uuid='todo-1'"
        ).fetchone()["sync_revision"]
        result = self.apply(
            self.proposal(
                "mutation-complete",
                base_revision=revision,
                payload={
                    "completed": True,
                    "in_progress": True,
                    "flagged": True,
                    "awaiting_review": True,
                },
            )
        )
        row = self.conn.execute(
            """
            SELECT completed, completed_at, in_progress, flagged, running,
                   awaiting_review, is_blocker, for_later
            FROM todos WHERE stable_uuid='todo-1'
            """
        ).fetchone()
        self.assertEqual(result["outcome"], "accept")
        self.assertEqual(row["completed"], 1)
        self.assertIsNotNone(row["completed_at"])
        self.assertEqual(tuple(row)[2:], (0, 0, 0, 0, 1, 1))

    def test_invalid_status_payload_is_rejected_without_partial_update(self):
        invalid_payloads = [
            {"flagged": 1},
            {"in_progress": "true"},
            {"effort": 4},
            {"effort": True},
            {"deadline": "08/31/2026"},
            {"deadline": "2026-02-30"},
            {"text": 42},
            {"running": True},
        ]
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(payload=payload):
                result = self.apply(
                    self.proposal(f"mutation-invalid-{index}", payload=payload)
                )
                self.assertEqual(result["outcome"], "conflict")
                self.assertEqual(result["reason"], "invalid_payload")
        row = self.conn.execute(
            """
            SELECT text, flagged, in_progress, effort, deadline, sync_revision
            FROM todos WHERE stable_uuid='todo-1'
            """
        ).fetchone()
        self.assertEqual(tuple(row), ("Before", 0, 0, None, None, 0))

    def test_stale_status_edit_preserves_canonical_status(self):
        self.apply(
            self.proposal("mutation-flag", payload={"flagged": True})
        )
        result = self.apply(
            self.proposal(
                "mutation-stale-flag",
                base_revision=0,
                payload={"flagged": False, "effort": 13},
            )
        )
        row = self.conn.execute(
            "SELECT flagged, effort FROM todos WHERE stable_uuid='todo-1'"
        ).fetchone()
        self.assertEqual(result["outcome"], "conflict")
        self.assertEqual(result["reason"], "stale_revision")
        self.assertEqual(tuple(row), (1, None))

    def test_project_structure_edit_applies_complete_sibling_group(self):
        self.seed_structure_todos()
        proposal = self.structure_proposal(
            "mutation-structure",
            [
                {
                    "stable_id": "todo-2",
                    "parent_stable_id": None,
                    "display_order": 0,
                    "for_later": False,
                },
                {
                    "stable_id": "todo-1",
                    "parent_stable_id": None,
                    "display_order": 10,
                    "for_later": False,
                },
            ],
        )
        first = self.apply(proposal)
        replay = self.apply(proposal)
        project = self.conn.execute(
            """
            SELECT sync_revision, sync_structure_revision
            FROM projects WHERE stable_uuid='project-1'
            """
        ).fetchone()
        roots = self.conn.execute(
            """
            SELECT stable_uuid, display_order FROM todos
            WHERE project_id=1 AND parent_id IS NULL
            ORDER BY display_order
            """
        ).fetchall()
        self.assertEqual(first, replay)
        self.assertEqual(first["outcome"], "accept")
        self.assertEqual(first["canonical_revision"], 1)
        self.assertEqual(first["canonical_structure_revision"], 1)
        self.assertEqual(tuple(project), (1, 1))
        self.assertEqual(
            [(row["stable_uuid"], row["display_order"]) for row in roots],
            [("todo-2", 0), ("todo-1", 10)],
        )

    def test_project_structure_edit_can_reparent_within_project(self):
        self.seed_structure_todos()
        result = self.apply(
            self.structure_proposal(
                "mutation-reparent",
                [
                    {
                        "stable_id": "todo-1",
                        "parent_stable_id": None,
                        "display_order": 0,
                        "for_later": False,
                    },
                    {
                        "stable_id": "todo-3",
                        "parent_stable_id": "todo-1",
                        "display_order": 0,
                        "for_later": False,
                    },
                    {
                        "stable_id": "todo-2",
                        "parent_stable_id": "todo-1",
                        "display_order": 10,
                        "for_later": False,
                    },
                ],
            )
        )
        moved = self.conn.execute(
            """
            SELECT parent.stable_uuid AS parent_uuid, child.display_order
            FROM todos child JOIN todos parent ON parent.id=child.parent_id
            WHERE child.stable_uuid='todo-2'
            """
        ).fetchone()
        self.assertEqual(result["outcome"], "accept")
        self.assertEqual(tuple(moved), ("todo-1", 10))

    def test_structure_edit_cascades_root_later_state_to_descendants(self):
        self.seed_structure_todos()
        result = self.apply(
            self.structure_proposal(
                "mutation-later-root",
                [
                    {
                        "stable_id": "todo-1",
                        "parent_stable_id": None,
                        "display_order": 0,
                        "for_later": True,
                    },
                    {
                        "stable_id": "todo-2",
                        "parent_stable_id": None,
                        "display_order": 0,
                        "for_later": False,
                    },
                ],
            )
        )
        child_later = self.conn.execute(
            "SELECT for_later FROM todos WHERE stable_uuid='todo-3'"
        ).fetchone()["for_later"]
        self.assertEqual(result["outcome"], "accept")
        self.assertEqual(child_later, 1)

    def test_structure_edit_ignores_completed_and_other_root_section(self):
        self.seed_structure_todos()
        now = "2026-08-09T20:00:00+00:00"
        self.conn.executemany(
            """
            INSERT INTO todos (
                project_id, text, display_order, completed, for_later,
                created_at, updated_at, stable_uuid
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("Completed root", 0, 1, 0, now, now, "todo-completed"),
                ("Later root", 0, 0, 1, now, now, "todo-later"),
            ],
        )
        result = self.apply(
            self.structure_proposal(
                "mutation-visible-roots-only",
                [
                    {
                        "stable_id": "todo-2",
                        "parent_stable_id": None,
                        "display_order": 0,
                        "for_later": False,
                    },
                    {
                        "stable_id": "todo-1",
                        "parent_stable_id": None,
                        "display_order": 10,
                        "for_later": False,
                    },
                ],
            )
        )
        rows = self.conn.execute(
            """
            SELECT stable_uuid, display_order, completed, for_later
            FROM todos
            WHERE stable_uuid IN (
                'todo-1', 'todo-2', 'todo-completed', 'todo-later'
            )
            ORDER BY stable_uuid
            """
        ).fetchall()
        by_id = {row["stable_uuid"]: tuple(row)[1:] for row in rows}
        self.assertEqual(result["outcome"], "accept")
        self.assertEqual(by_id["todo-1"], (10, 0, 0))
        self.assertEqual(by_id["todo-2"], (0, 0, 0))
        self.assertEqual(by_id["todo-completed"], (0, 1, 0))
        self.assertEqual(by_id["todo-later"], (0, 0, 1))

    def test_structure_edit_preserves_completed_ancestor_for_open_children(self):
        now = "2026-08-09T20:00:00+00:00"
        parent_id = self.conn.execute(
            """
            INSERT INTO todos (
                project_id, text, display_order, completed,
                created_at, updated_at, stable_uuid
            ) VALUES (1, 'Completed parent', 0, 1, ?, ?, 'done-parent')
            """,
            (now, now),
        ).lastrowid
        self.conn.executemany(
            """
            INSERT INTO todos (
                project_id, parent_id, text, display_order,
                created_at, updated_at, stable_uuid
            ) VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            [
                (parent_id, "Child A", 0, now, now, "child-a"),
                (parent_id, "Child B", 10, now, now, "child-b"),
            ],
        )

        result = self.apply(
            self.structure_proposal(
                "mutation-completed-ancestor",
                [
                    {
                        "stable_id": "child-b",
                        "parent_stable_id": "done-parent",
                        "display_order": 0,
                        "for_later": False,
                    },
                    {
                        "stable_id": "child-a",
                        "parent_stable_id": "done-parent",
                        "display_order": 10,
                        "for_later": False,
                    },
                ],
            )
        )

        self.assertEqual(result["outcome"], "accept")
        rows = self.conn.execute(
            """
            SELECT stable_uuid, display_order FROM todos
            WHERE stable_uuid IN ('child-a', 'child-b')
            ORDER BY display_order
            """
        ).fetchall()
        self.assertEqual(
            [(row["stable_uuid"], row["display_order"]) for row in rows],
            [("child-b", 0), ("child-a", 10)],
        )

    def test_stale_structure_revision_preserves_hierarchy(self):
        self.seed_structure_todos()
        self.conn.execute(
            """
            UPDATE projects
            SET sync_revision=1, sync_structure_revision=1
            WHERE stable_uuid='project-1'
            """
        )
        result = self.apply(
            self.structure_proposal(
                "mutation-stale-structure",
                [
                    {
                        "stable_id": "todo-2",
                        "parent_stable_id": None,
                        "display_order": 0,
                        "for_later": False,
                    },
                    {
                        "stable_id": "todo-1",
                        "parent_stable_id": None,
                        "display_order": 10,
                        "for_later": False,
                    },
                ],
                base_revision=1,
                base_structure_revision=0,
            )
        )
        order = self.conn.execute(
            "SELECT display_order FROM todos WHERE stable_uuid='todo-1'"
        ).fetchone()["display_order"]
        self.assertEqual(result["outcome"], "conflict")
        self.assertEqual(result["reason"], "stale_structure_revision")
        self.assertEqual(order, 0)

    def test_invalid_structure_payloads_do_not_partially_apply(self):
        self.seed_structure_todos()
        now = "2026-08-09T20:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO projects (
                name, category, created_at, updated_at, stable_uuid
            ) VALUES ('Other', 'side_project', ?, ?, 'project-2')
            """,
            (now, now),
        )
        self.conn.execute(
            """
            INSERT INTO todos (
                project_id, text, created_at, updated_at, stable_uuid
            ) VALUES (2, 'Outside', ?, ?, 'outside-project')
            """,
            (now, now),
        )
        invalid_updates = [
            [
                {
                    "stable_id": "todo-1",
                    "parent_stable_id": "todo-3",
                    "display_order": 0,
                    "for_later": False,
                },
                {
                    "stable_id": "todo-3",
                    "parent_stable_id": "todo-1",
                    "display_order": 0,
                    "for_later": False,
                },
                {
                    "stable_id": "todo-2",
                    "parent_stable_id": None,
                    "display_order": 0,
                    "for_later": False,
                },
            ],
            [
                {
                    "stable_id": "todo-1",
                    "parent_stable_id": None,
                    "display_order": 0,
                    "for_later": False,
                }
            ],
            [
                {
                    "stable_id": "todo-2",
                    "parent_stable_id": "outside-project",
                    "display_order": 0,
                    "for_later": False,
                },
                {
                    "stable_id": "todo-1",
                    "parent_stable_id": None,
                    "display_order": 10,
                    "for_later": False,
                },
            ],
        ]
        for index, updates in enumerate(invalid_updates):
            with self.subTest(index=index):
                result = self.apply(
                    self.structure_proposal(
                        f"mutation-invalid-structure-{index}",
                        updates,
                    )
                )
                self.assertEqual(result["outcome"], "conflict")
                self.assertEqual(result["reason"], "invalid_payload")
        project = self.conn.execute(
            """
            SELECT sync_revision, sync_structure_revision
            FROM projects WHERE stable_uuid='project-1'
            """
        ).fetchone()
        self.assertEqual(tuple(project), (0, 0))

    def test_structure_transaction_rollback_restores_rows_and_counters(self):
        self.seed_structure_todos()
        self.conn.execute("BEGIN")
        sync_recovery.apply_mutation(
            self.conn,
            entity_kind="project",
            mutation=self.structure_proposal(
                "mutation-structure-rollback",
                [
                    {
                        "stable_id": "todo-2",
                        "parent_stable_id": None,
                        "display_order": 0,
                        "for_later": False,
                    },
                    {
                        "stable_id": "todo-1",
                        "parent_stable_id": None,
                        "display_order": 10,
                        "for_later": False,
                    },
                ],
            ),
        )
        self.conn.execute("ROLLBACK")
        project = self.conn.execute(
            """
            SELECT sync_revision, sync_structure_revision
            FROM projects WHERE stable_uuid='project-1'
            """
        ).fetchone()
        order = self.conn.execute(
            "SELECT display_order FROM todos WHERE stable_uuid='todo-1'"
        ).fetchone()["display_order"]
        self.assertEqual(tuple(project), (0, 0))
        self.assertEqual(order, 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sync_mutations").fetchone()[0],
            0,
        )

    def test_project_tombstone_defeats_structure_edit(self):
        self.conn.execute("BEGIN")
        import sync_tombstones
        sync_tombstones.delete_project(self.conn, 1)
        self.conn.execute("COMMIT")
        result = self.apply(
            self.structure_proposal(
                "mutation-structure-deleted-project",
                [
                    {
                        "stable_id": "todo-1",
                        "parent_stable_id": None,
                        "display_order": 0,
                        "for_later": False,
                    }
                ],
                base_revision=1,
            )
        )
        self.assertEqual(result["outcome"], "conflict")
        self.assertEqual(result["reason"], "entity_deleted")

    def test_launch_revision_rekey_is_bounded_and_idempotent(self):
        db._migrate_rekey_schema2_launch_revisions(self.conn)
        db._migrate_rekey_schema2_launch_revisions(self.conn)
        project_revision = self.conn.execute(
            "SELECT sync_revision FROM projects WHERE stable_uuid='project-1'"
        ).fetchone()["sync_revision"]
        todo_revision = self.conn.execute(
            "SELECT sync_revision FROM todos WHERE stable_uuid='todo-1'"
        ).fetchone()["sync_revision"]
        self.assertEqual(project_revision, 1)
        self.assertEqual(todo_revision, 1)

        self.conn.execute(
            """
            INSERT INTO todos (
                project_id, text, created_at, updated_at, stable_uuid
            ) VALUES (1, 'Future', ?, ?, 'todo-future')
            """,
            (
                "2026-08-13T19:40:00+00:00",
                "2026-08-13T19:40:00+00:00",
            ),
        )
        db._migrate_rekey_schema2_launch_revisions(self.conn)
        future_revision = self.conn.execute(
            "SELECT sync_revision FROM todos WHERE stable_uuid='todo-future'"
        ).fetchone()["sync_revision"]
        self.assertEqual(future_revision, 0)

    def test_create_top_level_todo_is_idempotent(self):
        proposal = MutationProposal(
            schema_version=2,
            mutation_id="mutation-create",
            device_id="device-a",
            entity_id="todo-created",
            entity_kind=EntityKind.TODO,
            operation=MutationOperation.CREATE,
            base_revision=0,
            proposed_payload=json.dumps({
                "project_stable_id": "project-1",
                "text": "Created on iPhone",
            }, sort_keys=True),
        )
        first = self.apply(proposal)
        second = self.apply(proposal)
        self.assertEqual(first, second)
        self.assertEqual(first["canonical_revision"], 0)
        row = self.conn.execute(
            "SELECT text, parent_id, sync_revision FROM todos "
            "WHERE stable_uuid='todo-created'"
        ).fetchone()
        self.assertEqual(tuple(row), ("Created on iPhone", None, 0))

    def test_create_rejects_missing_project_without_inserting(self):
        proposal = MutationProposal(
            schema_version=2,
            mutation_id="mutation-bad-create",
            device_id="device-a",
            entity_id="todo-bad",
            entity_kind=EntityKind.TODO,
            operation=MutationOperation.CREATE,
            base_revision=0,
            proposed_payload=json.dumps({
                "project_stable_id": "missing",
                "text": "No project",
            }, sort_keys=True),
        )
        result = self.apply(proposal)
        self.assertEqual(result["outcome"], "conflict")
        self.assertEqual(result["reason"], "invalid_payload")

    def test_stale_edit_preserves_canonical_and_records_actionable_conflict(self):
        self.apply(self.proposal("mutation-1"))
        result = self.apply(
            self.proposal("mutation-2", base_revision=0, text="Stale")
        )
        self.assertEqual(result["outcome"], "conflict")
        self.assertEqual(result["reason"], "stale_revision")
        self.assertEqual(
            self.conn.execute(
                "SELECT text FROM todos WHERE stable_uuid='todo-1'"
            ).fetchone()["text"],
            "After",
        )
        conflict = sync_recovery.list_conflicts(self.conn)[0]
        self.assertEqual(conflict["proposed_payload"]["text"], "Stale")
        self.assertEqual(conflict["canonical_payload"]["text"], "After")

    def test_mutation_id_reuse_does_not_replace_immutable_result(self):
        original = self.apply(self.proposal("mutation-1"))
        reused = self.apply(self.proposal("mutation-1", text="Different"))
        self.assertEqual(reused["reason"], "mutation_id_reuse")
        stored = sync_recovery.list_results(self.conn)
        self.assertEqual(stored, [original])

    def test_transaction_rollback_leaves_no_partial_acknowledgement(self):
        self.conn.execute("BEGIN")
        sync_recovery.apply_mutation(
            self.conn,
            entity_kind="todo",
            mutation=self.proposal("mutation-crash"),
        )
        self.conn.execute("ROLLBACK")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sync_mutations").fetchone()[0],
            0,
        )
        row = self.conn.execute(
            "SELECT text, sync_revision FROM todos WHERE stable_uuid='todo-1'"
        ).fetchone()
        self.assertEqual((row["text"], row["sync_revision"]), ("Before", 0))

    def test_delete_mutation_tombstones_subtree_and_bumps_project_once(self):
        self.seed_structure_todos()
        deleted = self.apply(
            self.proposal(
                "mutation-delete",
                operation=MutationOperation.DELETE,
            )
        )
        replay = self.apply(
            self.proposal(
                "mutation-delete",
                operation=MutationOperation.DELETE,
            )
        )
        self.assertEqual(deleted, replay)
        self.assertEqual(deleted["outcome"], "accept")
        self.assertEqual(deleted["canonical_revision"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM todos WHERE stable_uuid IN ('todo-1','todo-3')"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sync_tombstones "
                "WHERE stable_uuid IN ('todo-1','todo-3') AND restored_at IS NULL"
            ).fetchone()[0],
            2,
        )
        project = self.conn.execute(
            "SELECT sync_revision, sync_structure_revision FROM projects WHERE id=1"
        ).fetchone()
        self.assertEqual(tuple(project), (1, 1))

    def test_stale_delete_preserves_live_subtree(self):
        self.seed_structure_todos()
        self.conn.execute("UPDATE todos SET text='Mac newer' WHERE id=1")
        deleted = self.apply(
            self.proposal(
                "mutation-stale-delete",
                operation=MutationOperation.DELETE,
            )
        )
        self.assertEqual(deleted["outcome"], "conflict")
        self.assertEqual(deleted["reason"], "stale_revision")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM todos WHERE stable_uuid IN ('todo-1','todo-3')"
            ).fetchone()[0],
            2,
        )

    def test_project_delete_mutation_remains_disabled(self):
        proposal = MutationProposal(
            schema_version=2,
            mutation_id="mutation-project-delete",
            device_id="device-a",
            entity_id="project-1",
            entity_kind=EntityKind.PROJECT,
            operation=MutationOperation.DELETE,
            base_revision=0,
        )
        deleted = self.apply(proposal)
        self.assertEqual(deleted["outcome"], "conflict")
        self.assertEqual(deleted["reason"], "runtime_operation_not_enabled")
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT id FROM projects WHERE stable_uuid='project-1'"
            ).fetchone()
        )

    def test_delete_transaction_rollback_restores_subtree_and_counters(self):
        self.seed_structure_todos()
        self.conn.execute("BEGIN")
        sync_recovery.apply_mutation(
            self.conn,
            entity_kind="todo",
            mutation=self.proposal(
                "mutation-delete-rollback",
                operation=MutationOperation.DELETE,
            ),
        )
        self.conn.execute("ROLLBACK")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0],
            3,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sync_tombstones").fetchone()[0],
            0,
        )
        project = self.conn.execute(
            "SELECT sync_revision, sync_structure_revision FROM projects WHERE id=1"
        ).fetchone()
        self.assertEqual(tuple(project), (0, 0))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sync_results").fetchone()[0],
            0,
        )

    def test_explicit_resurrection_advances_revision(self):
        self.conn.execute("BEGIN")
        import sync_tombstones
        sync_tombstones.delete_todo_tree(self.conn, 1)
        self.conn.execute("COMMIT")
        resurrected = self.apply(
            self.proposal(
                "mutation-resurrect",
                base_revision=1,
                operation=MutationOperation.RESURRECT,
            )
        )
        self.assertEqual(resurrected["outcome"], "accept")
        self.assertEqual(resurrected["canonical_revision"], 2)
        self.assertEqual(
            self.conn.execute(
                "SELECT sync_revision FROM todos WHERE stable_uuid='todo-1'"
            ).fetchone()["sync_revision"],
            2,
        )


if __name__ == "__main__":
    unittest.main()
