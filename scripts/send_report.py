#!/usr/bin/env python3
"""
A股报告邮件发送脚本
====================
读取 Markdown 分析报告，转换为 HTML 邮件，通过 QQ 邮箱 SMTP 发送。

用法：
  python scripts/send_report.py                     # 发送最新日报
  python scripts/send_report.py --test              # 发送测试邮件
  python scripts/send_report.py <file.md>           # 发送指定文件（自动识别日报/周报/月报）
  python scripts/send_report.py <file.md> --subject "自定义标题"  # 自定义邮件标题

自动识别规则（根据文件路径）：
  - 路径含 "daily"   → 标题 "A股日报 YYYY-MM-DD"
  - 路径含 "weekly"  → 标题 "A股周报 YYYY-WXX"
  - 路径含 "monthly" → 标题 "A股月报 YYYY-MM"
  - 其他             → 标题 "A股报告 YYYY-MM-DD"

配置：环境变量 > scripts/.env 文件
  SMTP_HOST    - SMTP 服务器 (默认 smtp.qq.com)
  SMTP_PORT    - SMTP 端口 (默认 465)
  SMTP_USE_SSL - 使用 SSL (默认 true)
  SMTP_USER    - QQ 邮箱地址
  SMTP_PASS    - QQ 邮箱 SMTP 授权码
  SMTP_FROM    - 发件人 (默认等于 SMTP_USER)
  SMTP_TO      - 收件人 (必填)
"""

import os
import sys
import json
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path

# 路径
VAULT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = VAULT_ROOT / "2-wiki" / "data"
DEFAULT_ANALYSIS = DATA_DIR / "daily_analysis.md"
META_FILE = DATA_DIR / "meta.json"
ENV_FILE = Path(__file__).resolve().parent / ".env"

# Windows GBK 终端编码
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ============================================================
# 配置加载
# ============================================================

def load_dotenv(path):
    """读取 .env 文件，将未设置的环境变量导入 os.environ。"""
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def get_config():
    """获取 SMTP 配置，缺失必填项时抛出明确错误。"""
    load_dotenv(ENV_FILE)

    config = {
        "host": os.environ.get("SMTP_HOST", "smtp.qq.com"),
        "port": int(os.environ.get("SMTP_PORT", "465")),
        "use_ssl": os.environ.get("SMTP_USE_SSL", "true").lower() in ("true", "1", "yes"),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASS", ""),
        "from_addr": os.environ.get("SMTP_FROM", "") or os.environ.get("SMTP_USER", ""),
        "to_addr": os.environ.get("SMTP_TO", ""),
    }

    missing = []
    if not config["user"]:
        missing.append("SMTP_USER (QQ email address)")
    if not config["password"]:
        missing.append("SMTP_PASS (QQ SMTP authorization code)")
    if not config["to_addr"]:
        missing.append("SMTP_TO (recipient email)")

    if missing:
        print("[ERROR] Missing SMTP configuration:")
        for m in missing:
            print(f"  - {m}")
        print(f"\n  Create scripts/.env from scripts/.env.example and fill in the values.")
        print(f"  Or set environment variables directly.")
        return None

    return config


# ============================================================
# Markdown -> HTML 转换
# ============================================================

