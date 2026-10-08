#!/usr/bin/env python3
"""Analyze and clean up merged git branches, their worktrees, and tracking refs.

The tool runs in two phases. ``analyze`` inspects the repository and writes a
plan describing what it believes is safe to delete and why. ``--execute`` reads
that plan back, revalidates every branch tip against it, and only then deletes
anything.

Merge detection is evidence-based. Each branch receives a verdict
(``CONTAINED``, ``SQUASHED``, ``DIVERGED``, ``UNKNOWN`` or ``PROTECTED``) and a
human-readable string saying which signal produced it. Only ``CONTAINED`` and
``SQUASHED`` are deletion candidates; ``UNKNOWN`` is surfaced for review rather
than silently skipped, because absence of evidence is not evidence of absence.

Git alone cannot detect a squash merge of a stacked pull request. When the
GitHub CLI is available the tool adds a forge pass that compares branch tips to
merged pull request heads and resolves leftover commits against other merged
pull requests in the stack. Without ``gh`` those branches come back ``UNKNOWN``.

Note on purity: ``analyze`` fetches all remotes before inspecting, so
remote-tracking refs may move. It never creates, moves or deletes a local
branch, tag or worktree. Some detection signals write unreferenced git objects,
which are reclaimed by a later ``git gc``.
"""

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

# Verdicts.
CONTAINED = 'CONTAINED'
SQUASHED = 'SQUASHED'
DIVERGED = 'DIVERGED'
UNKNOWN = 'UNKNOWN'
PROTECTED = 'PROTECTED'

DELETABLE = (CONTAINED, SQUASHED)

# git merge-tree --write-tree and the squash probe need this version.
MERGE_TREE_MIN_VERSION = (2, 38)

# Fields requested from the GitHub CLI for every pull request query.
PR_FIELDS = 'number,state,headRefName,baseRefName,headRefOid,mergeCommit'

# Sentinel key marking a pull request index as incomplete.
TRUNCATED = '__truncated__'

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AMBIGUOUS_REPO = 2
EXIT_PLAN_STALE = 3


# --------------------------------------------------------------------------
# Process helpers
# --------------------------------------------------------------------------

def run_cmd(args, cwd=None):
    """Runs a command and returns (returncode, stdout, stderr).

    A missing working directory or a missing executable is reported the same
    way as a command that failed, so callers probing a path that may not
    exist do not have to guard every call.
    """
    try:
        result = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    except OSError as exc:
        return 127, '', str(exc)
    return result.returncode, result.stdout, result.stderr


def git(path, *args):
    """Runs a git command in `path`."""
    return run_cmd(['git'] + list(args), cwd=path)


def git_out(path, *args):
    """Runs a git command and returns stripped stdout, or None on failure."""
    code, out, _ = git(path, *args)
    return out.strip() if code == 0 else None


def git_version(path):
    """Returns the git version as a (major, minor) tuple, or None."""
    out = git_out(path, '--version')
    if not out:
        return None
    for token in out.split():
        parts = token.split('.')
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return (int(parts[0]), int(parts[1]))
    return None


# --------------------------------------------------------------------------
# Repository discovery
# --------------------------------------------------------------------------

def is_git_repo(path):
    code, _, _ = git(path, 'rev-parse', '--is-inside-work-tree')
    return code == 0


def repo_toplevel(path):
    """Returns the top level of the repository containing `path`, or None."""
    out = git_out(path, 'rev-parse', '--show-toplevel')
    return os.path.realpath(out) if out else None


def common_git_dir(path):
    """Returns the shared git directory, which linked worktrees have in common."""
    out = git_out(path, 'rev-parse', '--path-format=absolute',
                  '--git-common-dir')
    return os.path.realpath(out.split('\n')[0].strip()) if out else None


def find_repo(path):
    """Resolves `path` to a git repository.

    Worktree-based checkouts often keep the real repository one level below a
    plain container directory. When `path` is not itself a repository, scan its
    immediate children. A child only counts when it is the top level of its own
    repository, which excludes subdirectories and symlinks pointing into a
    repository that is already listed.

    Several children of one container are usually worktrees of a single
    repository rather than separate repositories. They share a git directory,
    so they collapse to the worktree that owns it. Returns (repo_path,
    candidates); `repo_path` is None when the choice is genuinely ambiguous.
    """
    path = os.path.abspath(path)
    if is_git_repo(path):
        return path, []

    if not os.path.isdir(path):
        return None, []

    seen, candidates = set(), []
    for entry in sorted(os.listdir(path)):
        child = os.path.join(path, entry)
        if not os.path.isdir(child):
            continue
        toplevel = repo_toplevel(child)
        if not toplevel or toplevel != os.path.realpath(child):
            continue
        if toplevel in seen:
            continue
        seen.add(toplevel)
        candidates.append(child)

    if len(candidates) == 1:
        return candidates[0], candidates

    common_dirs = {common_git_dir(c) for c in candidates}
    if len(common_dirs) == 1 and None not in common_dirs:
        c_dir = common_dirs.pop()
        owner = os.path.dirname(c_dir)
        for candidate in candidates:
            if os.path.realpath(candidate) == owner:
                return candidate, candidates
        for candidate in candidates:
            if os.path.isdir(os.path.join(candidate, '.git')):
                return candidate, candidates
        return candidates[0], candidates

    return None, candidates


