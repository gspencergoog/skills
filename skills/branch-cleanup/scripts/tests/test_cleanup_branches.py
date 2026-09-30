#!/usr/bin/env python3
"""Tests for cleanup_branches.

Every test builds a real git repository in a temporary directory. Merge
detection is the thing being tested, and a mocked git would only prove that
the mock agrees with itself. The GitHub CLI is the one exception: the forge
tests replace `run_cmd` so the suite needs no network and no credentials.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..')))

import cleanup_branches as cb  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_SYSTEM=os.devnull,
    GIT_AUTHOR_NAME='Test',
    GIT_AUTHOR_EMAIL='test@example.com',
    GIT_COMMITTER_NAME='Test',
    GIT_COMMITTER_EMAIL='test@example.com',
    GIT_AUTHOR_DATE='2001-02-03T04:05:06+00:00',
    GIT_COMMITTER_DATE='2001-02-03T04:05:06+00:00',
)


def run(path, *args):
    """Runs git in `path` and fails loudly, so broken fixtures are obvious."""
    result = subprocess.run(['git'] + list(args), cwd=path, env=ENV,
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f'git {" ".join(args)} failed in {path}: {result.stderr.strip()}')
    return result.stdout.strip()


def write(path, name, text):
    full = os.path.join(path, name)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w', encoding='utf-8') as handle:
        handle.write(text)


def commit(path, name, text, message=None):
    write(path, name, text)
    run(path, 'add', '-A')
    run(path, 'commit', '-m', message or f'add {name}')
    return run(path, 'rev-parse', 'HEAD')


def init_repo(path):
    os.makedirs(path, exist_ok=True)
    run(path, 'init', '-q', '-b', 'main')
    commit(path, 'README.md', 'base\n', 'initial commit')
    return path


class Args:
    """Stands in for parsed command-line arguments."""

    def __init__(self, **overrides):
        self.repo_dir = '.'
        self.targets = []
        self.target_ref = 'remote'
        self.protect = []
        self.only = []
        self.exclude = []
        self.delete_remote = False
        self.allow_remote = []
        self.archive_tag_prefix = 'archive/'
        self.no_archive = False
        self.no_fetch = True
        self.allow_stale = False
        self.no_forge = True
        self.out = None
        self.json = False
        self.execute = None
        for key, value in overrides.items():
            setattr(self, key, value)


class TempRepoCase(unittest.TestCase):
    """Base class giving each test its own scratch directory."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='branch-cleanup-test-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.have_merge_tree = (cb.git_version(self.root) or (0, 0)) >= \
            cb.MERGE_TREE_MIN_VERSION

    def repo(self, name='repo'):
        return init_repo(os.path.join(self.root, name))

    def quietly(self, func, *args, **kwargs):
        """Calls `func` with stdout and stderr captured.

        The tool narrates what it is doing, which is useful in a terminal and
        noise in a test run. Returns (result, stdout_text).
        """
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            result = func(*args, **kwargs)
        return result, out.getvalue()


# --------------------------------------------------------------------------
# Process helpers and version parsing
# --------------------------------------------------------------------------

class ProcessHelperTest(TempRepoCase):

    def test_git_out_returns_none_on_failure(self):
        path = self.repo()
        self.assertIsNone(cb.git_out(path, 'rev-parse', 'no-such-ref'))

    def test_git_version_parses_vendor_suffixes(self):
        original = cb.git_out
        cb.git_out = lambda *a, **k: 'git version 2.39.3 (Apple Git-146)'
        try:
            self.assertEqual(cb.git_version('.'), (2, 39))
        finally:
            cb.git_out = original

    def test_git_version_returns_none_when_unparsable(self):
        original = cb.git_out
        cb.git_out = lambda *a, **k: 'not a version'
        try:
            self.assertIsNone(cb.git_version('.'))
        finally:
            cb.git_out = original


# --------------------------------------------------------------------------
# Repository discovery
# --------------------------------------------------------------------------

class FindRepoTest(TempRepoCase):

    def test_repository_resolves_to_itself(self):
        path = self.repo()
        found, candidates = cb.find_repo(path)
        self.assertEqual(found, path)
        self.assertEqual(candidates, [])

    def test_container_with_one_repository_resolves_to_it(self):
        container = os.path.join(self.root, 'workspace')
        os.makedirs(container)
        inner = init_repo(os.path.join(container, 'main'))
        found, candidates = cb.find_repo(container)
        self.assertEqual(found, inner)
        self.assertEqual(candidates, [inner])

    def test_container_of_worktrees_resolves_to_the_owning_repository(self):
        """Sibling worktrees are one repository, not several."""
        container = os.path.join(self.root, 'workspace')
        os.makedirs(container)
        inner = init_repo(os.path.join(container, 'main'))
        run(inner, 'branch', 'feature')
        run(inner, 'worktree', 'add', '-q',
            os.path.join(container, 'feature'), 'feature')
        found, candidates = cb.find_repo(container)
        self.assertEqual(found, inner)
        self.assertEqual(len(candidates), 2)

    def test_container_with_several_repositories_is_ambiguous(self):
        container = os.path.join(self.root, 'workspace')
        os.makedirs(container)
        first = init_repo(os.path.join(container, 'one'))
        second = init_repo(os.path.join(container, 'two'))
        found, candidates = cb.find_repo(container)
        self.assertIsNone(found)
        self.assertEqual(sorted(candidates), sorted([first, second]))

    def test_symlink_into_a_listed_repository_is_not_a_candidate(self):
        container = os.path.join(self.root, 'workspace')
        os.makedirs(container)
        inner = init_repo(os.path.join(container, 'main'))
        os.makedirs(os.path.join(inner, 'docs'))
        os.symlink(os.path.join(inner, 'docs'),
                   os.path.join(container, 'docs-link'))
        found, candidates = cb.find_repo(container)
        self.assertEqual(found, inner)
        self.assertEqual(candidates, [inner])

    def test_missing_path_is_not_a_repository(self):
        found, candidates = cb.find_repo(os.path.join(self.root, 'absent'))
        self.assertIsNone(found)
        self.assertEqual(candidates, [])


