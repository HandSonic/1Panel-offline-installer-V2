"""Conservative configuration contracts for authenticated community upgrades.

These structural profiles are evidence, not source authentication. Callers bind
the exact installer/control bytes to independently authenticated packages first.
Legacy means no edition selector existed; it never means cn or intl.
"""
import hashlib
from pathlib import Path
import re
import shlex

from installer_capabilities import edition_selection
from service_layout import complete_shell, command_boundary, reject_unconditional_termination

BASE_KEYS = ('BASE_DIR', 'ORIGINAL_PORT', 'ORIGINAL_USERNAME', 'ORIGINAL_PASSWORD',
             'ORIGINAL_ENTRANCE', 'LANGUAGE')
PROTECTED_KEYS = BASE_KEYS + ('PANEL_EDITION', 'CHANGE_USER_INFO')


def protected_assignments(text, protected_keys):
    """Recognize bounded static declarations and direct protected writes.

    Bash is used only with -n. Literal builtin destinations are inspected without
    evaluating expansions. Keep this function identical to the updater copy.
    """
    if not isinstance(text, str) or not 0 < len(text) <= 2 * 1024 * 1024 or '\0' in text:
        raise ValueError('Invalid control configuration script')
    joins = list(re.finditer(r'\\\n', text))
    if len(joins) > 128:
        raise ValueError('Configuration continuation count exceeds supported bounds')
    chunks, boundaries, previous = [], [], 0
    for index, join in enumerate(joins):
        chunks.append(text[previous:join.start()])
        boundaries.append(join.start() - 2 * index)
        previous = join.end()
    view = ''.join(chunks) + text[previous:]

    def original_span(match):
        first, last = match.start(), match.end() - 1
        return (first + 2 * sum(boundary <= first for boundary in boundaries),
                last + 2 * sum(boundary <= last for boundary in boundaries) + 1)

    names = '(?:' + '|'.join(map(re.escape, protected_keys)) + ')'
    patterns = [
        r'(?<![A-Za-z0-9_])(?P<key>' + names + r')(?P<operator>(?:\[[^\]\n]{0,128}\])?[ \t]*(?:\+\+|--|(?:<<|>>|[+*/%&|^\-])?=(?!=)))',
        r'(?<![A-Za-z0-9_])(?:\+\+|--)[ \t]*(?P<key>' + names + r')(?![A-Za-z0-9_])',
        r'\$\{(?P<key>' + names + r')(?::?=)',
    ]
    candidates = []
    for kind, pattern in enumerate(patterns):
        for candidate in re.finditer(pattern, view):
            candidates.append((kind, candidate))
            if len(candidates) > 64:
                raise ValueError('Configuration assignment candidate count exceeds supported bounds')
    commands = r'(?:eval|declare|typeset|readonly|export|local|let|unset|source|\.|read|readarray|mapfile|printf|getopts|for|select)'
    writers = []
    for writer in re.finditer(r'(?<![A-Za-z0-9_])(?:' + commands + r'|"' + commands + r'"|\'' + commands + r'\')(?=[ \t;\n]|$)', view):
        writers.append(writer)
        if len(candidates) + len(writers) > 64:
            raise ValueError('Configuration assignment candidate count exceeds supported bounds')
    if not complete_shell(text):
        raise ValueError('Invalid control configuration script')
    # Parameter default assignments may expand even inside double quotes and
    # heredocs; syntax-only parsing cannot establish their effect. This direct
    # write interface is unsupported. Read-only ${KEY} remains untouched.
    comments = set()
    for kind, candidate in candidates:
        start, _ = original_span(candidate)
        line_start = text.rfind('\n', 0, start) + 1
        if re.match(r'[ \t]*#', text[line_start:start]) and command_boundary(text[:line_start]):
            # A complete shell prefix excludes open quotes and heredocs. A '#'
            # in an expanding heredoc is data and must not hide a direct write.
            comments.add(candidate.start())
            continue
        if kind == 2:
            raise ValueError('Unsupported protected parameter default assignment')
        prefix = view[:candidate.start()]
        if prefix.rfind('$((') > prefix.rfind('))'):
            # Arithmetic expansion can write in double quotes or an expanding
            # heredoc. Bash -n cannot classify all such expansions safely.
            raise ValueError('Unsupported protected arithmetic expansion')

    def command_words(raw):
        lexer = shlex.shlex(raw.split('\n', 1)[0], posix=True, punctuation_chars=';&|()<>')
        lexer.whitespace_split = True
        words, previous = [], 0
        while True:
            word = lexer.get_token()
            end = lexer.instream.tell()
            original = raw[previous:end]
            previous = end
            if word is None:
                return words
            if word and all(char in ';&|()<>' for char in word) and not any(char in original for char in "'\"\\"):
                return words
            words.append(word)
            if len(words) > 128:
                raise ValueError('Configuration command argument limit exceeded')

    def output_argument(position):
        prefix = view[view.rfind('\n', 0, position) + 1:position]
        try:
            words = command_words(prefix)
        except ValueError:
            return False
        # A literal argument to an ordinary output command is not a write.
        # Exclude command lists/substitutions and unfinished quoting.
        return bool(words and words[0] in ('echo', 'printf') and
                    not re.search(r'[;&|()<>]', prefix) and complete_shell(prefix))

    def destination(name):
        # Unknown expansion or indexing may name a protected variable indirectly.
        match = re.fullmatch(r'([A-Za-z_][A-Za-z_0-9]*)(?:\[([0-9]+)\])?', name)
        if match is None or match[1] in protected_keys:
            raise ValueError('Unsupported protected or dynamic configuration destination')

    def inspect_writer(words):
        if not words:
            raise ValueError('Unknown configuration write interface')
        command, args = words[0], words[1:]
        if command in ('eval', 'let', 'source', '.'):
            raise ValueError('Unsupported dynamic configuration mutation')
        if command in ('for', 'select'):
            if args:
                destination(args[0])
            return
        if command == 'getopts':
            if len(args) < 2:
                raise ValueError('Incomplete getopts destination')
            destination(args[1]); return
        if command == 'printf':
            if args and args[0] == '--':
                args = args[1:]
            elif args and args[0].startswith('-v'):
                target = args.pop(0)[2:]
                if not target:
                    if not args: raise ValueError('Missing printf destination')
                    target = args.pop(0)
                destination(target)
            if args and ((re.search(r'%(?:[-+ #0]*[0-9]*(?:\.[0-9]+)?)n', args[0]) and
                          any(arg in protected_keys or re.search(r'[$`\[\]]', arg) for arg in args[1:])) or
                         (re.search(r'[$`]', args[0]) and any(arg in protected_keys for arg in args[1:]))):
                raise ValueError('Unsupported printf assignment format')
            return
        if command in ('declare', 'typeset', 'readonly', 'export', 'local', 'unset'):
            for arg in args:
                if arg == '--': continue
                if arg.startswith(('-', '+')):
                    if re.search('[in]', arg):
                        raise ValueError('Unsupported indirect declaration attribute')
                    if not re.fullmatch(r'[-+][aAfFglprtxv]+', arg):
                        raise ValueError('Unknown declaration option')
                    continue
                destination(arg.split('=', 1)[0])
            return
        if command not in ('read', 'readarray', 'mapfile'):
            raise ValueError('Unknown configuration write interface')
        names, index = [], 0
        while index < len(args):
            arg = args[index]; index += 1
            if arg == '--':
                names.extend(args[index:]); break
            if not arg.startswith('-') or arg == '-':
                names.append(arg); continue
            options = arg[1:]
            while options:
                option, options = options[0], options[1:]
                takes_value = 'adinNptu' if command == 'read' else 'dnOsucC'
                if option in takes_value:
                    value = options
                    if not value:
                        if index == len(args): raise ValueError('Missing read option argument')
                        value = args[index]; index += 1
                    options = ''
                    if option == 'a' and command == 'read': names.append(value)
                    if option == 'C' and command != 'read':
                        raise ValueError('Unsupported mapfile callback')
                elif option not in ('ersE' if command == 'read' else 't'):
                    raise ValueError('Unknown read option')
        for name in names:
            destination(name)

    fallback = re.compile(
        r'^if \[ -f "/usr/local/bin/lang/\$LANGUAGE\.sh" \]; then\n'
        r'[ \t]+(?P<source>source) "/usr/local/bin/lang/\$LANGUAGE\.sh"\n'
        r'else\n[ \t]+(?P<assignment>LANGUAGE=en)[ \t]*\nfi[ \t]*(?:\n|$)', re.M)
    fallbacks = list(fallback.finditer(view))
    fallback_positions = {match.start('assignment') for match in fallbacks}
    source_positions = {match.start('source') for match in fallbacks}
    for writer in writers:
        start, end = original_span(writer)
        if writer.start() in source_positions or complete_shell(text[:start] + '; if then ' + text[end:]):
            continue
        if output_argument(writer.start()):
            continue
        inspect_writer(command_words(view[writer.start():]))
    values = {}
    for kind, candidate in candidates:
        if candidate.start() in comments:
            continue
        start, end = original_span(candidate)
        if (complete_shell(text[:start] + '; if then ' + text[end:]) and
                complete_shell(text[:start] + ')); if then ((' + text[end:])) or output_argument(candidate.start()):
            continue
        if text[start:end] != candidate.group() or kind != 0:
            raise ValueError('Unsupported protected continued or arithmetic assignment')
        key = candidate['key']
        if key == 'LANGUAGE' and candidate.start() in fallback_positions:
            continue
        if candidate['operator'] != '=' or (candidate.start() and view[candidate.start() - 1] != '\n'):
            raise ValueError('Unsupported protected configuration assignment')
        if key in values:
            raise ValueError('Ambiguous installed configuration')
        if not command_boundary(text[:start]):
            raise ValueError('Configuration assignment is not at a static command boundary')
        raw = text[end:].split('\n', 1)[0]
        if not complete_shell(key + '=' + raw + '\n'):
            raise ValueError('Unsupported multiline configuration declaration')
        values[key] = {'raw': raw, 'position': start}
    return values


