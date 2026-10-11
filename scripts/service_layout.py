"""Recognize service resources used by supported installers, without execution.

This is a conservative interface recognizer, not a general Bash interpreter.
Source authentication remains the caller's responsibility. Unknown commands or
control flow require review; archive membership alone never grants a capability.
The checks reject simple unconditional exit/return prefixes, but do not prove
general runtime reachability. Actual installation still requires native testing.
"""
import re
import subprocess


KINDS = {'service': 'systemd', 'init': 'sysvinit', 'openrc': 'openrc', 'procd': 'procd'}
MAX_SERVICE_COPIES = 2 * len(KINDS)
MAX_TERMINATORS = 64
TERMINATOR = re.compile(r'(?:^|(?<=[;\n]))[ \t]*(?P<command>exit|return)(?=[ \t;\n]|$)', re.M)


def complete_shell(text):
    result = subprocess.run(['bash', '-n'], input=text, text=True,
                            capture_output=True, timeout=5)
    return result.returncode == 0 and not result.stderr


def command_boundary(prefix, closing=''):
    previous = prefix.rstrip('\n').rsplit('\n', 1)[-1]
    if (len(previous) - len(previous.rstrip('\\'))) % 2:
        return False
    return complete_shell(prefix + '\n:\n' + closing)


def terminator_candidates(text):
    candidates = []
    for match in TERMINATOR.finditer(text):
        candidates.append(match)
        if len(candidates) > MAX_TERMINATORS:
            raise ValueError('Termination candidate count exceeds supported bounds')
    return candidates


def reject_unconditional_termination(prefix, closing='', branch_start=None):
    for candidate in terminator_candidates(prefix):
        position = candidate.start('command')
        before = prefix[:position]
        # A terminator in an open function, conditional or quoted string is not
        # a top-level terminator. For a known service branch also inspect that
        # branch's scope, excluding already completed alternative branches.
        in_scope = command_boundary(before)
        scope_closing = ''
        if not in_scope and branch_start is not None and position > branch_start:
            in_scope = command_boundary(before, closing)
            scope_closing = closing
        if not in_scope:
            continue
        # An unfinished if must produce a syntax error in command context. When
        # Bash accepts this same-line probe, a comment consumed it instead.
        if complete_shell(before + 'if true; then' + scope_closing):
            continue
        raise ValueError('Unconditional termination precedes required installer interface')


def function_body(text, name):
    pattern = (r'^(?:function[ \t]+)?' + re.escape(name) +
               r'[ \t]*\(\)[ \t]*\{[^\S\n]*\n(?P<body>.*?)^}[^\S\n]*(?:\n|$)')
    matches = list(re.finditer(pattern, text, re.M | re.S))
    if len(matches) != 1 or not complete_shell(text[:matches[0].start()]):
        raise ValueError('Unknown or ambiguous service installer function: ' + name)
    return matches[0].group('body')


def require_call(body, name):
    candidates = list(re.finditer(r'^[ \t]*' + re.escape(name) +
                                 r'[ \t]*(?:#[^\n]*)?$', body, re.M))
    if len(candidates) != 1 or not command_boundary(body[:candidates[0].start()]):
        raise ValueError('Service installer lacks the supported static call to ' + name)
    reject_unconditional_termination(body[:candidates[0].start()])


def service_copy_candidates(body, limit):
    candidates = []
    for line in re.finditer(r'^.*(?:\n|$)', body, re.M):
        command = line.group().strip()
        service_copy = (re.match(r'cp[ \t]', command) and
                        re.search(r'1panel-(?:core|agent)\.|/etc/systemd/system|/etc/init.d/1panel-', command))
        if (not command or command.startswith('#') or not
                (service_copy or re.search(r'\./[^\s]*1panel-(?:core|agent)\.', command))):
            continue
        candidates.append(line)
        if len(candidates) > limit:
            raise ValueError('Service copy candidate count exceeds supported bounds')
    return candidates