def get_remotes(path):
    out = git_out(path, 'remote')
    return [r.strip() for r in out.split('\n') if r.strip()] if out else []


def fetch_all(path, allow_stale=False, no_fetch=False):
    """Fetches every remote. Returns True when verdicts rest on fresh refs."""
    if no_fetch:
        print('Skipping fetch (--no-fetch): verdicts are based on local refs, '
              'which may be stale.')
        return False

    print('Fetching all remotes...')
    code, _, err = git(path, 'fetch', '--all', '--prune')
    if code == 0:
        return True

    message = err.strip() or 'unknown error'
    if allow_stale:
        print(f'Warning: fetch failed ({message}); continuing with stale refs '
              'because --allow-stale was given.', file=sys.stderr)
        return False

    print(f'Error: fetch failed: {message}\n'
          'Refusing to judge merge status against stale refs. '
          'Pass --allow-stale to override, or --no-fetch to work offline.',
          file=sys.stderr)
    sys.exit(EXIT_ERROR)


# --------------------------------------------------------------------------
# Ref identity
# --------------------------------------------------------------------------

def normalize_ref(path, name):
    """Resolves any spelling of a ref to (full_ref, oid).

    Accepts ``v1_0``, ``refs/heads/v1_0``, ``upstream/v1_0`` and
    ``refs/remotes/upstream/v1_0`` as the same ref. Returns (None, None) when
    the name does not resolve. Comparing the returned oid, rather than the
    name, is what prevents a branch being mistaken for a different ref that
    merely spells its name differently.
    """
    full = git_out(path, 'rev-parse', '--symbolic-full-name', name)
    if not full:
        return None, None
    # An ambiguous name can yield several lines; take the first.
    full = full.split('\n')[0].strip()
    if not full:
        return None, None
    oid = git_out(path, 'rev-parse', full + '^{commit}')
    if not oid:
        return None, None
    return full, oid


def ref_branch_name(ref):
    """Returns the branch name a full ref denotes.

    ``refs/heads/v1_0``, ``refs/remotes/upstream/v1_0`` and ``v1_0`` all name
    the same branch. Protection compares these short names so that a target
    resolved on a remote still protects the local branch that mirrors it.
    """
    if ref.startswith('refs/heads/'):
        return ref[len('refs/heads/'):]
    if ref.startswith('refs/remotes/'):
        rest = ref[len('refs/remotes/'):]
        _, _, name = rest.partition('/')
        return name or rest
    return ref.rsplit('/', 1)[-1]


def target_candidates(path, name, prefer_remote=True):
    """Lists every (ref, oid) a target name could mean, in preference order.

    A name like ``main`` can exist as a local branch and once per remote. A
    repository checked out from a fork carries both the fork's copy and the
    shared upstream's copy, and the two are rarely at the same commit. The
    name as written is considered too, so an explicit ``upstream/main``
    resolves to exactly that ref.
    """
    local, remote = [], []
    full, oid = normalize_ref(path, f'refs/heads/{name}')
    if full:
        local.append((full, oid))
    for remote_name in get_remotes(path):
        full, oid = normalize_ref(path, f'refs/remotes/{remote_name}/{name}')
        if full:
            remote.append((full, oid))

    ordered = (remote + local) if prefer_remote else (local + remote)
    literal, literal_oid = normalize_ref(path, name)
    if literal:
        ordered.append((literal, literal_oid))

    seen, unique = set(), []
    for ref, oid in ordered:
        if ref in seen:
            continue
        seen.add(ref)
        unique.append((ref, oid))
    return unique


def most_advanced(path, candidates):
    """Picks the candidate that contains all the others.

    Choosing by remote name would bake repository-specific assumptions into the
    skill, and choosing the first match picks whichever remote git happens to
    list first. Containment is the property that actually matters: a branch
    merged into the shared upstream is not yet merged into a fork that lags
    behind it, so classifying against the lagging copy reports false negatives.
    Falls back to the longest history when no candidate dominates, which is the
    case for genuinely divergent copies.
    """
    if not candidates:
        return None, None
    if len(candidates) == 1:
        return candidates[0]
    for ref, oid in candidates:
        others = [o for r, o in candidates if r != ref]
        if all(_signal_ancestor(path, other, oid) for other in others):
            return ref, oid
    best_ref, best_oid, best_count = candidates[0][0], candidates[0][1], -1
    for ref, oid in candidates:
        out = git_out(path, 'rev-list', '--count', oid)
        count = int(out) if out and out.isdigit() else 0
        if count > best_count:
            best_ref, best_oid, best_count = ref, oid, count
    return best_ref, best_oid


def resolve_target(path, name, prefer_remote=True):
    """Resolves a target branch name to the most advanced ref that spells it.

    Local refs drift behind their remotes, and a stale local target makes
    landed branches look unmerged. Preferring remote-tracking refs, then the
    most advanced among them, is the fix for that class of false negative.
    """
    candidates = target_candidates(path, name, prefer_remote=prefer_remote)
    if not candidates:
        return None, None
    preferred_kind = 'refs/remotes/' if prefer_remote else 'refs/heads/'
    preferred = [c for c in candidates if c[0].startswith(preferred_kind)]
    return most_advanced(path, preferred or candidates)


