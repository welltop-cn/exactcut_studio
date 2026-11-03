#!/usr/bin/env python3
"""Run the test suite under the layer probe and record what every test does.

Example (from source/):
    python devtools/test_layers/probe.py --out /tmp/layer-probe -- -n auto
    python devtools/test_layers/classify.py --events /tmp/layer-probe --out /tmp/layer-probe/classification.json
    python devtools/test_layers/apply_markers.py /tmp/layer-probe/classification.json --dry-run

Arguments after ``--`` go to pytest unchanged. The full suite is selected
(``-m ""``) unless you pass your own ``-m``. The probe only records events; it
does not change test behaviour.
"""
import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent.parent


def build(out, pytest_args):
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(p for p in (str(HERE), str(SOURCE), env.get('PYTHONPATH', '')) if p)
    env['TEST_LAYERS_PROBE_DIR'] = str(out)
    env['TEST_LAYERS_PROBE_SRC'] = str(SOURCE)
    cmd = [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-p', 'probe_plugin', '-rfEs']
    if not any(a.startswith('-m') for a in pytest_args):
        cmd += ['-m', '']
    return cmd + pytest_args, env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', required=True, type=Path, help='directory for <pid>.jsonl events and collection.tsv (must be empty or missing)')
    parser.add_argument('--dry-run', action='store_true', help='print the command and environment, do not run')
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER, help='arguments for pytest, after --')
    args = parser.parse_args(argv)
    pytest_args = args.pytest_args[1:] if args.pytest_args[:1] == ['--'] else args.pytest_args
    out = args.out.resolve()
    cmd, env = build(out, pytest_args)
    if args.dry_run:
        for key in ('PYTHONPATH', 'TEST_LAYERS_PROBE_DIR', 'TEST_LAYERS_PROBE_SRC'):
            print(f'{key}={shlex.quote(env[key])}')
        print('cd', shlex.quote(str(SOURCE)), '&&', shlex.join(cmd))
        return 0
    if out.exists() and any(out.iterdir()):
        parser.error(f'{out} is not empty; events would be mixed with an earlier run')
    out.mkdir(parents=True, exist_ok=True)
    return subprocess.call(cmd, cwd=SOURCE, env=env)


if __name__ == '__main__':
    sys.exit(main())
