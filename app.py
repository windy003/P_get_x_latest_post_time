from flask import Flask, render_template, request
import requests
import feedparser
import re
import json
from pathlib import Path
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

# 持久化存储文件：保存用户上次输入的 URL 列表
SAVED_URLS_FILE = Path(__file__).parent / 'saved_urls.json'


def load_saved_urls() -> str:
    """从文件读取上次保存的 URL 列表，返回多行字符串。"""
    try:
        data = json.loads(SAVED_URLS_FILE.read_text(encoding='utf-8'))
        return '\n'.join(data.get('urls', []))
    except Exception:
        return ''


def save_urls(urls_text: str) -> None:
    """将当前输入的 URL 列表保存到文件。"""
    urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
    SAVED_URLS_FILE.write_text(
        json.dumps({'urls': urls}, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

# 可用的 Nitter 实例列表（Nitter 是开源的 Twitter/X 前端，提供 RSS）
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.net",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
    "https://nitter.woodland.cafe",
]

# 无效的 URL 路径段（非用户名）
INVALID_USERNAMES = {
    'home', 'explore', 'notifications', 'messages', 'i', 'settings',
    'search', 'login', 'signup', 'tos', 'privacy',
}


def extract_username(text: str) -> str | None:
    text = text.strip()
    # 匹配 twitter.com 或 x.com 的用户主页链接
    match = re.search(
        r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)',
        text
    )
    if match:
        username = match.group(1)
        if username.lower() not in INVALID_USERNAMES:
            return username
        return None
    # 直接输入用户名（支持带 @ 和不带 @）
    if re.match(r'^@?[A-Za-z0-9_]{1,50}$', text):
        return text.lstrip('@')
    return None


def format_date(date_str: str) -> tuple[str, str]:
    """将 RFC 2822 日期格式化为可读字符串，并计算相对时间。"""
    try:
        tz_beijing = timezone(timedelta(hours=8))
        dt = parsedate_to_datetime(date_str).astimezone(tz_beijing)
        now = datetime.now(tz_beijing)
        delta = now - dt
        formatted = dt.strftime('%Y-%m-%d %H:%M CST')

        if delta.days < 0:
            ago = '刚刚'
        elif delta.days == 0:
            hours = delta.seconds // 3600
            if hours == 0:
                minutes = delta.seconds // 60
                ago = f'{minutes} 分钟前' if minutes > 0 else '刚刚'
            else:
                ago = f'{hours} 小时前'
        elif delta.days == 1:
            ago = '昨天'
        elif delta.days < 7:
            ago = f'{delta.days} 天前'
        elif delta.days < 30:
            ago = f'{delta.days // 7} 周前'
        elif delta.days < 365:
            ago = f'{delta.days // 30} 个月前'
        else:
            ago = f'{delta.days // 365} 年前'

        return formatted, ago
    except Exception:
        return date_str, ''


def fetch_from_nitter(username: str) -> tuple[list | None, str | None]:
    """依次尝试各 Nitter 实例，返回最新帖子列表和成功的实例地址。"""
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; RSS-Checker/1.0)'}
    for instance in NITTER_INSTANCES:
        try:
            rss_url = f"{instance}/{username}/rss"
            resp = requests.get(rss_url, timeout=8, headers=headers)
            if resp.status_code != 200:
                continue
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                continue

            posts = []
            for entry in feed.entries[:3]:  # 多取一条，防止某条数据异常
                date_str = entry.get('published') or entry.get('updated', '')
                formatted, ago = format_date(date_str)
                # 将 Nitter 链接转换回 x.com 链接
                link = re.sub(r'https?://[^/]+/', 'https://x.com/', entry.get('link', ''))
                posts.append({
                    'date': formatted,
                    'ago': ago,
                    'title': entry.get('title', '')[:160],
                    'link': link,
                })
            return posts, instance
        except Exception:
            continue
    return None, None


def check_single_user(raw_input: str) -> dict:
    username = extract_username(raw_input)
    if not username:
        return {'input': raw_input, 'username': None,
                'error': '无法解析用户名，请检查输入格式', 'posts': []}

    posts, source = fetch_from_nitter(username)
    if posts:
        return {'input': raw_input, 'username': username,
                'error': None, 'posts': posts[:2], 'source': source}
    return {'input': raw_input, 'username': username,
            'error': '所有 Nitter 实例均请求失败，可能是账号不存在、已私有，或网络问题',
            'posts': []}


@app.route('/', methods=['GET', 'POST'])
def index():
    results = []

    if request.method == 'GET':
        # 恢复上次保存的 URL 列表
        urls_text = load_saved_urls()
        return render_template('index.html', results=[], urls_text=urls_text)

    # POST：保存本次输入，然后查询
    urls_text = request.form.get('urls', '').strip()
    save_urls(urls_text)
    urls = [u.strip() for u in urls_text.splitlines() if u.strip()]

    if not urls:
        return render_template('index.html', results=[], urls_text=urls_text)

    # 多用户并发请求
    ordered_results = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=min(len(urls), 8)) as executor:
        futures = {executor.submit(check_single_user, url): i
                   for i, url in enumerate(urls)}
        for future in as_completed(futures):
            idx = futures[future]
            ordered_results[idx] = future.result()

    results = ordered_results

    return render_template('index.html', results=results, urls_text=urls_text)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5006)
