from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_board import roadmap


class RoadmapStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.messages = {"msg-context", "msg-decision", "msg-evidence"}
        self.store = roadmap.RoadmapStore(
            self.root,
            message_exists=self.messages.__contains__,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def item(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "actor": "sol-master",
            "item_id": "A.1",
            "title": "Semantic naming",
            "summary": "Make service roles understandable",
            "status": "IN_PROGRESS",
            "owner": "shared",
            "progress": 40,
            "change_summary": "Started reader-boundary work",
            "expected_revision": 0,
        }
        value.update(overrides)
        return value

    @staticmethod
    def legacy_pending() -> tuple[dict[str, object], bytes]:
        value: dict[str, object] = {
            "schema_version": 1,
            "items": [
                {
                    "id": "LEGACY",
                    "title": "Legacy item",
                    "summary": "Imported atomically",
                    "status": "PENDING",
                    "owner": "shared",
                    "progress": 0,
                    "blocker": "",
                    "updated_at": "2026-08-30T12:00:00Z",
                    "revision": 3,
                }
            ],
        }
        return value, json.dumps(value, indent=2).encode("utf-8")

    def test_path_and_first_revision(self) -> None:
        result = self.store.upsert_item(**self.item())
        self.assertEqual(self.store.path, self.root / "roadmap.v2.sqlite3")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["change_seq"], 1)
        self.assertEqual(self.store.get_item("A.1"), result)
        revision = self.store.get_revision("A.1", 1)
        self.assertEqual(revision["change_summary"], "Started reader-boundary work")
        self.assertEqual(revision["source_kind"], "NATIVE")

    def test_exact_typed_occ_conflict(self) -> None:
        self.store.upsert_item(**self.item())
        with self.assertRaises(roadmap.RoadmapConflict) as caught:
            self.store.upsert_item(
                **self.item(
                    title="Stale",
                    expected_revision=0,
                )
            )
        self.assertEqual(caught.exception.item_id, "A.1")
        self.assertEqual(caught.exception.expected_revision, 0)
        self.assertEqual(caught.exception.current_revision, 1)

    def test_twenty_same_item_writers_have_one_winner(self) -> None:
        self.store.upsert_item(**self.item())

        def attempt(index: int) -> str:
            try:
                self.store.upsert_item(
                    **self.item(
                        title=f"Writer {index}",
                        progress=41 + index,
                        expected_revision=1,
                    )
                )
                return "won"
            except roadmap.RoadmapConflict:
                return "lost"

        with ThreadPoolExecutor(max_workers=20) as pool:
            outcomes = list(pool.map(attempt, range(20)))
        self.assertEqual(outcomes.count("won"), 1)
        self.assertEqual(outcomes.count("lost"), 19)
        self.assertEqual(self.store.get_item("A.1")["revision"], 2)
        self.assertEqual(len(self.store.list_revisions("A.1")), 2)

    def test_twenty_distinct_items_succeed(self) -> None:
        def create(index: int) -> int:
            result = self.store.upsert_item(
                **self.item(
                    item_id=f"ITEM-{index}",
                    title=f"Item {index}",
                )
            )
            return result["change_seq"]

        with ThreadPoolExecutor(max_workers=20) as pool:
            sequences = list(pool.map(create, range(20)))
        self.assertEqual(len(self.store.list_items()), 20)
        self.assertEqual(sorted(sequences), list(range(1, 21)))

    def test_fault_rolls_back_change_revision_and_current(self) -> None:
        def fail(point: str) -> None:
            if point == "after_revision_insert":
                raise RuntimeError("injected crash")

        broken = roadmap.RoadmapStore(
            self.root,
            message_exists=self.messages.__contains__,
            fault_injector=fail,
        )
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            broken.upsert_item(**self.item())
        self.assertEqual(self.store.list_items(), [])
        self.assertEqual(self.store.list_changes(), [])
        connection = sqlite3.connect(self.store.path)
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM item_revisions").fetchone()[0], 0)
        finally:
            connection.close()

    def test_history_is_immutable_and_snapshots_do_not_drift(self) -> None:
        first = self.store.upsert_item(**self.item())
        second = self.store.upsert_item(
            **self.item(
                title="Semantic naming ready",
                summary="Readers now show human labels",
                progress=80,
                expected_revision=1,
                change_summary="Added registry readers",
            )
        )
        history = self.store.list_revisions("A.1")
        self.assertEqual(history[0]["title"], first["title"])
        self.assertEqual(history[1]["title"], second["title"])
        connection = sqlite3.connect(self.store.path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE item_revisions SET title='rewritten' WHERE item_id='A.1'"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"):
                connection.execute("DELETE FROM items WHERE item_id='A.1'")
        finally:
            connection.close()

    def test_no_op_rejected_but_new_link_is_a_change(self) -> None:
        first = self.store.upsert_item(**self.item())
        with self.assertRaises(roadmap.RoadmapNoOp):
            self.store.upsert_item(**self.item(expected_revision=1))
        linked = self.store.upsert_item(
            **self.item(
                expected_revision=1,
                message_links=[("msg-context", "CONTEXT")],
            )
        )
        self.assertEqual(linked, first)
        links = self.store.list_links("A.1", 1)
        self.assertEqual(links[0]["relation"], "CONTEXT")
        self.assertEqual([change["kind"] for change in self.store.list_changes()], [
            "ITEM_REVISION", "MESSAGE_LINK"
        ])
        with self.assertRaises(roadmap.RoadmapNoOp):
            self.store.upsert_item(
                **self.item(
                    expected_revision=1,
                    message_links=[("msg-context", "CONTEXT")],
                )
            )

    def test_links_validate_relation_and_message_existence(self) -> None:
        self.store.upsert_item(**self.item())
        result = self.store.link_message(
            actor="claude-master",
            item_id="A.1",
            revision=1,
            message_id="msg-decision",
            relation="decision",
        )
        self.assertEqual(result["relation"], "DECISION")
        with self.assertRaisesRegex(roadmap.RoadmapValidationError, "unknown board message"):
            self.store.link_message(
                actor="sol-master",
                item_id="A.1",
                revision=1,
                message_id="missing",
                relation="EVIDENCE",
            )
        with self.assertRaisesRegex(roadmap.RoadmapValidationError, "relation"):
            self.store.link_message(
                actor="sol-master",
                item_id="A.1",
                revision=1,
                message_id="msg-evidence",
                relation="RELATED",
            )

    def test_links_fail_closed_without_verifier_and_cap_at_twenty(self) -> None:
        unverified = roadmap.RoadmapStore(self.root / "unverified")
        unverified.upsert_item(**self.item())
        with self.assertRaisesRegex(roadmap.RoadmapValidationError, "existence validator"):
            unverified.link_message(
                actor="sol-master", item_id="A.1", revision=1,
                message_id="msg-context", relation="CONTEXT",
            )

        many_messages = {f"msg-{index}" for index in range(21)}
        capped = roadmap.RoadmapStore(
            self.root / "capped", message_exists=many_messages.__contains__
        )
        capped.upsert_item(**self.item())
        for index in range(20):
            capped.link_message(
                actor="sol-master", item_id="A.1", revision=1,
                message_id=f"msg-{index}", relation="CONTEXT",
            )
        with self.assertRaisesRegex(roadmap.RoadmapValidationError, "at most 20"):
            capped.link_message(
                actor="sol-master", item_id="A.1", revision=1,
                message_id="msg-20", relation="CONTEXT",
            )

    def test_complete_and_impact_cross_field_rules(self) -> None:
        invalid = (
            ({"status": "COMPLETE", "progress": 100}, "completion impact"),
            ({"status": "COMPLETE", "progress": 99, "completion_impact": "Done"}, "progress 100"),
            ({"completion_impact": "Too soon"}, "only COMPLETE"),
            ({"status": "BLOCKED", "blocker": ""}, "require a blocker"),
            ({"status": "PENDING", "blocker": "blocked"}, "only BLOCKED"),
        )
        for overrides, error in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(roadmap.RoadmapValidationError, error):
                    self.store.upsert_item(**self.item(**overrides))
        complete = self.store.upsert_item(
            **self.item(
                status="COMPLETE",
                progress=100,
                completion_impact="Operators understand component names without a glossary.",
            )
        )
        self.assertEqual(complete["status"], "COMPLETE")

    def test_required_change_summary_and_field_caps(self) -> None:
        with self.assertRaisesRegex(roadmap.RoadmapValidationError, "change summary"):
            self.store.upsert_item(**self.item(change_summary=""))
        capped_fields = (
            ("title", "x" * (roadmap.FIELD_CAPS["title"] + 1)),
            ("summary", "x" * (roadmap.FIELD_CAPS["summary"] + 1)),
            ("owner", "x" * (roadmap.FIELD_CAPS["owner"] + 1)),
            ("change_summary", "x" * (roadmap.FIELD_CAPS["change_summary"] + 1)),
        )
        for field, value in capped_fields:
            with self.subTest(field=field):
                with self.assertRaisesRegex(roadmap.RoadmapValidationError, "exceeds"):
                    self.store.upsert_item(**self.item(**{field: value}))
        with self.assertRaisesRegex(roadmap.RoadmapValidationError, "unauthorized"):
            self.store.upsert_item(**self.item(actor="invented-master"))
        with self.assertRaisesRegex(roadmap.RoadmapValidationError, "owner"):
            self.store.upsert_item(**self.item(owner="invented-owner"))

    def test_pagination_is_stable_and_bounded(self) -> None:
        for index in range(4):
            self.store.upsert_item(
                **self.item(item_id=f"ITEM-{index}", title=f"Item {index}")
            )
        first = self.store.list_items(limit=2)
        second = self.store.list_items(after_id=first[-1]["id"], limit=2)
        self.assertEqual([item["id"] for item in first + second], [
            "ITEM-0", "ITEM-1", "ITEM-2", "ITEM-3"
        ])
        self.assertEqual(
            self.store.list_revisions("ITEM-0", after_revision=1), []
        )
        with self.assertRaisesRegex(roadmap.RoadmapValidationError, "1 through 200"):
            self.store.list_items(limit=201)
        oversized = roadmap.SQLITE_MAX_INTEGER + 1
        for operation in (
            lambda: self.store.list_changes(after_seq=oversized),
            lambda: self.store.get_feed_state(after_seq=oversized),
            lambda: self.store.get_revision("ITEM-0", oversized),
        ):
            with self.assertRaisesRegex(roadmap.RoadmapValidationError, "0 through"):
                operation()

    def test_v1_legacy_revision_seven_imports_as_eight_once(self) -> None:
        legacy = {
            "schema_version": 1,
            "items": [
                {
                    "id": "LEGACY-1",
                    "title": "Old roadmap item",
                    "summary": "Preserve this state",
                    "status": "IN_PROGRESS",
                    "owner": "shared",
                    "progress": 60,
                    "blocker": "Waiting for review",
                    "updated_at": "2026-08-30T12:00:00Z",
                    "revision": 7,
                }
            ],
        }
        original = copy.deepcopy(legacy)
        source = json.dumps(legacy, indent=2).encode("utf-8")
        store = roadmap.RoadmapStore(
            self.root,
            _database_path=self.root / ".legacy-import.tmp",
        )
        imported = store.import_v1(legacy, actor="sol-master", source_bytes=source)
        self.assertEqual(legacy, original)
        self.assertEqual(imported[0]["revision"], 8)
        self.assertEqual(imported[0]["status"], "BLOCKED")
        history = store.list_revisions("LEGACY-1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["revision"], 8)
        self.assertEqual(history[0]["source_kind"], "V1_IMPORT")
        self.assertTrue(store.get_meta()["v1_source_sha256"].startswith("sha256:"))
        again = store.import_v1(legacy, actor="claude-master", source_bytes=source)
        self.assertEqual(again, imported)
        self.assertEqual(len(store.list_changes()), 1)

    def test_v1_complete_receives_explicit_sentinel_impact(self) -> None:
        legacy = {
            "schema_version": 1,
            "items": [
                {
                    "id": "DONE",
                    "title": "Completed old item",
                    "summary": "Legacy had no impact field",
                    "status": "COMPLETE",
                    "owner": "sol-master",
                    "progress": 100,
                    "blocker": "",
                    "updated_at": "2026-08-30T12:00:00Z",
                    "revision": 4,
                }
            ],
        }
        source = json.dumps(legacy).encode("utf-8")
        store = roadmap.RoadmapStore(
            self.root,
            _database_path=self.root / ".complete-import.tmp",
        )
        result = store.import_v1(
            legacy, actor="sol-master", source_bytes=source
        )
        self.assertEqual(result[0]["revision"], 4)
        self.assertEqual(
            result[0]["completion_impact"], roadmap.MIGRATED_COMPLETE_IMPACT
        )

    def test_failed_v1_import_is_fully_rolled_back(self) -> None:
        fail_root = self.root / "failed-import"

        def fail(point: str) -> None:
            if point == "after_v1_items":
                raise RuntimeError("power loss")

        store = roadmap.RoadmapStore(
            fail_root,
            fault_injector=fail,
            _database_path=fail_root / ".failed-import.tmp",
        )
        legacy = {
            "schema_version": 1,
            "items": [
                {
                    "id": "ONE", "title": "One", "summary": "First",
                    "status": "PENDING", "owner": "shared", "progress": 0,
                    "blocker": "", "updated_at": "2026-08-30T12:00:00Z", "revision": 1,
                }
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "power loss"):
            store.import_v1(
                legacy,
                actor="sol-master",
                source_bytes=json.dumps(legacy).encode("utf-8"),
            )
        self.assertEqual(store.list_items(), [])
        self.assertNotIn("v1_source_sha256", store.get_meta())

    def test_v1_source_bytes_must_match_parsed_store(self) -> None:
        parsed = {"schema_version": 1, "items": []}
        other = {"schema_version": 1, "items": [{"different": True}]}
        store = roadmap.RoadmapStore(
            self.root,
            _database_path=self.root / ".mismatch-import.tmp",
        )
        with self.assertRaisesRegex(roadmap.RoadmapValidationError, "does not match"):
            store.import_v1(
                parsed,
                actor="sol-master",
                source_bytes=json.dumps(other).encode("utf-8"),
            )

    def test_change_feed_is_global_and_monotonic(self) -> None:
        one = self.store.upsert_item(**self.item(item_id="ONE"))
        two = self.store.upsert_item(**self.item(item_id="TWO", title="Two"))
        link = self.store.link_message(
            actor="sol-master", item_id="ONE", revision=1,
            message_id="msg-evidence", relation="EVIDENCE",
        )
        self.assertEqual((one["change_seq"], two["change_seq"], link["change_seq"]), (1, 2, 3))
        self.assertEqual(
            [change["change_seq"] for change in self.store.list_changes(after_seq=1)],
            [2, 3],
        )
        hydrated = self.store.get_change(3)
        self.assertIsNone(hydrated["revision"])
        self.assertEqual(hydrated["link"]["message_id"], "msg-evidence")
        reverse = self.store.list_items_for_message("msg-evidence")
        self.assertEqual(reverse[0]["item_id"], "ONE")

    def test_feed_id_persists_and_detects_replacement(self) -> None:
        initial = self.store.get_feed_state()
        self.assertRegex(initial["feed_id"], r"^[0-9a-f]{32}$")
        self.assertFalse(initial["reset_required"])
        self.store.upsert_item(**self.item())
        current = self.store.get_feed_state(
            client_feed_id=initial["feed_id"], after_seq=1
        )
        self.assertEqual(current["latest_seq"], 1)
        self.assertFalse(current["reset_required"])
        self.assertTrue(
            self.store.get_feed_state(
                client_feed_id="replacement", after_seq=1
            )["reset_required"]
        )

    def test_atomic_v1_migration_never_publishes_empty_final(self) -> None:
        root = self.root / "atomic"
        root.mkdir()
        legacy, source = self.legacy_pending()
        v1_path = root / "roadmap.v1.json"
        v1_path.write_bytes(source)

        def fail(point: str) -> None:
            if point == "before_publish":
                raise RuntimeError("pre-publish crash")

        with self.assertRaisesRegex(RuntimeError, "pre-publish crash"):
            roadmap.RoadmapStore.migrate_v1_atomic(
                root,
                actor="sol-master",
                legacy_writes_quiesced=lambda: True,
                fault_injector=fail,
            )
        self.assertFalse((root / roadmap.DATABASE_NAME).exists())
        self.assertEqual(v1_path.read_bytes(), source)
        self.assertEqual(list(root.glob(f".{roadmap.DATABASE_NAME}.*.tmp")), [])

        published = roadmap.RoadmapStore.migrate_v1_atomic(
            root,
            actor="sol-master",
            legacy_writes_quiesced=lambda: True,
        )
        self.assertEqual(published.get_item("LEGACY")["revision"], 3)
        self.assertTrue(published.verify()["ok"])
        self.assertEqual(v1_path.read_bytes(), source)

    def test_atomic_v1_post_publish_crash_is_valid_and_retryable(self) -> None:
        root = self.root / "post-publish"
        legacy, source = self.legacy_pending()
        root.mkdir()
        (root / "roadmap.v1.json").write_bytes(source)

        def fail(point: str) -> None:
            if point == "after_publish":
                raise RuntimeError("post-publish crash")

        with self.assertRaisesRegex(RuntimeError, "post-publish crash"):
            roadmap.RoadmapStore.migrate_v1_atomic(
                root,
                actor="sol-master",
                legacy_writes_quiesced=lambda: True,
                fault_injector=fail,
            )
        final_path = root / roadmap.DATABASE_NAME
        self.assertTrue(final_path.exists())
        opened = roadmap.RoadmapStore(root)
        self.assertEqual(opened.get_item("LEGACY")["revision"], 3)
        self.assertTrue(opened.verify()["ok"])

        retried = roadmap.RoadmapStore.migrate_v1_atomic(
            root,
            actor="claude-master",
            legacy_writes_quiesced=lambda: True,
        )
        self.assertEqual(len(retried.list_revisions("LEGACY")), 1)

    def test_atomic_migration_requires_quiescence_and_rejects_empty_final(self) -> None:
        legacy, source = self.legacy_pending()
        guarded_root = self.root / "guarded"
        guarded_root.mkdir()
        (guarded_root / "roadmap.v1.json").write_bytes(source)
        with self.assertRaisesRegex(roadmap.RoadmapError, "not quiesced"):
            roadmap.RoadmapStore.migrate_v1_atomic(
                guarded_root,
                actor="sol-master",
                legacy_writes_quiesced=lambda: False,
            )
        self.assertFalse((guarded_root / roadmap.DATABASE_NAME).exists())

        incomplete_root = self.root / "incomplete"
        roadmap.RoadmapStore(incomplete_root)
        (incomplete_root / "roadmap.v1.json").write_bytes(source)
        with self.assertRaisesRegex(roadmap.RoadmapError, "not the completed import"):
            roadmap.RoadmapStore.migrate_v1_atomic(
                incomplete_root,
                actor="sol-master",
                legacy_writes_quiesced=lambda: True,
            )

    def test_atomic_migration_reads_locked_v1_and_rejects_stale_observation(self) -> None:
        root = self.root / "stale-source"
        root.mkdir()
        observed, source_a = self.legacy_pending()
        v1_path = root / "roadmap.v1.json"
        v1_path.write_bytes(source_a)
        observed_sha = "sha256:" + sha256(source_a).hexdigest()

        current = copy.deepcopy(observed)
        current["items"][0]["title"] = "Writer B committed under the v1 lock"
        source_b = json.dumps(current, indent=2).encode("utf-8")
        v1_path.write_bytes(source_b)

        with self.assertRaises(roadmap.RoadmapSourceConflict) as caught:
            roadmap.RoadmapStore.migrate_v1_atomic(
                root,
                actor="sol-master",
                legacy_writes_quiesced=lambda: True,
                expected_v1_source_sha256=observed_sha,
            )
        self.assertEqual(caught.exception.expected_sha256, observed_sha)
        self.assertFalse((root / roadmap.DATABASE_NAME).exists())

        migrated = roadmap.RoadmapStore.migrate_v1_atomic(
            root,
            actor="sol-master",
            legacy_writes_quiesced=lambda: True,
        )
        self.assertEqual(
            migrated.get_item("LEGACY")["title"],
            "Writer B committed under the v1 lock",
        )
        self.assertEqual(v1_path.read_bytes(), source_b)

    def test_public_constructor_and_import_cannot_bypass_atomic_cutover(self) -> None:
        root = self.root / "bypass"
        root.mkdir()
        legacy, source = self.legacy_pending()
        (root / "roadmap.v1.json").write_bytes(source)
        with self.assertRaisesRegex(roadmap.RoadmapError, "migrate_v1_atomic"):
            roadmap.RoadmapStore(root)
        self.assertFalse((root / roadmap.DATABASE_NAME).exists())

        with self.assertRaisesRegex(roadmap.RoadmapError, "restricted"):
            self.store.import_v1(
                legacy,
                actor="sol-master",
                source_bytes=source,
            )

    def test_verify_reports_integrity_and_compaction_is_explicitly_off(self) -> None:
        self.store.upsert_item(**self.item())
        result = self.store.verify()
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], 1)
        self.assertFalse(result["compaction_supported"])
        self.assertFalse(roadmap.COMPACTION_SUPPORTED)

    def test_newest_first_history_has_before_sequence_cursor(self) -> None:
        first = self.store.upsert_item(**self.item())
        second = self.store.upsert_item(
            **self.item(
                expected_revision=1,
                progress=60,
                change_summary="Advanced implementation",
            )
        )
        newest = self.store.list_history("A.1", limit=1)
        self.assertEqual(newest[0]["revision"], 2)
        older = self.store.list_history(
            "A.1", before_seq=second["change_seq"], limit=1
        )
        self.assertEqual(older[0]["change_seq"], first["change_seq"])


if __name__ == "__main__":
    unittest.main()
