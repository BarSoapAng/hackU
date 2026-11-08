"""Run a module with this checkout's .env, without shell-sourcing secrets."""
import os
from pathlib import Path
import re
import runpy
import shlex
import sys


def read_environment(path=Path('.env')):
    values = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        name, separator, value = line.partition('=')
        name, value = name.strip(), value.strip()
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise ValueError('Invalid environment file entry')
        if value[:1] in ('\"', "'"):
            parts = shlex.split(value, comments=False)
            if len(parts) != 1:
                raise ValueError('Invalid quoted environment value')
            value = parts[0]
        values[name] = value
    return values


def update_environment(changes, path=Path('.env')):
    text = path.read_text(encoding='utf-8-sig')
    for name, value in changes.items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise ValueError('Invalid environment name')
        entry = name + '=' + shlex.quote(value)
        pattern = rf'^(?:export )?{re.escape(name)}=.*$'
        if re.search(pattern, text, re.MULTILINE):
            text = re.sub(pattern, lambda _: entry, text, flags=re.MULTILINE)
        else:
            text = text.rstrip() + '\n' + entry + '\n'
    path.write_text(text, encoding='utf-8')


def main():
    if len(sys.argv) < 2:
        raise SystemExit('Usage: python -m tools.local MODULE [ARGS...]')
    os.environ.update(read_environment())
    sys.argv = sys.argv[1:]
    runpy.run_module(sys.argv[0], run_name='__main__')


if __name__ == '__main__':
    main()
