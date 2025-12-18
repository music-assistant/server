#!/usr/bin/env python3
"""Generate release notes based on PRs between two tags.

Reads configuration from .github/release-notes-config.yml for categorization and formatting.
"""

import os
import re
import sys
from collections import defaultdict

import yaml
from github import Github, GithubException


def load_config():
    """Load the release-notes-config.yml configuration."""
    config_path = ".github/release-notes-config.yml"
    if not os.path.exists(config_path):
        print(f"Error: {config_path} not found")  # noqa: T201
        sys.exit(1)

    with open(config_path) as f:
        return yaml.safe_load(f)


def get_prs_between_tags(repo, previous_tag, current_branch):
    """Get all merged PRs between the previous tag and current HEAD."""
    if not previous_tag:
        print("No previous tag specified, will include all PRs from branch history")  # noqa: T201
        # Get the first commit on the branch
        commits = list(repo.get_commits(sha=current_branch))
        # Limit to last 100 commits to avoid going too far back
        commits = commits[:100]
    else:
        print(f"Finding PRs between {previous_tag} and {current_branch}")  # noqa: T201
        comparison = repo.compare(previous_tag, current_branch)
        commits = comparison.commits
        print(f"Found {comparison.total_commits} commits")  # noqa: T201

    # Extract PR numbers from commit messages
    pr_numbers = set()
    pr_pattern = re.compile(r"#(\d+)")
    merge_pattern = re.compile(r"Merge pull request #(\d+)")

    for commit in commits:
        message = commit.commit.message
        # First check for merge commits
        merge_match = merge_pattern.search(message)
        if merge_match:
            pr_numbers.add(int(merge_match.group(1)))
        else:
            # Look for PR references in the message
            for match in pr_pattern.finditer(message):
                pr_numbers.add(int(match.group(1)))

    print(f"Found {len(pr_numbers)} unique PRs")  # noqa: T201

    # Fetch the actual PR objects
    prs = []
    for pr_num in sorted(pr_numbers):
        try:
            pr = repo.get_pull(pr_num)
            if pr.merged:
                prs.append(pr)
        except GithubException as e:
            print(f"Warning: Could not fetch PR #{pr_num}: {e}")  # noqa: T201

    return prs


def categorize_prs(prs, config):
    """Categorize PRs based on their labels using the config."""
    categories = defaultdict(list)
    uncategorized = []

    # Get category definitions from config
    category_configs = config.get("categories", [])

    # Get excluded labels
    exclude_labels = set(config.get("exclude-labels", []))
    include_labels = config.get("include-labels")
    if include_labels:
        include_labels = set(include_labels)

    for pr in prs:
        # Check if PR should be excluded
        pr_labels = {label.name for label in pr.labels}

        if exclude_labels and pr_labels & exclude_labels:
            continue

        if include_labels and not (pr_labels & include_labels):
            continue

        # Try to categorize
        categorized = False
        for cat_config in category_configs:
            cat_title = cat_config.get("title", "Other")
            cat_labels = cat_config.get("labels", [])
            if isinstance(cat_labels, str):
                cat_labels = [cat_labels]

            # Check if PR has any of the category labels
            if pr_labels & set(cat_labels):
                categories[cat_title].append(pr)
                categorized = True
                break

        if not categorized:
            uncategorized.append(pr)

    return categories, uncategorized


def get_contributors(prs, config):
    """Extract unique contributors from PRs."""
    excluded = set(config.get("exclude-contributors", []))
    contributors = set()

    for pr in prs:
        author = pr.user.login
        if author not in excluded:
            contributors.add(author)

    return sorted(contributors)


def format_change_line(pr, config):
    """Format a single PR line using the change-template from config."""
    template = config.get("change-template", "- $TITLE (by @$AUTHOR in #$NUMBER)")

    # Get title and escape characters if specified
    title = pr.title
    escapes = config.get("change-title-escapes", "")
    if escapes:
        for char in escapes:
            if char in title:
                title = title.replace(char, "\\" + char)

    # Replace template variables
    result = template.replace("$TITLE", title)
    result = result.replace("$AUTHOR", pr.user.login)
    result = result.replace("$NUMBER", str(pr.number))
    return result.replace("$URL", pr.html_url)