def service_layout(installer, member_names):
    if not isinstance(installer, bytes) or not 0 < len(installer) <= 2 * 1024 * 1024 or b'\0' in installer:
        raise ValueError('Invalid service installer bytes')
    text = installer.decode('utf-8')
    # Bound adversarial candidate floods before starting even the first Bash
    # process. The extra slots permit existing Docker service helper copies.
    service_copy_candidates(text, MAX_SERVICE_COPIES + 8)
    terminator_candidates(text)
    if not complete_shell(text):
        raise ValueError('Invalid service installer Bash')
    require_call(text, 'main')
    require_call(function_body(text, 'main'), 'Init_Panel')
    init = function_body(text, 'Init_Panel')
    helper = bool(re.search(r'^[ \t]*install_and_configure[ \t]*(?:#[^\n]*)?$', init, re.M))
    if helper:
        require_call(init, 'install_and_configure')
        body = function_body(text, 'install_and_configure')
        # These dispatch predicates are the supported multi-init interface.
        controls = [(line.start(), line.group().strip()) for line in
                    re.finditer(r'^[ \t]*(?:if\b[^\n]*|elif\b[^\n]*|else|fi)[ \t]*$', body, re.M)]
        if [value for _, value in controls] != [
                'if command -v systemctl &>/dev/null; then', 'else',
                'if [ -f /etc/rc.common ]; then', 'elif [ -f /sbin/openrc-run ]; then',
                'else', 'fi', 'fi']:
            raise ValueError('Unknown service-manager dispatch interface')
    else:
        body = init
    references = []
    # Only literal cp commands in the recognized static interface count.
    # bash -n rejects strings, heredocs and unclosed preceding constructs.
    # Bound the entire candidate list before invoking Bash for any copy prefix.
    # A near-limit script full of duplicate copies must not spawn per-line jobs.
    for line in service_copy_candidates(body, MAX_SERVICE_COPIES if helper else 2):
        command = line.group().strip()
        match = re.fullmatch(r'cp[ \t]+\./(?P<directory>initscript/)?1panel-(?P<role>core|agent)\.'
                             r'(?P<kind>service|init|openrc|procd)[ \t]+'
                             r'(?P<destination>/etc/systemd/system|/etc/init.d/1panel-(?:core|agent))'
                             r'[ \t]*(?:#[^\n]*)?', command)
        if match is None:
            raise ValueError('Unknown service resource copy command')
        directory, role, kind, destination = (match[k] for k in ('directory', 'role', 'kind', 'destination'))
        expected = '/etc/systemd/system' if kind == 'service' else '/etc/init.d/1panel-' + role
        if destination != expected or bool(directory) != helper or (not helper and kind != 'service'):
            raise ValueError('Conflicting service resource layout')
        if helper:
            first, last = {'service': (0, 1), 'procd': (2, 3), 'openrc': (3, 4), 'init': (4, 5)}[kind]
            if not controls[first][0] < line.start() < controls[last][0]:
                raise ValueError('Service resource differs from its manager branch')
        closing = ('\nfi\n' if kind == 'service' else '\nfi\nfi\n') if helper else ''
        if not command_boundary(body[:line.start()], closing):
            raise ValueError('Service copy is not at a supported command boundary')
        reject_unconditional_termination(body[:line.start()], closing,
                                         controls[first][0] if helper else None)
        references.append((directory or '') + '1panel-' + role + '.' + kind)
    kinds = tuple(KINDS) if helper else ('service',)
    expected = {(('initscript/' if helper else '') + '1panel-' + role + '.' + kind)
                for kind in kinds for role in ('core', 'agent')}
    if len(references) != len(expected) or set(references) != expected:
        raise ValueError('Incomplete or duplicate service resource pairs')
    if not expected <= set(member_names):
        raise ValueError('Required service resource missing: ' + ', '.join(sorted(expected - set(member_names))))
    return {'layout': 'initscript' if helper else 'root-systemd',
            'managers': [KINDS[kind] for kind in kinds], 'required': sorted(expected)}
