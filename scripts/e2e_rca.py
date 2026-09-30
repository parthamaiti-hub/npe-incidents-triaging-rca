"""E2E RCA test: takes a real Jira issue key, fetches it from the
configured Jira Cloud instance, classifies it, executes its workflow via
app/workflow_orchestrator.py (a CNCF Serverless Workflow-style interpreter,
with check_type results currently stubbed per app/checks.py), prints the
synthesized RCA, and (with --post-comment) posts it back to the issue as a
Jira comment.

Usage:
    uv run python -m scripts.e2e_rca TT-1
    uv run python -m scripts.e2e_rca TT-1 --post-comment
"""

import argparse
import asyncio

import httpx

from app import classification_status
from app.db import get_database, make_mongo_client
from app.e2e_pipeline import run_e2e_for_jira_key


async def run(jira_key: str, post_rca_comment: bool) -> dict:
    mongo = make_mongo_client()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            return await run_e2e_for_jira_key(get_database(mongo), client, jira_key, post_rca_comment=post_rca_comment)
    finally:
        await mongo.close()


def print_report(result: dict) -> None:
    sep = "=" * 78
    print(sep)
    print(f"E2E RCA -- {result['jira_key']}")
    print(sep)
    print(f"Summary:  {result['summary']}")
    print(f"Status:   {result['status']}   Priority: {result['priority']}")

    c = result["classification"]
    print()
    print(
        f"Classification Status: {classification_status.display_status(c['status'])}  "
        f"source_system_id={c['source_system_id']}  category={c['category']}  "
        f"matched_rule_id={c['matched_rule_id']}"
    )

    if result["evidence"] is not None:
        print()
        print("Evidence (playbook execution):")
        for e in result["evidence"]:
            print(f"  [{e['status']:5}] {e['check']}: {e['details']}")

    if result["rca"] is not None:
        rca = result["rca"]
        print()
        print(f"RCA Status: {rca['rca_status']}  (pattern={rca['matched_pattern']})")
        print(f"  {rca['root_cause_summary']}")
        if rca["contributing_factors"]:
            print()
            print("Contributing Factors:")
            for f in rca["contributing_factors"]:
                print(f"  - {f}")
        if rca["recommended_actions"]:
            print()
            print("Recommended Actions:")
            for a in rca["recommended_actions"]:
                print(f"  - {a}")

    if result["comment_id"] is not None:
        print()
        print(f"Posted as Jira comment id={result['comment_id']}")

    print(sep)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("jira_key", help="Jira issue key, e.g. TT-1")
    parser.add_argument(
        "--post-comment",
        action="store_true",
        help="Post the RCA back to the issue as a Jira comment (writes to the real ticket).",
    )
    args = parser.parse_args()

    result = asyncio.run(run(args.jira_key, args.post_comment))
    print_report(result)


if __name__ == "__main__":
    main()
