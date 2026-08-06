# ruff: noqa: INP001  # standalone CI helper, not part of a package
"""
Post Automated PR Review findings (the JSON array the CLI printed to stdout) as a PR review.

Falls back: inline review -> summary-only review -> plain PR comment (so it also works on
closed/merged PRs while testing).

Env: ``REPO=owner/name``, ``PR_NUMBER=<n>``, ``GH_TOKEN`` with pull-requests:write.
Usage: ``python post_pr_review.py findings.json``
"""

import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger("pr_review")

SEV = {"CRITICAL": 0, "PROBLEM": 1, "SUGGESTION": 2}
HEADER = (
    "## 🤖 Automated PR Review\n\n"
    "Reviewed against the project's coding standards. Each note links where the standard "
    "is documented.\n"
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
    """
    Return the first ``[...]`` in the output that parses as a JSON list of findings.

    Uses ``raw_decode`` (which honours JSON string quoting) rather than counting brackets,
    so ``[``/``]`` inside a finding's text can't break detection. Trailing prose is ignored.
    """
    decoder = json.JSONDecoder()
    for i, char in enumerate(raw):
        if char != "[":
            continue
        try:
            data = decoder.raw_decode(raw[i:])[0]
        except json.JSONDecodeError:
            continue
        if isinstance(data, list) and (not data or isinstance(data[0], dict)):
            return data
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
            f"**{len(findings)} finding(s)** against the project's coding standards."
            if findings
            else "No standards violations found."
        ),
        "",
    ]

    comments, overflow = [], []
    for finding in findings:
        body = f"**[{finding.get('severity', 'SUGGESTION')}]** {finding.get('issue', '')}"
        if finding.get("citation_url"):
            body += f"\n\n_Standard: {finding.get('principle', '')} — {finding['citation_url']}_"
        anchored = bool(finding.get("path")) and isinstance(finding.get("line"), int)
        suggestion = finding.get("suggestion")
        if anchored and isinstance(suggestion, str) and suggestion.strip():
            # A fenced ```suggestion block is a one-click change a maintainer can apply, replacing
            # the anchored line; the model fills it for confident, self-contained in-diff fixes.
            block = suggestion.rstrip("\n")
            body += f"\n\n```suggestion\n{block}\n```"
        scaffold = finding.get("scaffold")
        scaffold_md = ""
        if isinstance(scaffold, str) and scaffold.strip():
            # A starter test can't be a one-click suggestion (its target file isn't in the diff),
            # so offer it as a copy-paste block the author adapts and verifies.
            fence = f"```python\n{scaffold.strip()}\n```"
            summary = "Starter test — copy into tests/ and adapt"
            scaffold_md = f"\n\n<details><summary>{summary}</summary>\n\n{fence}\n\n</details>"
        if anchored:
            comments.append(
                {
                    "path": finding["path"],
                    "line": finding["line"],
                    "side": "RIGHT",
                    "body": body + scaffold_md,
                },
            )
        else:
            overflow.append(summary_line(finding) + scaffold_md)

    parts = list(intro)
    if overflow:
        parts += ["### Notes (not anchored to diff lines)", "", "\n\n".join(overflow)]
    review_body = "\n".join(parts)
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
