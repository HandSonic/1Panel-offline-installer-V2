#!/usr/bin/env python3
"""Inspect supported installer interfaces; no version table and no script execution.

Recognition selects the existing CLI/semantic-PTY adapter. It does not replace
native installation/upgrade tests. Unknown required interfaces are rejected.
"""
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile

from patch_installer import patch, MARKER

PROMPTS = ('TXT_SET_INSTALL_DIR', 'TXT_SET_PANEL_PORT', 'TXT_SET_PANEL_ENTRANCE',
           'TXT_SET_PANEL_USER', 'TXT_SET_PANEL_PASSWORD')
OPTIONS = ('non-interactive', 'lang', 'install-dir', 'port', 'entrance', 'username')


def function(text, name):
    matches = list(re.finditer(r'^function\s+' + re.escape(name) +
                              r'\s*\(\)\s*\{.*?^}[^\S\n]*(?:\n|$)', text, re.M | re.S))
    if len(matches) > 1:
        raise ValueError('Duplicate installer interface: ' + name)
    return matches[0].group() if matches else None


def require_argument_parser_call(text):
    """Require one literal call at a syntactically complete Bash boundary.

    Bash parses each preceding source prefix without executing it. A call inside
    an open function, conditional, group or quote has an incomplete prefix;
    unfinished heredocs also produce diagnostics even if Bash returns zero.
    """
    candidates = list(re.finditer(r'^parse_args[ \t]+"\$(?:@|\{@\})"[ \t]*(?:#[^\n]*)?$', text, re.M))
    if not 1 <= len(candidates) <= 16:
        raise ValueError('Installer argument parser candidate count exceeds supported bounds')
    active = 0
    for candidate in candidates:
        prefix = text[:candidate.start()]
        previous_line = prefix[:-1].rsplit('\n', 1)[-1] if prefix.endswith('\n') else prefix
        trailing = len(previous_line) - len(previous_line.rstrip('\\'))
        if trailing % 2:
            continue  # Bash joins this physical line to the preceding command.
        syntax = subprocess.run(['bash', '-n'], input=prefix, text=True,
                                capture_output=True, timeout=5)
        if syntax.returncode == 0 and not syntax.stderr:
            active += 1
    if active != 1:
        raise ValueError('Installer does not invoke its supported argument parser at a complete command boundary')


def optional_appstore(body):
    """Recognize an absent-payload return before any AppStore side effects.

    This is a supported shell interface, not an edition or version inference.
    Unknown guards remain required; merely mentioning a return is insufficient.
    """
    return body is not None and re.match(
        r'^function\s+Install_AppStore\s*\(\)\s*\{[ \t]*\n\s*'
        r'local[ \t]+(?P<variable>[A-Za-z_][A-Za-z_0-9]*)="\$(?:\{CURRENT_DIR\}|CURRENT_DIR)/appstore\.tar\.gz"[ \t]*\n\s*'
        r'if[ \t]+\[\[[ \t]+![ \t]+-f[ \t]+"\$(?:\{(?P=variable)\}|(?P=variable))"[ \t]+\]\];[ \t]*then[ \t]*\n\s*'
        r'return(?:[ \t]+0)?[ \t]*\n\s*fi[ \t]*(?:\n|$)', body) is not None


def edition_selection(text):
    """Recognize the selector interface, including incomplete/renamed remnants.

    Shared by original-source inspection and the bound offline upgrade reader.
    Absence describes a legacy interface; it does not identify a region.
    """
    active = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
    top_level = re.sub(r'^function\s+\w+\s*\(\)\s*\{.*?^}[^\S\n]*(?:\n|$)', '', text, flags=re.M | re.S)
    # Legacy Docker prompts mention geographic regions. Only selector/control
    # identifiers (including common renamed selector remnants) imply editions.
    edition = bool(re.search(r'\b(?:[A-Za-z_]*edition[A-Za-z_]*|(?:PANEL|SELECTED)_REGION|REGION_FILE)\b', active, re.I))
    if edition:
        assignment = r'^EDITION_FILE=(?:"\.selected_edition"|\'\.selected_edition\'|\.selected_edition)[ \t]*(?:#[^\n]*)?$'
        selector_read = r'^\s*selected_edition=\$\(cat[ \t]+"\$CURRENT_DIR/\$EDITION_FILE"\)[ \t]*$'
        control_write = r'^\s*sed\s+[^\n]*PANEL_EDITION[^\n]*(?:selected_edition|ESCAPED_SELECTED_EDITION)[^\n]*/usr/local/bin/1pctl[ \t]*$'
        if len(re.findall(assignment, active, re.M)) != 1 or \
                len(re.findall(selector_read, top_level, re.M)) != 1 or \
                len(re.findall(control_write, active, re.M)) != 1:
            raise ValueError('Unknown or incomplete installer edition-selection interface')
    return edition


