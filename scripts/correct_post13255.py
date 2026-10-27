"""One fixed reviewed post correction. Managed Actions only; no arbitrary writer."""
import base64
import copy
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APPROVED = {'repository': 'wallyrebel/alcorn', 'host': 'alcornnewsms.com', 'branch': 'codex/clean-post13255-20261027', 'before': {'id': 13255, 'status': 'publish', 'slug': 'the-5-best-ground-blinds-for-alcorn-county-deer-season', 'link': 'https://alcornnewsms.com/alcorn-county-news/the-5-best-ground-blinds-for-alcorn-county-deer-season/', 'date': '2026-10-27T06:14:38', 'date_gmt': '2026-10-27T11:14:38', 'modified_gmt': '2026-10-27T11:14:38', 'author': 1, 'featured_media': 13254, 'categories': [3], 'tags': [303, 1149, 1206, 1578, 1579, 1154], 'rendered_sha256': {'title': 'e5986474dd4a7b77ca3bfdc75fa4f0cb8984b0d5ac777e0a567aeb8b1ffced86', 'content': 'a69723f97b42396256a0474b289ecfeeda89bef4de68744282ed75c65cdfd41e', 'excerpt': 'ea502514b574cf215c7afbde13f4b740217938efb6ec3e42773024e88c02cda8'}}, 'payload_sha256': '11228ec94ad36b47f6eb96a758a9410f22961b3de6eba9a0518bfc5e151474b5', 'ledger_sha256': '3b6f1a174bf81f67b5c552f183850940021161efcd3268ab1461d53b912b6d2a', 'operation': {'text': '; confirm live price at publish', 'replacement': '', 'count': 5}, 'after_rendered_sha256': '6ba4df35946d41643fbcee93ff0fe0562ba72080dee10b29a3f3961c948e5a8e'}
ROOT = Path(__file__).resolve().parents[1]
REQUEST = ROOT / "corrections" / ("post" + str(APPROVED["before"]["id"])) / "request.json"
LEDGER = REQUEST.parent / "fact-ledger.json"
DRY_RECEIPT = REQUEST.parent / "approved-dry-run.json"
OUTPUT = ROOT / "data" / ("correction-post" + str(APPROVED["before"]["id"])) / "result.json"
PRESERVE = ("id", "status", "slug", "link", "date", "date_gmt", "author",
            "featured_media", "categories", "tags", "comment_status",
            "ping_status", "sticky", "template", "format")
FIELDS = ("content",)


class GuardError(Exception):
    pass


class HTTPFailureError(Exception):
    def __init__(self, status):
        super().__init__("HTTP request failed")
        self.status = status