def control_values(text):
    declarations = protected_assignments(text, PROTECTED_KEYS)
    values = {key: declaration['raw'] for key, declaration in declarations.items()}
    for declaration in declarations.values():
        reject_unconditional_termination(text[:declaration['position']])
    if not set(BASE_KEYS) <= set(values):
        raise ValueError('Missing installed user configuration')
    return values


def edition_value(values):
    raw = values.get('PANEL_EDITION')
    if raw is None:
        return 'legacy'
    words = shlex.split(raw, comments=True)
    if len(words) != 1 or words[0] not in ('cn', 'intl'):
        raise ValueError('Unknown control regional edition')
    return words[0]


def configuration_profile(installer, control):
    if not isinstance(installer, bytes) or not isinstance(control, bytes):
        raise ValueError('Configuration profile requires exact script bytes')
    text = control.decode()
    values = control_values(text)
    if not 0 < len(installer) <= 2 * 1024 * 1024 or b'\0' in installer or not complete_shell(installer.decode()):
        raise ValueError('Invalid configuration installer')
    has_selector = edition_selection(installer.decode())
    active = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
    if not has_selector and re.search(r'[A-Za-z_]*(?:edition|region)[A-Za-z_]*', active, re.I):
        raise ValueError('Control regional interface lacks its installer selector')
    default = edition_value(values)
    if has_selector != (default != 'legacy'):
        raise ValueError('Installer selector and control edition disagree')
    return {'edition_selection': has_selector, 'default_edition': default,
            'protected_keys': sorted(values),
            'installer_sha256': hashlib.sha256(installer).hexdigest(),
            'control_sha256': hashlib.sha256(control).hexdigest()}


