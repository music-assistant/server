"""Post OHF Sage findings (the JSON array Copilot CLI printed to stdout) as a PR review.

Clearly labeled as an automated AI review. Falls back: inline review -> summary-only review ->
plain PR comment (so it also works on closed/merged PRs while testing).

Env: REPO=owner/name, PR_NUMBER=<n>, GH_TOKEN with pull-requests:write.
Usage: python sage_post_review.py findings.json
"""
import json
import os
import re
import subprocess
import sys

SEV = {"CRITICAL": 0, "PROBLEM": 1, "SUGGESTION": 2}
HEADER = (
    "## 🤖 OHF Sage — automated principle review\n\n"
    "_Automated review by **OHF Sage**, applying the project leads' engineering principles mined "
    "from past PR reviews. AI-generated — a maintainer has the final say; treat findings as you "
    "would any review comment. Each finding links the original PR discussion behind the rule._\n"
)


def gh(args, inp=None):
    p = subprocess.run(["gh"] + args, input=inp, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise RuntimeError(p.stderr.strip())
    return p.stdout


def extract_findings(raw):
    """Pull the first JSON array out of the CLI output (tolerant of stray prose)."""
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def summary_line(f):
    cite = f.get("citation_url")
    loc = f"`{f.get('path','—')}`" + (f":{f['line']}" if isinstance(f.get("line"), int) else "")
    return (f"- **[{f.get('severity','SUGGESTION')}]** {loc} — {f.get('issue','')}"
            + (f" ([source]({cite}))" if cite else ""))


def main():
    repo, pr = os.environ["REPO"], os.environ["PR_NUMBER"]
    findings = sorted(extract_findings(open(sys.argv[1], encoding="utf-8", errors="replace").read()),
                      key=lambda f: SEV.get(f.get("severity"), 3))

    intro = [HEADER, "",
             (f"**{len(findings)} finding(s)** against the cited principles."
              if findings else "No principle violations found."), ""]

    comments, overflow = [], []
    for f in findings:
        body = f"**[{f.get('severity','SUGGESTION')}]** {f.get('issue','')}"
        if f.get("citation_url"):
            body += f"\n\n_Principle: {f.get('principle','')} — {f['citation_url']}_"
        if f.get("path") and isinstance(f.get("line"), int):
            comments.append({"path": f["path"], "line": f["line"], "side": "RIGHT", "body": body})
        else:
            overflow.append(summary_line(f))

    review_body = "\n".join(intro + (["### Notes (not anchored to diff lines)", *overflow]
                                     if overflow else []))
    summary_only = "\n".join(intro + [summary_line(f) for f in findings])

    def review(payload):
        gh(["api", f"repos/{repo}/pulls/{pr}/reviews", "-X", "POST", "--input", "-"],
           inp=json.dumps(payload))

    # 1) full review with inline comments
    try:
        payload = {"body": review_body, "event": "COMMENT"}
        if comments:
            payload["comments"] = comments
        review(payload)
        print(f"posted review: {len(comments)} inline, {len(overflow)} notes")
        return
    except RuntimeError as e:
        print(f"inline review failed ({e})", file=sys.stderr)
    # 2) summary-only review (inline lines not in the diff)
    try:
        review({"body": summary_only, "event": "COMMENT"})
        print("posted summary-only review")
        return
    except RuntimeError as e:
        print(f"summary review failed ({e})", file=sys.stderr)
    # 3) plain PR comment (works on closed/merged PRs)
    gh(["api", f"repos/{repo}/issues/{pr}/comments", "-X", "POST", "--input", "-"],
       inp=json.dumps({"body": summary_only}))
    print("posted plain comment (reviews API unavailable — PR likely closed)")


if __name__ == "__main__":
    main()
