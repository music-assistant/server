// Finds the pull request into `base` a "PR event" run was started for. The run
// of a fork does not name its pull request, so match its head repository,
// branch and exact commit. Returns null unless exactly one pull request matches.
async function findPullRequest({ github, context, core, base, state = 'open' }) {
  const run = context.payload.workflow_run;
  if (!run.head_repository) {
    core.info('The head repository of this run no longer exists; nothing to do.');
    return null;
  }
  const pulls = await github.paginate(github.rest.pulls.list, {
    ...context.repo,
    state,
    base,
    head: `${run.head_repository.owner.login}:${run.head_branch}`,
    per_page: 100,
  });
  const matches = pulls.filter(pr =>
    pr.head.sha === run.head_sha && pr.head.repo?.id === run.head_repository.id);
  if (matches.length !== 1) {
    core.info(`${matches.length} ${state} pull requests have head ${run.head_sha}; nothing to do.`);
    return null;
  }
  return matches[0];
}

module.exports = { findPullRequest };