def extract_frontend_changes(prs):
    """Extract frontend changes from frontend update PRs.

    Returns tuple of (frontend_changes_list, frontend_contributors_set)
    """
    frontend_changes = []
    frontend_contributors = set()

    # Pattern to match frontend update PRs
    frontend_pr_pattern = re.compile(r"^⬆️ Update music-assistant-frontend to \d")

    for pr in prs:
        if not frontend_pr_pattern.match(pr.title):
            continue

        print(f"Processing frontend PR #{pr.number}: {pr.title}")  # noqa: T201

        if not pr.body:
            continue

        # Extract bullet points from PR body, excluding headers and dependabot lines
        for body_line in pr.body.split("\n"):
            stripped_line = body_line.strip()
            # Check if it's a bullet point
            if stripped_line.startswith(("- ", "* ", "• ")):
                # Skip thank you lines and dependency updates
                if "🙇" in stripped_line:
                    continue
                if re.match(r"^[•\-\*]\s*Chore\(deps", stripped_line, re.IGNORECASE):
                    continue
                # Skip "No changes" entries
                if re.match(r"^[•\-\*]\s*No changes\s*$", stripped_line, re.IGNORECASE):
                    continue

                # Add the change
                frontend_changes.append(stripped_line)

                # Extract contributors mentioned in this line
                contributors_in_line = re.findall(r"@([a-zA-Z0-9_-]+)", stripped_line)
                frontend_contributors.update(contributors_in_line)

                # Limit to 20 changes per PR
                if len(frontend_changes) >= 20:
                    break

    return frontend_changes, frontend_contributors


def generate_release_notes(  # noqa: PLR0915
    config,
    categories,
    uncategorized,
    contributors,
    previous_tag,
    frontend_changes=None,
    important_notes=None,
):
    """Generate the formatted release notes."""
    lines = []

    # Add important notes section first if provided
    if important_notes and important_notes.strip():
        lines.append("## ⚠️ Important Notes")
        lines.append("")
        # Convert literal \n to actual newlines and preserve existing newlines
        formatted_notes = important_notes.strip().replace("\\n", "\n")
        lines.append(formatted_notes)
        lines.append("")
        lines.append("---")
        lines.append("")

    # Add header if previous tag exists
    if previous_tag:
        repo_url = (
            os.environ.get("GITHUB_SERVER_URL", "https://github.com")
            + "/"
            + os.environ["GITHUB_REPOSITORY"]
        )
        channel = os.environ.get("CHANNEL", "").title()
        if channel:
            lines.append(f"## 📦 {channel} Release")
            lines.append("")
        lines.append(f"_Changes since [{previous_tag}]({repo_url}/releases/tag/{previous_tag})_")
        lines.append("")

    # Add categorized PRs - first pass: categories without "after-other" flag
    category_configs = config.get("categories", [])
    deferred_categories = []

    for cat_config in category_configs:
        # Defer categories marked with after-other
        if cat_config.get("after-other", False):
            deferred_categories.append(cat_config)
            continue

        cat_title = cat_config.get("title", "Other")
        if cat_title not in categories or not categories[cat_title]:
            continue

        prs = categories[cat_title]
        lines.append(f"### {cat_title}")
        lines.append("")

        # Check if category should be collapsed
        collapse_after = cat_config.get("collapse-after")
        if collapse_after and len(prs) > collapse_after:
            lines.append("<details>")
            lines.append(f"<summary>{len(prs)} changes</summary>")
            lines.append("")

        for pr in prs:
            lines.append(format_change_line(pr, config))

        if collapse_after and len(prs) > collapse_after:
            lines.append("")
            lines.append("</details>")

        lines.append("")

    # Add frontend changes if any (before "Other Changes")
    if frontend_changes and len(frontend_changes) > 0:
        lines.append("### 🎨 Frontend Changes")
        lines.append("")
        for change in frontend_changes:
            lines.append(change)
        lines.append("")

    # Add uncategorized PRs if any
    if uncategorized:
        lines.append("### Other Changes")
        lines.append("")
        for pr in uncategorized:
            lines.append(format_change_line(pr, config))
        lines.append("")

    # Add deferred categories (after "Other Changes")
    for cat_config in deferred_categories:
        cat_title = cat_config.get("title", "Other")
        if cat_title not in categories or not categories[cat_title]:
            continue

        prs = categories[cat_title]
        lines.append(f"### {cat_title}")
        lines.append("")

        # Check if category should be collapsed
        collapse_after = cat_config.get("collapse-after")
        if collapse_after and len(prs) > collapse_after:
            lines.append("<details>")
            lines.append(f"<summary>{len(prs)} changes</summary>")
            lines.append("")

        for pr in prs:
            lines.append(format_change_line(pr, config))

        if collapse_after and len(prs) > collapse_after:
            lines.append("")
            lines.append("</details>")

        lines.append("")

    # Add contributors section using template
    if contributors:
        template = config.get("template", "")
        if "$CONTRIBUTORS" in template or not template:
            lines.append("## :bow: Thanks to our contributors")
            lines.append("")
            lines.append(
                "Special thanks to the following contributors who helped with this release:"
            )
            lines.append("")
            lines.append(", ".join(f"@{c}" for c in contributors))

    return "\n".join(lines)