def md_to_html(md_text):
    """将 daily_analysis.md 转为 HTML 邮件正文。简单的行级转换。"""
    lines = md_text.split("\n")
    html_lines = []
    in_table = False
    in_thead = False
    in_code_block = False
    first_header = True

    for i, line in enumerate(lines):
        # 跳过 YAML frontmatter
        if i == 0 and line.strip() == "---":
            # 找到结束的 ---
            end = i + 1
            while end < len(lines) and lines[end].strip() != "---":
                end += 1
            # 跳到 frontmatter 之后
            for j in range(i, min(end + 1, len(lines))):
                pass
            continue
        if i > 0 and line.strip() == "---" and i < 5:
            continue

        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                html_lines.append('<pre style="background:#f4f4f4;padding:8px;border-radius:4px;overflow-x:auto;">')
            else:
                html_lines.append('</pre>')
            continue

        if in_code_block:
            html_lines.append(escape_html(line))
            continue

        # 表格行
        if "|" in stripped and stripped.startswith("|"):
            if not in_table:
                html_lines.append('<table style="border-collapse:collapse;width:100%;margin:8px 0;">')
                in_table = True
                in_thead = True

            cells = [c.strip() for c in stripped.split("|")[1:-1]]

            # 分隔行 (|---|---|)
            if all(re.match(r"^:?-{3,}:?$", c) for c in cells):
                in_thead = False
                continue

            tag = "th" if in_thead else "td"
            style = 'style="border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:14px;"'
            if in_thead:
                style = 'style="border:1px solid #ddd;padding:6px 10px;text-align:left;font-weight:bold;background:#f0f0f0;font-size:14px;"'

            row = "<tr>" + "".join(f"<{tag} {style}>{format_cell(c)}</{tag}>" for c in cells) + "</tr>"
            html_lines.append(row)
            continue
        elif in_table:
            html_lines.append('</table>')
            in_table = False
            in_thead = False

        # 空行
        if not stripped:
            html_lines.append("<br>")
            continue

        # 标题
        if stripped.startswith("# "):
            html_lines.append(f'<h2 style="color:#1a1a1a;border-bottom:2px solid #e0e0e0;padding-bottom:4px;margin-top:24px;">{escape_html(stripped[2:])}</h2>')
            continue
        if stripped.startswith("## "):
            html_lines.append(f'<h3 style="color:#333;margin-top:20px;">{escape_html(stripped[3:])}</h3>')
            continue
        if stripped.startswith("### "):
            html_lines.append(f'<h4 style="color:#555;margin-top:16px;">{escape_html(stripped[4:])}</h4>')
            continue

        # 引用块
        if stripped.startswith("> "):
            content = format_inline(stripped[2:])
            # 特殊样式：关键信号
            if "🔑" in content or "⚠" in content or "🎯" in content:
                html_lines.append(f'<blockquote style="background:#fff8e1;border-left:4px solid #ffc107;padding:10px 14px;margin:8px 0;font-size:14px;">{content}</blockquote>')
            else:
                html_lines.append(f'<blockquote style="background:#f5f5f5;border-left:4px solid #ccc;padding:10px 14px;margin:8px 0;font-size:14px;color:#666;">{content}</blockquote>')
            continue

        # 水平线
        if stripped == "---":
            html_lines.append('<hr style="border:none;border-top:1px solid #e0e0e0;margin:16px 0;">')
            continue

        # 普通段落
        html_lines.append(f'<p style="font-size:15px;line-height:1.7;margin:6px 0;">{format_inline(stripped)}</p>')

    # 关闭未结束的表格
    if in_table:
        html_lines.append('</table>')

    return "\n".join(html_lines)


