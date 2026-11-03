#!/usr/bin/env python3
"""Write explicit pytest layer marks from a classify.py result.

For each test module under --root:
  * one heavy layer covering at least (1 - threshold) of its test functions
    -> module-level ``pytestmark = pytest.mark.<layer>``
  * otherwise -> ``@pytest.mark.<layer>`` on each heavy test function
A parametrized function takes the heaviest layer of its cases. ``--flaky-fs``
adds ``@pytest.mark.flaky_fs`` to the named functions.

Idempotent: marks that are already present are left alone, so the script can be
rerun after new tests are added. It never removes marks; tests whose existing
mark disagrees with the measurement are listed for manual review.
Use --dry-run to print a unified diff without writing.
"""
import argparse
import ast
import collections
import difflib
import json
import re
import sys
from pathlib import Path

RANK = {'unit': 0, 'ui': 1, 'integration': 2}
SOURCE = Path(__file__).resolve().parent.parent.parent


def function_layers(layer_by_node):
    layers = collections.defaultdict(lambda: 'unit')
    for node, layer in layer_by_node.items():
        path, name = node.split('::', 1)
        if '::' in name:  # class-based tests are left for manual marking
            continue
        key = (path, name.split('[')[0])
        if RANK[layer] > RANK[layers[key]]:
            layers[key] = layer
    files = collections.defaultdict(dict)
    for (path, name), layer in layers.items():
        files[path][name] = layer
    return files


def mark_names(expr):
    """Names of pytest.mark.X found in a decorator or pytestmark expression."""
    return set(re.findall(r'pytest\.mark\.(\w+)', expr or ''))


def module_marks(tree, src):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'pytestmark' for t in node.targets):
            return node, mark_names(ast.get_source_segment(src, node.value))
    return None, set()


def header_end(tree):
    """Line after the module docstring and top-level imports."""
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node.end_lineno
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and last == 0:
            last = node.end_lineno
        else:
            break
    return last


def ensure_import(lines, tree):
    if any(isinstance(n, ast.Import) and any(a.name == 'pytest' for a in n.names) for n in tree.body):
        return
    index = 0
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and index == 0:
            index = node.end_lineno
        elif isinstance(node, ast.ImportFrom) and node.module == '__future__':
            index = node.end_lineno
        else:
            break
    lines.insert(index, 'import pytest')


def plan_file(path, functions, flaky, threshold, review):
    src = path.read_text(encoding='utf-8')
    tree = ast.parse(src)
    lines = src.split('\n')
    heavy = collections.Counter(v for v in functions.values() if v != 'unit')
    module_node, existing_module = module_marks(tree, src)
    file_level = None
    if len(heavy) == 1:
        layer, count = heavy.most_common(1)[0]
        if (len(functions) - count) / len(functions) < threshold:
            file_level = layer
    inserts = []
    replace = None
    if file_level and file_level not in existing_module:
        if module_node:
            value = ast.get_source_segment(src, module_node.value)
            joined = value[1:-1] if isinstance(module_node.value, ast.List) else value
            replace = (module_node.lineno - 1, module_node.end_lineno, f'pytestmark = [pytest.mark.{file_level}, {joined}]')
        else:
            inserts.append((header_end(tree), f'\npytestmark = pytest.mark.{file_level}'))
    module_layer = file_level or next((l for l in ('integration', 'ui') if l in existing_module), None)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in functions:
            continue
        have = set().union(*(mark_names(ast.get_source_segment(src, d)) for d in node.decorator_list)) | existing_module
        measured = functions[node.name]
        want = []
        if not module_layer and measured != 'unit' and measured not in have:
            want.append(measured)
        if node.name in flaky and 'flaky_fs' not in have:
            want.append('flaky_fs')
        marked = {l for l in ('ui', 'integration') if l in have}
        if marked and measured not in marked and not (measured == 'unit' and module_layer):
            review.append(f'{path.name}::{node.name} marked {sorted(marked)} but measured {measured}')
        start = min([d.lineno for d in node.decorator_list] + [node.lineno])
        indent = ' ' * node.col_offset
        inserts += [(start - 1, f'{indent}@pytest.mark.{w}') for w in want]
    if not inserts and not replace:
        return None, file_level
    for index, text in sorted(inserts, key=lambda x: -x[0]):
        lines.insert(index, text)
    if replace:
        a, b, text = replace
        shift = sum(1 for index, _ in inserts if index <= a)
        lines[a + shift:b + shift] = [text]
    ensure_import(lines, tree)
    return '\n'.join(lines), file_level


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('classification', help='JSON written by classify.py')
    parser.add_argument('--root', type=Path, default=SOURCE, help='directory the node ids are relative to (default: source/)')
    parser.add_argument('--threshold', type=float, default=0.1,
                        help='max share of unit functions for a module-level mark (default 0.1)')
    parser.add_argument('--flaky-fs', action='append', default=[], metavar='PATH::FUNC',
                        help='also mark this test function flaky_fs (repeatable)')
    parser.add_argument('--dry-run', action='store_true', help='print a diff, write nothing')
    args = parser.parse_args(argv)
    with open(args.classification, encoding='utf-8') as f:
        files = function_layers(json.load(f)['layer'])
    flaky = collections.defaultdict(set)
    for spec in args.flaky_fs:
        path, name = spec.split('::', 1)
        flaky[path].add(name)
        files[path].setdefault(name, 'unit')
    report, review, changed = collections.Counter(), [], 0
    for rel, functions in sorted(files.items()):
        path = args.root / rel
        if not path.exists():
            print('missing', rel, file=sys.stderr)
            continue
        new, file_level = plan_file(path, functions, flaky[rel], args.threshold, review)
        heavy = any(v != 'unit' for v in functions.values())
        report['file-' + file_level if file_level else 'function-level' if heavy else 'unit-only'] += 1
        if new is None:
            continue
        changed += 1
        old = path.read_text(encoding='utf-8')
        if args.dry_run:
            sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True), 'a/' + rel, 'b/' + rel))
        else:
            path.write_text(new, encoding='utf-8')
    print(f'{"would change" if args.dry_run else "changed"} {changed} files; {dict(report)}', file=sys.stderr)
    for line in review:
        print('review:', line, file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
