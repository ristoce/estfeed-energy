"""Offline repository checks; complements, but does not replace, HACS/hassfest."""
import argparse
import ast
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def validate(root=ROOT, allow_template=False, tag=None):
    errors = []
    def check(ok, message):
        if not ok: errors.append(message)
    folder = root / 'custom_components' / 'estfeed_energy'
    directories = sorted(p.name for p in (root/'custom_components').iterdir() if p.is_dir() and p.name != '__pycache__')
    check(directories == ['estfeed_energy'], 'Exactly one integration must exist: custom_components/estfeed_energy')
    manifest = json.loads((folder/'manifest.json').read_text())
    for key in ('domain', 'name', 'documentation', 'issue_tracker', 'codeowners', 'version', 'iot_class'):
        check(bool(manifest.get(key)), f'Manifest missing {key}')
    check(manifest['domain'] == 'estfeed_energy', 'Domain must remain estfeed_energy')
    check(bool(re.fullmatch(r'\d+\.\d+\.\d+', manifest['version'])), 'Use a numeric major.minor.patch version')
    check(list(manifest) == ['domain','name'] + sorted(set(manifest)-{'domain','name'}), 'Manifest keys must follow hassfest ordering')
    check(manifest.get('integration_type') == 'service', 'integration_type must be service')
    check(manifest['issue_tracker'] == manifest['documentation'] + '/issues', 'Issue URL must point to this repository')
    if not allow_template:
        check('YOUR_GITHUB_USERNAME' not in json.dumps(manifest), 'Run scripts/configure_repository.py OWNER/REPOSITORY first')
        check(bool(re.fullmatch(r'https://github.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', manifest['documentation'])), 'Documentation must point to your GitHub repository')
        check(all(re.fullmatch(r'@[A-Za-z0-9-]+', owner) for owner in manifest['codeowners']), 'Set a real GitHub codeowner')
    if tag:
        check(tag == 'v' + manifest['version'], f'Tag {tag!r} must equal v{manifest["version"]}')
    hacs = json.loads((root/'hacs.json').read_text())
    check(bool(hacs.get('name')) and hacs.get('render_readme') is True, 'HACS name and render_readme are required')
    check(not hacs.get('content_in_root'), 'HACS must use the custom_components directory')
    check(not hacs.get('zip_release'), 'HACS installs source from a release tag, not the full-project ZIP asset')
    for name in ('README.md', 'LICENSE', 'requirements-dev.txt',
                 '.github/workflows/validate.yaml', '.github/workflows/release.yaml',
                 'dashboards/estfeed-consumption-comparison-v1.8.yaml', 'dashboards/estfeed-electricity-prices.yaml'):
        check((root/name).is_file(), f'Missing required file: {name}')
    for path in (root/'custom_components').rglob('*.py'):
        ast.parse(path.read_text(), filename=str(path))
    for private in ('secrets.yaml', 'configuration.yaml', '.env', '.storage'):
        check(not (root/private).exists(), f'Remove private runtime file from repository: {private}')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-template', action='store_true', help='For package preparation only; release checks never use this')
    parser.add_argument('--tag', help='Release tag to compare with manifest.version')
    args = parser.parse_args()
    errors = validate(allow_template=args.allow_template, tag=args.tag)
    if errors:
        for error in errors: print('ERROR:', error, file=sys.stderr)
        raise SystemExit(1)
    print('Offline repository checks passed.' + (' GitHub identity is still a template.' if args.allow_template else ''))


if __name__ == '__main__':
    main()
