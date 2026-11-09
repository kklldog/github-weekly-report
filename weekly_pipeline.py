#!/usr/bin/env python3
"""Weekly GitHub rank -> WeChat tie-tu preview/publish pipeline.

The job intentionally only reads rank #1 from OpenGithubs/github-weekly-rank.
It stores the generated package under data/<week-id>/ and sends a preview email.
Publishing is gated by a reply containing the exact word 确认.
"""
from __future__ import annotations

import base64
import email
import email.message
import email.policy
import email.utils
import imaplib
import json
import os
import re
import smtplib
import ssl
import hashlib
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from confirmation import find_confirmation_uid


CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
SOURCE_REPO = "https://github.com/OpenGithubs/github-weekly-rank"
RAW_README = "https://raw.githubusercontent.com/OpenGithubs/github-weekly-rank/main/README.md"
LOG = logging.getLogger('github-weekly')


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def preview_subject(rank: dict, editorial: dict) -> str:
    return f"[GitHub周榜贴图预览] {rank['week_id']} · {editorial['title']}"


def package_hash(folder: Path) -> str:
    post = json.loads((folder / 'post.json').read_text(encoding='utf-8'))
    digest = hashlib.sha256(json.dumps({'editorial': post['editorial'], 'author': post['author']},
                                     ensure_ascii=False, sort_keys=True).encode())
    for image in sorted((folder / 'images').glob('*.png')):
        digest.update(image.name.encode())
        digest.update(image.read_bytes())
    return digest.hexdigest()


def validate_package(folder: Path) -> None:
    post = json.loads((folder / 'post.json').read_text(encoding='utf-8'))
    editorial = post['editorial']
    if not 1 <= len(editorial['title'].encode('utf-16-le')) // 2 <= 20:
        raise ValueError('贴图标题超过20字；必须重新预览后确认，不能自动截断')
    caption = editorial['content'] + '\n\n作者：' + post['author']
    if not 1 <= len(caption.encode('utf-16-le')) // 2 <= 1000:
        raise ValueError('贴图正文超过1000字；必须重新预览后确认，不能自动截断')
    if not 3 <= len(list((folder / 'images').glob('*.png'))) <= 9:
        raise ValueError('贴图必须有3至9张图片')


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "github-weekly-top1/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        # The local deployment may need the supplied SOCKS proxy. curl is
        # available on Ubuntu and understands socks5h directly.
        proxy = env("SOCKS_PROXY", "socks5h://192.168.0.124:7890")
        p = subprocess.run(["curl", "-fsSL", "--max-time", "45", "--proxy", proxy, url], capture_output=True)
        if p.returncode:
            raise RuntimeError(f"cannot fetch {url}: {p.stderr.decode(errors='replace')[:300]}")
        return p.stdout.decode("utf-8", "replace")


def latest_week_id(text: str) -> str:
    dates = re.findall(r"(\d{4}\.\d{2}\.\d{2})(?:-(?:\d{4}\.)?(\d{2}\.\d{2}))?\s+周榜排行", text)
    if dates:
        end = dates[0][1]
        start = dates[0][0].replace(".", "-")
        return start + ("-" + end.replace(".", "-") if end else "")
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def parse_top1(text: str) -> dict:
    week_id = latest_week_id(text)
    # The highlighted section is stable and contains all fields needed for a
    # useful short post. Fall back to the detail section for older revisions.
    m = re.search(r"榜单增长：第1名:\s*([^\s*]+).*?(?=\n\s*\*\*榜单增长：第2名|\Z)", text, re.S)
    if not m:
        m = re.search(r"###\s*1\.\s*(https?://github\.com/[^\s\n]+).*?(?=\n###\s*2\.|\Z)", text, re.S)
        block = m.group(0) if m else ""
        repo = re.search(r"github\.com/([\w.-]+/[\w.-]+)", block)
        repo_name = repo.group(1) if repo else ""
    else:
        block = m.group(0)
        repo_name = m.group(1).strip().rstrip("*")
    if not repo_name:
        raise RuntimeError("rank #1 was not found in the source page")
    def find(pattern: str, default: str = "") -> str:
        q = re.search(pattern, block, re.I)
        if not q:
            return default
        return (q.group(1) if q.lastindex else q.group(0)).strip()
    url = find(r"https?://github\.com/[\w.-]+/[\w.-]+", f"https://github.com/{repo_name}")
    # find() returns the full URL for URL patterns.
    stars = find(r"总星标数量：\s*([^\n*]+)", "未知")
    weekly = find(r"周Star增长量：\s*([^\n*]+)", find(r"上周增长数量：\s*([^\n*]+)", "未知"))
    opened = find(r"开源时间：\s*([^\n*]+)", "未知")
    desc = find(r"项目描述：\s*(.*?)(?:\n|\r|\*\*)", "暂无项目描述")
    if desc == "暂无项目描述":
        desc = find(r"项目描述：\s*(.*)", desc)
    return {"week_id": week_id, "repo": repo_name, "url": url, "stars": stars,
            "weekly_growth": weekly, "opened": opened, "description": re.sub(r"\s+", " ", desc).strip()}