def require(condition, message):
    if not condition:
        raise GuardError(message)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_digest(payload):
    return digest(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def raw_hashes(post):
    return {key: digest(post[key]["raw"]) for key in ("title", "content", "excerpt")}


def rendered_hashes(post):
    return {key: digest(post[key]["rendered"]) for key in ("title", "content", "excerpt")}


def identity(post):
    return {key: copy.deepcopy(post.get(key)) for key in PRESERVE}


def desired(post, payload):
    try:
        return (digest(post["content"]["rendered"]) == APPROVED["after_rendered_sha256"]
                and all(digest(post[k]["rendered"]) == APPROVED["before"]["rendered_sha256"][k]
                        for k in ("title", "excerpt")))
    except (KeyError, TypeError):
        return False


def derive_payload(post):
    operation = APPROVED["operation"]
    original = post["content"]["raw"]
    require(original.count(operation["text"]) == operation["count"],
            "Raw removal occurrence count drift")
    rendered = post["content"]["rendered"]
    require(rendered.count(operation["text"]) == operation["count"],
            "Rendered removal occurrence count drift")
    require(digest(rendered.replace(operation["text"], operation["replacement"])) ==
            APPROVED["after_rendered_sha256"], "Unreviewed rendered transformation")
    return {"content": original.replace(operation["text"], operation["replacement"])}


def validate_intake(request, ledger_bytes):
    require(set(request) == {"version", "stage", "target_id", "expected_content_sha256",
                             "payload_sha256", "payload", "dry_run_receipt_sha256"},
            "Unexpected intake fields")
    require(request["version"] == 1, "Unexpected intake version")
    require(request["stage"] in ("preflight", "dry-run", "apply"), "Unexpected stage")
    require(request["target_id"] == APPROVED["before"]["id"], "Unexpected post ID")
    require(request["expected_content_sha256"] ==
            APPROVED["before"]["rendered_sha256"]["content"], "Unexpected before digest")
    require(set(request["payload"]) == set(FIELDS), "Only title/content/excerpt are allowed")
    require(all(isinstance(v, str) and v for v in request["payload"].values()),
            "Empty or invalid candidate field")
    require(payload_digest(request["payload"]) == APPROVED["payload_sha256"] ==
            request["payload_sha256"], "Unreviewed candidate payload")
    require(hashlib.sha256(ledger_bytes).hexdigest() == APPROVED["ledger_sha256"],
            "Fact ledger changed")
    if request["stage"] != "apply":
        require(request["dry_run_receipt_sha256"] is None, "Unexpected apply approval")


def validate_identity(post):
    require(isinstance(post, dict), "Post lookup returned invalid data")
    for key, expected in APPROVED["before"].items():
        if key in ("rendered_sha256", "modified_gmt"):
            continue
        require(post.get(key) == expected, "Post identity/publication field drift: " + key)
    require(not post.get("password"), "Protected post outside this correction scope")
    for key in ("title", "content", "excerpt"):
        require(isinstance(post.get(key), dict) and
                isinstance(post[key].get("raw"), str) and
                isinstance(post[key].get("rendered"), str),
                "Edit-context field unavailable: " + key)


def validate_before(post):
    validate_identity(post)
    require(post.get("modified_gmt") == APPROVED["before"]["modified_gmt"],
            "Post modification time drift")
    require(rendered_hashes(post) == APPROVED["before"]["rendered_sha256"],
            "Current rendered content/title/excerpt digest drift")


def validate_dry_receipt(raw, request, implementation_sha):
    require(isinstance(raw, bytes) and
            hashlib.sha256(raw).hexdigest() == request["dry_run_receipt_sha256"],
            "Missing or changed reviewed dry-run receipt")
    receipt = json.loads(raw)
    require(receipt.get("state") == "dry_run_verified", "Dry-run did not verify")
    require(receipt.get("repository") == APPROVED["repository"] and
            receipt.get("post_id") == APPROVED["before"]["id"] and
            receipt.get("host") == APPROVED["host"], "Dry-run target mismatch")
    require(receipt.get("payload_sha256") == APPROVED["payload_sha256"] and
            receipt.get("ledger_sha256") == APPROVED["ledger_sha256"] and
            receipt.get("implementation_sha256") == implementation_sha,
            "Dry-run implementation or payload drift")
    require(receipt.get("before_rendered_sha256") == APPROVED["before"]["rendered_sha256"],
            "Dry-run before snapshot mismatch")
    require(str(receipt.get("run_id", "")).isdigit() and receipt.get("commit"),
            "Dry-run Action provenance absent")
    return receipt


class Runner:
    def __init__(self, client, request, ledger_bytes, save, context,
                 dry_receipt_bytes=None, implementation_sha=""):
        validate_intake(request, ledger_bytes)
        self.client = client
        self.request = request
        self.payload = request["payload"]
        self.save_callback = save
        self.receipt = {
            "state": "started", "repository": APPROVED["repository"],
            "host": APPROVED["host"], "post_id": APPROVED["before"]["id"],
            "stage": request["stage"], "payload_sha256": APPROVED["payload_sha256"],
            "ledger_sha256": APPROVED["ledger_sha256"],
            "implementation_sha256": implementation_sha, "run_id": context["run_id"],
            "commit": context["commit"], "writes_attempted": 0,
        }
        self.dry = None
        if request["stage"] == "apply":
            self.dry = validate_dry_receipt(dry_receipt_bytes, request, implementation_sha)

    def save(self, state, **fields):
        self.receipt.update(state=state, **fields)
        self.save_callback(copy.deepcopy(self.receipt))

    def after(self, post, baseline):
        validate_identity(post)
        require(identity(post) == baseline, "Unrelated fields changed after update")
        require(desired(post, self.payload), "Exact approved rendered change not confirmed")
        require(post["content"]["raw"] == self.payload["content"], "Unrelated raw content changed")
        require(all(raw_hashes(post)[k] == self.receipt["before_raw_sha256"][k]
                    for k in ("title", "excerpt")), "Untouched raw title or excerpt changed")
        self.receipt.update(after_raw_sha256=raw_hashes(post),
                            after_rendered_sha256=rendered_hashes(post),
                            after_modified_gmt=post.get("modified_gmt"),
                            preserved_identity=baseline)

    def run(self):
        self.save("prechecking")
        before = self.client.get_post()
        validate_identity(before)
        if desired(before, self.payload):
            self.save("already_applied_verified", after_raw_sha256=raw_hashes(before),
                      preserved_identity=identity(before))
            return self.receipt
        validate_before(before)
        self.payload = derive_payload(before)
        self.receipt["raw_payload_sha256"] = payload_digest(self.payload)
        baseline = identity(before)
        self.receipt.update(before_raw_sha256=raw_hashes(before),
                            before_rendered_sha256=rendered_hashes(before),
                            before_modified_gmt=before["modified_gmt"],
                            before_identity=baseline)
        self.save("preflight_verified")
        if self.request["stage"] == "preflight":
            return self.receipt
        if self.request["stage"] == "dry-run":
            self.save("dry_run_verified")
            return self.receipt
        require(self.dry["raw_payload_sha256"] == payload_digest(self.payload),
                "Candidate raw payload drift since dry-run")
        require(self.dry["before_raw_sha256"] == raw_hashes(before) and
                self.dry["before_identity"] == baseline, "Raw snapshot drift since dry-run")
        # A final authenticated same-identity read occurs immediately before the only update.
        fresh = self.client.get_post()
        validate_before(fresh)
        require(identity(fresh) == baseline and raw_hashes(fresh) == raw_hashes(before),
                "Fresh snapshot drift before write")
        self.client.approved_before_raw = raw_hashes(fresh)
        self.save("write_started", writes_attempted=1)
        try:
            response = self.client.update_post(self.payload)
            require(response.get("id") == APPROVED["before"]["id"],
                    "Update response identity mismatch")
        except HTTPFailureError as error:
            if error.status in (401, 403):
                self.save("denied_no_retry", http_status=error.status)
                raise
            return self.reconcile(baseline, before)
        except Exception:
            return self.reconcile(baseline, before)
        after = self.client.get_post()
        self.after(after, baseline)
        self.save("applied_verified")
        return self.receipt

    def reconcile(self, baseline, before):
        self.save("reconciling_same_post", writes_attempted=1)
        after = self.client.get_post()
        validate_identity(after)
        if desired(after, self.payload) and after["content"]["raw"] == self.payload["content"]:
            self.after(after, baseline)
            self.save("applied_reconciled_verified")
            return self.receipt
        if identity(after) == baseline and raw_hashes(after) == raw_hashes(before):
            self.save("not_applied_verified", automatic_retry=False)
            raise GuardError("Write not applied; no automatic retry")
        self.save("uncertain_stopped_for_review", automatic_retry=False)
        raise GuardError("Same-post outcome requires review; no retry")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ManagedClient:
    def __init__(self):
        require(os.environ.get("GITHUB_ACTIONS") == "true", "Managed Actions execution required")
        require(os.environ.get("GITHUB_REPOSITORY") == APPROVED["repository"],
                "Unexpected repository")
        require(os.environ.get("GITHUB_REF") == "refs/heads/" + APPROVED["branch"],
                "Unexpected correction branch")
        base = os.environ["WORDPRESS_BASE_URL"].rstrip("/")
        parsed = urllib.parse.urlsplit(base)
        require(parsed.scheme == "https" and parsed.hostname == APPROVED["host"] and
                parsed.path in ("", "/") and not parsed.query and not parsed.fragment and
                not parsed.username and not parsed.password, "Unexpected managed host")
        self.url = base + "/wp-json/wp/v2/posts/" + str(APPROVED["before"]["id"])
        user = os.environ["WORDPRESS_USERNAME"]
        password = os.environ["WORDPRESS_APP_PASSWORD"]
        self.authorization = "Basic " + base64.b64encode((user + ":" + password).encode()).decode()
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, method, payload=None):
        # Endpoint is fixed in the constructor; no callers can pass an arbitrary URL or ID.
        url = self.url + ("?context=edit" if method == "GET" else "")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": self.authorization, "Accept": "application/json",
                   "Cache-Control": "no-cache", "User-Agent": "MNG-fixed-post-correction"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=45) as response:
                require(response.status == 200, "Unexpected successful HTTP status")
                value = json.loads(response.read())
                require(isinstance(value, dict), "Invalid post JSON")
                return value
        except urllib.error.HTTPError as error:
            raise HTTPFailureError(error.code) from None

    def get_post(self):
        return self.call("GET")

    def update_post(self, payload):
        require(set(payload) == {"content"}, "Only the fixed content operation is allowed")
        current = self.get_post()
        validate_before(current)
        require(getattr(self, "approved_before_raw", None) == raw_hashes(current),
                "Immediate raw snapshot drift before update")
        require(payload == derive_payload(current), "Update is not the exact fixed deletion")
        return self.call("POST", payload)


