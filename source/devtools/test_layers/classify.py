#!/usr/bin/env python3
"""Turn probe events into a per-test layer (unit / ui / integration).

Rules (same as tests/conftest.py's unit guard):
  integration  ran ffmpeg/ffprobe (directly or via exactcut.processing_child) or opened a media file
  ui           started or reused a Playwright browser, and is not integration
  unit         everything else

Writes JSON with ``layer`` (node id -> layer), ``cap`` (node id -> observed
capabilities), ``mixed`` (files whose tests span layers) and side findings
(fixed-port binds, network, writes into the source tree or /tmp) and prints a
summary. The output contains absolute paths of the machine it ran on; keep it
out of the repository.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys


def load_events(directory):
    for path in sorted(glob.glob(os.path.join(directory, '*.jsonl'))):
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def load_collection(directory):
    path = os.path.join(directory, 'collection.tsv')
    nodes = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                nodes.append(line.rstrip('\n').split('\t')[0])
    return nodes


def capability(event):
    kind, detail = event['k'], event['d']
    if kind == 'popen':
        tool = detail[2] if len(detail) > 2 else 'other'
        return {'media': 'media', 'browser': 'browser', 'node': 'node', 'python': 'subproc-python'}.get(tool, 'proc:' + detail[0])
    return {'browser': 'browser', 'media': 'mediafile', 'bind': 'bind', 'connect': 'net',
            'dns': 'net', 'srcwrite': 'srcwrite'}.get(kind)


def layer_of(caps):
    if 'media' in caps or 'mediafile' in caps:
        return 'integration'
    if 'browser' in caps:
        return 'ui'
    return 'unit'


def classify(directory):
    nodes = load_collection(directory)
    cap = collections.defaultdict(set)
    findings = {k: collections.Counter() for k in ('binds', 'net', 'srcw', 'tmpw')}
    for event in load_events(directory):
        test, kind, detail = event['t'], event['k'], event['d']
        found = capability(event)
        if found:
            cap[test].add(found)
        module = test.split('::')[0]
        if kind == 'bind':
            findings['binds'][(module, detail[1])] += 1
        elif kind in ('connect', 'dns'):
            findings['net'][(module, str(detail))] += 1
        elif kind == 'srcwrite':
            findings['srcw'][(module, re.sub(r'\d+', 'N', detail))] += 1
        elif kind == 'tmpwrite':
            findings['tmpw'][(module, detail)] += 1
    layer = {node: layer_of(cap.get(node, set())) for node in nodes}
    per_file = collections.defaultdict(collections.Counter)
    for node, value in layer.items():
        per_file[node.split('::')[0]][value] += 1
    mixed = {f: dict(c) for f, c in per_file.items() if len(c) > 1}
    result = {'layer': layer, 'cap': {k: sorted(v) for k, v in cap.items() if k in layer}, 'mixed': mixed}
    for key, counter in findings.items():
        result[key] = [[a, b, n] for (a, b), n in counter.items()]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--events', required=True, help='directory written by probe.py')
    parser.add_argument('--out', required=True, help='classification JSON to write')
    args = parser.parse_args(argv)
    result = classify(args.events)
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=0)
    counts = collections.Counter(result['layer'].values())
    print('layers:', dict(counts), 'tests:', len(result['layer']))
    print('mixed files:', len(result['mixed']))
    for name, c in sorted(result['mixed'].items()):
        print('  ', name, c)
    fixed = [b for b in result['binds'] if b[1] not in (0, None)]
    if fixed:
        print('fixed-port binds (xdist conflict risk):', fixed)
    if result['net']:
        print('non-loopback network:', result['net'][:20])
    if result['srcw']:
        print('writes into source tree:', sorted({b[0] for b in result['srcw']}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