def detect_default_branch(path):
    """Detects the repository default branch without assuming a name."""
    for remote in get_remotes(path):
        out = git_out(path, 'symbolic-ref', '--short',
                      f'refs/remotes/{remote}/HEAD')
        if out:
            ref = out.strip()
            prefix = f'{remote}/'
            if ref.startswith(prefix):
                ref = ref[len(prefix):]
            if ref:
                return ref
    for name in ('main', 'master'):
        code, _, _ = git(path, 'show-ref', '--verify', '--quiet',
                         f'refs/heads/{name}')
        if code == 0:
            return name
    return None


def get_branches(path):
    """Lists local branches with upstream, worktree and tip oid."""
    fmt = ('%(refname:short)%09%(objectname)%09%(upstream:short)'
           '%09%(upstream:track)%09%(worktreepath)')
    code, out, err = git(path, 'for-each-ref', f'--format={fmt}', 'refs/heads')
    if code != 0:
        print(f'Error listing branches: {err.strip()}', file=sys.stderr)
        sys.exit(EXIT_ERROR)

    branches = []
    for line in out.split('\n'):
        if not line.strip():
            continue
        parts = line.split('\t')
        while len(parts) < 5:
            parts.append('')
        branches.append({
            'name': parts[0],
            'oid': parts[1],
            'upstream': parts[2],
            'track': parts[3],
            'worktree': parts[4],
        })
    return branches


def is_worktree_clean(worktree_path):
    """True when the worktree has no uncommitted or untracked changes."""
    if not worktree_path or not os.path.isdir(worktree_path):
        return True
    code, out, _ = git(worktree_path, 'status', '--porcelain')
    return code == 0 and not out.strip()


# --------------------------------------------------------------------------
# Protection
# --------------------------------------------------------------------------

def build_protected(path, branches, target_oids, protect_globs, open_pr_heads):
    """Returns {branch_name: reason} for every branch that must not be deleted.

    Identity is decided by branch name, taken from the resolved ref, so a
    target named ``upstream/v1_0`` protects the local branch ``v1_0`` and
    cannot be judged merged into itself. Matching on commit id instead would
    also catch any unrelated branch parked at the same tip, which belongs in
    the merged-and-deletable bucket rather than here.

    A branch checked out in the *main* worktree is protected because git will
    not let it be deleted. A branch in a *linked* worktree is not protected
    here: reaping a merged branch together with its worktree is the tool's
    main job. Linked worktrees holding uncommitted work are withheld later,
    once the merge verdict is known.
    """
    protected = {}
    main_worktree = os.path.realpath(path)

    target_names = {}
    for ref in target_oids.values():
        target_names.setdefault(ref_branch_name(ref), ref)

    remote_heads = {}
    for remote in get_remotes(path):
        ref, _ = normalize_ref(path, f'{remote}/HEAD')
        if ref:
            remote_heads.setdefault(ref_branch_name(ref), f'{remote}/HEAD')

    for branch in branches:
        name = branch['name']
        worktree = branch['worktree']

        if name in target_names:
            protected[name] = (f'is an integration target '
                               f'({target_names[name]})')
        elif name in remote_heads:
            protected[name] = (f'is a remote default branch '
                               f'({remote_heads[name]})')
        elif name in ('main', 'master'):
            protected[name] = f'is the conventional branch {name}'
        elif worktree and os.path.realpath(worktree) == main_worktree:
            protected[name] = 'checked out in the main worktree'
        elif name in open_pr_heads:
            protected[name] = (f'has an open pull request '
                               f'(#{open_pr_heads[name]})')
        else:
            for pattern in protect_globs:
                if fnmatch.fnmatchcase(name, pattern):
                    protected[name] = f'matches --protect {pattern!r}'
                    break

    return protected


# --------------------------------------------------------------------------
# Git evidence
# --------------------------------------------------------------------------

def _signal_ancestor(path, branch, target_ref):
    code, _, _ = git(path, 'merge-base', '--is-ancestor', branch, target_ref)
    return code == 0


def _signal_patch_ids(path, branch, target_ref):
    """True when git cherry finds no commit lacking an equivalent upstream."""
    code, out, _ = git(path, 'cherry', target_ref, branch)
    if code != 0 or not out.strip():
        return False
    return not any(l.strip().startswith('+') for l in out.strip().split('\n'))


def _is_oid(text):
    return bool(text) and all(c in '0123456789abcdef' for c in text) and (
        len(text) in (40, 64))


def _signal_squash_tree(path, branch, target_ref, have_merge_tree):
    """Detects a squash merge by merging the branch into the target in memory.

    A squash merge rewrites history, so neither ancestry nor patch ids match:
    the squash commit has a different parent and a different patch id from
    every commit it replaced. What survives is the tree. Merging the branch
    into the target produces the target's own tree exactly when the branch
    contributes nothing the target does not already have.
    """
    if not have_merge_tree:
        return False

    target_tree = git_out(path, 'rev-parse', f'{target_ref}^{{tree}}')
    if not target_tree:
        return False

    # A conflicting merge exits non-zero but still prints a tree on line one,
    # and that tree will not equal the target's, so the comparison decides.
    _, out, _ = git(path, 'merge-tree', '--write-tree', target_ref, branch)
    merged_tree = out.split('\n')[0].strip() if out else ''
    if not _is_oid(merged_tree):
        return False
    return merged_tree == target_tree


