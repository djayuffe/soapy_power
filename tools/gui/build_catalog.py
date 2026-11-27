#!/usr/bin/env python3
"""Generate GUI controls from actual CLI parsers, without opening a receiver."""

import argparse
import importlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
MODULES = {
    'acquisition': 'soapypower.__main__',
    'calibration': 'soapypower.calibration_cli',
    'analysis': 'soapypower.analysis_cli',
    'telemetry': 'soapypower.telemetry_cli',
    'spurs': 'soapypower.spur_cli',
    'experiments': 'soapypower.experiments',
    'noise': 'soapypower.noise_audit',
    'scan-spurs': 'soapypower.scan_spurs',
    'scan-report': 'soapypower.scan_report',
}
INPUTS = {
    'input',
    'configuration',
    'profile',
    'reference',
    'observed',
    'records',
    'manifest',
    'measurements',
    'context',
    'holdouts',
    'model',
    'iq',
    'intervals',
    'metadata',
    'target_edges',
    'frequency_grid',
    'observation',
    'suite',
    'usable_policy',
    'calibration',
    'receiver_profile',
    'settling_model',
    'telemetry_policy',
    'policy',
}
MANAGED = {'help', 'version', 'output', 'output_fd', 'rejection_evidence'}


class Captured(BaseException):
    def __init__(self, parser):
        self.parser = parser


def parser_for(module):
    loaded = importlib.import_module(module)
    for name in ('setup_argument_parser', 'build_parser', 'parser'):
        candidate = getattr(loaded, name, None)
        if callable(candidate):
            return candidate()

    def stop(parser, *args, **kwargs):
        raise Captured(parser)

    with patch.object(argparse.ArgumentParser, 'parse_args', stop):
        try:
            loaded.main([])
        except Captured as result:
            return result.parser
    raise ValueError('CLI did not expose a parser: ' + module)


def role(tool, command, dest):
    if tool == 'experiments' and command == 'analyze' and dest == 'directory':
        return 'input'
    if tool == 'experiments' and command == 'compare' and dest in ('terminated', 'antenna'):
        return 'input'
    if tool == 'experiments' and dest in ('input', 'antenna'):
        return 'value'
    if dest in ('output', 'out', 'directory'):
        return 'output'
    if dest in MANAGED:
        return 'managed'
    if tool == 'acquisition' and dest in ('endless', 'runs', 'elapsed'):
        return 'managed'
    if dest in ('force', 'overwrite', 'soapy_power'):
        return 'managed'
    if dest == 'source_hook':
        return 'source'
    if tool == 'spurs' and dest == 'profile':
        return 'value'
    if tool == 'calibration' and command == 'build' and dest in ('reference', 'reference_plane'):
        return 'value'
    if tool == 'experiments' and command == 'calibration-config' and dest in ('reference', 'reference_plane'):
        return 'value'
    if dest in INPUTS:
        return 'input'
    return 'value'


def option(action, group, tool, command):
    kind = (
        'boolean'
        if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction))
        else 'integer'
        if action.type is int
        else 'number'
        if action.type is float
        else 'text'
    )
    default = action.default if isinstance(action.default, (str, int, float, bool, list, type(None))) else None
    return dict(
        dest=action.dest,
        flags=action.option_strings,
        kind=kind,
        nargs=action.nargs,
        required=action.required,
        default=default,
        choices=list(action.choices) if action.choices is not None else None,
        help=str(action.help or '').replace('%(default)s', str(default)),
        group=group,
        converter=getattr(action.type, '__name__', None),
        role=role(tool, command, action.dest),
        polarity=not isinstance(action, argparse._StoreFalseAction),
    )


def generate():
    commands = []

    def walk(parser, tool, path):
        children = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if children:
            for child in children:
                for name, p in child.choices.items():
                    walk(p, tool, path + [name])
            return
        command = ' '.join(path)
        opts = []
        for group in parser._action_groups:
            for action in group._group_actions:
                if isinstance(action, argparse._SubParsersAction):
                    continue
                opts.append(option(action, group.title, tool, command))
        commands.append(
            dict(
                id=tool + (':' + ':'.join(path) if path else ''),
                tool=tool,
                module=MODULES[tool],
                command=path,
                description=parser.description or (tool + ' ' + command).strip(),
                options=opts,
                mutex=[[a.dest for a in g._group_actions] for g in parser._mutually_exclusive_groups],
            )
        )

    for tool, module in MODULES.items():
        walk(parser_for(module), tool, [])
    return dict(version=1, commands=commands)


if __name__ == '__main__':
    result = generate()
    target = ROOT / 'soapypower/web/options.json'
    target.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(
        '{} commands; {} option bindings'.format(
            len(result['commands']), sum(len(c['options']) for c in result['commands'])
        )
    )
