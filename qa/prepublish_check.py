"""비파괴 공개 준비 검사. 발견한 비밀의 값은 출력하지 않는다."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess

import pathspec

ROOT = Path(__file__).resolve().parents[1]
SECRET = re.compile(r'(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35}|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,}|xox[baprs]-[A-Za-z0-9-]{20,})')
LOCAL_PATH = re.compile(r'(?:[A-Z]:[\\/](?:Users|경남 AI SW)[\\/]|/home/[^/\s]+/|/Users/[^/\s]+/)')
TEXT = {'.py', '.js', '.cjs', '.json', '.md', '.txt', '.toml', '.yml', '.yaml', '.sql', '.env', '.example', '.wsf'}
PRIVATE = {'.env', 'secrets.env'}
# 직접 검토한 고정 합성 키만 예외로 처리한다.
SYNTHETIC_SHA256 = frozenset({
    '06751fa589fd41d945e409bcf87eca4ab4d71dbf15c3cf7fe82ff8a9c96ef788',
    '219c6595a0b06d9c1ccc5ba3be1465254cf25b510277e698fdd694790864bbb0',
    '2393ee179ea17d269b0eeee0e66247db3bc89b110c4fa48ee8c542cac2bb2be1',
    '23c0b291f71bb9e545b503dbfb1c9e92abbc76de494e38fde87a2f8be90a57ee',
    '48e01164acfddfd8f3dcf13bc0ee1fa094c901b8c0aebad6dd1d1ef898d6a723',
    '552b9e1a936cd47e9bf45c8230d7d35f201adee10457249e5d586352e601f934',
    '5f0b167c2532f5fd85cae57500f682f68197c39e3912f4fb63a9cd7afb2971d7',
    '98cdd25bc7c6ce4fcc6bb4f1ff70a32858842b09a053de7a9f1ea945ceea0d1b',
    'db4665d72437afb47b57b600b9af0c70c60680ba2188cfd076fa82f57f6525ff',
    'f301da1e7db6a4116b53270ad70145e50ec75a67f9b6613aa23f5ed5ffb415d8',
})


def encoded_write(path, value):
    value.encode('utf-8', errors='strict')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def sensitive_path(relative):
    p = Path(relative)
    return (p.name in PRIVATE or (p.name.startswith('.env.') and p.name != '.env.example')
            or p.suffix.lower() in {'.key', '.pem', '.pfx', '.p12', '.sqlite', '.sqlite3', '.db', '.gguf', '.safetensors'}
            or any(k in p.parts for k in {'workspace', 'workspaces', 'research_workspace', 'research_workspaces'})
            or relative.startswith('qa/live_api_test/') and p.name != 'README.md')


def scan_text(relative, text):
    result = []
    active = [v for k, v in os.environ.items() if v and len(v) >= 12 and (k.endswith('_KEY') or 'TOKEN' in k or 'SECRET' in k)]
    for number, line in enumerate(text.splitlines(), 1):
        matches = [m.group() for m in SECRET.finditer(line)] + [v for v in active if v in line]
        for value in set(matches):
            synthetic = sha256(value.encode('utf-8')).hexdigest() in SYNTHETIC_SHA256 and value not in active
            result.append({'path': relative, 'line': number, 'kind': 'EXPECTED_SYNTHETIC' if synthetic else 'SUSPECTED_SECRET',
                           'severity': 'INFO' if synthetic else 'P1', 'value': '[MASKED]', 'fingerprint': sha256(value.encode()).hexdigest()[:12]})
        if LOCAL_PATH.search(line):
            result.append({'path': relative, 'line': number, 'kind': 'ABSOLUTE_LOCAL_PATH', 'severity': 'P3'})
    return result


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=False, timeout=60)


def core_regression(record, source):
    current = record.get('source_fingerprint') == source
    status = 'PASS' if current and record.get('passed') is True else ('FAILED' if current and record.get('passed') is False else 'NOT_VALIDATED')
    return {'status': status, 'passed': status == 'PASS', 'current_source': current}


def inspect(root, *, max_bytes=10_000_000):
    spec = pathspec.GitIgnoreSpec.from_lines((root / '.gitignore').read_text(encoding='utf-8').splitlines())
    probe = git(root, 'rev-parse', '--show-toplevel')
    git_available = probe.returncode == 0 and Path(probe.stdout.decode().strip()).resolve() == root.resolve()
    tracked = git(root, 'ls-files', '-z').stdout.decode('utf-8').split('\x00') if git_available else []
    tracked = [x for x in tracked if x]
    findings, files, ignored = [], [], 0
    for base, dirs, names in os.walk(root, followlinks=False):
        relative_base = Path(base).relative_to(root)
        safe_dirs = []
        for directory in dirs:
            target = Path(base) / directory
            relative = (relative_base / directory).as_posix()
            if directory == '.git' or spec.match_file(relative + '/'):
                continue
            if target.is_symlink() or getattr(target, 'is_junction', lambda: False)():
                findings.append({'path': relative, 'kind': 'SYMLINK_REVIEW', 'severity': 'P2'})
            else:
                safe_dirs.append(directory)
        dirs[:] = safe_dirs
        for name in names:
            path = Path(base) / name
            relative = path.relative_to(root).as_posix()
            if spec.match_file(relative) and relative not in tracked:
                ignored += 1
                continue
            files.append(relative)
            if sensitive_path(relative):
                findings.append({'path': relative, 'kind': 'FORBIDDEN_FILE', 'severity': 'P1'})
            if path.is_symlink():
                findings.append({'path': relative, 'kind': 'SYMLINK_REVIEW', 'severity': 'P2'})
                continue
            size = path.stat().st_size
            if size > max_bytes:
                findings.append({'path': relative, 'kind': 'LARGE_FILE', 'bytes': size, 'severity': 'P2'})
                continue
            if path.suffix in TEXT or path.name in {'.gitignore', '.env.example'}:
                try:
                    findings.extend(scan_text(relative, path.read_text(encoding='utf-8', errors='strict')))
                except UnicodeError:
                    findings.append({'path': relative, 'kind': 'UTF8_REQUIRED', 'severity': 'P2'})
    # 무시 규칙 아래에 이미 추적된 자료도 별도로 검사한다.
    tracked_sensitive = [p for p in tracked if sensitive_path(p) or spec.match_file(p)]
    for relative in tracked_sensitive:
        findings.append({'path': relative, 'kind': 'TRACKED_SENSITIVE_OR_GENERATED', 'severity': 'P1'})
        path = root / relative
        if not path.is_symlink() and not getattr(path, 'is_junction', lambda: False)() and path.is_file() and path.stat().st_size <= max_bytes:
            try:
                findings.extend(scan_text(relative, path.read_text(encoding='utf-8')))
            except UnicodeError:
                pass
    history_status, history_objects = 'NOT_VALIDATED', 0
    if git_available:
        objects = git(root, 'rev-list', '--objects', '--all')
        history_status = 'VALIDATED' if objects.returncode == 0 else 'NOT_VALIDATED'
        for line in objects.stdout.decode('utf-8').splitlines():
            identity, sep, relative = line.partition(' ')
            if not sep or Path(relative).suffix not in TEXT:
                continue
            size = git(root, 'cat-file', '-s', identity)
            if size.returncode or int(size.stdout) > max_bytes:
                history_status = 'NOT_VALIDATED'
                continue
            blob = git(root, 'cat-file', 'blob', identity)
            if blob.returncode:
                history_status = 'NOT_VALIDATED'
                continue
            history_objects += 1
            try:
                findings.extend(scan_text('history/' + relative, blob.stdout.decode('utf-8')))
            except UnicodeError:
                pass
    required = ['src/htrsa/workbench.py', 'tests/test_workbench.py', 'docs/README.md', 'db/migrations/001_initial.sql', '.env.example', 'qa/fixtures/f3p_eval_config.json', '.github/workflows/offline.yml']
    ignored_required = [p for p in required if spec.match_file(p)]
    for p in ignored_required:
        findings.append({'path': p, 'kind': 'IGNORED_REQUIRED_SOURCE', 'severity': 'P1'})
    license_present = any(root.glob('LICENSE*'))
    blockers = []
    if any(f['severity'] in {'P0', 'P1'} for f in findings):
        blockers.append('UNRESOLVED_SECURITY_FINDING')
    if not git_available:
        blockers.append('GIT_TRACKED_AND_HISTORY_NOT_VALIDATED')
    elif history_status != 'VALIDATED':
        blockers.append('HISTORY_NOT_VALIDATED')
    if not license_present:
        blockers.append('LICENSE_PENDING_OWNER_CHOICE')
    return {'publication_status': 'BLOCKED' if blockers else 'READY', 'blockers': blockers,
            'git_status': 'VALIDATED' if git_available else 'NOT_A_GIT_REPOSITORY', 'history_status': history_status,
            'history_objects_scanned': history_objects, 'public_candidate_files': len(files), 'ignored_files_seen': ignored,
            'tracked_sensitive_files': tracked_sensitive, 'ignored_required_source_files': ignored_required,
            'suspected_secrets': sum(f['kind'] == 'SUSPECTED_SECRET' for f in findings),
            'large_files': [f for f in findings if f['kind'] == 'LARGE_FILE'],
            'absolute_local_paths': [f for f in findings if f['kind'] == 'ABSOLUTE_LOCAL_PATH'],
            'findings': findings, 'large_file_threshold': max_bytes, 'license_present': license_present,
            'github_settings': 'NOT_VALIDATED', 'dependency_findings': 'RUN_SEPARATELY', 'code_findings': 'RUN_SEPARATELY',
            'limits': ['무시된 로컬 자료는 공개 후보 검사에서 제외', '패턴·활성 비밀 검사 외 수동 사용자 자료 검토 필요', 'Git 이력이 없으면 공개 준비 완료로 표시하지 않음']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=ROOT / 'qa/prepublish/security_summary.json')
    parser.add_argument('--max-bytes', type=int, default=10_000_000)
    parser.add_argument('--require-ready', action='store_true')
    args = parser.parse_args()
    if args.max_bytes < 1:
        parser.error('크기 상한은 양수여야 합니다.')
    value = inspect(args.root.resolve(), max_bytes=args.max_bytes)
    for field, name in (('dependency_findings', 'dependency_audit.json'), ('code_findings', 'code_findings.json'),
                        ('workflow_findings', 'workflow_findings.json'), ('security_test_results', 'security_test_results.json')):
        report = args.output.parent / name
        if report.is_file():
            value[field] = json.loads(report.read_text(encoding='utf-8', errors='strict'))
    from htrsa.preflight import ROOT as runtime_root, _source_fingerprint
    record = {}
    marker = args.root.resolve() / 'build/validation/core.json'
    if args.root.resolve() == runtime_root.resolve() and marker.is_file():
        try:
            record = json.loads(marker.read_text(encoding='utf-8', errors='strict'))
        except (OSError, UnicodeError, ValueError):
            pass
    value['core_regression'] = core_regression(record, _source_fingerprint())
    if not value['core_regression']['passed']:
        value['publication_status'] = 'BLOCKED'
        value['blockers'].append('CORE_REGRESSION_' + value['core_regression']['status'])
    encoded_write(args.output, json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: value[k] for k in ('publication_status', 'blockers', 'public_candidate_files', 'suspected_secrets')}, ensure_ascii=False))
    if value['suspected_secrets'] or value['tracked_sensitive_files'] or value['ignored_required_source_files'] or (args.require_ready and value['publication_status'] != 'READY'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