def classify_with_git(path, branch, targets, have_merge_tree):
    """Applies git-only signals. Returns (verdict, evidence) or (None, None)."""
    for target_name, (target_ref, _) in targets.items():
        if _signal_ancestor(path, branch, target_ref):
            return CONTAINED, f'ancestor of {target_name}'

        if _signal_patch_ids(path, branch, target_ref):
            return CONTAINED, (f'every commit has an equivalent patch in '
                               f'{target_name}')

        if _signal_squash_tree(path, branch, target_ref, have_merge_tree):
            return SQUASHED, f'squash-equivalent to {target_name}'

    return None, None


# --------------------------------------------------------------------------
# Forge (GitHub) evidence
# --------------------------------------------------------------------------

def gh_available(path):
    code, _, _ = run_cmd(['gh', 'auth', 'status'], cwd=path)
    return code == 0


def fetch_pull_requests(path, limit=500):
    """Fetches recent pull requests in one call, indexed by head branch name.

    One batched query keeps the common case to a single round trip. When the
    repository has more pull requests than `limit`, the index is marked
    truncated and individual branches fall back to targeted lookups.
    """
    code, out, err = run_cmd([
        'gh', 'pr', 'list', '--state', 'all', '--limit', str(limit),
        '--json', PR_FIELDS,
    ], cwd=path)
    if code != 0:
        print(f'Warning: could not list pull requests: {err.strip()}',
              file=sys.stderr)
        return None

    try:
        prs = json.loads(out)
    except (ValueError, TypeError):
        return None

    index = {}
    for pr in prs:
        index.setdefault(pr.get('headRefName'), []).append(pr)

    if len(prs) >= limit:
        index[TRUNCATED] = True
        print(f'Note: more than {limit} pull requests exist; branches missing '
              'from the first page are looked up individually.')
    return index


def open_pull_request_heads(pr_index):
    """Returns {branch_name: pr_number} for branches with an open PR."""
    heads = {}
    if not pr_index:
        return heads
    for head, prs in pr_index.items():
        if head == TRUNCATED:
            continue
        for pr in prs:
            if pr.get('state') == 'OPEN':
                heads[head] = pr.get('number')
                break
    return heads


def _merged_pull_requests(pr_index):
    for head, prs in (pr_index or {}).items():
        if head == TRUNCATED:
            continue
        for pr in prs:
            if pr.get('state') == 'MERGED':
                yield pr


def _account_for_commit(path, commit, merged_prs, exclude_number):
    """Finds a merged pull request that already contains `commit`."""
    for pr in merged_prs:
        if pr.get('number') == exclude_number:
            continue
        head_oid = pr.get('headRefOid')
        if not head_oid:
            continue
        if commit == head_oid:
            return pr.get('number')
        code, _, _ = git(path, 'merge-base', '--is-ancestor', commit, head_oid)
        if code == 0:
            return pr.get('number')
    return None


def _is_content_free_merge(path, commit, unresolved, have_merge_tree):
    """True when `commit` is a merge that adds nothing beyond its parents.

    Branches are routinely refreshed with a merge from the base after their
    pull request lands, which puts a commit on the branch that no pull request
    recorded. Such a merge carries no change of its own exactly when its tree
    is what git produces by merging its parents automatically; a conflict
    resolved by hand, or an edit folded into the merge, breaks that equality.
    The merge only counts once every parent has itself been accounted for,
    which `unresolved` tracks.
    """
    if not have_merge_tree:
        return False
    line = git_out(path, 'rev-list', '--parents', '-n', '1', commit)
    parents = (line or '').split()[1:]
    if len(parents) != 2 or any(p in unresolved for p in parents):
        return False
    tree = git_out(path, 'rev-parse', f'{commit}^{{tree}}')
    _, out, _ = git(path, 'merge-tree', '--write-tree', parents[0], parents[1])
    merged_tree = out.split('\n')[0].strip() if out else ''
    return _is_oid(merged_tree) and merged_tree == tree