# --------------------------------------------------------------------------
# Ref identity
# --------------------------------------------------------------------------

class RefIdentityTest(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.origin = self.repo('origin')
        self.path = os.path.join(self.root, 'clone')
        run(self.root, 'clone', '-q', self.origin, self.path)

    def test_every_spelling_of_a_ref_yields_the_same_oid(self):
        oid = run(self.path, 'rev-parse', 'refs/heads/main')
        for name in ('main', 'refs/heads/main'):
            full, resolved = cb.normalize_ref(self.path, name)
            self.assertEqual(full, 'refs/heads/main')
            self.assertEqual(resolved, oid)
        for name in ('origin/main', 'refs/remotes/origin/main'):
            full, resolved = cb.normalize_ref(self.path, name)
            self.assertEqual(full, 'refs/remotes/origin/main')
            self.assertEqual(resolved, oid)

    def test_unknown_ref_resolves_to_nothing(self):
        self.assertEqual(cb.normalize_ref(self.path, 'no-such-branch'),
                         (None, None))

    def test_candidates_list_local_and_every_remote(self):
        candidates = cb.target_candidates(self.path, 'main')
        refs = [ref for ref, _ in candidates]
        self.assertEqual(refs, ['refs/remotes/origin/main', 'refs/heads/main'])

    def test_local_preference_reverses_the_order(self):
        candidates = cb.target_candidates(self.path, 'main',
                                          prefer_remote=False)
        refs = [ref for ref, _ in candidates]
        self.assertEqual(refs, ['refs/heads/main', 'refs/remotes/origin/main'])


class MostAdvancedTest(TempRepoCase):
    """A fork that lags behind upstream must not become the target.

    This is the defect that made the tool report nothing to clean up: it
    resolved `main` against a fork whose copy predated every merge.
    """

    def setUp(self):
        super().setUp()
        self.upstream = self.repo('upstream')
        # `origin` is the fork: a clone taken before upstream moved on.
        self.fork = os.path.join(self.root, 'fork')
        run(self.root, 'clone', '-q', self.upstream, self.fork)
        self.behind = run(self.fork, 'rev-parse', 'origin/main')

        run(self.fork, 'remote', 'add', 'upstream', self.upstream)
        commit(self.upstream, 'feature.txt', 'landed\n')
        run(self.fork, 'fetch', '-q', 'upstream')
        self.ahead = run(self.fork, 'rev-parse', 'upstream/main')

    def test_the_containing_candidate_wins(self):
        ref, oid = cb.resolve_target(self.fork, 'main')
        self.assertEqual(ref, 'refs/remotes/upstream/main')
        self.assertEqual(oid, self.ahead)
        self.assertNotEqual(oid, self.behind)

    def test_local_preference_still_selects_the_local_ref(self):
        ref, _ = cb.resolve_target(self.fork, 'main', prefer_remote=False)
        self.assertEqual(ref, 'refs/heads/main')

    def test_divergent_candidates_fall_back_to_the_longer_history(self):
        run(self.fork, 'checkout', '-q', 'main')
        commit(self.fork, 'local-only.txt', 'one\n')
        commit(self.fork, 'local-only-2.txt', 'two\n')
        commit(self.fork, 'local-only-3.txt', 'three\n')
        longest = run(self.fork, 'rev-parse', 'main')
        candidates = cb.target_candidates(self.fork, 'main')
        _, oid = cb.most_advanced(self.fork, candidates)
        self.assertEqual(oid, longest)

    def test_no_candidates_resolve_to_nothing(self):
        self.assertEqual(cb.most_advanced(self.fork, []), (None, None))
        self.assertEqual(cb.resolve_target(self.fork, 'nope'), (None, None))


class DefaultBranchTest(TempRepoCase):

    def test_remote_head_names_the_default_branch(self):
        origin = self.repo('origin')
        run(origin, 'branch', '-m', 'main', 'trunk')
        path = os.path.join(self.root, 'clone')
        run(self.root, 'clone', '-q', origin, path)
        self.assertEqual(cb.detect_default_branch(path), 'trunk')

    def test_conventional_names_are_the_last_resort(self):
        path = self.repo()
        self.assertEqual(cb.detect_default_branch(path), 'main')

    def test_unconventional_name_without_a_remote_is_undetectable(self):
        path = self.repo()
        run(path, 'branch', '-m', 'main', 'trunk')
        self.assertIsNone(cb.detect_default_branch(path))


# --------------------------------------------------------------------------
# Merge detection
# --------------------------------------------------------------------------

class GitSignalTest(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.path = self.repo()
        self.targets = {'main': ('refs/heads/main',
                                 run(self.path, 'rev-parse', 'main'))}

    def classify(self, branch):
        targets = {'main': ('refs/heads/main',
                            run(self.path, 'rev-parse', 'main'))}
        return cb.classify_with_git(self.path, branch, targets,
                                    self.have_merge_tree)

    def test_merged_branch_is_contained(self):
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'feature.txt', 'work\n')
        run(self.path, 'checkout', '-q', 'main')
        run(self.path, 'merge', '-q', '--no-ff', '-m', 'merge', 'feature')
        verdict, evidence = self.classify('feature')
        self.assertEqual(verdict, cb.CONTAINED)
        self.assertIn('ancestor', evidence)

    def test_cherry_picked_branch_is_contained_by_patch_id(self):
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'feature.txt', 'work\n')
        picked = run(self.path, 'rev-parse', 'HEAD')
        run(self.path, 'checkout', '-q', 'main')
        commit(self.path, 'unrelated.txt', 'other\n')
        run(self.path, 'cherry-pick', picked)
        verdict, evidence = self.classify('feature')
        self.assertEqual(verdict, cb.CONTAINED)
        self.assertIn('equivalent patch', evidence)

    def test_squash_merged_branch_is_squashed(self):
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'a.txt', 'one\n')
        commit(self.path, 'b.txt', 'two\n')
        run(self.path, 'checkout', '-q', 'main')
        run(self.path, 'merge', '-q', '--squash', 'feature')
        run(self.path, 'commit', '-q', '-m', 'squashed feature (#1)')
        verdict, evidence = self.classify('feature')
        self.assertEqual(verdict, cb.SQUASHED)
        self.assertIn('squash-equivalent', evidence)

    def test_squash_detection_survives_later_target_commits(self):
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'a.txt', 'one\n')
        commit(self.path, 'b.txt', 'two\n')
        run(self.path, 'checkout', '-q', 'main')
        run(self.path, 'merge', '-q', '--squash', 'feature')
        run(self.path, 'commit', '-q', '-m', 'squashed feature (#1)')
        commit(self.path, 'later.txt', 'after the squash\n')
        verdict, _ = self.classify('feature')
        self.assertEqual(verdict, cb.SQUASHED)

    def test_unmerged_branch_produces_no_verdict(self):
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'feature.txt', 'work\n')
        run(self.path, 'checkout', '-q', 'main')
        self.assertEqual(self.classify('feature'), (None, None))

    def test_conflicting_branch_produces_no_verdict(self):
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'shared.txt', 'branch version\n')
        run(self.path, 'checkout', '-q', 'main')
        commit(self.path, 'shared.txt', 'main version\n')
        self.assertEqual(self.classify('feature'), (None, None))

    def test_squash_detection_is_disabled_on_old_git(self):
        """Without the squash signal the branch is reported as unknown.

        Reporting it as unmerged would be worse than admitting ignorance:
        the content did land, git 2.37 just cannot show that it did.
        """
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'a.txt', 'one\n')
        commit(self.path, 'b.txt', 'two\n')
        run(self.path, 'checkout', '-q', 'main')
        run(self.path, 'merge', '-q', '--squash', 'feature')
        run(self.path, 'commit', '-q', '-m', 'squashed')
        self.assertFalse(cb._signal_squash_tree(self.path, 'feature', 'main',
                                                False))
        verdict, evidence = cb.classify_with_git(
            self.path, 'feature',
            {'main': ('refs/heads/main', run(self.path, 'rev-parse', 'main'))},
            False)
        self.assertIsNone(verdict)
        self.assertIsNone(evidence)

    def test_second_target_is_consulted(self):
        run(self.path, 'checkout', '-q', '-b', 'release')
        commit(self.path, 'release.txt', 'rel\n')
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'feature-a.txt', 'first\n')
        commit(self.path, 'feature-b.txt', 'second\n')
        run(self.path, 'checkout', '-q', 'release')
        run(self.path, 'merge', '-q', '--squash', 'feature')
        run(self.path, 'commit', '-q', '-m', 'squashed into release')
        targets = {
            'main': ('refs/heads/main', run(self.path, 'rev-parse', 'main')),
            'release': ('refs/heads/release',
                        run(self.path, 'rev-parse', 'release')),
        }
        verdict, evidence = cb.classify_with_git(self.path, 'feature', targets,
                                                 self.have_merge_tree)
        self.assertEqual(verdict, cb.SQUASHED)
        self.assertIn('release', evidence)

    def test_oid_shaped_strings_are_recognised(self):
        self.assertTrue(cb._is_oid('a' * 40))
        self.assertTrue(cb._is_oid('0' * 64))
        self.assertFalse(cb._is_oid(''))
        self.assertFalse(cb._is_oid('z' * 40))
        self.assertFalse(cb._is_oid('abc'))