def escape_html(text):
    """转义 HTML 特殊字符。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_cell(text):
    """格式化表格单元格内容。"""
    text = format_inline(text)
    # 颜色编码
    if text.startswith("+") and "%" in text:
        return f'<span style="color:#c00;">{text}</span>'
    if text.startswith("-") and "%" in text:
        return f'<span style="color:#080;">{text}</span>'
    return text


def format_inline(text):
    """格式化行内元素：粗体、代码、Wiki 链接。"""
    # 先转义
    text = escape_html(text)
    # 粗体 **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # 行内代码 `code`
    text = re.sub(r"`([^`]+)`", r'<code style="background:#f0f0f0;padding:1px 4px;border-radius:3px;font-size:13px;">\1</code>', text)
    # Wiki 链接 [[link]] 或 [[path|text]]
    text = re.sub(r"\[\[([^\]|]+?)\]\]", r"\1", text)
    text = re.sub(r"\[\[[^\]]+?\|([^\]]+?)\]\]", r"\1", text)
    # Markdown 链接 [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" style="color:#1a73e8;">\1</a>', text)
    # Emoji 保留
    return text


# ============================================================
# 邮件发送
# ============================================================

def send_email(config, html_body, subject):
    """通过 SMTP 发送邮件。"""
    # 纯文本备选：去除 HTML 标签
    plain_body = re.sub(r"<[^>]+>", "", html_body)
    plain_body = re.sub(r"\n{3,}", "\n\n", plain_body)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["from_addr"]
    msg["To"] = config["to_addr"]
    msg["Date"] = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0800")

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if config["use_ssl"]:
            server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=30)
        else:
            server = smtplib.SMTP(config["host"], config["port"], timeout=30)
            server.starttls()

        server.login(config["user"], config["password"])
        server.sendmail(config["from_addr"], config["to_addr"].split(","), msg.as_string())
        server.quit()
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        return False, f"SMTP authentication failed: {e}\n  -> Check SMTP_USER and SMTP_PASS (use QQ authorization code, not login password)"
    except smtplib.SMTPConnectError as e:
        return False, f"SMTP connection failed: {e}\n  -> Check SMTP_HOST and SMTP_PORT"
    except Exception as e:
        return False, str(e)


# ============================================================
# 主流程
# ============================================================

def detect_report_type(filepath):
    """根据文件路径自动识别报告类型，返回 (标签, 默认标题)。"""
    path_str = str(filepath).lower()
    fname = Path(filepath).name

    if "monthly" in path_str:
        # 从文件名提取 YYYY-MM，如 "2026-05.md"
        base = fname.replace(".md", "")
        return "月报", f"A股月报 {base}"
    elif "weekly" in path_str:
        # 从文件名提取 YYYY-WXX，如 "2026-W22.md"
        base = fname.replace(".md", "")
        return "周报", f"A股周报 {base}"
    elif "daily" in path_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
        return "日报", f"A股日报 {date_str}"
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")
        return "报告", f"A股报告 {date_str}"


def main():
    test_mode = "--test" in sys.argv

    # 解析 --subject 参数
    custom_subject = None
    subject_idx = None
    for i, arg in enumerate(sys.argv):
        if arg == "--subject" and i + 1 < len(sys.argv):
            custom_subject = sys.argv[i + 1]
            subject_idx = i
            break

    # 确定要发送的文件
    if test_mode:
        analysis_path = None
    elif len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        analysis_path = Path(sys.argv[1])
        if not analysis_path.exists():
            print(f"[ERROR] File not found: {analysis_path}")
            return 1
    else:
        analysis_path = DEFAULT_ANALYSIS

    # 加载配置
    config = get_config()
    if config is None:
        return 1

    print(f"[Email] SMTP: {config['host']}:{config['port']} (SSL={config['use_ssl']})")
    print(f"[Email] From: {config['user']}")
    print(f"[Email] To:   {config['to_addr']}")

    if test_mode:
        # 发送测试邮件
        print("\n[Email] Sending TEST email...")
        subject = "[测试] A股自动化日报系统 - 测试邮件"
        html_body = f"""
        <html><body style="font-family:sans-serif;padding:20px;">
        <h2>A股自动化日报系统 - 测试邮件</h2>
        <p>如果你收到这封邮件，说明 SMTP 配置正确，系统可以正常发送报告。</p>
        <hr>
        <p style="color:#999;font-size:12px;">
            发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}<br>
            由 Claude Code 自动化系统发送
        </p>
        </body></html>
        """
        ok, err = send_email(config, html_body, subject)
        if ok:
            print("[Email] [OK] Test email sent successfully!")
        else:
            print(f"[Email] [FAIL] {err}")
            return 1
        return 0

    # 读取分析文件
    if not analysis_path.exists():
        print(f"[ERROR] Analysis file not found: {analysis_path}")
        print(f"  Run the analysis step first to generate daily_analysis.md")
        return 1

    with open(analysis_path, "r", encoding="utf-8") as f:
        md_text = f.read()

    # 邮件标题：优先自定义，其次自动识别
    if custom_subject:
        subject = custom_subject
    else:
        label, subject = detect_report_type(analysis_path)
        print(f"[Email] Detected report type: {label}")

    # 日报模式：从 meta.json 提取日期
    if subject == f"A股日报 {datetime.now().strftime('%Y-%m-%d')}":
        if META_FILE.exists():
            try:
                meta = json.load(open(META_FILE, "r", encoding="utf-8"))
                last = meta.get("last_analysis") or meta.get("last_update", "")
                if "T" in last:
                    subject = f"A股日报 {last[:10]}"
            except Exception:
                pass

    # 转换并发送
    print(f"\n[Email] Converting {analysis_path.name} to HTML...")
    html_body = md_to_html(md_text)

    # 包裹完整 HTML
    full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:800px;margin:0 auto;color:#333;padding:10px;">
{html_body}
<hr style="border:none;border-top:1px solid #e0e0e0;margin:24px 0;">
<p style="color:#999;font-size:12px;text-align:center;">
    由 Claude Code 自动生成 | 数据来源：东方财富、新浪财经、腾讯行情<br>
    发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
</p>
</body></html>"""

    print(f"[Email] Sending report ({len(full_html)} chars HTML)...")
    ok, err = send_email(config, full_html, subject)

    if ok:
        print(f"[Email] [OK] Report sent to {config['to_addr']}")
        return 0
    else:
        print(f"[Email] [FAIL] {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