def _classify_against_merged_pr(path, branch, tip, pr, merged_prs,
                                have_merge_tree):
    """Classifies a branch whose own pull request was merged.

    The authoritative question is not whether the local tip matches what
    GitHub recorded as the pull request head — rebases routinely break that,
    and the recorded head often is not present locally at all. It is whether
    the branch's content now lives in the pull request's base branch.
    """
    number = pr.get('number')
    base = pr.get('baseRefName')
    merge_commit = (pr.get('mergeCommit') or {}).get('oid')

    # GitHub names the base as a bare branch name. Resolving it the same way
    # as an integration target picks the most advanced copy, so a local
    # `main` that lags behind the remote it was merged into does not make
    # the merge look as if it never happened.
    base_ref, _ = resolve_target(path, base) if base else (None, None)
    if not base_ref or not merge_commit:
        return UNKNOWN, (f'pull request #{number} merged but its base or merge '
                         'commit is unavailable locally')

    code, _, _ = git(path, 'merge-base', '--is-ancestor', merge_commit, base_ref)
    if code != 0:
        return UNKNOWN, (f'pull request #{number} reports merged but its merge '
                         f'commit is not in {base}')

    # Strongest evidence: the branch is already reachable from the base.
    if _signal_ancestor(path, branch, base_ref):
        return CONTAINED, (f'pull request #{number} merged into {base}; '
                           f'branch is an ancestor of {base}')

    # Squash merges rewrite history, so compare content instead. This covers
    # the stacked case too: if everything on the branch folds into the base
    # with no residue, every commit in the stack landed.
    if _signal_squash_tree(path, branch, base_ref, have_merge_tree):
        return SQUASHED, (f'pull request #{number} merged into {base}; '
                          f'branch content is squash-equivalent to {base}')

    if _signal_patch_ids(path, branch, base_ref):
        return CONTAINED, (f'pull request #{number} merged into {base}; '
                           f'every commit has an equivalent patch in {base}')

    # Content is not fully in the base. Fall back to attributing the extra
    # commits to other merged pull requests in the stack.
    head_oid = pr.get('headRefOid')
    if not head_oid:
        return UNKNOWN, (f'pull request #{number} merged into {base}, but the '
                         f'branch content is not in {base} and its recorded '
                         'head is unknown')

    ahead = git_out(path, 'rev-list', f'{head_oid}..{tip}')
    commits = [c for c in (ahead or '').split('\n') if c.strip()]
    if not commits:
        return SQUASHED, (f'pull request #{number} merged into {base}; '
                          f'local branch tip matches merged pull request head')

    # Commits already in the base have landed by definition. Parents-first
    # order lets a merge commit be judged after the commits it merges.
    ahead = git_out(path, 'rev-list', '--topo-order', '--reverse',
                    f'{head_oid}..{tip}', f'^{base_ref}')
    commits = [c for c in (ahead or '').split('\n') if c.strip()]
    remaining = set(commits)

    accounted, unaccounted = [], []
    for commit in commits:
        owner = _account_for_commit(path, commit, merged_prs, number)
        if owner:
            accounted.append(owner)
            remaining.discard(commit)
        elif _is_content_free_merge(path, commit, remaining, have_merge_tree):
            remaining.discard(commit)
        else:
            unaccounted.append(commit)

    if unaccounted:
        short = ', '.join(c[:9] for c in unaccounted[:3])
        more = f' (+{len(unaccounted) - 3} more)' if len(unaccounted) > 3 else ''
        return UNKNOWN, (f'pull request #{number} merged into {base}, but '
                         f'{len(unaccounted)} commit(s) are unaccounted for: '
                         f'{short}{more}')

    if not accounted:
        return SQUASHED, (f'pull request #{number} merged into {base}; '
                          f'{len(commits)} ahead-commit(s) are merges of '
                          f'{base} that add nothing')

    stack = ', '.join(f'#{n}' for n in sorted(set(accounted)))
    return SQUASHED, (f'pull request #{number} merged into {base}; '
                      f'{len(commits)} ahead-commit(s) resolve to merged {stack}')


def _gh_json(path, args):
    """Runs a gh command returning JSON. Returns a list of records."""
    code, out, _ = run_cmd(['gh'] + args, cwd=path)
    if code != 0:
        return []
    try:
        payload = json.loads(out)
    except (ValueError, TypeError):
        return []
    return payload if isinstance(payload, list) else [payload]


def _lookup_pull_requests(path, branch, pr_index):
    """Finds pull requests for `branch`, falling back to targeted queries.

    The batched listing covers recent pull requests. Busy repositories have
    more than the batch limit, so a branch missing from the index gets its own
    lookup rather than being reported as unmerged on incomplete data.
    """
    prs = list((pr_index or {}).get(branch, []))
    if prs:
        return prs

    if pr_index is not None and pr_index.get(TRUNCATED):
        prs = _gh_json(path, ['pr', 'list', '--state', 'all', '--head', branch,
                              '--json', PR_FIELDS])
        if prs:
            return prs

    # A branch named for a pull request number matches no head ref.
    if branch.startswith('pr_') and branch[3:].isdigit():
        return _gh_json(path, ['pr', 'view', branch[3:], '--json', PR_FIELDS])

    return []


def classify_with_forge(path, branch, tip, pr_index, have_merge_tree):
    """Applies GitHub evidence. Returns (verdict, evidence) or (None, None)."""
    if pr_index is None:
        return None, None

    prs = _lookup_pull_requests(path, branch, pr_index)
    if not prs:
        return None, None

    merged_prs = list(_merged_pull_requests(pr_index))

    for pr in prs:
        if pr.get('state') == 'MERGED':
            return _classify_against_merged_pr(path, branch, tip, pr,
                                               merged_prs, have_merge_tree)

    if any(pr.get('state') == 'CLOSED' for pr in prs):
        numbers = ', '.join(f"#{p.get('number')}" for p in prs)
        return UNKNOWN, f'pull request(s) {numbers} closed without merging'

    return None, None


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def classify_branch(path, branch, targets, pr_index, have_merge_tree, use_forge):
    """Produces a (verdict, evidence) pair for one branch."""
    name = branch['name']

    verdict, evidence = classify_with_git(path, name, targets, have_merge_tree)
    if verdict:
        return verdict, evidence

    if use_forge:
        verdict, evidence = classify_with_forge(path, name, branch['oid'],
                                                pr_index, have_merge_tree)
        if verdict:
            return verdict, evidence
        return DIVERGED, 'no git or pull request evidence that it landed'

    if not have_merge_tree:
        return UNKNOWN, ('git is older than 2.38 so squash detection is '
                         'unavailable, and no pull request data was consulted')
    return UNKNOWN, 'no git evidence; pull request data unavailable'