# --------------------------------------------------------------------------
# Protection
# --------------------------------------------------------------------------

class ProtectionTest(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.path = self.repo()

    def branches(self):
        return cb.get_branches(self.path)

    def test_a_branch_is_never_merged_into_itself(self):
        """The target's own local branch is protected, whatever it is called.

        Protection is keyed on object id, so a target resolved as
        `upstream/release` still protects the local branch `release`.
        """
        run(self.path, 'branch', 'release')
        oid = run(self.path, 'rev-parse', 'release')
        protected = cb.build_protected(self.path, self.branches(),
                                       {oid: 'refs/remotes/upstream/release'},
                                       [], {})
        self.assertIn('release', protected)
        self.assertIn('integration target', protected['release'])

    def test_branch_in_the_main_worktree_is_protected(self):
        commit(self.path, 'x.txt', 'x\n')
        run(self.path, 'checkout', '-q', '-b', 'wip')
        protected = cb.build_protected(self.path, self.branches(), {}, [], {})
        self.assertEqual(protected.get('wip'), 'checked out in the main '
                                                'worktree')

    def test_branch_in_a_linked_worktree_is_reapable(self):
        run(self.path, 'branch', 'feature')
        linked = os.path.join(self.root, 'feature-tree')
        run(self.path, 'worktree', 'add', '-q', linked, 'feature')
        protected = cb.build_protected(self.path, self.branches(), {}, [], {})
        self.assertNotIn('feature', protected)

    def test_untracked_files_make_a_worktree_dirty(self):
        """Untracked files count: deleting the worktree would lose them."""
        run(self.path, 'branch', 'feature')
        linked = os.path.join(self.root, 'feature-tree')
        run(self.path, 'worktree', 'add', '-q', linked, 'feature')
        self.assertTrue(cb.is_worktree_clean(linked))
        write(linked, 'scratch.txt', 'not committed\n')
        self.assertFalse(cb.is_worktree_clean(linked))

    def test_a_missing_worktree_path_counts_as_clean(self):
        self.assertTrue(cb.is_worktree_clean(''))
        self.assertTrue(cb.is_worktree_clean(
            os.path.join(self.root, 'not-there')))

    def test_open_pull_request_protects_its_head(self):
        run(self.path, 'branch', 'feature')
        protected = cb.build_protected(self.path, self.branches(), {}, [],
                                       {'feature': 17})
        self.assertIn('#17', protected['feature'])

    def test_protect_glob_is_honoured(self):
        run(self.path, 'branch', 'release-1.2')
        protected = cb.build_protected(self.path, self.branches(), {},
                                       ['release-*'], {})
        self.assertIn('release-*', protected['release-1.2'])

    def test_conventional_branch_is_protected_without_a_remote(self):
        commit(self.path, 'x.txt', 'x\n')
        run(self.path, 'checkout', '-q', '-b', 'side')
        protected = cb.build_protected(self.path, self.branches(), {}, [], {})
        self.assertIn('main', protected)

    def test_remote_head_is_protected(self):
        origin = self.repo('origin')
        run(origin, 'branch', '-m', 'main', 'trunk')
        clone = os.path.join(self.root, 'clone')
        run(self.root, 'clone', '-q', origin, clone)
        run(clone, 'checkout', '-q', '-b', 'side')
        protected = cb.build_protected(clone, cb.get_branches(clone), {}, [],
                                       {})
        self.assertIn('remote default branch', protected['trunk'])


# --------------------------------------------------------------------------
# Forge evidence
# --------------------------------------------------------------------------

class FakeGh:
    """Replaces `run_cmd` so the forge pass runs without network access."""

    def __init__(self, responses, real=None):
        self.responses = responses
        self.real = real or cb.run_cmd
        self.calls = []

    def __call__(self, args, cwd=None):
        if args and args[0] == 'gh':
            self.calls.append(args)
            for match, payload in self.responses:
                if all(token in args for token in match):
                    return 0, json.dumps(payload), ''
            return 1, '', 'no stub matched'
        return self.real(args, cwd=cwd)


class ForgeTest(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.path = self.repo()
        self.original_run_cmd = cb.run_cmd
        self.addCleanup(setattr, cb, 'run_cmd', self.original_run_cmd)

    def stub(self, responses):
        cb.run_cmd = FakeGh(responses, real=self.original_run_cmd)
        return cb.run_cmd

    def squash_merge(self, branch, files, message):
        run(self.path, 'checkout', '-q', '-b', branch)
        for name, text in files.items():
            commit(self.path, name, text)
        tip = run(self.path, 'rev-parse', 'HEAD')
        run(self.path, 'checkout', '-q', 'main')
        run(self.path, 'merge', '-q', '--squash', branch)
        run(self.path, 'commit', '-q', '-m', message)
        return tip, run(self.path, 'rev-parse', 'main')

    def test_merged_pull_request_classifies_a_squashed_branch(self):
        if not self.have_merge_tree:
            self.skipTest('git is older than 2.38')
        tip, merge_commit = self.squash_merge(
            'feature', {'a.txt': 'one\n'}, 'feature (#7)')
        index = {'feature': [{
            'number': 7, 'state': 'MERGED', 'headRefName': 'feature',
            'baseRefName': 'main', 'headRefOid': tip,
            'mergeCommit': {'oid': merge_commit},
        }]}
        verdict, evidence = cb.classify_with_forge(self.path, 'feature', tip,
                                                   index, True)
        self.assertEqual(verdict, cb.SQUASHED)
        self.assertIn('#7', evidence)

    def test_closed_pull_request_needs_review(self):
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'a.txt', 'one\n')
        tip = run(self.path, 'rev-parse', 'HEAD')
        index = {'feature': [{'number': 9, 'state': 'CLOSED',
                              'headRefName': 'feature',
                              'baseRefName': 'main'}]}
        verdict, evidence = cb.classify_with_forge(self.path, 'feature', tip,
                                                   index, True)
        self.assertEqual(verdict, cb.UNKNOWN)
        self.assertIn('#9', evidence)
        self.assertIn('closed without merging', evidence)

    def test_open_pull_request_alone_gives_no_verdict(self):
        run(self.path, 'branch', 'feature')
        tip = run(self.path, 'rev-parse', 'feature')
        index = {'feature': [{'number': 3, 'state': 'OPEN',
                              'headRefName': 'feature',
                              'baseRefName': 'main'}]}
        self.assertEqual(
            cb.classify_with_forge(self.path, 'feature', tip, index, True),
            (None, None))

    def test_merged_pull_request_whose_merge_commit_is_absent(self):
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'a.txt', 'one\n')
        tip = run(self.path, 'rev-parse', 'HEAD')
        index = {'feature': [{
            'number': 11, 'state': 'MERGED', 'headRefName': 'feature',
            'baseRefName': 'main', 'headRefOid': tip,
            'mergeCommit': {'oid': 'f' * 40},
        }]}
        verdict, evidence = cb.classify_with_forge(self.path, 'feature', tip,
                                                   index, True)
        self.assertEqual(verdict, cb.UNKNOWN)
        self.assertIn('not in main', evidence)

    def test_stacked_branch_resolves_through_other_merged_requests(self):
        """A branch carrying a commit that landed under a sibling request.

        The branch is ahead of the head its own pull request recorded, and
        the extra commit is not in the base, so neither ancestry nor squash
        equivalence resolves it. It is reachable from another merged pull
        request's head, which is what makes the branch safe to delete.
        """
        run(self.path, 'checkout', '-q', '-b', 'lower')
        commit(self.path, 'lower.txt', 'lower\n')
        lower_tip = run(self.path, 'rev-parse', 'HEAD')

        run(self.path, 'checkout', '-q', '-b', 'upper')
        commit(self.path, 'upper.txt', 'upper\n')
        tip = run(self.path, 'rev-parse', 'HEAD')

        # Only the lower half was squashed onto main, so `upper.txt` is not
        # in the base and the content signals cannot settle the question.
        run(self.path, 'checkout', '-q', 'main')
        run(self.path, 'merge', '-q', '--squash', 'lower')
        run(self.path, 'commit', '-q', '-m', 'lower (#1)')
        merge_commit = run(self.path, 'rev-parse', 'main')

        # A sibling pull request whose head contains the leftover commit.
        run(self.path, 'checkout', '-q', '-b', 'sibling', 'upper')
        commit(self.path, 'sibling.txt', 'sibling\n')
        sibling_head = run(self.path, 'rev-parse', 'HEAD')
        run(self.path, 'checkout', '-q', 'main')

        index = {
            'upper': [{
                'number': 2, 'state': 'MERGED', 'headRefName': 'upper',
                'baseRefName': 'main', 'headRefOid': lower_tip,
                'mergeCommit': {'oid': merge_commit},
            }],
            'sibling': [{
                'number': 3, 'state': 'MERGED', 'headRefName': 'sibling',
                'baseRefName': 'main', 'headRefOid': sibling_head,
                'mergeCommit': {'oid': merge_commit},
            }],
        }
        verdict, evidence = cb.classify_with_forge(self.path, 'upper', tip,
                                                   index, self.have_merge_tree)
        self.assertEqual(verdict, cb.SQUASHED)
        self.assertIn('#3', evidence)
        self.assertIn('resolve to merged', evidence)

    def test_unaccounted_commits_need_review(self):
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'a.txt', 'one\n')
        recorded_head = run(self.path, 'rev-parse', 'HEAD')
        commit(self.path, 'b.txt', 'two\n')
        tip = run(self.path, 'rev-parse', 'HEAD')
        run(self.path, 'checkout', '-q', 'main')
        run(self.path, 'merge', '-q', '--squash', recorded_head)
        run(self.path, 'commit', '-q', '-m', 'feature (#4)')
        merge_commit = run(self.path, 'rev-parse', 'main')

        index = {'feature': [{
            'number': 4, 'state': 'MERGED', 'headRefName': 'feature',
            'baseRefName': 'main', 'headRefOid': recorded_head,
            'mergeCommit': {'oid': merge_commit},
        }]}
        verdict, evidence = cb.classify_with_forge(self.path, 'feature', tip,
                                                   index, self.have_merge_tree)
        self.assertEqual(verdict, cb.UNKNOWN)
        self.assertIn('unaccounted for', evidence)

    def test_open_heads_skip_the_truncation_sentinel(self):
        index = {cb.TRUNCATED: True,
                 'feature': [{'number': 5, 'state': 'OPEN'}]}
        self.assertEqual(cb.open_pull_request_heads(index), {'feature': 5})
        self.assertEqual([pr['number'] for pr in cb._merged_pull_requests(
            {cb.TRUNCATED: True,
             'x': [{'number': 6, 'state': 'MERGED'}]})], [6])

    def test_truncated_index_triggers_a_targeted_lookup(self):
        record = {'number': 12, 'state': 'CLOSED', 'headRefName': 'late',
                  'baseRefName': 'main'}
        gh = self.stub([(['--head', 'late'], [record])])
        prs = cb._lookup_pull_requests(self.path, 'late',
                                       {cb.TRUNCATED: True})
        self.assertEqual(prs, [record])
        self.assertTrue(gh.calls)

    def test_branch_named_for_a_pull_request_number_is_looked_up(self):
        record = {'number': 42, 'state': 'MERGED', 'headRefName': 'whatever',
                  'baseRefName': 'main'}
        self.stub([(['view', '42'], record)])
        prs = cb._lookup_pull_requests(self.path, 'pr_42', {})
        self.assertEqual(prs, [record])

    def test_complete_index_does_not_query_for_missing_branches(self):
        gh = self.stub([])
        self.assertEqual(cb._lookup_pull_requests(self.path, 'feature', {}), [])
        self.assertEqual(gh.calls, [])

    def test_pull_request_listing_marks_truncation(self):
        records = [{'number': n, 'state': 'MERGED', 'headRefName': f'b{n}'}
                   for n in range(3)]
        self.stub([(['list'], records)])
        index, _ = self.quietly(cb.fetch_pull_requests, self.path, 3)
        self.assertTrue(index[cb.TRUNCATED])
        self.assertIn('b0', index)

    def test_unparsable_pull_request_output_is_rejected(self):
        cb.run_cmd = lambda args, cwd=None: (0, 'not json', '')
        self.assertIsNone(cb.fetch_pull_requests(self.path))

    def test_failed_pull_request_listing_is_reported(self):
        cb.run_cmd = lambda args, cwd=None: (1, '', 'gh: not authenticated')
        result, _ = self.quietly(cb.fetch_pull_requests, self.path)
        self.assertIsNone(result)

    def test_an_unauthenticated_cli_counts_as_unavailable(self):
        cb.run_cmd = lambda args, cwd=None: (1, '', 'not logged in')
        self.assertFalse(cb.gh_available(self.path))
        cb.run_cmd = lambda args, cwd=None: (0, 'logged in', '')
        self.assertTrue(cb.gh_available(self.path))

    def test_loading_is_skipped_when_the_forge_is_turned_off(self):
        self.assertEqual(cb.load_forge(self.path, Args(no_forge=True)),
                         (None, False))

    def test_loading_returns_the_index_when_the_cli_answers(self):
        record = {'number': 1, 'state': 'MERGED', 'headRefName': 'feature'}
        self.stub([(['auth'], {}), (['list'], [record])])
        (index, use_forge), _ = self.quietly(cb.load_forge, self.path,
                                             Args(no_forge=False))
        self.assertTrue(use_forge)
        self.assertIn('feature', index)

    def test_a_branch_with_no_pull_request_is_reported_as_unmerged(self):
        """With pull request data in hand, silence means the work never landed.

        This is the one case that may be reported as diverged rather than
        unknown, because both sources of evidence were consulted.
        """
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'a.txt', 'one\n')
        branch = {'name': 'feature',
                  'oid': run(self.path, 'rev-parse', 'HEAD'),
                  'upstream': '', 'track': '', 'worktree': ''}
        targets = {'main': ('refs/heads/main',
                            run(self.path, 'rev-parse', 'main'))}
        verdict, evidence = cb.classify_branch(self.path, branch, targets, {},
                                               True, True)
        self.assertEqual(verdict, cb.DIVERGED)
        self.assertIn('no git or pull request evidence', evidence)

    def test_old_git_without_forge_data_reports_the_reason(self):
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'a.txt', 'one\n')
        branch = {'name': 'feature',
                  'oid': run(self.path, 'rev-parse', 'HEAD'),
                  'upstream': '', 'track': '', 'worktree': ''}
        targets = {'main': ('refs/heads/main',
                            run(self.path, 'rev-parse', 'main'))}
        verdict, evidence = cb.classify_branch(self.path, branch, targets,
                                               None, False, False)
        self.assertEqual(verdict, cb.UNKNOWN)
        self.assertIn('2.38', evidence)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

