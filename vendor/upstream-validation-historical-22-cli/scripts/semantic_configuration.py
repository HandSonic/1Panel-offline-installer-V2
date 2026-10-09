#!/usr/bin/env python3
"""Strict YAML validation and surgical production-value normalization.

Unknown fields and source layout survive. Aliases, custom tags and duplicate keys
are rejected rather than permitting YAML-dependent interpretation differences.
"""
import re
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

FLAGS = ('is_demo', 'is_offline', 'is_fxplay', 'is_enterprise')


def parse(data):
    try:
        text = data.decode('utf-8') if isinstance(data, bytes) else data
    except UnicodeError as exc:
        raise ValueError('Configuration must be UTF-8') from exc
    if not isinstance(text, str) or len(text) > 1024 * 1024:
        raise ValueError('Configuration must be bounded UTF-8 YAML')
    try:
        if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken))
               for token in yaml.scan(text)):
            raise ValueError('YAML aliases, anchors and explicit tags are unsupported')
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        def check(current):
            if isinstance(current, MappingNode):
                seen = set()
                for key, value in current.value:
                    if not isinstance(key, ScalarNode) or key.tag != 'tag:yaml.org,2002:str':
                        raise ValueError('YAML mapping keys must be strings')
                    if key.value in seen:
                        raise ValueError('Duplicate YAML key: ' + key.value)
                    seen.add(key.value)
                    check(value)
            elif isinstance(current, SequenceNode):
                for value in current.value:
                    check(value)
            elif not isinstance(current, ScalarNode):
                raise ValueError('Unsupported YAML node')
        check(node)
        value = yaml.safe_load(text)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ValueError('Malformed configuration YAML') from exc
    if not isinstance(value, dict):
        raise ValueError('Configuration must be a mapping')
    return text, node, value


def production(data, version, component, mode):
    if component not in ('core', 'agent') or mode not in ('stable', 'beta', 'dev'):
        raise ValueError('Unsupported configuration component or channel')
    if not re.fullmatch(r'v2\.\d+\.\d+(?:-(?:beta|dev)\.\d+)?', version):
        raise ValueError('Expected supported explicit version')
    text, root, value = parse(data)
    wanted = {'base': {'mode': mode}, 'log': {'level': 'info'}}
    if component == 'core':
        wanted['base']['version'] = version
    for section, fields in wanted.items():
        if not isinstance(value.get(section), dict):
            raise ValueError('Missing required mapping: ' + section)
        for key in fields:
            if not isinstance(value[section].get(key), str) or not value[section][key].strip():
                raise ValueError('Missing or malformed critical setting: ' + section + '.' + key)
    for key in FLAGS:
        if key in value['base']:
            if type(value['base'][key]) is not bool:
                raise ValueError('Production flag must be boolean: ' + key)
            wanted['base'][key] = 'false'
    changes = []
    for section_node, mapping in root.value:
        if section_node.value not in wanted:
            continue
        for key_node, scalar in mapping.value:
            replacement = wanted[section_node.value].get(key_node.value)
            if replacement is None:
                continue
            if not isinstance(scalar, ScalarNode) or scalar.style in ('|', '>'):
                raise ValueError('Critical settings must be simple YAML scalars')
            changes.append((scalar.start_mark.index, scalar.end_mark.index, replacement))
    for start, end, replacement in sorted(changes, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text.encode('utf-8')


def semantic(data):
    """Typed comparison avoids Python treating false, zero and 0.0 as equal."""
    def freeze(value):
        if isinstance(value, dict):
            return ('mapping', tuple(sorted((key, freeze(item)) for key, item in value.items())))
        if isinstance(value, list):
            return ('sequence', tuple(freeze(item) for item in value))
        return (type(value).__name__, repr(value))
    return freeze(parse(data)[2])
