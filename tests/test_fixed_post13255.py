import copy
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixed", ROOT / "scripts" / "correct_post13255.py")
fixed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixed)
FOLDER = ROOT / "corrections" / "post13255"
REQUEST = json.loads((FOLDER / "request.json").read_text())
LEDGER = (FOLDER / "fact-ledger.json").read_bytes()
PUBLIC = json.loads((FOLDER / "before-public.json").read_text())
IMPLEMENTATION = hashlib.sha256((ROOT / "scripts" / "correct_post13255.py").read_bytes()).hexdigest()


def baseline():
    post = copy.deepcopy(PUBLIC)
    for field in ("title", "content", "excerpt"):
        post[field]["raw"] = post[field]["rendered"]
    return post


class FakeClient:
    def __init__(self):
        self.current = baseline()
        self.gets = 0
        self.updates = []
        self.behavior = "ok"
        self.drift_on_second_read = False

    def get_post(self):
        self.gets += 1
        if self.drift_on_second_read and self.gets == 2:
            self.current["content"]["raw"] += " unpublished-markup-drift"
        return copy.deepcopy(self.current)

    def update_post(self, payload):
        self.updates.append(copy.deepcopy(payload))
        if self.behavior == "denied":
            raise fixed.HTTPFailureError(403)
        if self.behavior == "timeout_before":
            raise TimeoutError()
        for field, value in payload.items():
            self.current[field] = {"raw": value, "rendered": value}
        self.current["modified_gmt"] = "2026-10-27T15:30:00"
        if self.behavior == "timeout_after":
            raise TimeoutError()
        if self.behavior == "timeout_drift":
            self.current["content"]["raw"] = "A different concurrent edit."
            raise TimeoutError()
        if self.behavior == "metadata_drift":
            self.current["featured_media"] += 1
        return {"id": self.current["id"]}


def request(stage="dry-run"):
    value = copy.deepcopy(REQUEST)
    value["stage"] = stage
    value["dry_run_receipt_sha256"] = None
    return value


def run_dry():
    receipts = []
    runner = fixed.Runner(FakeClient(), request(), LEDGER, receipts.append,
                          {"run_id": "100", "commit": "offline-test"}, implementation_sha=IMPLEMENTATION)
    return runner.run()