class PlanTest(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.path = self.repo()

    def plan(self, **overrides):
        args = Args(**overrides)
        plan, _ = self.quietly(cb.build_plan, self.path, args, False)
        return plan

    def entry(self, plan, name):
        for item in plan['entries']:
            if item['branch'] == name:
                return item
        raise AssertionError(f'{name} is not in the plan')

    def add_squashed_branch(self, name='feature'):
        """Squashes a two-commit branch.

        Two commits matter: a single-commit squash keeps its patch id, so it
        is detected as CONTAINED and never reaches the squash signal.
        """
        run(self.path, 'checkout', '-q', '-b', name)
        commit(self.path, f'{name}-a.txt', 'first\n')
        commit(self.path, f'{name}-b.txt', 'second\n')
        run(self.path, 'checkout', '-q', 'main')
        run(self.path, 'merge', '-q', '--squash', name)
        run(self.path, 'commit', '-q', '-m', f'{name} squashed')

    def test_squashed_branch_becomes_a_deletion_candidate(self):
        if not self.have_merge_tree:
            self.skipTest('git is older than 2.38')
        self.add_squashed_branch()
        entry = self.entry(self.plan(targets=['main'], target_ref='local'),
                           'feature')
        self.assertEqual(entry['verdict'], cb.SQUASHED)
        self.assertIn('branch -D feature', entry['actions'])

    def test_unmerged_branch_without_forge_data_needs_review(self):
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'feature.txt', 'work\n')
        run(self.path, 'checkout', '-q', 'main')
        entry = self.entry(self.plan(targets=['main'], target_ref='local'),
                           'feature')
        self.assertEqual(entry['verdict'], cb.UNKNOWN)
        self.assertEqual(entry['actions'], [])

    def test_dirty_worktree_withholds_a_merged_branch(self):
        """Uncommitted work outranks a correct merge verdict."""
        if not self.have_merge_tree:
            self.skipTest('git is older than 2.38')
        self.add_squashed_branch()
        linked = os.path.join(self.root, 'feature-tree')
        run(self.path, 'worktree', 'add', '-q', linked, 'feature')
        write(linked, 'feature-a.txt', 'edited but not committed\n')

        entry = self.entry(self.plan(targets=['main'], target_ref='local'),
                           'feature')
        self.assertEqual(entry['verdict'], cb.PROTECTED)
        self.assertTrue(entry['worktree_dirty'])
        self.assertIn('uncommitted changes', entry['evidence'])
        self.assertEqual(entry['actions'], [])

    def test_clean_worktree_is_removed_as_part_of_the_plan(self):
        if not self.have_merge_tree:
            self.skipTest('git is older than 2.38')
        self.add_squashed_branch()
        linked = os.path.join(self.root, 'feature-tree')
        run(self.path, 'worktree', 'add', '-q', linked, 'feature')
        entry = self.entry(self.plan(targets=['main'], target_ref='local'),
                           'feature')
        self.assertEqual(entry['verdict'], cb.SQUASHED)
        self.assertIn(f'worktree remove {os.path.realpath(linked)}',
                      entry['actions'])

    def test_exclude_glob_drops_a_branch_from_the_plan(self):
        self.add_squashed_branch('feature')
        plan = self.plan(targets=['main'], target_ref='local',
                         exclude=['feat*'])
        self.assertEqual([e['branch'] for e in plan['entries']], ['main'])

    def test_only_glob_restricts_the_plan(self):
        self.add_squashed_branch('feature')
        self.add_squashed_branch('other')
        plan = self.plan(targets=['main'], target_ref='local', only=['other'])
        names = sorted(e['branch'] for e in plan['entries'])
        self.assertEqual(names, ['main', 'other'])

    def test_protected_branches_stay_in_the_plan_as_a_record(self):
        plan = self.plan(targets=['main'], target_ref='local')
        self.assertEqual(self.entry(plan, 'main')['verdict'], cb.PROTECTED)

    def test_archive_prefix_appears_in_the_planned_actions(self):
        if not self.have_merge_tree:
            self.skipTest('git is older than 2.38')
        self.add_squashed_branch()
        entry = self.entry(
            self.plan(targets=['main'], target_ref='local',
                      archive_tag_prefix='attic/'), 'feature')
        self.assertIn('tag attic/feature', entry['actions'])

    def test_no_archive_removes_the_tag_action(self):
        if not self.have_merge_tree:
            self.skipTest('git is older than 2.38')
        self.add_squashed_branch()
        entry = self.entry(
            self.plan(targets=['main'], target_ref='local', no_archive=True),
            'feature')
        self.assertFalse(any(a.startswith('tag ') for a in entry['actions']))

    def test_missing_target_is_a_fatal_error(self):
        with self.assertRaises(SystemExit) as raised:
            self.quietly(cb.resolve_targets, self.path, Args(targets=['nope']))
        self.assertEqual(raised.exception.code, cb.EXIT_ERROR)

    def test_undetectable_default_branch_is_a_fatal_error(self):
        run(self.path, 'branch', '-m', 'main', 'trunk')
        with self.assertRaises(SystemExit) as raised:
            self.quietly(cb.resolve_target_names, self.path, Args())
        self.assertEqual(raised.exception.code, cb.EXIT_ERROR)


