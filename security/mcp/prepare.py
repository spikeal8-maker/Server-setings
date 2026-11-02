"""Prepare only new candidate sources; never reads a live deployment/configuration."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PINS = json.loads((ROOT / 'security/mcp/pins.json').read_text())


def prepare(destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name, repository, revision in (
        ('gateway', 'https://github.com/sigbit/mcp-auth-proxy.git', PINS['gateway_commit']),
        ('panel', 'https://github.com/1Panel-dev/mcp-1panel.git', PINS['panel_commit']),
    ):
        path = destination / name
        subprocess.run(['git', 'clone', '--quiet', repository, str(path)], check=True)
        subprocess.run(['git', '-C', str(path), 'checkout', '--quiet', '--detach', revision], check=True)
        actual = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != revision:
            raise RuntimeError('Upstream revision mismatch')
    # Reuse the reviewed exact-source OAuth/RFC9728/DCR changes without editing
    # the production installer or the existing patcher's compatibility contract.
    subprocess.run([sys.executable, str(ROOT / 'scripts/prepare-mcp-release.py'), str(destination / 'gateway')], check=True)
    modules = '\n'.join(f'{name}@{version}' for name, version in PINS['gateway_modules'].items()) + '\n'
    (destination / 'gateway-modules.txt').write_text(modules)
    print('CANDIDATE_SOURCES_READY_NO_DEPLOYMENT')


if __name__ == '__main__':
    prepare(sys.argv[1])
