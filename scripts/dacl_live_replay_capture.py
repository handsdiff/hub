#!/usr/bin/env python3
"""Capture best-effort DACL replay inputs from the public GitHub API.

Usage:
  python3 hub/scripts/dacl_live_replay_capture.py \
    --repo alexjaniak/DACL \
    --profile-json hub/docs/profiles/dacl-review-v1.profile.json \
    --out hub/docs/profiles/live-replay-001/input.jsonl \
    --once

Notes:
- This is a public-API capture helper, not a full private watcher tap.
- It appends one JSON object per poll in the `input.jsonl` shape documented in
  `hub/docs/profiles/dacl-live-replay-001-format.md`.
- If no open PRs exist, it prints `NO_OPEN_PRS` and exits 2.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any


USER_AGENT = "OpenClaw-Brain/dacl-live-replay-capture"
PLACEHOLDER_PREFIX = '{"status":"placeholder'


def fetch_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")


def load_profile(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def trusted_actor_weight(login: str, profile: dict[str, Any]) -> float:
    for actor in profile.get("trusted_actors", []):
        if actor.get("subject") == login:
            try:
                return float(actor.get("weight", 0))
            except Exception:
                return 0.0
    return 0.0


def compile_phrase_rules(profile: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rules = []
    for rule in profile.get(key, []):
        pattern = rule.get("pattern", "")
        if not pattern:
            continue
        if rule.get("pattern_type") == "regex":
            rx = re.compile(pattern, re.IGNORECASE)
        else:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)
        rules.append({**rule, "_rx": rx})
    return rules


def score_semantic(body: str, author: str | None, source_kind: str, profile: dict[str, Any]) -> tuple[bool, int]:
    text = body or ""
    blocking = 0.0
    non_blocking = 0.0
    actor_weight = trusted_actor_weight(author or "", profile)

    def scope_matches(rule: dict[str, Any]) -> bool:
        scopes = rule.get("source_scope", [])
        return "any" in scopes or source_kind in scopes

    for rule in compile_phrase_rules(profile, "blocking_phrases"):
        if not scope_matches(rule):
            continue
        if rule.get("trusted_actor_only") and actor_weight <= 0:
            continue
        if rule["_rx"].search(text):
            blocking += float(rule.get("weight", 0))

    for rule in compile_phrase_rules(profile, "non_blocking_phrases"):
        if not scope_matches(rule):
            continue
        if rule.get("trusted_actor_only") and actor_weight <= 0:
            continue
        if rule["_rx"].search(text):
            non_blocking += float(rule.get("weight", 0))

    score = max(0.0, min(99.0, blocking - non_blocking + min(actor_weight * 0.2, 20.0)))
    return score > 0, int(round(score))


def map_check_status(check_run: dict[str, Any]) -> str:
    status = check_run.get("status")
    conclusion = check_run.get("conclusion")

    if status != "completed":
        return "pending"
    if conclusion in {"success", "neutral", "skipped"}:
        return "success"
    if conclusion in {"cancelled", "timed_out", "action_required", "failure", "startup_failure"}:
        return "failed" if conclusion not in {"cancelled"} else "cancelled"
    return "missing"


def latest_valid_approval(reviews: list[dict[str, Any]]) -> dict[str, Any] | None:
    approvals = [r for r in reviews if r.get("state") == "APPROVED" and r.get("commit_id")]
    if not approvals:
        return None
    approvals.sort(key=lambda r: r.get("submitted_at") or "")
    latest = approvals[-1]
    return {"author": latest.get("user", {}).get("login"), "commitId": latest.get("commit_id")}


def to_review_event(review: dict[str, Any]) -> dict[str, Any]:
    return {
        "author": review.get("user", {}).get("login") or "unknown",
        "state": review.get("state") or "COMMENTED",
        "commitId": review.get("commit_id"),
        "submittedAt": review.get("submitted_at") or review.get("submittedAt") or "",
    }


def to_comment_artifact(comment: dict[str, Any], source_kind: str, profile: dict[str, Any]) -> dict[str, Any]:
    body = comment.get("body") or ""
    author = comment.get("user", {}).get("login")
    semantic_blocking, intent_confidence = score_semantic(body, author, source_kind, profile)
    applies = comment.get("commit_id") if source_kind == "review_thread" else None
    return {
        "id": f"thread-{comment.get('id')}",
        "sourceKind": source_kind,
        "author": author,
        "semanticBlocking": semantic_blocking,
        "formalBlocking": False,
        "intentConfidencePct": intent_confidence,
        "appliesToHeadSha": applies,
        "resolvedInUi": False,
        "resolvedSemantically": None,
        "evidenceExcerpt": body[:160],
    }


def to_formal_review_artifact(review: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    body = review.get("body") or ""
    author = review.get("user", {}).get("login")
    semantic_blocking, intent_confidence = score_semantic(body, author, "review_state", profile)
    return {
        "id": f"review-{review.get('id')}",
        "sourceKind": "review_state",
        "author": author,
        "semanticBlocking": semantic_blocking,
        "formalBlocking": review.get("state") == "CHANGES_REQUESTED",
        "intentConfidencePct": intent_confidence,
        "appliesToHeadSha": review.get("commit_id"),
        "resolvedInUi": False,
        "resolvedSemantically": None,
        "evidenceExcerpt": body[:160],
    }


def build_raw_snapshot(owner: str, repo: str, pr_number: int, profile: dict[str, Any]) -> dict[str, Any]:
    base = f"https://api.github.com/repos/{owner}/{repo}"
    pr = fetch_json(f"{base}/pulls/{pr_number}")
    reviews = fetch_json(f"{base}/pulls/{pr_number}/reviews")
    issue_comments = fetch_json(f"{base}/issues/{pr_number}/comments")
    review_comments = fetch_json(f"{base}/pulls/{pr_number}/comments")
    check_runs = fetch_json(f"{base}/commits/{pr['head']['sha']}/check-runs").get("check_runs", [])

    configured_required = profile.get("policy_overrides", {}).get("required_check_names", []) or []
    required_names = list(dict.fromkeys(configured_required + [c.get("name") for c in check_runs if c.get("name")]))

    required_checks = []
    for name in required_names:
        run = next((c for c in check_runs if c.get("name") == name), None)
        required_checks.append(
            {
                "name": name,
                "status": map_check_status(run) if run else "missing",
                **({"url": run.get("html_url") or run.get("details_url")} if run else {}),
            }
        )

    blocking_artifacts = []
    for review in reviews:
        if review.get("state") == "CHANGES_REQUESTED":
            blocking_artifacts.append(to_formal_review_artifact(review, profile))
    for comment in review_comments:
        blocking_artifacts.append(to_comment_artifact(comment, "review_thread", profile))
    for comment in issue_comments:
        blocking_artifacts.append(to_comment_artifact(comment, "issue_comment", profile))

    requested_reviewers = [u.get("login") for u in pr.get("requested_reviewers", []) if u.get("login")]
    requested_teams = [t.get("slug") for t in pr.get("requested_teams", []) if t.get("slug")]

    return {
        "repo": f"{owner}/{repo}",
        "prNumber": pr_number,
        "prUrl": pr.get("html_url"),
        "headSha": pr["head"]["sha"],
        "baseBranch": pr["base"]["ref"],
        "profileId": profile.get("profile_id") or profile.get("profileId"),
        "profileVersion": str(profile.get("version")),
        "requiredChecks": required_checks,
        "requiredReviewersOutstanding": requested_reviewers + requested_teams,
        "latestValidApproval": latest_valid_approval(reviews),
        "reviewEvents": [to_review_event(r) for r in reviews],
        "blockingArtifacts": blocking_artifacts,
        "policyFailures": [],
    }


def choose_pr(owner: str, repo: str, explicit_pr: int | None) -> int:
    if explicit_pr is not None:
        return explicit_pr
    pulls = fetch_json(f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=20")
    if not pulls:
        raise RuntimeError("NO_OPEN_PRS")
    return int(pulls[0]["number"])


def append_jsonl(path: pathlib.Path, record: dict[str, Any]) -> None:
    existing = []
    if path.exists():
        existing = path.read_text().splitlines()
        existing = [line for line in existing if line.strip() and not line.startswith(PLACEHOLDER_PREFIX)]
    existing.append(json.dumps(record, sort_keys=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(existing) + "\n")


def capture_once(args: argparse.Namespace) -> int:
    owner, repo = args.repo.split("/", 1)
    profile = load_profile(pathlib.Path(args.profile_json))

    try:
        pr_number = choose_pr(owner, repo, args.pr)
    except RuntimeError as e:
        if str(e) == "NO_OPEN_PRS":
            print("NO_OPEN_PRS")
            return 2
        raise

    raw = build_raw_snapshot(owner, repo, pr_number, profile)
    out_path = pathlib.Path(args.out)
    seq = 1
    if out_path.exists():
        lines = [line for line in out_path.read_text().splitlines() if line.strip() and not line.startswith(PLACEHOLDER_PREFIX)]
        seq = len(lines) + 1

    record = {
        "seq": seq,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": args.repo,
        "pr": pr_number,
        "capture_label": args.capture_label,
        "notes": args.notes or "",
        "raw": raw,
    }
    append_jsonl(out_path, record)
    print(json.dumps({"ok": True, "out": str(out_path), "seq": seq, "pr": pr_number, "headSha": raw["headSha"]}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="alexjaniak/DACL")
    ap.add_argument("--pr", type=int)
    ap.add_argument("--profile-json", default="hub/docs/profiles/dacl-review-v1.profile.json")
    ap.add_argument("--out", default="hub/docs/profiles/live-replay-001/input.jsonl")
    ap.add_argument("--capture-label", default="live-replay-001")
    ap.add_argument("--notes", default="")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if args.once:
        return capture_once(args)

    print("Use --once for now; continuous polling intentionally not enabled in v0.1 capture helper.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
