# ruff: noqa: INP001  # standalone CI helper, not part of a package
"""
Post OHF Sage findings (the JSON array Copilot CLI printed to stdout) as a PR review.

Clearly labeled as an automated AI review. Falls back: inline review -> summary-only review ->
plain PR comment (so it also works on closed/merged PRs while testing).

Env: ``REPO=owner/name``, ``PR_NUMBER=<n>``, ``GH_TOKEN`` with pull-requests:write.
Usage: ``python sage_post_review.py findings.json``
"""

import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger("sage_post_review")

SEV = {"CRITICAL": 0, "PROBLEM": 1, "SUGGESTION": 2}
HEADER = (
    "## 🤖 OHF Sage — automated principle review\n\n"
    "_Automated review by **OHF Sage**, applying the project leads' engineering principles mined "
    "from past PR reviews. AI-generated — a maintainer has the final say; treat findings as you "
    "would any review comment. Each finding links the original PR discussion behind the rule._\n"
)


def gh(args, inp=None):
    """Run a ``gh`` command, returning stdout; raise ``RuntimeError`` on failure."""
    proc = subprocess.run(  # noqa: S603  # args are built here, not from untrusted input
        ["gh", *args],  # noqa: S607  # gh is a trusted CLI on the runner PATH
        input=inp,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout


def extract_findings(raw):
    """Return the first balanced ``[...]`` in the output that parses to a JSON list."""
    depth = 0
    start = -1
    for i, char in enumerate(raw):
        if char == "[":
            if depth == 0:
                start = i
            depth += 1
        elif char == "]" and depth > 0:
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    data = json.loads(raw[start : i + 1])
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(data, list):
                    return data
                start = -1
    return []


def summary_line(finding):
    """Render one finding as a Markdown summary bullet."""
    cite = finding.get("citation_url")
    line = finding.get("line")
    loc = f"`{finding.get('path', '—')}`" + (f":{line}" if isinstance(line, int) else "")
    return f"- **[{finding.get('severity', 'SUGGESTION')}]** {loc} — {finding.get('issue', '')}" + (
        f" ([source]({cite}))" if cite else ""
    )


def post_review(repo, pr, payload):
    """Submit a PR review payload via the GitHub API."""
    gh(
        ["api", f"repos/{repo}/pulls/{pr}/reviews", "-X", "POST", "--input", "-"],
        inp=json.dumps(payload),
    )


def main():
    """Read findings JSON, post it as a PR review, falling back as needed."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo, pr = os.environ["REPO"], os.environ["PR_NUMBER"]
    with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
        raw = handle.read()
    findings = sorted(extract_findings(raw), key=lambda f: SEV.get(f.get("severity"), 3))

    intro = [
        HEADER,
        "",
        (
            f"**{len(findings)} finding(s)** against the cited principles."
            if findings
            else "No principle violations found."
        ),
        "",
    ]

    comments, overflow = [], []
    for finding in findings:
        body = f"**[{finding.get('severity', 'SUGGESTION')}]** {finding.get('issue', '')}"
        if finding.get("citation_url"):
            body += f"\n\n_Principle: {finding.get('principle', '')} — {finding['citation_url']}_"
        if finding.get("path") and isinstance(finding.get("line"), int):
            comments.append(
                {"path": finding["path"], "line": finding["line"], "side": "RIGHT", "body": body},
            )
        else:
            overflow.append(summary_line(finding))

    review_body = "\n".join(
        intro + (["### Notes (not anchored to diff lines)", *overflow] if overflow else []),
    )
    summary_only = "\n".join(intro + [summary_line(f) for f in findings])

    payload = {"body": review_body, "event": "COMMENT"}
    if comments:
        payload["comments"] = comments
    try:
        post_review(repo, pr, payload)
        logger.info("posted review: %s inline, %s notes", len(comments), len(overflow))
        return
    except RuntimeError as err:
        logger.warning("inline review failed (%s)", err)
    try:
        post_review(repo, pr, {"body": summary_only, "event": "COMMENT"})
        logger.info("posted summary-only review")
        return
    except RuntimeError as err:
        logger.warning("summary review failed (%s)", err)
    gh(
        ["api", f"repos/{repo}/issues/{pr}/comments", "-X", "POST", "--input", "-"],
        inp=json.dumps({"body": summary_only}),
    )
    logger.info("posted plain comment (reviews API unavailable — PR likely closed)")


if __name__ == "__main__":
    main()
