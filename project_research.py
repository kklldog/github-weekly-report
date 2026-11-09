"""Collect a bounded, commit-pinned evidence pack from the Top 1 repository."""
from __future__ import annotations
import base64
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlparse


def collect_project_evidence(rank: dict, fetch) -> dict:
    repo = rank['repo']
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('Invalid GitHub repository')
    api = f'https://api.github.com/repos/{repo}'
    metadata = json.loads(fetch(api))
    branch = metadata.get('default_branch', 'main')
    commit = json.loads(fetch(api + '/commits/' + quote(branch, safe='')))['sha']
    readme = json.loads(fetch(api + '/readme?ref=' + commit))
    if readme.get('encoding') != 'base64':
        raise RuntimeError('README evidence is unavailable')
    body = base64.b64decode(readme['content']).decode('utf-8', 'replace')
    root = f'https://raw.githubusercontent.com/{repo}/{commit}/'
    readme_url = root + quote(readme['path'], safe='/')
    sources = [{'url': readme_url, 'path': readme['path'], 'text': body[:22000]}]
    links = []
    for label, target in re.findall(r'\[([^\]]+)\]\(([^\s)]+)\)', body):
        if not re.search(r'\.md(?:#.*)?$', target, re.I):
            continue
        if re.search(r'changelog|contribut|code.of.conduct|license', target, re.I):
            continue
        url = urljoin(readme_url, target.split('#')[0])
        # Only same-repository, same-commit documents can enter the evidence pack.
        if not url.startswith(root) or url == readme_url:
            continue
        score = 0
        for pattern, weight in [('feature|usage|quick|overview', 5), ('execution|panel|authorization', 4), ('architecture|setup|guide', 2)]:
            if re.search(pattern, label + ' ' + target, re.I): score += weight
        links.append((score, url))
    seen = {readme_url}
    failures = []
    for _, url in sorted(links, key=lambda x: -x[0])[:6]:
        if url in seen: continue
        seen.add(url)
        try:
            document = fetch(url)
            sources.append({'url': url, 'path': urlparse(url).path.split(commit + '/', 1)[-1], 'text': document[:12000]})
        except Exception:
            failures.append(url)
        if len(sources) >= 4: break
    if len(body.strip()) < 150:
        raise RuntimeError('项目资料不足，停止生成空泛推荐，等待人工补充资料')
    return {'repo':repo, 'commit':commit, 'retrieved_at':datetime.now(timezone.utc).isoformat(),
            'sources':sources, 'unavailable_sources':failures}