def package_configuration(package):
    return configuration_profile((Path(package) / 'install.sh').read_bytes(),
                                 (Path(package) / '1pctl').read_bytes())


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != {
            'edition_selection', 'default_edition', 'protected_keys', 'installer_sha256', 'control_sha256'}:
        raise ValueError('Missing or malformed configuration profile')
    if type(profile['edition_selection']) is not bool:
        raise ValueError('Malformed edition capability')
    keys = profile['protected_keys']
    if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys) or keys != sorted(set(keys)) or \
            not set(BASE_KEYS) <= set(keys) <= set(PROTECTED_KEYS):
        raise ValueError('Malformed protected configuration keys')
    if any(not isinstance(profile[k], str) or not re.fullmatch('[0-9a-f]{64}', profile[k])
           for k in ('installer_sha256', 'control_sha256')):
        raise ValueError('Missing bound configuration script hashes')
    selection = profile['edition_selection']
    if ('PANEL_EDITION' in keys) != selection or \
            profile['default_edition'] not in (('cn', 'intl') if selection else ('legacy',)):
        raise ValueError('Configuration profile edition disagreement')


def edition_transition(before, after, selected='intl'):
    validate_profile(before); validate_profile(after)
    if selected not in ('cn', 'intl'):
        raise ValueError('Unsupported predecessor edition selection')
    if before['edition_selection']:
        if not after['edition_selection']:
            raise ValueError('Upgrade would lose the installed edition selector')
        return {'kind': 'preserve-selection', 'before': selected, 'after': selected}
    if after['edition_selection']:
        return {'kind': 'introduce-target-default', 'before': 'legacy', 'after': after['default_edition']}
    return {'kind': 'legacy-no-selector', 'before': 'legacy', 'after': 'legacy'}


def installed_configuration(text, expected='intl'):
    values = control_values(text)
    if expected not in ('legacy', 'cn', 'intl') or edition_value(values) != expected:
        raise ValueError('Installed regional edition was not retained')
    return values


def migrated_configuration(before, target_text):
    """Mirror the reviewed upgrader's literal preservation, without execution."""
    result = control_values(target_text)
    result.update(before)
    words = shlex.split(before['BASE_DIR'], comments=True)
    if len(words) != 1:
        raise ValueError('Invalid installed data directory')
    result['BASE_DIR'] = shlex.quote(words[0])
    return result