class RemotePermissionTest(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.origin = self.repo('origin')
        self.path = os.path.join(self.root, 'clone')
        run(self.root, 'clone', '-q', self.origin, self.path)
        self.branch = {'name': 'feature', 'oid': 'x', 'upstream':
                       'origin/feature', 'track': '', 'worktree': ''}

    def test_remote_deletion_needs_the_global_switch(self):
        self.assertFalse(cb.remote_allowed(self.path, 'origin',
                                           Args(delete_remote=False)))

    def test_single_remote_is_allowed_without_naming_it(self):
        self.assertTrue(cb.remote_allowed(self.path, 'origin',
                                          Args(delete_remote=True)))

    def test_second_remote_must_be_named(self):
        run(self.path, 'remote', 'add', 'upstream', self.origin)
        args = Args(delete_remote=True)
        self.assertFalse(cb.remote_allowed(self.path, 'origin', args))
        args.allow_remote = ['origin']
        self.assertTrue(cb.remote_allowed(self.path, 'origin', args))

    def test_gone_upstream_is_not_pushed_to(self):
        args = Args(delete_remote=True)
        branch = dict(self.branch, track='[gone]')
        actions = cb.plan_actions(self.path, branch, cb.SQUASHED, args)
        self.assertFalse(any('push' in a for a in actions))

    def test_permitted_remote_appears_in_the_actions(self):
        actions = cb.plan_actions(self.path, self.branch, cb.SQUASHED,
                                  Args(delete_remote=True))
        self.assertIn('push origin/feature --delete', actions)

    def test_non_deletable_verdicts_have_no_actions(self):
        self.assertEqual(
            cb.plan_actions(self.path, self.branch, cb.UNKNOWN, Args()), [])


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

class RenderTest(TempRepoCase):

    def plan(self):
        return {
            'created': '2001-02-03T04:05:06Z',
            'repo': '/tmp/repo',
            'refs_fresh': True,
            'forge': True,
            'targets': {},
            'entries': [
                {'branch': 'gone', 'tip': 'a' * 40, 'verdict': cb.SQUASHED,
                 'evidence': 'squash-equivalent to main', 'worktree': None,
                 'worktree_dirty': False, 'upstream': None,
                 'upstream_state': None,
                 'actions': ['tag archive/gone', 'branch -D gone']},
                {'branch': 'dirty', 'tip': 'b' * 40, 'verdict': cb.PROTECTED,
                 'evidence': 'withheld', 'worktree': '/tmp/dirty',
                 'worktree_dirty': True, 'upstream': None,
                 'upstream_state': None, 'actions': []},
                {'branch': 'mystery', 'tip': 'c' * 40, 'verdict': cb.UNKNOWN,
                 'evidence': 'no evidence', 'worktree': None,
                 'worktree_dirty': False, 'upstream': None,
                 'upstream_state': None, 'actions': []},
            ],
        }

    def test_markdown_groups_by_verdict(self):
        text = cb.render_markdown(self.plan())
        self.assertIn('## Deletable — squash merged (1)', text)
        self.assertIn('## Needs review (1)', text)
        self.assertIn('## Protected (1)', text)
        self.assertIn('(uncommitted changes)', text)
        self.assertIn('`git tag archive/gone`', text)

    def test_summary_flags_branches_needing_review(self):
        _, text = self.quietly(cb.print_summary, self.plan())
        self.assertIn('1 branch(es) need review', text)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

class ExecuteTest(TempRepoCase):

    def setUp(self):
        super().setUp()
        if not self.have_merge_tree:
            self.skipTest('git is older than 2.38')
        self.origin = self.repo('origin')
        self.path = os.path.join(self.root, 'clone')
        run(self.root, 'clone', '-q', self.origin, self.path)

        # A squash-merged branch with a clean linked worktree.
        run(self.path, 'checkout', '-q', '-b', 'feature')
        commit(self.path, 'feature-a.txt', 'first\n')
        commit(self.path, 'feature-b.txt', 'second\n')
        run(self.path, 'push', '-q', '-u', 'origin', 'feature')
        self.tip = run(self.path, 'rev-parse', 'feature')
        run(self.path, 'checkout', '-q', 'main')
        run(self.path, 'merge', '-q', '--squash', 'feature')
        run(self.path, 'commit', '-q', '-m', 'feature squashed (#1)')
        self.worktree = os.path.join(self.root, 'feature-tree')
        run(self.path, 'worktree', 'add', '-q', self.worktree, 'feature')

        self.plan_path = os.path.join(self.root, 'plan.json')

    def analyze(self, **overrides):
        args = Args(repo_dir=self.path, targets=['main'], target_ref='local',
                    out=self.plan_path, **overrides)
        code, _ = self.quietly(cb.cmd_analyze, args)
        self.assertEqual(code, cb.EXIT_OK)
        with open(self.plan_path, encoding='utf-8') as handle:
            return json.load(handle)

    def execute(self, **overrides):
        args = Args(repo_dir=self.path, execute=self.plan_path, **overrides)
        return self.quietly(cb.cmd_execute, args)

    def test_analyze_then_execute_deletes_branch_worktree_and_tags_it(self):
        plan = self.analyze()
        entry = next(e for e in plan['entries'] if e['branch'] == 'feature')
        self.assertEqual(entry['verdict'], cb.SQUASHED)

        code, output = self.execute()
        self.assertEqual(code, cb.EXIT_OK)
        self.assertIn('deleted local branch', output)

        self.assertIsNone(cb.normalize_ref(self.path, 'feature')[0])
        self.assertFalse(os.path.isdir(self.worktree))
        self.assertEqual(run(self.path, 'rev-parse', 'archive/feature'),
                         self.tip)

    def test_an_archived_branch_can_be_restored(self):
        self.analyze()
        self.execute()
        run(self.path, 'branch', 'feature', 'archive/feature')
        self.assertEqual(run(self.path, 'rev-parse', 'feature'), self.tip)

    def test_execution_refuses_when_a_branch_tip_moved(self):
        """A plan is a snapshot; acting on a stale one deletes new work."""
        self.analyze()
        run(self.worktree, 'commit', '-q', '--allow-empty', '-m', 'new work')
        moved = run(self.worktree, 'rev-parse', 'HEAD')
        self.assertNotEqual(moved, self.tip)

        code, _ = self.execute()
        self.assertEqual(code, cb.EXIT_PLAN_STALE)
        self.assertIsNotNone(cb.normalize_ref(self.path, 'feature')[0])
        self.assertTrue(os.path.isdir(self.worktree))

    def test_a_worktree_dirtied_after_planning_is_skipped(self):
        self.analyze()
        write(self.worktree, 'feature-a.txt', 'edited after the plan\n')
        code, output = self.execute()
        self.assertEqual(code, cb.EXIT_OK)
        self.assertIn('gained uncommitted changes', output)
        self.assertIsNotNone(cb.normalize_ref(self.path, 'feature')[0])

    def test_the_remote_branch_survives_without_the_deletion_flags(self):
        self.analyze()
        self.execute()
        self.assertTrue(run(self.origin, 'rev-parse', '--verify', 'feature'))

    def test_the_remote_branch_is_deleted_when_permitted(self):
        self.analyze(delete_remote=True)
        code, output = self.execute(delete_remote=True)
        self.assertEqual(code, cb.EXIT_OK)
        self.assertIn('deleted remote branch', output)
        self.assertNotEqual(
            subprocess.run(['git', 'show-ref', '--verify', '--quiet',
                            'refs/heads/feature'],
                           cwd=self.origin, env=ENV,
                           capture_output=True).returncode, 0)

    def test_a_named_remote_is_required_when_there_are_several(self):
        run(self.path, 'remote', 'add', 'other', self.origin)
        self.analyze(delete_remote=True)
        _, output = self.execute(delete_remote=True)
        self.assertIn('not deleting origin/feature', output)
        self.assertTrue(run(self.origin, 'rev-parse', '--verify', 'feature'))

    def test_no_archive_deletes_without_leaving_a_tag(self):
        self.analyze(no_archive=True)
        self.execute(no_archive=True)
        self.assertIsNone(cb.normalize_ref(self.path, 'archive/feature')[0])
        self.assertIsNone(cb.normalize_ref(self.path, 'feature')[0])

    def test_an_unreadable_plan_is_an_error(self):
        args = Args(execute=os.path.join(self.root, 'missing.json'))
        code, _ = self.quietly(cb.cmd_execute, args)
        self.assertEqual(code, cb.EXIT_ERROR)

    def test_a_plan_naming_a_vanished_repository_is_an_error(self):
        plan = self.analyze()
        plan['repo'] = os.path.join(self.root, 'not-a-repo')
        with open(self.plan_path, 'w', encoding='utf-8') as handle:
            json.dump(plan, handle)
        code, _ = self.execute()
        self.assertEqual(code, cb.EXIT_ERROR)

    def test_a_plan_with_nothing_deletable_exits_cleanly(self):
        plan = self.analyze()
        for entry in plan['entries']:
            entry['verdict'] = cb.PROTECTED
        with open(self.plan_path, 'w', encoding='utf-8') as handle:
            json.dump(plan, handle)
        code, output = self.execute()
        self.assertEqual(code, cb.EXIT_OK)
        self.assertIn('no deletable branches', output)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

class CommandLineTest(TempRepoCase):

    def test_ambiguous_container_exits_with_its_own_code(self):
        container = os.path.join(self.root, 'workspace')
        os.makedirs(container)
        init_repo(os.path.join(container, 'one'))
        init_repo(os.path.join(container, 'two'))
        code, _ = self.quietly(cb.main, ['analyze', '--repo-dir', container,
                                         '--no-fetch', '--no-forge'])
        self.assertEqual(code, cb.EXIT_AMBIGUOUS_REPO)

    def test_a_path_that_is_not_a_repository_is_an_error(self):
        plain = os.path.join(self.root, 'plain')
        os.makedirs(plain)
        code, _ = self.quietly(cb.main, ['analyze', '--repo-dir', plain,
                                         '--no-fetch', '--no-forge'])
        self.assertEqual(code, cb.EXIT_ERROR)

    def test_json_mode_prints_a_plan_without_writing_files(self):
        path = self.repo()
        code, output = self.quietly(
            cb.main, ['analyze', '--repo-dir', path, '--no-fetch',
                      '--no-forge', '--json', '--targets', 'main',
                      '--target-ref', 'local'])
        self.assertEqual(code, cb.EXIT_OK)
        plan = json.loads(output[output.index('{'):])
        self.assertEqual(plan['repo'], path)
        self.assertFalse(os.path.exists(
            os.path.join(path, '.git', 'branch-cleanup-plan.json')))

    def test_the_default_plan_location_is_inside_the_git_directory(self):
        path = self.repo()
        code, _ = self.quietly(
            cb.main, ['analyze', '--repo-dir', path, '--no-fetch',
                      '--no-forge', '--targets', 'main', '--target-ref',
                      'local'])
        self.assertEqual(code, cb.EXIT_OK)
        self.assertTrue(os.path.exists(
            os.path.join(path, '.git', 'branch-cleanup-plan.json')))
        self.assertTrue(os.path.exists(
            os.path.join(path, '.git', 'branch-cleanup-plan.md')))

    def test_a_failed_fetch_stops_the_run(self):
        """Stale refs produce wrong verdicts, so a failed fetch is fatal."""
        path = self.repo()
        run(path, 'remote', 'add', 'origin',
            os.path.join(self.root, 'does-not-exist'))
        with self.assertRaises(SystemExit) as raised:
            self.quietly(cb.fetch_all, path)
        self.assertEqual(raised.exception.code, cb.EXIT_ERROR)

    def test_allow_stale_continues_after_a_failed_fetch(self):
        path = self.repo()
        run(path, 'remote', 'add', 'origin',
            os.path.join(self.root, 'does-not-exist'))
        fresh, _ = self.quietly(cb.fetch_all, path, True)
        self.assertFalse(fresh)

    def test_no_fetch_reports_that_refs_are_not_fresh(self):
        fresh, output = self.quietly(cb.fetch_all, self.repo(), False, True)
        self.assertFalse(fresh)
        self.assertIn('--no-fetch', output)

    def test_a_successful_fetch_reports_fresh_refs(self):
        origin = self.repo('origin')
        clone = os.path.join(self.root, 'clone')
        run(self.root, 'clone', '-q', origin, clone)
        fresh, _ = self.quietly(cb.fetch_all, clone)
        self.assertTrue(fresh)


if __name__ == '__main__':
    unittest.main(verbosity=2)
