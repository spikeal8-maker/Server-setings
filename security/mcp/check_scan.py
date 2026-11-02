"""Fail closed on findings, EOL OS, or absent executable/package coverage."""
import json
from pathlib import Path
import sys


def check(document, binaries, require_os):
    results = document.get('Results', [])
    os_info = document.get('Metadata', {}).get('OS', {})
    if require_os and (os_info.get('Family') != 'debian' or os_info.get('Name') != '12' and not os_info.get('Name', '').startswith('12.')):
        raise ValueError('Expected supported Debian 12 runtime detection')
    if os_info.get('EOSL'):
        raise ValueError('EOL operating system')
    for binary in binaries:
        matches = [r for r in results if r.get('Class') == 'lang-pkgs' and r.get('Type') == 'gobinary' and r.get('Target', '').endswith('/' + binary)]
        if len(matches) != 1 or not matches[0].get('Packages'):
            raise ValueError('Missing Go binary/package coverage: ' + binary)
    findings = [v for r in results for v in r.get('Vulnerabilities', []) if v.get('Severity') in ('HIGH', 'CRITICAL') and v.get('FixedVersion')]
    if findings:
        raise ValueError(f'{len(findings)} fixable HIGH/CRITICAL findings')
    print('PASS supported OS and executable coverage; zero fixable HIGH/CRITICAL')


if __name__ == '__main__':
    mode, path = sys.argv[1:]
    if mode not in ('image', 'binary'):
        raise ValueError('Mode must be image or binary')
    check(json.loads(Path(path).read_text()), ['mcp-auth-proxy', 'mcp-1panel'] if mode == 'image' else ['mcp-1panel'], mode == 'image')