def main():
    """Generate release notes for the target version."""
    # Get environment variables
    github_token = os.environ.get("GITHUB_TOKEN")
    version = os.environ.get("VERSION")
    previous_tag = os.environ.get("PREVIOUS_TAG", "")
    branch = os.environ.get("BRANCH")
    channel = os.environ.get("CHANNEL")
    repo_name = os.environ.get("GITHUB_REPOSITORY")
    important_notes = os.environ.get("IMPORTANT_NOTES", "")

    if not all([github_token, version, branch, channel, repo_name]):
        print("Error: Missing required environment variables")  # noqa: T201
        sys.exit(1)

    print(f"Generating release notes for {version} ({channel} channel)")  # noqa: T201
    print(f"Repository: {repo_name}")  # noqa: T201
    print(f"Branch: {branch}")  # noqa: T201
    print(f"Previous tag: {previous_tag or 'None (first release)'}")  # noqa: T201

    # Initialize GitHub API
    g = Github(github_token)
    repo = g.get_repo(repo_name)

    # Load configuration
    config = load_config()
    print(f"Loaded config with {len(config.get('categories', []))} categories")  # noqa: T201

    # Get PRs between tags
    prs = get_prs_between_tags(repo, previous_tag, branch)
    print(f"Processing {len(prs)} merged PRs")  # noqa: T201

    if not prs:
        print("No PRs found in range")  # noqa: T201
        no_changes = config.get("no-changes-template", "* No changes")
        notes = no_changes
        contributors_list = []
    else:
        # Categorize PRs
        categories, uncategorized = categorize_prs(prs, config)
        print(f"Categorized into {len(categories)} categories, {len(uncategorized)} uncategorized")  # noqa: T201

        # Extract frontend changes and contributors
        frontend_changes_list, frontend_contributors_set = extract_frontend_changes(prs)
        print(  # noqa: T201
            f"Found {len(frontend_changes_list)} frontend changes "
            f"from {len(frontend_contributors_set)} contributors"
        )

        # Get server contributors
        contributors_list = get_contributors(prs, config)

        # Merge frontend contributors with server contributors
        all_contributors = set(contributors_list) | frontend_contributors_set
        contributors_list = sorted(all_contributors)
        print(  # noqa: T201
            f"Total {len(contributors_list)} unique contributors (server + frontend)"
        )

        # Generate formatted notes
        notes = generate_release_notes(
            config,
            categories,
            uncategorized,
            contributors_list,
            previous_tag,
            frontend_changes_list,
            important_notes,
        )

    # Output to GitHub Actions
    # Use multiline output format
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write("release-notes<<EOF\n")
            f.write(notes)
            f.write("\nEOF\n")
            f.write("contributors<<EOF\n")
            f.write(",".join(contributors_list))
            f.write("\nEOF\n")
    else:
        print("\n=== Generated Release Notes ===\n")  # noqa: T201
        print(notes)  # noqa: T201
        print("\n=== Contributors ===\n")  # noqa: T201
        print(", ".join(contributors_list))  # noqa: T201


if __name__ == "__main__":
    main()
