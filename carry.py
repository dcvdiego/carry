#!/usr/bin/env python3
"""Rebuild each fork's carry/main from upstream + carried PRs/branches.

Reads carry.toml, then for every project:
  1. starts from upstream's base branch and merges each carried item in order
     (a PR's head, falling back to a resolved fork branch on conflict),
  2. force-pushes the result to the fork's carry/main (only if it changed),
  3. reports PR state: merged, released, closed,
and finally proposes the resulting pins to the consumer repo as a PR.

State that needs a human becomes an issue in this repo (opened while the
condition holds, closed once it clears). Runs in GitHub Actions; locally,
`--dry-run` builds and reports without pushing or touching issues.

Env: CARRY_TOKEN pushes to forks and the consumer (fine-grained PAT);
GH_TOKEN reads PRs and manages issues in this repo.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

CARRY_BRANCH = "carry/main"
IDENTITY = {"GIT_AUTHOR_NAME": "carry", "GIT_AUTHOR_EMAIL": "carry@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "carry", "GIT_COMMITTER_EMAIL": "carry@users.noreply.github.com"}
ISSUE_PREFIX = "[carry]"


def run(args, cwd=None, env=None, check=True):
    result = subprocess.run(args, cwd=cwd, env={**os.environ, **(env or {})}, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}\n{result.stderr.strip()}")
    return result


def git(repo, *args, env=None, check=True):
    return run(["git", *args], cwd=repo, env=env, check=check)


def gh_json(*args):
    return json.loads(run(["gh", *args]).stdout or "null")


def remote_url(slug, token):
    return f"https://x-access-token:{token}@github.com/{slug}.git" if token else f"https://github.com/{slug}.git"


def latest_release(upstream):
    """Tag of the newest non-draft, non-prerelease release, or None."""
    releases = gh_json("release", "list", "-R", upstream, "--exclude-drafts", "--exclude-pre-releases",
                       "--limit", "1", "--json", "tagName")
    return releases[0]["tagName"] if releases else None


def build(project, token, workdir):
    """Rebuild carry/main for one project. Returns a result dict."""
    name, upstream, fork, base = project["name"], project["upstream"], project["fork"], project.get("base", "main")
    repo = Path(workdir) / name
    git(None, "init", "-q", str(repo))
    git(repo, "remote", "add", "upstream", f"https://github.com/{upstream}.git")
    git(repo, "remote", "add", "fork", remote_url(fork, token))
    refspecs = [f"+refs/heads/{base}:refs/remotes/upstream/{base}", "+refs/tags/*:refs/tags/*"]
    refspecs += [f"+refs/pull/{item['pr']}/head:refs/remotes/upstream/pr/{item['pr']}"
                 for item in project.get("carry", []) if "pr" in item]
    git(repo, "fetch", "-q", "upstream", *refspecs)
    git(repo, "fetch", "-q", "fork", "+refs/heads/*:refs/remotes/fork/*")

    base_sha = git(repo, "rev-parse", f"upstream/{base}").stdout.strip()
    # Fixed dates and identity: the same inputs always produce the same commits,
    # so an unchanged upstream doesn't churn carry/main or the consumer pins.
    stamp = git(repo, "show", "-s", "--format=%cI", base_sha).stdout.strip()
    env = {**IDENTITY, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    git(repo, "checkout", "-q", "-B", CARRY_BRANCH, base_sha)
    release = latest_release(upstream)

    items, active, conflict = [], 0, None
    for item in project.get("carry", []):
        if "pr" in item:
            number = item["pr"]
            pr = gh_json("pr", "view", str(number), "-R", upstream, "--json",
                         "state,title,url,mergeCommit,headRefOid")
            entry = {"kind": "pr", "id": f"#{number}", "title": pr["title"], "url": pr["url"], "state": pr["state"]}
            if pr["state"] == "MERGED":
                merge = (pr.get("mergeCommit") or {}).get("oid")
                released = bool(release and merge and git(repo, "merge-base", "--is-ancestor", merge, release,
                                                          check=False).returncode == 0)
                entry["state"] = "RELEASED" if released else "MERGED"
                entry["release"] = release if released else None
                items.append(entry)
                continue  # already in upstream's base
            active += 1
            candidates = [("head", f"upstream/pr/{number}")]
            if item.get("resolved"):
                candidates.append(("resolved", f"fork/{item['resolved']}"))
        else:
            entry = {"kind": "branch", "id": item["branch"], "title": item["branch"],
                     "url": f"https://github.com/{fork}/tree/{item['branch']}", "state": "FORK-ONLY"}
            active += 1
            candidates = [("branch", f"fork/{item['branch']}")]

        for source, ref in candidates:
            if git(repo, "rev-parse", "--verify", "-q", ref, check=False).returncode != 0:
                continue
            merged = git(repo, "merge", "--no-ff", "-q", "-m", f"carry: {entry['id']} ({source})", ref,
                         env=env, check=False)
            if merged.returncode == 0:
                entry["source"] = source
                break
            git(repo, "merge", "--abort", check=False)
        else:
            conflict = entry
            entry["source"] = None
            items.append(entry)
            break
        items.append(entry)

    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    previous = git(repo, "rev-parse", "--verify", "-q", f"fork/{CARRY_BRANCH}", check=False).stdout.strip() or None
    return {"name": name, "upstream": upstream, "fork": fork, "base": base, "base_sha": base_sha,
            "release": release, "items": items, "active": active, "conflict": conflict,
            "sha": sha, "previous": previous, "repo": repo}


def push(result, dry_run):
    if result["conflict"] or result["active"] == 0 or result["sha"] == result["previous"]:
        return False
    if not dry_run:
        git(result["repo"], "push", "-q", "--force", "fork", f"{CARRY_BRANCH}:refs/heads/{CARRY_BRANCH}")
    return True


def pins(results):
    """Consumer pins: the fork's carry/main while anything is carried, else upstream."""
    out = {}
    for r in results:
        if r["active"] and not r["conflict"]:
            out[r["name"]] = {"source": "fork", "repo": r["fork"], "rev": r["sha"]}
        elif r["active"]:  # conflicted: keep the last good build
            out[r["name"]] = {"source": "fork", "repo": r["fork"], "rev": r["previous"]}
        else:
            out[r["name"]] = {"source": "upstream", "repo": r["upstream"], "rev": r["release"] or r["base_sha"]}
    return out