def main():
    receipt = {"state": "startup_failed", "writes_attempted": 0}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    def save(value):
        nonlocal receipt
        receipt = value
        temp = OUTPUT.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        temp.replace(OUTPUT)

    try:
        request = json.loads(REQUEST.read_text())
        ledger = LEDGER.read_bytes()
        validate_intake(request, ledger)
        runner = Runner(
            ManagedClient(), request, ledger, save,
            {"run_id": os.environ["GITHUB_RUN_ID"], "commit": os.environ["GITHUB_SHA"]},
            DRY_RECEIPT.read_bytes() if request["stage"] == "apply" else None,
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        )
        result = runner.run()
        print("FIXED_CORRECTION_RECEIPT " + json.dumps(result, sort_keys=True))
    except Exception as error:
        receipt["error_type"] = type(error).__name__
        if isinstance(error, GuardError):
            receipt["reason"] = str(error)
        if isinstance(error, HTTPFailureError):
            receipt["http_status"] = error.status
        if receipt.get("state") not in ("denied_no_retry", "not_applied_verified",
                                       "uncertain_stopped_for_review"):
            receipt["state"] = ("stopped_after_write" if receipt.get("writes_attempted")
                                else "stopped_before_write")
        save(receipt)
        print("FIXED_CORRECTION_STOPPED " + json.dumps(receipt, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