def remote_allowed(path, remote, args):
    """True when deletions are permitted on `remote`.

    Deleting a branch on a shared remote affects other people, so it needs
    both the global switch and a named opt-in. A repository with a single
    remote has no ambiguity about which remote is meant, so naming it again
    adds nothing.
    """
    if not args.delete_remote:
        return False
    if remote in set(args.allow_remote):
        return True
    remotes = get_remotes(path)
    return len(remotes) == 1 and remotes[0] == remote


def plan_actions(path, branch, verdict, args):
    """Lists the actions that executing this entry would perform."""
    if verdict not in DELETABLE:
        return []
    actions = []
    if not args.no_archive:
        actions.append(f"tag {args.archive_tag_prefix}{branch['name']}")
    if branch['worktree']:
        actions.append(f"worktree remove {branch['worktree']}")
    actions.append(f"branch -D {branch['name']}")

    upstream = branch['upstream']
    if upstream and '[gone]' not in branch['track']:
        remote = upstream.split('/', 1)[0]
        if remote_allowed(path, remote, args):
            actions.append(f'push {upstream} --delete')
    return actions


def make_entry(branch, verdict, evidence, actions):
    return {
        'branch': branch['name'],
        'tip': branch['oid'],
        'verdict': verdict,
        'evidence': evidence,
        'worktree': branch['worktree'] or None,
        'worktree_dirty': False,
        'upstream': branch['upstream'] or None,
        'upstream_state': 'gone' if '[gone]' in branch['track'] else None,
        'actions': actions,
    }


def resolve_target_names(path, args):
    if args.targets:
        names = []
        for chunk in args.targets:
            names.extend(t.strip() for t in chunk.split(',') if t.strip())
        return names
    detected = detect_default_branch(path)
    if not detected:
        print('Error: could not determine the default branch. '
              'Pass --targets explicitly.', file=sys.stderr)
        sys.exit(EXIT_ERROR)
    return [detected]


def resolve_targets(path, args):
    """Resolves every target name to (ref, oid). Exits when one is missing."""
    targets, target_oids = {}, {}
    for name in resolve_target_names(path, args):
        ref, oid = resolve_target(
            path, name, prefer_remote=(args.target_ref == 'remote'))
        if not ref:
            print(f'Error: target branch not found: {name}', file=sys.stderr)
            sys.exit(EXIT_ERROR)
        targets[name] = (ref, oid)
        target_oids[oid] = ref
    return targets, target_oids


def passes_filters(name, args):
    for pattern in args.exclude:
        if fnmatch.fnmatchcase(name, pattern):
            return False
    if args.only:
        return any(fnmatch.fnmatchcase(name, p) for p in args.only)
    return True


def load_forge(path, args):
    """Returns (pr_index, use_forge)."""
    if args.no_forge or not gh_available(path):
        return None, False
    pr_index = fetch_pull_requests(path)
    return pr_index, pr_index is not None