def render_pins(data):
    lines = ["# Written by https://github.com/dcvdiego/carry; edit carry.toml there instead.", "carry:"]
    for name, pin in sorted(data.items()):
        lines.append(f"  {name}:")
        lines += [f"    {key}: {json.dumps(value)}" for key, value in pin.items()]
    return "\n".join(lines) + "\n"


def propose_pins(consumer, data, token, workdir, dry_run):
    """Open or update a PR on the consumer repo when the pins file changes."""
    repo = Path(workdir) / "consumer"
    branch = "carry/pins"
    git(None, "clone", "-q", "--depth", "1", "-b", consumer["branch"], remote_url(consumer["repo"], token), str(repo))
    path = repo / consumer["file"]
    text = render_pins(data)
    if path.exists() and path.read_text() == text:
        return "unchanged"
    if dry_run:
        return "would propose:\n" + text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(repo, "checkout", "-q", "-B", branch)
    git(repo, "add", consumer["file"])
    git(repo, "commit", "-q", "-m", "carry: update pins", env=IDENTITY)
    git(repo, "push", "-q", "--force", "origin", f"{branch}:refs/heads/{branch}")
    existing = gh_json("pr", "list", "-R", consumer["repo"], "--head", branch, "--state", "open", "--json", "number")
    body = "Updated by the carry workflow. Review the fork changes before merging: they run inside every agent.\n\n" \
           + "\n".join(f"- **{n}** → `{p['source']}` {p['repo']}@`{(p['rev'] or '')[:12]}`" for n, p in sorted(data.items()))
    if existing:
        run(["gh", "pr", "edit", str(existing[0]["number"]), "-R", consumer["repo"], "--body", body],
            env={"GH_TOKEN": token})
        return f"updated PR #{existing[0]['number']}"
    url = run(["gh", "pr", "create", "-R", consumer["repo"], "--base", consumer["branch"], "--head", branch,
               "--title", "carry: update pinned forks", "--body", body], env={"GH_TOKEN": token}).stdout.strip()
    return f"opened {url}"


