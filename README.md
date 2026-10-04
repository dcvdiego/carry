# carry

Run forks of tools with unmerged upstream PRs on top, keep them current, and
switch back to upstream once the work is merged and released.

A daily workflow rebuilds each fork's `carry/main` from scratch: upstream's base
branch, then every item in [`carry.toml`](carry.toml) merged in order. Rebuilding
(instead of rebasing a long-lived fork) means the fork never drifts: it is always
"upstream + exactly these changes".

| Situation | What happens |
| --- | --- |
| Everything merges | `carry/main` is force-pushed to the fork if it changed. Builds are deterministic, so an unchanged upstream is a no-op. |
| An item conflicts | Nothing is pushed; the fork keeps its last good build and an issue names the item. For PRs, a `resolved` fork branch is tried before giving up. |
| A PR is merged | It is skipped (upstream's base already has it). Once a published release contains it, an issue says to drop it. |
| A PR is closed unmerged | Still carried; an issue asks whether to keep it as a fork branch. |
| Nothing left to carry | The consumer pin switches the project back to upstream, and an issue says so. |

Issues open while a condition holds and close themselves when it clears, so
GitHub notifications are the to-do list.

## Consumer pins

The resulting revisions go to [`dcvdiego/dotfiles`](https://github.com/dcvdiego/dotfiles)
as `.chezmoidata/carry.yaml`, proposed as a PR (`carry/pins`). The agents box
installs from those pins, so new fork code only runs after that PR is merged.

## Setup

1. Create a fine-grained personal access token (Settings → Developer settings →
   Fine-grained tokens), resource owner `dcvdiego`, repository access limited to
   the forks listed in `carry.toml` and the consumer repo, with permissions:
   - **Contents: Read and write** (push `carry/main`, the pins branch)
   - **Workflows: Read and write** (upstream history contains workflow files)
   - **Pull requests: Read and write** (the consumer PR)
2. Save it as the `CARRY_TOKEN` repository secret here.
3. Run the workflow (Actions → carry → Run workflow), or wait for the schedule.

Locally: `GH_TOKEN=$(gh auth token) python3 carry.py --dry-run` builds everything
and prints the report without pushing or touching issues.
