// The release-notes labels a pull request can carry. The pull request
// template puts each one in backticks at the end of a "Types of changes"
// checkbox bullet, e.g.  - [x] CI / workflow change — `ci`
const KNOWN = [
  'breaking-change', 'bugfix', 'refactor',
  'new-feature', 'enhancement', 'new-provider',
  'maintenance', 'ci', 'dependencies', 'documentation',
];

// Returns { label } for a ticked box, { existing } for a known label already on the PR, else { error }.
function chooseLabel(pr) {
  const re = /^\s*-\s*\[x\][^`\n]*`([^`]+)`/gim;
  const ticked = [];
  for (const m of (pr.body || '').matchAll(re)) {
    const name = m[1].trim();
    if (KNOWN.includes(name) && !ticked.includes(name)) {
      ticked.push(name);
    }
  }
  if (ticked.length > 1) {
    return {
      error: `Multiple "Types of changes" checkboxes are ticked ` +
        `(${ticked.join(', ')}). Tick exactly one.`,
    };
  }
  // The description is the source of truth, so a ticked box always wins.
  // That keeps the label in step when someone reconsiders the type after
  // opening. Only with nothing ticked does a label already on the pull
  // request count, covering pull requests opened without the template.
  // Two or more known labels are ambiguous, so those fail rather than guess.
  if (ticked.length === 1) {
    return { label: ticked[0] };
  }
  const existing = (pr.labels || [])
    .map(l => l.name)
    .filter(n => KNOWN.includes(n));
  if (existing.length === 1) {
    return { existing: existing[0] };
  }
  return {
    error: 'No "Types of changes" checkbox is ticked in the PR ' +
      'description. Edit the description and tick exactly ' +
      'one box so this PR can be release-noted.',
  };
}

module.exports = { KNOWN, chooseLabel };