def apply_runner(client=None, receipt=None):
    previous = receipt or run_dry()
    raw = json.dumps(previous, sort_keys=True).encode()
    value = request("apply")
    value["dry_run_receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    receipts = []
    runner = fixed.Runner(client or FakeClient(), value, LEDGER, receipts.append,
                          {"run_id": "101", "commit": "offline-apply"},
                          dry_receipt_bytes=raw, implementation_sha=IMPLEMENTATION)
    return runner, receipts


class FixedCorrectionTests(unittest.TestCase):
    def test_dry_run_authenticates_reads_and_never_updates(self):
        client = FakeClient()
        receipts = []
        runner = fixed.Runner(client, request(), LEDGER, receipts.append,
                              {"run_id": "100", "commit": "test"}, implementation_sha=IMPLEMENTATION)
        result = runner.run()
        self.assertEqual(result["state"], "dry_run_verified")
        self.assertEqual(client.gets, 1)
        self.assertEqual(client.updates, [])
        self.assertEqual(result["writes_attempted"], 0)

    def test_preflight_never_updates(self):
        client = FakeClient()
        runner = fixed.Runner(client, request("preflight"), LEDGER, lambda r: None,
                              {"run_id": "100", "commit": "test"}, implementation_sha=IMPLEMENTATION)
        self.assertEqual(runner.run()["state"], "preflight_verified")
        self.assertEqual(client.updates, [])

    def test_wrong_target_intake_rejected(self):
        value = request()
        value["target_id"] += 1
        with self.assertRaises(fixed.GuardError):
            fixed.validate_intake(value, LEDGER)

    def test_extra_request_field_rejected(self):
        value = request()
        value["endpoint"] = "https://other.example/posts"
        with self.assertRaises(fixed.GuardError):
            fixed.validate_intake(value, LEDGER)

    def test_unapproved_payload_and_metadata_rejected(self):
        for field in ["status", "slug", "date", "author", "featured_media", "meta"]:
            with self.subTest(field=field):
                value = request()
                value["payload"][field] = "forbidden"
                with self.assertRaises(fixed.GuardError):
                    fixed.validate_intake(value, LEDGER)
        value = request()
        value["payload"]["content"] += "unreviewed"
        with self.assertRaises(fixed.GuardError):
            fixed.validate_intake(value, LEDGER)

    def test_changed_ledger_or_before_digest_rejected(self):
        with self.assertRaises(fixed.GuardError):
            fixed.validate_intake(request(), LEDGER + b"drift")
        value = request()
        value["expected_content_sha256"] = "0" * 64
        with self.assertRaises(fixed.GuardError):
            fixed.validate_intake(value, LEDGER)

    def test_identity_and_status_drift_fail_without_update(self):
        changes = {"id": -1, "status": "draft", "slug": "different", "link": "https://other.example/",
                   "date": "2000-01-01T00:00:00", "date_gmt": "2000-01-01T00:00:00",
                   "author": -1, "featured_media": -1, "categories": [999999], "tags": [999999]}
        for field, value in changes.items():
            with self.subTest(field=field):
                client = FakeClient()
                client.current[field] = value
                runner, _ = apply_runner(client)
                with self.assertRaises(fixed.GuardError):
                    runner.run()
                self.assertEqual(client.updates, [])

    def test_title_content_excerpt_and_modified_drift_rejected(self):
        for field in ("title", "content", "excerpt"):
            with self.subTest(field=field):
                client = FakeClient()
                client.current[field]["rendered"] += " changed"
                runner, _ = apply_runner(client)
                with self.assertRaises(fixed.GuardError):
                    runner.run()
                self.assertEqual(client.updates, [])
        client = FakeClient()
        client.current["modified_gmt"] = "2026-10-27T15:00:01"
        runner, _ = apply_runner(client)
        with self.assertRaises(fixed.GuardError):
            runner.run()
        self.assertEqual(client.updates, [])

    def test_raw_drift_between_preflight_and_update_rejected(self):
        client = FakeClient()
        client.drift_on_second_read = True
        runner, _ = apply_runner(client)
        with self.assertRaises(fixed.GuardError):
            runner.run()
        self.assertEqual(client.updates, [])

    def test_apply_requires_successful_matching_dry_run(self):
        for field, value in [("state", "failed"), ("repository", "other/repo"),
                             ("post_id", -1), ("host", "other.example"),
                             ("payload_sha256", "0" * 64),
                             ("implementation_sha256", "different")]:
            with self.subTest(field=field):
                receipt = run_dry()
                receipt[field] = value
                with self.assertRaises(fixed.GuardError):
                    apply_runner(receipt=receipt)

    def test_apply_removes_exactly_five_notes_and_preserves_everything_else(self):
        client = FakeClient()
        original = fixed.identity(client.current)
        runner, receipts = apply_runner(client)
        result = runner.run()
        self.assertEqual(result["state"], "applied_verified")
        self.assertEqual(len(client.updates), 1)
        self.assertEqual(set(client.updates[0]), set(fixed.FIELDS))
        self.assertEqual(fixed.identity(client.current), original)
        self.assertTrue(any(x["state"] == "write_started" for x in receipts))
        self.assertEqual(client.updates[0], REQUEST["payload"])

    def test_denial_stops_without_retry_or_followup_lookup(self):
        client = FakeClient()
        client.behavior = "denied"
        runner, receipts = apply_runner(client)
        with self.assertRaises(fixed.HTTPFailureError):
            runner.run()
        self.assertEqual(len(client.updates), 1)
        self.assertEqual(client.gets, 2)
        self.assertEqual(receipts[-1]["state"], "denied_no_retry")

    def test_timeout_after_success_reconciles_without_second_update(self):
        client = FakeClient()
        client.behavior = "timeout_after"
        runner, _ = apply_runner(client)
        self.assertEqual(runner.run()["state"], "applied_reconciled_verified")
        self.assertEqual(len(client.updates), 1)

    def test_timeout_before_write_proves_no_apply_without_retry(self):
        client = FakeClient()
        client.behavior = "timeout_before"
        runner, receipts = apply_runner(client)
        with self.assertRaises(fixed.GuardError):
            runner.run()
        self.assertEqual(len(client.updates), 1)
        self.assertEqual(receipts[-1]["state"], "not_applied_verified")

    def test_unknown_same_post_outcome_stops_without_retry(self):
        client = FakeClient()
        client.behavior = "timeout_drift"
        runner, receipts = apply_runner(client)
        with self.assertRaises(fixed.GuardError):
            runner.run()
        self.assertEqual(len(client.updates), 1)
        self.assertEqual(receipts[-1]["state"], "uncertain_stopped_for_review")

    def test_after_metadata_drift_is_not_success(self):
        client = FakeClient()
        client.behavior = "metadata_drift"
        runner, _ = apply_runner(client)
        with self.assertRaises(fixed.GuardError):
            runner.run()
        self.assertEqual(len(client.updates), 1)

    def test_exact_after_state_is_idempotent_without_duplicate_note(self):
        client = FakeClient()
        for field, value in REQUEST["payload"].items():
            client.current[field] = {"raw": value, "rendered": value}
        client.current["modified_gmt"] = "2026-10-27T15:00:00"
        runner, _ = apply_runner(client)
        self.assertEqual(runner.run()["state"], "already_applied_verified")
        self.assertEqual(client.updates, [])
        self.assertNotIn("confirm live price at publish", client.current["content"]["raw"])

    def test_equivalent_entity_encoding_is_recognized(self):
        post = baseline()
        for field, value in REQUEST["payload"].items():
            post[field]["raw"] = value.replace("&#x27;", "&#039;")
            post[field]["rendered"] = value
        self.assertTrue(fixed.desired(post, REQUEST["payload"]))

    def test_unrelated_hidden_content_is_not_accepted(self):
        post = baseline()
        for field, value in REQUEST["payload"].items():
            post[field]["raw"] = value
            post[field]["rendered"] = value
        post["content"]["raw"] += "<!-- unapproved content -->"
        post["content"]["rendered"] += "<!-- unapproved content -->"
        self.assertFalse(fixed.desired(post, REQUEST["payload"]))

    def test_client_requires_managed_actions_before_credentials(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(fixed.GuardError):
                fixed.ManagedClient()

    def test_client_rejects_wrong_repository_branch_and_host(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": fixed.APPROVED["repository"],
               "GITHUB_REF": "refs/heads/" + fixed.APPROVED["branch"],
               "WORDPRESS_BASE_URL": "https://" + fixed.APPROVED["host"]}
        for field, value in [("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_REF", "refs/heads/main"),
                             ("WORDPRESS_BASE_URL", "https://other.example"),
                             ("WORDPRESS_BASE_URL", "http://" + fixed.APPROVED["host"])]:
            with self.subTest(field=field):
                with patch.dict("os.environ", dict(env, **{field: value}), clear=True):
                    with self.assertRaises(fixed.GuardError):
                        fixed.ManagedClient()

    def test_redirects_are_not_followed(self):
        self.assertIsNone(fixed.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))


    def test_raw_html_format_is_preserved_except_fixed_deletions(self):
        post = baseline()
        post["content"]["raw"] = "\n" + post["content"]["raw"] + "\n"
        result = fixed.derive_payload(post)["content"]
        self.assertEqual(result, post["content"]["raw"].replace("; confirm live price at publish", ""))
        self.assertEqual(post["content"]["raw"].count("; confirm live price at publish"), 5)

    def test_occurrence_count_drift_is_rejected(self):
        post = baseline()
        post["content"]["raw"] = post["content"]["raw"].replace("; confirm live price at publish", "", 1)
        with self.assertRaises(fixed.GuardError):
            fixed.derive_payload(post)

    def test_managed_update_cannot_send_other_content_or_fields(self):
        client = object.__new__(fixed.ManagedClient)
        post = baseline()
        client.get_post = lambda: copy.deepcopy(post)
        client.approved_before_raw = fixed.raw_hashes(post)
        calls = []
        client.call = lambda method, payload: calls.append((method, payload)) or {"id": post["id"]}
        payload = fixed.derive_payload(post)
        with self.assertRaises(fixed.GuardError):
            client.update_post({"content": payload["content"] + " unapproved"})
        with self.assertRaises(fixed.GuardError):
            client.update_post(dict(payload, title="unapproved"))
        self.assertEqual(calls, [])
        client.update_post(payload)
        self.assertEqual(calls, [("POST", payload)])


if __name__ == "__main__":
    unittest.main()