def _inspect_interface(raw, member_names, source, *, repacked=False):
    if source not in ('official', 'custom', 'enterprise'):
        raise ValueError('Unknown installer source')
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 2 * 1024 * 1024 or b'\0' in raw:
        raise ValueError('Invalid installer script bytes')
    text = raw.decode('utf-8')
    syntax = subprocess.run(['bash', '-n'], input=text, text=True, capture_output=True, timeout=5)
    if syntax.returncode:
        raise ValueError('Installer is not valid Bash')
    if not repacked and MARKER in text:
        raise ValueError('Expected original vendor/source installer, not already repacked input')
    parser = function(text, 'parse_args')
    noninteractive = bool(re.search(r'^NON_INTERACTIVE=', text, re.M))
    if bool(parser) != noninteractive:
        raise ValueError('Incomplete non-interactive installer interface')
    if parser:
        require_argument_parser_call(text)
        for option in OPTIONS:
            if not re.search(r'^\s*--' + option + r'(?:\|[^)\n]+)?\)\s*$', parser, re.M):
                raise ValueError('Missing required installer option: --' + option)
        if not re.search(r'^CONFIG_PASSWORD=[^\n]*\$\{PANEL_PASSWORD(?::-[^}]*)?\}', text, re.M):
            raise ValueError('Installer lacks the supported password environment interface')
        mode = 'cli'
    else:
        if any(key not in text for key in PROMPTS):
            raise ValueError('Installer lacks required semantic interactive prompts')
        mode = 'interactive'
    edition = edition_selection(text)
    appstore_body = function(text, 'Install_AppStore')
    appstore = appstore_body is not None
    appstore_call = bool(re.search(r'^\s*Install_AppStore\s*$', text, re.M))
    appstore_payload = 'appstore.tar.gz' in member_names
    appstore_optional = optional_appstore(appstore_body)
    if appstore != appstore_call or (appstore_payload and not appstore):
        raise ValueError('Installer AppStore interface and archive payload disagree')
    if appstore and not appstore_payload and not appstore_optional:
        raise ValueError('Installer AppStore interface requires an archive payload')
    return {'adapter': mode, 'edition_selection': edition,
            'appstore_required': appstore_payload, 'appstore_optional': appstore_optional,
            'installer_sha256': hashlib.sha256(raw).hexdigest()}


def inspect_installer(raw, member_names, source):
    result = _inspect_interface(raw, member_names, source)
    # Current packages must still match the complete current byte-producing recipe.
    with tempfile.TemporaryDirectory(prefix='installer-capabilities-') as temporary:
        path = Path(temporary) / 'install.sh'
        path.write_bytes(raw)
        patch(path)
        patched_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(result, patched_installer_sha256=patched_sha)


def inspect_predecessor_installer(raw, member_names, source, original=None):
    """Check the historical interface, never regenerate it with today's recipe.

    The authenticated publication archive and its manifest bind executable bytes.
    These structural checks establish the supported native adapter/local-payload
    interface; they are not a Bash security sandbox or a no-network proof.
    """
    result = _inspect_interface(raw, member_names, source, repacked=True)
    text = raw.decode('utf-8')
    docker, compose = function(text, 'Install_Docker'), function(text, 'install_compose_offline')
    marker = re.escape(MARKER) + r'[ \t]*\n(?=function\s+install_compose_offline\s*\(\))'
    local_path = r'\$(?:\{CURRENT_DIR\}|CURRENT_DIR)/'
    if (text.count(MARKER) != 1 or re.search(r'^' + marker, text, re.M) is None or
            docker is None or compose is None or
            any(re.search(local_path + re.escape(name), docker) is None
                for name in ('docker.tgz', 'docker.service', 'docker-compose')) or
            re.search(local_path + 'docker-compose', compose) is None or
            '/usr/local/lib/docker/cli-plugins/docker-compose' not in compose or
            re.search(r'^\s*install_compose_offline\b', docker, re.M) is None):
        raise ValueError('Unsupported predecessor offline Docker/Compose interface')
    if original is not None:
        previous = _inspect_interface(original, member_names, source)
        if any(result[key] != previous[key] for key in
               ('adapter', 'edition_selection', 'appstore_required', 'appstore_optional')):
            raise ValueError('Predecessor installer interface differs from authenticated source')

        def source_body(script):
            # Only the offline Docker replacement and public-IP removal are
            # owned by the lower publisher. Preserve the application's source
            # logic exactly outside those blocks. Do not globally strip comments
            # or blank lines: inside heredocs/quoted strings they are source data.
            script = re.sub(r'^' + marker, '', script, flags=re.M)
            for name in ('Install_Docker', 'install_compose_offline',
                         'Install_Iptables_Offline', 'Install_Docker_Offline'):
                body = function(script, name)
                if body is not None:
                    start = script.index(body)
                    end = start + len(body)
                    prefix = re.sub(r'\n(?:[ \t]*\n)*$', '\n', script[:start])
                    separator = re.match(r'(?:[ \t]*\n)*', script[end:]).group()
                    script = prefix + script[end + len(separator):]
            script = re.sub(r'^[ \t]*PUBLIC_IP=\$\(curl[^\n]*api64\.ipify\.org\)[ \t]*(?:#[^\n]*)?(?:\n|$)', '', script, flags=re.M)
            script = re.sub(r'^[ \t]*PUBLIC_IP=(?:"[^"$`\\]*"|\'[^\']*\')[ \t]*(?:#[^\n]*)?(?:\n|$)', '', script, flags=re.M)
            return script

        if source_body(text) != source_body(original.decode('utf-8')):
            raise ValueError('Predecessor installer changed authenticated application source')
    return result