def read_key() -> str:
    path = Path(env("GPT_KEY_FILE", str(ROOT / ".secrets" / "gptkey.txt")))
    if not path.exists():
        raise RuntimeError(f"GPT key file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def chat_json(rank: dict) -> dict:
    prompt = f"""你是中文科技公众号编辑。只围绕 GitHub 周榜第 1 名写一条微信贴图。
数据（仅可使用这些事实，不要臆造）：{json.dumps(rank, ensure_ascii=False)}
输出严格 JSON：title（不超过20个汉字，吸引但准确）、content（不超过600字，分3段，适合贴图说明，末尾含3个话题标签）、image_prompts（恰好3条英文视觉提示词；要求无文字、无Logo、水印，3:4竖版，分别体现项目主题/工作流/结果）。
"""
    base = env("GPT_BASE_URL", "https://ai.dns-pro.net/v1").rstrip("/")
    data = json.dumps({"model": env("GPT_MODEL", "gpt-6.1-sol"), "temperature": 0.4,
                       "messages": [{"role": "system", "content": "只输出合法 JSON，不要 Markdown。"},
                                    {"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=data,
                                 headers={"Authorization": "Bearer " + read_key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = json.loads(r.read().decode())
        content = payload["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.I | re.M).strip()
        out = json.loads(content)
        if not isinstance(out.get("image_prompts"), list) or len(out["image_prompts"]) < 3:
            raise ValueError("model returned fewer than 3 image prompts")
        return out
    except Exception as exc:
        # A deterministic fallback keeps a weekly run useful during a model
        # outage while making the error visible in the preview email.
        return {"title": f"本周GitHub第1名：{rank['repo'].split('/')[-1]}",
                "content": (f"本周 GitHub 飙升榜第 1 名是 {rank['repo']}。项目当前约有 {rank['stars']} 个 Star，"
                            f"本周新增 {rank['weekly_growth']}。它的核心方向是：{rank['description']}\n\n"
                            f"如果你正在关注 AI 编程和自动化工作流，这个项目值得点开看看。开源时间：{rank['opened']}。\n\n"
                            "#GitHub周榜 #开源项目 #AI工具"),
                "image_prompts": [
                    "Editorial vertical illustration of an AI coding studio with many autonomous agent workstations, cinematic blue and amber lighting, clean modern 3D, no text, no logo, no watermark, 3:4 portrait",
                    "Editorial vertical illustration of a coordinated software team represented by luminous nodes and workflow paths around a code repository, polished technology magazine style, no text, no logo, no watermark, 3:4 portrait",
                    "Editorial vertical illustration of a finished indie game prototype emerging from an AI powered development studio, celebratory but professional, detailed 3D render, no text, no logo, no watermark, 3:4 portrait",
                ], "model_error": str(exc)}


def generate_images(prompts: list[str], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cli = Path(env("IMAGEGEN_CLI", str(ROOT / "image_gen.py")))
    if not cli.exists():
        raise RuntimeError(f"imagegen CLI not found: {cli}")
    # The imagegen skill's bundled CLI is used so provider/model settings are
    # explicit and reproducible. The provider is loaded by the calling shell.
    outputs = []
    for i, prompt in enumerate(prompts[:3], 1):
        target = out_dir / f"{i:02d}.png"
        cmd = [sys.executable, str(cli), "generate", "--model", env("IMAGE_MODEL", "gpt-image-2"),
               "--prompt", prompt, "--size", "1024x1536", "--quality", "high", "--out", str(target)]
        child_env = os.environ.copy()
        # Keep the image CLI on the same explicitly configured provider as
        # the editorial model; cron jobs do not inherit an interactive shell.
        child_env["OPENAI_API_KEY"] = read_key()
        child_env["OPENAI_BASE_URL"] = env("GPT_BASE_URL", "https://ai.dns-pro.net/v1")
        p = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, env=child_env)
        if p.returncode:
            raise RuntimeError(f"image generation failed for image {i}: {p.stderr[-500:]}")
        outputs.append(target)
    return outputs


def save_package(rank: dict, editorial: dict, images: list[Path]) -> tuple[Path, Path]:
    week = re.sub(r"[^0-9-]", "", rank["week_id"]) or datetime.now(CN_TZ).strftime("%Y-%m-%d")
    folder = DATA / week
    folder.mkdir(parents=True, exist_ok=True)
    post = {"rank": rank, "editorial": editorial, "author": env("AUTHOR", "MJZHOU"),
            "created_at": datetime.now(CN_TZ).isoformat(), "status": "preview"}
    (folder / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    md = f"---\ntitle: {editorial['title']}\nauthor: {env('AUTHOR', 'MJZHOU')}\n---\n\n{editorial['content']}\n\n原榜单：{rank['url']}\n"
    (folder / "post.md").write_text(md, encoding="utf-8")
    pending = DATA / "pending.json"
    pending.write_text(json.dumps({"week_id": week, "folder": str(folder), "status": "preview"}, ensure_ascii=False, indent=2), encoding="utf-8")
    return folder, pending


def send_preview(folder: Path, rank: dict, editorial: dict) -> None:
    sender = env("SMTP_USER", "17014842@qq.com")
    recipient = env("PREVIEW_TO", "zhouminjie1986@foxmail.com")
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = preview_subject(rank, editorial)
    msg['Message-ID'] = email.utils.make_msgid(domain='qq.com')
    msg['Date'] = email.utils.format_datetime(datetime.now(CN_TZ))
    model_error = f"\n\n模型提示：{editorial['model_error']}" if editorial.get("model_error") else ""
    body = (f"这是 {rank['week_id']} GitHub 周榜第 1 名的贴图预览。\n\n"
            f"标题：{editorial['title']}\n作者：{env('AUTHOR', 'MJZHOU')}\n"
            f"项目：{rank['repo']}\n链接：{rank['url']}\n\n"
            f"正文（{len(editorial['content'])} 字）：\n{editorial['content']}"
            f"{model_error}\n\n确认以上文字及3张附图后，请直接回复本邮件，单独写：确认\n"
            "收到确认后系统会进入微信公众号发表流程；如需管理员扫码，会另发二维码邮件。")
    msg.set_content(body)
    images = sorted((folder / 'images').glob('*.png'))
    if len(images) < 3:
        raise ValueError('预览邮件必须附带至少3张图片')
    for p in images:
        msg.add_attachment(p.read_bytes(), maintype="image", subtype="png", filename=p.name)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(env("SMTP_HOST", "smtp.qq.com"), int(env("SMTP_PORT", "465")), context=context, timeout=45) as s:
        s.login(sender, env("SMTP_AUTHCODE"))
        s.send_message(msg)
    pending_path = DATA / 'pending.json'
    pending = json.loads(pending_path.read_text(encoding='utf-8'))
    pending.update({'preview_subject': str(msg['Subject']), 'preview_message_id': str(msg['Message-ID']),
                    'preview_sent_at': datetime.now(CN_TZ).isoformat(), 'package_hash': package_hash(folder)})
    write_json(pending_path, pending)


def notify(subject: str, body: str, attachment: Path | None = None) -> None:
    msg = email.message.EmailMessage()
    msg['From'] = env('SMTP_USER', '17014842@qq.com')
    msg['To'] = env('PREVIEW_TO', 'zhouminjie1986@foxmail.com')
    msg['Subject'] = '[GitHub周榜贴图] ' + subject
    msg['Message-ID'] = email.utils.make_msgid(domain='qq.com')
    msg['Date'] = email.utils.format_datetime(datetime.now(CN_TZ))
    msg.set_content(body)
    if attachment:
        msg.add_attachment(attachment.read_bytes(), maintype='image', subtype='png', filename=attachment.name)
    with smtplib.SMTP_SSL(env('SMTP_HOST', 'smtp.qq.com'), int(env('SMTP_PORT', '465')),
                          context=ssl.create_default_context(), timeout=45) as smtp:
        smtp.login(msg['From'], env('SMTP_AUTHCODE'))
        smtp.send_message(msg)


def run_once() -> Path:
    source = http_get(RAW_README)
    rank = parse_top1(source)
    period = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})', rank['week_id'])
    if period:
        year, start_month, _, end_month, end_day = map(int, period.groups())
        end_year = year + (end_month < start_month)
        ended = datetime(end_year, end_month, end_day, tzinfo=CN_TZ).date()
        today = datetime.now(CN_TZ).date()
        latest_sunday = today - timedelta(days=today.weekday() + 1)
        if ended < latest_sunday:
            raise RuntimeError('本周榜单尚未更新，保留现有内容并稍后重试')
    folder = DATA / re.sub(r'[^0-9-]', '', rank['week_id'])
    if (folder / 'post.json').exists():
        pending_path = DATA / 'pending.json'
        pending = json.loads(pending_path.read_text(encoding='utf-8')) if pending_path.exists() else {}
        if pending.get('folder') == str(folder) and pending.get('status') == 'preview' and not pending.get('preview_sent_at'):
            post = json.loads((folder / 'post.json').read_text(encoding='utf-8'))
            validate_package(folder)
            send_preview(folder, post['rank'], post['editorial'])
        LOG.info('本期已生成，保留预览及确认状态：%s', folder)
        return folder
    pending_file = DATA / 'pending.json'
    if pending_file.exists():
        active = json.loads(pending_file.read_text(encoding='utf-8'))
        if active.get('status') not in {'preview', 'published'}:
            raise RuntimeError('上一期仍在发表流程，暂不覆盖待处理确认')
    editorial = chat_json(rank)
    images = generate_images(editorial["image_prompts"], DATA / re.sub(r"[^0-9-]", "", rank["week_id"]) / "images")
    folder, _ = save_package(rank, editorial, images)
    validate_package(folder)
    send_preview(folder, rank, editorial)
    print(json.dumps({"status": "preview_sent", "folder": str(folder), "week_id": rank["week_id"], "repo": rank["repo"]}, ensure_ascii=False))
    return folder


def publish(folder: Path) -> None:
    """Publish a durably approved package and require platform evidence."""
    validate_package(folder)
    post = json.loads((folder / "post.json").read_text(encoding="utf-8"))
    if post.get('status') == 'published':
        return
    pending_path = DATA / 'pending.json'
    pending = json.loads(pending_path.read_text(encoding='utf-8'))
    if not pending.get('confirmed_uid') or pending.get('package_hash') != package_hash(folder):
        raise RuntimeError('没有与当前内容匹配的邮件确认，禁止发表')
    if Path(pending['folder']).resolve() != folder.resolve():
        raise RuntimeError('确认所属期次与发表目录不一致')
    cmd = env("WECHAT_PUBLISH_CMD")
    if not cmd:
        # The server install writes a wrapper command. Keeping this explicit
        # prevents accidental publication when the browser setup is absent.
        raise RuntimeError("WECHAT_PUBLISH_CMD is not configured")
    env2 = os.environ.copy()
    env2.update({"WECHAT_TITLE": post["editorial"]["title"], "WECHAT_CONTENT": post["editorial"]["content"],
                 'WECHAT_POST_DIR': str(folder.resolve()), 'WECHAT_AUTHOR': post['author'],
                 'WECHAT_RESULT_FILE': str(folder / 'wechat-result.json')})
    if pending.get('status') in {'publish_unknown', 'reviewing'}:
        env2['WECHAT_RECONCILE_ONLY'] = '1'
    state_path = folder / 'wechat-state.json'
    result_path = folder / 'wechat-result.json'
    pending['status'] = 'publishing'
    write_json(pending_path, pending)
    state_path.unlink(missing_ok=True)
    result_path.unlink(missing_ok=True)
    notified = set(pending.get('qr_notifications', []))
    log_path = folder / 'publish.log'
    with log_path.open('a', encoding='utf-8') as log:
        p = subprocess.Popen(cmd, shell=True, cwd=str(ROOT), env=env2,
                             stdout=log, stderr=subprocess.STDOUT)
        while p.poll() is None:
            try:
                if state_path.exists():
                    state = json.loads(state_path.read_text(encoding='utf-8'))
                    stage = state.get('status', 'publishing')
                    pending['browser_stage'] = stage
                    pending['updated_at'] = datetime.now(CN_TZ).isoformat()
                    qr_file = state.get('qr_path')
                    if qr_file and Path(qr_file).exists():
                        key = stage + ':' + state.get('qr_id', qr_file)
                        pending['status'] = stage
                        if key not in notified:
                            notify(f"{pending['week_id']} 需要微信扫码",
                                   '已收到本期确认。请用管理员微信扫描附件二维码。扫码后系统将继续发表；'
                                   '登录验证和最终发表验证可能分别需要扫码。', Path(qr_file))
                            notified.add(key)
                            pending['qr_notifications'] = sorted(notified)
                    write_json(pending_path, pending)
            except Exception:
                LOG.exception('读取浏览器状态或发送二维码失败')
            time.sleep(2)
    result = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else {}
    if result.get('status') in {'reviewing', 'publish_unknown'}:
        pending.update({'status': result['status'], 'platform_result': result,
                        'error': result.get('error', '微信后台结果尚待核实')})
        write_json(pending_path, pending)
        if not pending.get('submitted_notice_sent'):
            notify(f"{pending['week_id']} 已提交，等待微信结果",
                   '确认已保留，已进入微信发表流程。系统只会核实后台状态，不会重复提交。\n' + pending['error'])
            pending['submitted_notice_sent'] = True
            write_json(pending_path, pending)
        return
    if p.returncode or result.get('status') != 'published':
        stage = result.get('status', 'failed')
        if stage not in {'waiting_login', 'waiting_publish_scan', 'publish_unknown', 'failed'}:
            stage = 'failed'
        pending.update({'status': stage, 'error': result.get('error', f'浏览器退出码 {p.returncode}；详见 {log_path}')})
        write_json(pending_path, pending)
        raise RuntimeError(pending['error'])
    pending.update({'status': 'published', 'platform_result': result, 'published_at': datetime.now(CN_TZ).isoformat()})
    post.update({'status': 'published', 'platform_result': result, 'published_at': pending['published_at']})
    write_json(folder / 'post.json', post)
    write_json(pending_path, pending)
    notify(f"{pending['week_id']} 已发表", f"{post['editorial']['title']}\n\n微信后台已确认发表成功。\n"
           f"{result.get('public_url', '')}\n作者：{post['author']}")


def poll_once(*, publish_confirmed: bool = True) -> None:
    pending_file = DATA / 'pending.json'
    if not pending_file.exists():
        return
    pending = json.loads(pending_file.read_text(encoding='utf-8'))
    if pending.get('status') == 'preview':
        folder = Path(pending['folder'])
        post = json.loads((folder / 'post.json').read_text(encoding='utf-8'))
        with imaplib.IMAP4_SSL(env('IMAP_HOST', 'imap.qq.com'), int(env('IMAP_PORT', '993')), timeout=30) as imap:
            imap.login(env('SMTP_USER', '17014842@qq.com'), env('SMTP_AUTHCODE'))
            imap.select('INBOX', readonly=True)
            uid = find_confirmation_uid(imap, week_id=pending['week_id'],
                                        expected_sender=env('PREVIEW_TO', 'zhouminjie1986@foxmail.com'),
                                        mailbox=env('SMTP_USER', '17014842@qq.com'),
                                        preview_subject=pending.get('preview_subject', preview_subject(post['rank'], post['editorial'])),
                                        preview_message_id=pending.get('preview_message_id'))
        if uid:
            actual_hash = package_hash(folder)
            if pending.get('package_hash') and pending['package_hash'] != actual_hash:
                raise RuntimeError('预览发送后内容已发生变化，请重新发送预览')
            pending.update({'status': 'confirmed', 'confirmed_uid': uid.decode(),
                            'confirmed_at': datetime.now(CN_TZ).isoformat(), 'package_hash': actual_hash})
            write_json(pending_file, pending)
            LOG.info('确认已持久保存：week=%s uid=%s', pending['week_id'], uid.decode())
    if publish_confirmed and pending.get('status') in {
        'confirmed', 'publishing', 'waiting_login', 'waiting_publish_scan', 'reviewing', 'publish_unknown'
    }:
        publish(Path(pending['folder']))


def watch() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    # Linux flock prevents a manual recovery and the service from publishing
    # concurrently. The lock is released by the OS on a process crash.
    lock = (DATA / 'watcher.lock').open('a')
    if sys.platform != 'win32':
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    while True:
        try:
            poll_once()
        except Exception as exc:
            LOG.exception('发布监听失败')
            error_path = DATA / 'watcher-error.log'
            with error_path.open('a', encoding='utf-8') as log:
                log.write(f'{datetime.now(CN_TZ).isoformat()} {exc}\n')
            try:
                notify('发表未完成', f'本期自动发表遇到问题：{exc}\n确认已保留，不需要重复回复。')
            except Exception:
                LOG.exception('错误通知发送失败')
        time.sleep(int(env("POLL_SECONDS", "300")))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if len(sys.argv) < 2 or sys.argv[1] == "run":
        run_once()
    elif sys.argv[1] == "watch":
        watch()
    elif sys.argv[1] == "publish":
        publish(Path(sys.argv[2]))
    elif sys.argv[1] == 'check-confirmation':
        poll_once(publish_confirmed=False)
    else:
        raise SystemExit("usage: weekly_pipeline.py [run|watch|publish FOLDER]")
