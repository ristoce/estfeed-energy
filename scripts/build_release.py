"""Create a complete manual-install/project ZIP; HACS uses GitHub source instead."""
import argparse
import json
from pathlib import Path
import zipfile
from validate_repository import ROOT, validate

TOP_FILES = {
    'README.md', 'LICENSE', 'hacs.json', 'requirements-dev.txt', 'PUBLISHING.md', 'CHANGELOG.md',
    '.gitignore', '.gitattributes',
    'configuration-example.yaml', 'pricing-example.yaml', 'price-signals-example.yaml',
    'PRICING.md', 'PRICE-SIGNALS.md',
    'UPGRADE-0.3.1.md', 'UPGRADE-0.3.2.md', 'UPGRADE-0.3.3.md', 'UPGRADE-0.4.0.md',
}
FOLDERS = ('custom_components', 'dashboards', 'automations', 'tests', 'scripts', '.github')


def build(allow_template=False, tag=None):
    errors = validate(allow_template=allow_template, tag=tag)
    if errors: raise ValueError('; '.join(errors))
    version = json.loads((ROOT/'custom_components/estfeed_energy/manifest.json').read_text())['version']
    files = {ROOT/name for name in TOP_FILES if (ROOT/name).is_file()}
    for name in FOLDERS:
        files.update(p for p in (ROOT/name).rglob('*') if p.is_file() and not p.is_symlink()
                     and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo')
                     and p.name not in ('secrets.yaml', 'configuration.yaml', '.env'))
    output = ROOT/'dist'/f'estfeed-energy-v{version}.zip'
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            if path.is_symlink(): raise ValueError('Release files must not be symlinks')
            archive.write(path, path.relative_to(ROOT))
    print(output)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag')
    parser.add_argument('--allow-template', action='store_true', help='Only for preparing an unpublished repository template')
    args = parser.parse_args()
    build(args.allow_template, args.tag)
