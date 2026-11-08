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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
SOURCE_REPO = "https://github.com/OpenGithubs/github-weekly-rank"
RAW_README = "https://raw.githubusercontent.com/OpenGithubs/github-weekly-rank/main/README.md"


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
    msg["Subject"] = f"[GitHub周榜贴图预览] {rank['week_id']} · {editorial['title']}"
    model_error = f"\n\n模型提示：{editorial['model_error']}" if editorial.get("model_error") else ""
    body = (f"这是 {rank['week_id']} GitHub 周榜第 1 名的贴图预览。\n\n"
            f"标题：{editorial['title']}\n作者：{env('AUTHOR', 'MJZHOU')}\n"
            f"项目：{rank['repo']}\n链接：{rank['url']}\n\n"
            f"正文（{len(editorial['content'])} 字）：\n{editorial['content']}"
            f"{model_error}\n\n如需进入微信公众号贴图草稿流程，请直接回复本邮件：确认\n"
            "回复内容需包含“确认”，系统仅处理本期预览。")
    msg.set_content(body)
    for p in sorted(folder.glob("*.png")):
        msg.add_attachment(p.read_bytes(), maintype="image", subtype="png", filename=p.name)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(env("SMTP_HOST", "smtp.qq.com"), int(env("SMTP_PORT", "465")), context=context, timeout=45) as s:
        s.login(sender, env("SMTP_AUTHCODE"))
        s.send_message(msg)


def send_qr_email(path: Path) -> None:
    """Send a one-off WeChat login QR screenshot to the preview recipient."""
    msg = email.message.EmailMessage()
    msg["From"] = env("SMTP_USER", "17014842@qq.com")
    msg["To"] = env("PREVIEW_TO", "zhouminjie1986@foxmail.com")
    msg["Subject"] = "[GitHub周榜贴图] 微信登录需要扫码"
    msg.set_content("服务器发布流程已打开微信公众号登录页，请扫描附件中的二维码。扫码后保持页面打开，系统会继续保存贴图草稿。")
    msg.add_attachment(path.read_bytes(), maintype="image", subtype="png", filename="wechat-login-qr.png")
    with smtplib.SMTP_SSL(env("SMTP_HOST", "smtp.qq.com"), int(env("SMTP_PORT", "465")),
                          context=ssl.create_default_context(), timeout=45) as s:
        s.login(msg["From"], env("SMTP_AUTHCODE"))
        s.send_message(msg)


def run_once() -> Path:
    source = http_get(RAW_README)
    rank = parse_top1(source)
    editorial = chat_json(rank)
    images = generate_images(editorial["image_prompts"], DATA / re.sub(r"[^0-9-]", "", rank["week_id"]) / "images")
    folder, _ = save_package(rank, editorial, images)
    send_preview(folder, rank, editorial)
    print(json.dumps({"status": "preview_sent", "folder": str(folder), "week_id": rank["week_id"], "repo": rank["repo"]}, ensure_ascii=False))
    return folder


def parse_confirmation(msg_bytes: bytes, week_id: str) -> bool:
    m = email.message_from_bytes(msg_bytes, policy=email.policy.default)
    subject = str(m.get("Subject", ""))
    if week_id not in subject:
        return False
    text = m.get_body(preferencelist=("plain", "html"))
    body = text.get_content() if text else ""
    # Require a deliberate standalone confirmation token, allowing normal
    # quoted reply text around it.
    return bool(re.search(r"(?<!\S)确认(?!\S)", body))


def publish(folder: Path) -> None:
    post = json.loads((folder / "post.json").read_text(encoding="utf-8"))
    cmd = env("WECHAT_PUBLISH_CMD")
    if not cmd:
        # The server install writes a wrapper command. Keeping this explicit
        # prevents accidental publication when the browser setup is absent.
        raise RuntimeError("WECHAT_PUBLISH_CMD is not configured")
    env2 = os.environ.copy()
    env2.update({"WECHAT_TITLE": post["editorial"]["title"], "WECHAT_CONTENT": post["editorial"]["content"],
                 "WECHAT_IMAGES_DIR": str(folder / "images"), "WECHAT_MARKDOWN": str(folder / "post.md")})
    p = subprocess.Popen(cmd, shell=True, cwd=str(ROOT), env=env2, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    qr_path = DATA / "wechat-login-qr.png"
    qr_sent = False
    output: list[str] = []
    while p.poll() is None:
        if qr_path.exists() and not qr_sent:
            try:
                send_qr_email(qr_path)
                qr_sent = True
            except Exception as exc:
                output.append(f"QR email failed: {exc}")
        if p.stdout:
            line = p.stdout.readline()
            if line:
                output.append(line.rstrip())
        time.sleep(1)
    if p.stdout:
        output.extend(p.stdout.read().splitlines())
    if p.returncode:
        raise RuntimeError(f"WeChat publish failed: {' '.join(output[-20:])[-1500:]}")
    post["status"] = "draft_saved"
    (folder / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")


def watch() -> None:
    while True:
        pending_file = DATA / "pending.json"
        if pending_file.exists():
            pending = json.loads(pending_file.read_text(encoding="utf-8"))
            if pending.get("status") == "preview":
                week = pending["week_id"]
                try:
                    imap = imaplib.IMAP4_SSL(env("IMAP_HOST", "imap.qq.com"), int(env("IMAP_PORT", "993")), timeout=30)
                    imap.login(env("SMTP_USER", "17014842@qq.com"), env("SMTP_AUTHCODE"))
                    imap.select("INBOX")
                    _, data = imap.search(None, "UNSEEN")
                    confirmed = False
                    for num in data[0].split()[-20:]:
                        _, msg = imap.fetch(num, "(RFC822)")
                        raw = msg[0][1] if msg and isinstance(msg[0], tuple) else b""
                        if raw and parse_confirmation(raw, week):
                            confirmed = True
                            imap.store(num, "+FLAGS", "\\Seen")
                            break
                    imap.logout()
                    if confirmed:
                        publish(Path(pending["folder"]))
                        pending["status"] = "draft_saved"
                        pending_file.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as exc:
                    (DATA / "watcher-error.log").write_text(f"{datetime.now(CN_TZ).isoformat()} {exc}\n", encoding="utf-8")
        time.sleep(int(env("POLL_SECONDS", "300")))


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "run":
        run_once()
    elif sys.argv[1] == "watch":
        watch()
    elif sys.argv[1] == "publish":
        publish(Path(sys.argv[2]))
    else:
        raise SystemExit("usage: weekly_pipeline.py [run|watch|publish FOLDER]")