def build_plan(path, args, fresh):
    """Inspects the repository and returns the plan dictionary."""
    branches = get_branches(path)
    targets, target_oids = resolve_targets(path, args)

    version = git_version(path)
    have_merge_tree = bool(version) and version >= MERGE_TREE_MIN_VERSION
    if not have_merge_tree:
        print(f'Warning: git {version} is older than 2.38; squash detection is '
              'unavailable and affected branches report UNKNOWN.',
              file=sys.stderr)

    pr_index, use_forge = load_forge(path, args)
    if not use_forge:
        print('Pull request data unavailable; relying on git evidence only.')

    protected = build_protected(path, branches, target_oids, args.protect,
                                open_pull_request_heads(pr_index))

    described = ', '.join(f'{n} -> {r}' for n, (r, _) in targets.items())
    print(f'Targets: {described}')
    print(f'Protected: {len(protected)} branch(es)')
    for name, reason in sorted(protected.items()):
        print(f'  {name}: {reason}')
    print()

    entries = []
    for branch in branches:
        name = branch['name']

        if name in protected:
            entries.append(make_entry(branch, PROTECTED, protected[name], []))
            continue
        if not passes_filters(name, args):
            continue

        verdict, evidence = classify_branch(path, branch, targets, pr_index,
                                            have_merge_tree, use_forge)

        dirty = (bool(branch['worktree'])
                 and not is_worktree_clean(branch['worktree']))
        if dirty and verdict in DELETABLE:
            verdict = PROTECTED
            evidence = (f"{evidence}; withheld because worktree "
                        f"{branch['worktree']} has uncommitted changes")

        actions = plan_actions(path, branch, verdict, args)
        entry = make_entry(branch, verdict, evidence, actions)
        entry['worktree_dirty'] = dirty
        entries.append(entry)

    return {
        'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'repo': path,
        'targets': {n: {'ref': r, 'oid': o} for n, (r, o) in targets.items()},
        'refs_fresh': fresh,
        'forge': use_forge,
        'entries': entries,
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

HEADINGS = {
    CONTAINED: 'Deletable — contained',
    SQUASHED: 'Deletable — squash merged',
    UNKNOWN: 'Needs review',
    DIVERGED: 'Not merged',
    PROTECTED: 'Protected',
}

VERDICT_ORDER = (CONTAINED, SQUASHED, UNKNOWN, DIVERGED, PROTECTED)


def render_markdown(plan):
    lines = ['# Branch cleanup plan', '']
    lines.append(f"- Repository: `{plan['repo']}`")
    lines.append(f"- Created: {plan['created']}")
    lines.append(f"- Refs fresh: {plan['refs_fresh']}")
    lines.append(f"- Pull request data: {'yes' if plan['forge'] else 'no'}")
    lines.append('')

    buckets = {}
    for entry in plan['entries']:
        buckets.setdefault(entry['verdict'], []).append(entry)

    for verdict in VERDICT_ORDER:
        items = buckets.get(verdict, [])
        if not items:
            continue
        lines.append(f'## {HEADINGS[verdict]} ({len(items)})')
        lines.append('')
        for entry in sorted(items, key=lambda e: e['branch']):
            lines.append(f"- **{entry['branch']}** `{entry['tip'][:9]}`")
            lines.append(f"  - {entry['evidence']}")
            if entry['worktree']:
                dirty = ' (uncommitted changes)' if entry['worktree_dirty'] else ''
                lines.append(f"  - worktree: `{entry['worktree']}`{dirty}")
            for action in entry['actions']:
                lines.append(f'  - action: `git {action}`')
        lines.append('')

    return '\n'.join(lines)


def print_summary(plan):
    counts = {}
    for entry in plan['entries']:
        counts[entry['verdict']] = counts.get(entry['verdict'], 0) + 1
    print('--- Summary ---')
    for verdict in VERDICT_ORDER:
        if counts.get(verdict):
            print(f'  {verdict:<10} {counts[verdict]}')
    needs_review = counts.get(UNKNOWN, 0)
    if needs_review:
        print(f'\n{needs_review} branch(es) need review; they are never '
              'deleted automatically.')


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def revalidate(path, plan):
    """Returns a list of branches whose tip no longer matches the plan."""
    drifted = []
    for entry in plan['entries']:
        if entry['verdict'] not in DELETABLE:
            continue
        _, oid = normalize_ref(path, entry['branch'])
        if oid != entry['tip']:
            drifted.append((entry['branch'], entry['tip'], oid))
    return drifted


def archive_branch(path, name, oid, prefix):
    tag = f'{prefix}{name}'
    code, _, err = git(path, 'tag', '-f', tag, oid)
    if code != 0:
        print(f'Error: could not tag {tag}: {err.strip()}', file=sys.stderr)
        return False
    print(f'  archived as {tag}')
    return True


def maybe_delete_remote(path, entry, args):
    upstream = entry.get('upstream')
    if (not args.delete_remote or not upstream
            or entry.get('upstream_state') == 'gone'):
        return False

    remote, _, remote_branch = upstream.partition('/')
    if not remote_branch:
        return False

    if not remote_allowed(path, remote, args):
        print(f'  not deleting {upstream}: pass --allow-remote {remote} to '
              'permit deletions on that remote')
        return False

    code, _, err = git(path, 'push', remote, '--delete', remote_branch)
    if code != 0:
        print(f'  warning: could not delete {upstream}: {err.strip()}',
              file=sys.stderr)
        return False
    print(f'  deleted remote branch {upstream}')
    return True


def execute_entry(path, entry, args):
    """Deletes one branch. Returns a dict of what succeeded."""
    name = entry['branch']
    done = {'archived': False, 'worktree': False, 'branch': False,
            'remote': False}
    print(f"{name} ({entry['verdict']}: {entry['evidence']})")

    if not args.no_archive:
        done['archived'] = archive_branch(path, name, entry['tip'],
                                          args.archive_tag_prefix)
        if not done['archived']:
            print('  skipping deletion because archiving failed')
            return done

    if entry['worktree']:
        if not is_worktree_clean(entry['worktree']):
            print('  skipping: worktree gained uncommitted changes')
            return done
        code, _, err = git(path, 'worktree', 'remove', entry['worktree'])
        if code != 0:
            print(f'  error removing worktree: {err.strip()}', file=sys.stderr)
            return done
        done['worktree'] = True
        print(f"  removed worktree {entry['worktree']}")

    code, _, err = git(path, 'branch', '-D', name)
    if code != 0:
        print(f'  error deleting branch: {err.strip()}', file=sys.stderr)
        return done
    done['branch'] = True
    print('  deleted local branch')

    done['remote'] = maybe_delete_remote(path, entry, args)
    return done


def load_plan(plan_path):
    try:
        with open(plan_path, encoding='utf-8') as handle:
            return json.load(handle), None
    except (OSError, ValueError) as exc:
        return None, str(exc)


def cmd_execute(args):
    plan_path = os.path.abspath(args.execute)
    plan, error = load_plan(plan_path)
    if error:
        print(f'Error: could not read plan {plan_path}: {error}',
              file=sys.stderr)
        return EXIT_ERROR

    path = plan['repo']
    if not is_git_repo(path):
        print(f'Error: plan repository is missing: {path}', file=sys.stderr)
        return EXIT_ERROR

    fetch_all(path, args.allow_stale, args.no_fetch)

    drifted = revalidate(path, plan)
    if drifted:
        print('Refusing to execute: the repository moved since the plan was '
              'written.', file=sys.stderr)
        for name, planned, actual in drifted:
            actual = actual or 'missing'
            print(f'  {name}: planned {planned[:9]}, now {actual[:9]}',
                  file=sys.stderr)
        print('Re-run analyze to produce a fresh plan.', file=sys.stderr)
        return EXIT_PLAN_STALE

    candidates = [e for e in plan['entries'] if e['verdict'] in DELETABLE]
    if not candidates:
        print('Plan contains no deletable branches.')
        return EXIT_OK

    print(f'Executing {len(candidates)} deletion(s) from {plan_path}\n')
    totals = {'archived': 0, 'worktree': 0, 'branch': 0, 'remote': 0}
    for entry in candidates:
        result = execute_entry(path, entry, args)
        for key, value in result.items():
            totals[key] += 1 if value else 0

    print(f"\n--- Summary ---\n"
          f"  branches deleted : {totals['branch']}\n"
          f"  worktrees removed: {totals['worktree']}\n"
          f"  remotes deleted  : {totals['remote']}\n"
          f"  archive tags     : {totals['archived']}")
    if totals['archived']:
        print('\nRestore any branch with: '
              f'git branch <name> {args.archive_tag_prefix}<name>')
    return EXIT_OK


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def cmd_analyze(args):
    path, candidates = find_repo(args.repo_dir)
    if not path:
        if candidates:
            print(f'Error: {args.repo_dir} is not a repository, but it '
                  'contains several:', file=sys.stderr)
            for candidate in candidates:
                print(f'  {candidate}', file=sys.stderr)
            print('Re-run with --repo-dir set to one of them.', file=sys.stderr)
            return EXIT_AMBIGUOUS_REPO
        print(f'Error: {args.repo_dir} is not a git repository.',
              file=sys.stderr)
        return EXIT_ERROR

    if path != os.path.abspath(args.repo_dir):
        print(f'Using repository {path}')

    fresh = fetch_all(path, args.allow_stale, args.no_fetch)
    plan = build_plan(path, args, fresh)

    if args.json:
        print(json.dumps(plan, indent=2))
        return EXIT_OK

    out_path = args.out or os.path.join(path, '.git',
                                        'branch-cleanup-plan.json')
    out_path = os.path.abspath(out_path)
    md_path = os.path.splitext(out_path)[0] + '.md'
    try:
        with open(out_path, 'w', encoding='utf-8') as handle:
            json.dump(plan, handle, indent=2)
        with open(md_path, 'w', encoding='utf-8') as handle:
            handle.write(render_markdown(plan))
    except OSError as exc:
        print(f'Error: could not write plan: {exc}', file=sys.stderr)
        return EXIT_ERROR

    print(render_markdown(plan))
    print_summary(plan)
    print(f'\nPlan written to {out_path}')
    print(f'Review it, then run: {os.path.basename(sys.argv[0])} '
          f'--execute {out_path}')
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        description='Analyze and clean up merged git branches.',
        epilog='Run analyze first, review the plan, then --execute it.')

    parser.add_argument('mode', nargs='?', default='analyze',
                        choices=['analyze'],
                        help='Analyze the repository (default).')
    parser.add_argument('--execute', metavar='PLAN',
                        help='Execute a plan produced by analyze.')

    parser.add_argument('--repo-dir', default=os.getcwd(),
                        help='Repository path. If it only contains worktrees, '
                             'the single repository below it is used.')
    parser.add_argument('--targets', '--main-branch', action='append', default=[],
                        dest='targets',
                        help='Comma-separated integration branches. Defaults '
                             'to the detected default branch. Repeatable.')
    parser.add_argument('--target-ref', choices=['local', 'remote'],
                        default='remote',
                        help='Resolve targets to remote-tracking refs '
                             '(default) or local branches.')

    parser.add_argument('--protect', action='append', default=[],
                        metavar='GLOB',
                        help='Never delete branches matching this glob. '
                             'Repeatable.')
    parser.add_argument('--only', action='append', default=[], metavar='GLOB',
                        help='Consider only branches matching this glob. '
                             'Repeatable.')
    parser.add_argument('--exclude', action='append', default=[],
                        metavar='GLOB',
                        help='Skip branches matching this glob. Repeatable.')

    parser.add_argument('--delete-remote', action='store_true',
                        help='Allow deleting remote tracking branches.')
    parser.add_argument('--allow-remote', action='append', default=[],
                        metavar='NAME',
                        help='Permit remote deletions on this remote. Required '
                             'when the repository has more than one remote.')

    parser.add_argument('--archive-tag-prefix', default='archive/',
                        help='Prefix for recovery tags (default: archive/).')
    parser.add_argument('--no-archive', action='store_true',
                        help='Do not create recovery tags before deleting.')

    parser.add_argument('--no-fetch', action='store_true',
                        help='Do not fetch; judge against local refs.')
    parser.add_argument('--allow-stale', action='store_true',
                        help='Continue when fetching fails.')
    parser.add_argument('--no-forge', action='store_true',
                        help='Skip the GitHub pull request pass.')

    parser.add_argument('--out', metavar='PATH',
                        help='Where to write the plan '
                             '(default: <repo>/.git/branch-cleanup-plan.json).')
    parser.add_argument('--json', action='store_true',
                        help='Print the plan as JSON instead of writing files.')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.execute:
        return cmd_execute(args)
    return cmd_analyze(args)


if __name__ == '__main__':
    sys.exit(main())