def wanted_issues(results):
    """Issues that should be open now: {title: body}."""
    issues = {}
    for r in results:
        if r["conflict"]:
            c = r["conflict"]
            issues[f"{ISSUE_PREFIX} {r['name']}: {c['id']} no longer merges"] = (
                f"{c['title']} ({c['url']}) conflicts with `{r['upstream']}@{r['base']}` "
                f"({r['base_sha'][:12]}).\n\nThe fork keeps its last good `{CARRY_BRANCH}`. Rebase or re-resolve it "
                f"(update the `resolved` branch for a PR), then re-run the workflow.")
        for item in r["items"]:
            if item["state"] == "RELEASED":
                issues[f"{ISSUE_PREFIX} {r['name']}: {item['id']} is released, drop it"] = (
                    f"{item['title']} ({item['url']}) is merged and in **{item['release']}**. Remove it from "
                    f"carry.toml.")
            elif item["state"] == "CLOSED":
                issues[f"{ISSUE_PREFIX} {r['name']}: {item['id']} was closed without merging"] = (
                    f"{item['title']} ({item['url']}) was closed upstream. It is still carried; decide whether to "
                    f"keep it as a fork branch or drop it.")
        if r["active"] == 0:
            issues[f"{ISSUE_PREFIX} {r['name']}: nothing left to carry, switch back to upstream"] = (
                f"Every carried item for {r['name']} is merged. The consumer pin now points at "
                f"`{r['upstream']}` ({r['release'] or 'base branch'}). Merge that PR, then remove the project "
                f"from carry.toml and archive the fork if you no longer need it.")
    return issues


def sync_issues(wanted, dry_run):
    repo = os.environ.get("GITHUB_REPOSITORY", "dcvdiego/carry")
    current = {i["title"]: i["number"] for i in gh_json("issue", "list", "-R", repo, "--state", "open", "--limit",
                                                         "200", "--search", f"in:title \"{ISSUE_PREFIX}\"",
                                                         "--json", "number,title")}
    actions = []
    for title, body in wanted.items():
        if title not in current:
            actions.append(f"open: {title}")
            if not dry_run:
                run(["gh", "issue", "create", "-R", repo, "--title", title, "--body", body])
    for title, number in current.items():
        if title.startswith(ISSUE_PREFIX) and title not in wanted:
            actions.append(f"close: #{number} {title}")
            if not dry_run:
                run(["gh", "issue", "close", str(number), "-R", repo, "--comment", "Resolved on the latest run."])
    return actions


def summary(results, pushed, consumer_note, issue_actions, dry_run=False):
    out = ["# carry", ""]
    for r in results:
        status = "conflict" if r["conflict"] else ("upstream" if r["active"] == 0 else "ok")
        out.append(f"## {r['name']}: {status}")
        out.append(f"base `{r['upstream']}@{r['base']}` {r['base_sha'][:12]}, latest release {r['release'] or '-'}")
        for item in r["items"]:
            out.append(f"- {item['id']} {item['title']}: **{item['state']}**"
                       + (f" via {item['source']}" if item.get("source") else ""))
        out.append(f"carry/main {r['sha'][:12]}" + ((" (would push)" if dry_run else " (pushed)") if pushed.get(r["name"]) else " (unchanged)"))
        out.append("")
    out += ["## consumer", consumer_note, "", "## issues", *(issue_actions or ["none"])]
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="build and report; push nothing, touch no issues")
    parser.add_argument("--no-issues", action="store_true", help="leave this repo's issues alone (local runs)")
    parser.add_argument("--config", default=Path(__file__).with_name("carry.toml"))
    args = parser.parse_args()
    config = tomllib.loads(Path(args.config).read_text())
    token = os.environ.get("CARRY_TOKEN", "")
    # Without the token nothing can be pushed: build and report anyway, and keep
    # an issue open until the secret exists.
    readonly = args.dry_run or not token

    with tempfile.TemporaryDirectory() as workdir:
        results = [build(project, token, workdir) for project in config["project"]]
        pushed = {r["name"]: push(r, readonly) for r in results}
        consumer_note = propose_pins(config["consumer"], pins(results), token, workdir, readonly) \
            if "consumer" in config else "no consumer configured"
        wanted = wanted_issues(results)
        if not token:
            wanted[f"{ISSUE_PREFIX} setup: add the CARRY_TOKEN secret"] = (
                "Runs are read-only until the `CARRY_TOKEN` repository secret exists; see the README's Setup.")
        issue_actions = ["skipped (--no-issues)"] if args.no_issues else sync_issues(wanted, args.dry_run)
        report = summary(results, pushed, consumer_note, issue_actions, readonly)

    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(report)
    return 1 if any(r["conflict"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
