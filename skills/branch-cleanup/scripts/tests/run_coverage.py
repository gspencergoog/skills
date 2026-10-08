#!/usr/bin/env python3
"""Runs the test suite under the stdlib tracer and reports line coverage.

Usage:
    python3 scripts/tests/run_coverage.py [minimum_percent]

Exits non-zero when coverage falls below the minimum, which defaults to 85.
"""

import os
import sys
import sysconfig
import trace
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, '..'))
TARGET = os.path.join(SCRIPTS, 'cleanup_branches.py')

# Ignore the standard library by its actual directories. `sys.prefix` is too
# coarse: on a system Python it is `/usr`, which also swallows a checkout
# under `/usr/local`, and the tracer then reports nothing at all.
STDLIB_DIRS = sorted({sysconfig.get_paths()[key]
                      for key in ('stdlib', 'platstdlib', 'purelib', 'platlib')})

sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

MINIMUM = float(sys.argv[1]) if len(sys.argv) > 1 else 85.0

# Lines that carry no runtime behaviour worth counting.
SKIPPED_EXACTLY = {'else:', 'try:', 'finally:', 'pass', '"""', "'''"}


def executable_lines(path):
    """Approximates the executable lines of a file.

    The tracer only reports lines it saw, so the denominator has to be
    reconstructed. Blank lines, comments and block openers are excluded;
    docstring bodies are skipped by tracking the quoting state.
    """
    lines, in_docstring = [], False
    with open(path, encoding='utf-8') as handle:
        for number, raw in enumerate(handle, 1):
            text = raw.strip()
            if in_docstring:
                if text.endswith('"""') or text.endswith("'''"):
                    in_docstring = False
                continue
            if text.startswith('"""') or text.startswith("'''"):
                body = text[3:]
                if not (body.endswith('"""') or body.endswith("'''")):
                    in_docstring = True
                continue
            if not text or text.startswith('#') or text in SKIPPED_EXACTLY:
                continue
            if text.startswith('except') and text.endswith(':'):
                continue
            lines.append((number, raw.rstrip()))
    return lines


def run_suite():
    import test_cleanup_branches
    suite = unittest.TestLoader().loadTestsFromModule(test_cleanup_branches)
    return unittest.TextTestRunner(verbosity=1).run(suite)


def main():
    tracer = trace.Trace(count=1, trace=0, ignoredirs=STDLIB_DIRS)
    result = tracer.runfunc(run_suite)
    counts = tracer.results().counts

    seen = {line for (filename, line) in counts
            if os.path.abspath(filename) == TARGET}

    candidates = executable_lines(TARGET)
    missing = [(number, text) for number, text in candidates
               if number not in seen]
    total = len(candidates)
    covered = total - len(missing)
    percent = (covered / total * 100) if total else 0.0

    print('\n================ Coverage ================')
    print(f'File    : {TARGET}')
    print(f'Covered : {covered}/{total} ({percent:.1f}%)')
    if missing:
        print('\nUncovered lines:')
        for number, text in missing:
            print(f'  L{number}: {text}')
    print('==========================================')

    if result is not None and not result.wasSuccessful():
        return 1
    if percent < MINIMUM:
        print(f'Coverage {percent:.1f}% is below the {MINIMUM:.0f}% minimum.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
