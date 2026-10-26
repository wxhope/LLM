# -*- coding: utf-8 -*-
"""网页正文抓取工具：FetchWeb。

为什么要调 curl 而不是 urllib：
  真实网站有一堆 urllib 处理起来很难受的东西 —— 压缩、重定向、TLS 指纹、
  反爬的 UA 检查。curl 是本机现成的、久经考验的下载器，Windows 10 1803+
  自带，让它去干脏活，我们只负责"把 HTML 变成能读的 Markdown"。

为什么不用 shell=True：
  参数列表直接交给 execve，不经过 shell 解析。URL 里一个 `&`、`|`、`$()`
  在 shell 模式下就是命令注入；这里根本不存在这个面。URL 是模型给的，
  天然不可信，这条底线不能破。

整体分三段：
  1. run_curl()        —— 下载，拿到 (最终URL, content_type, 字节, 状态码)
  2. html_to_markdown() —— 解析，砍掉导航/广告/页脚，保留结构
  3. fetch_web()       —— 串起来，截断到 max_chars，拼成给模型的文本

最贵的一步是"让模型读一整页 HTML"。不砍噪音，一次抓取就能吃掉几千 token
的 CSS 和菜单文字，所以这里的取舍是：**宁可少抓一点，也不要抓一堆垃圾**。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from ..base import DangerLevel, ToolContext, ToolResult, integer, params, string, tool
from .fs_guard import decode_bytes

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_CHARS = 12000

MAX_BYTES = 3 * 1024 * 1024        # 单页最多下 3MB，再多是个人都读不完
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 10

# curl 的 -w 尾巴：一次把状态码、内容类型、重定向后的真实地址都要回来。
# 用 \x1f（单元分隔符）当字段分隔，它不可能出现在 URL 或 content-type 里。
_META_SEP = "\n__XIAOA_META__="
_FIELD_SEP = "\x1f"
_WRITE_OUT = _META_SEP + "%{http_code}" + _FIELD_SEP + "%{content_type}" \
    + _FIELD_SEP + "%{url_effective}"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# curl 退出码 → 人话。只列常见的，其余走兜底。
CURL_ERRORS = {
    1: "curl 参数不被支持",
    2: "curl 参数有误",
    3: "URL 格式不对",
    5: "代理地址无法解析",
    6: "域名解析失败：这个域名不存在，或者本机 DNS 出问题了",
    7: "连接失败：目标拒绝连接，或者被代理/防火墙拦了下来",
    18: "传输中断：服务器只发了一部分内容就断了",
    22: "服务器返回了错误状态码",
    23: "写入失败",
    28: "请求超时：页面太久没有响应",
    35: "TLS/SSL 握手失败",
    47: "重定向次数过多",
    51: "服务器证书校验失败",
    52: "服务器返回了空响应",
    55: "网络发送失败",
    56: "接收数据失败：连接被中途重置",
    58: "本地证书文件有问题",
    60: "HTTPS 证书不受信任（过期或域名不匹配）",
    63: "页面超过大小上限，已放弃下载",
}

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

# 整块丢掉的标签：要么是代码，要么是界面，反正不是正文
BLOCK_SKIP_TAGS = {
    "script", "style", "noscript", "svg", "iframe", "canvas", "template",
    "form", "button", "select", "option", "input", "textarea", "label",
    "nav", "footer", "aside", "dialog", "video", "audio", "object", "embed",
    "map", "head", "title",
}

# class/id 里出现这些词，基本可以断定是页面架子而不是正文。
# 「widget」不能放进来：python.org 的文档目录就是用 class="small-widget" 装正文的，
# 一刀切会把整页内容删空。宁可漏删几个侧栏，也不能误杀正文。
NOISE_TOKENS = {
    "nav", "navbar", "navigation", "menu", "menubar", "breadcrumb", "breadcrumbs",
    "footer", "sidebar", "banner", "ad", "ads", "adv", "advert", "adsense",
    "advertisement", "sponsor", "sponsored", "promo", "cookie", "cookies",
    "consent", "popup", "modal", "overlay", "subscribe", "newsletter",
    "share", "sharing", "social", "comment", "comments", "related",
    "recommend", "recommended", "pagination", "pager", "toolbar", "login",
    "signup", "register", "giscus", "disqus", "copyright",
    "skip", "skiplink", "backtotop", "qrcode",
}

# class/id 里出现这些词，说明这层容器很可能就是正文。
# 同样要克制：「text」这种词在 Bootstrap 里满地都是（text-muted/text-sm），不能当信号。
MAIN_TOKENS = {
    "article", "articlebody", "content", "entry", "post", "main", "markdown",
    "rich_media_content", "detail", "story", "blog", "body",
}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")
_CHARSET_META = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([\w\-]+)", re.I)
_CHARSET_HEADER = re.compile(r"charset\s*=\s*[\"']?([\w\-]+)", re.I)

MIN_BODY_CHARS = 80        # 低于这个字数就当"没提到正文"
MIN_MAIN_RATIO = 0.25      # 正文容器至少占整页可读文本的四分之一，才敢只留它

# 这些标签的结束标记在 HTML 里经常被省略（<p> 不写 </p> 是合法的）。
# 如果跳过区正好是它们，就不能靠嵌套计数退出，否则会把整页都吞掉。
OPTIONAL_END_TAGS = {"p", "li", "dt", "dd", "tr", "td", "th", "option",
                     "thead", "tbody", "tfoot", "colgroup", "caption"}


class FetchError(Exception):
    """抓取失败。message 已经是可以直接给模型/用户看的中文。"""


# --------------------------------------------------------------------- 1. 下载


def find_curl() -> Optional[str]:
    """找 curl。Windows 10 1803+ 自带，老系统或精简版可能没有。"""
    return shutil.which("curl")


def build_curl_command(url: str, timeout: float, curl: str) -> List[str]:
    """拼 curl 参数列表。

    刻意用列表：如果把整条命令塞进一个字符串再 shell=True，
    URL 里的 `&` `;` `$(...)` 就会变成命令执行。这里是模型给的 URL，
    绝不能让它碰到 shell。
    """
    return [
        curl,
        "-sS",                       # 静默进度条，但保留错误信息
        "-L",                        # 跟随重定向
        "--max-redirs", str(MAX_REDIRECTS),
        "--compressed",              # 自动解 gzip/deflate/br，否则读到的是乱码
        "--max-time", "%g" % timeout,
        "--connect-timeout", str(CONNECT_TIMEOUT),
        "--max-filesize", str(MAX_BYTES),
        "--proto", "=http,https",            # 只许这两个协议
        "--proto-redir", "=http,https",      # 重定向也不许跳出 http/https
        "-A", USER_AGENT,
        "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-w", _WRITE_OUT,
        url,
    ]


def normalize_url(raw: str) -> str:
    """补协议、转义中文、挡掉非 http(s)。

    模型经常给 `example.com/x` 这种没协议的东西；也可能给 `file:///C:/`，
    那不是"网页"，是本地文件，必须挡掉。
    """
    text = (raw or "").strip().strip("<>\"'")
    if not text:
        raise FetchError("url 是空的。请传入完整网址，例如 https://example.com/page")

    # 协议判断不能只看"有没有冒号"：`x.com:8080/a` 里的 `x.com:` 长得很像协议名。
    # 真正的判据是"冒号后面跟不跟 //"，再加上几个已知的伪协议白名单式拒绝。
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        pass
    elif re.match(r"^[a-z][a-z0-9+.\-]*://", lowered):
        scheme = lowered.split("://", 1)[0]
        raise FetchError(
            "只支持 http/https 网址，收到的是「%s://」。"
            "本地文件请用 readFile 工具读，不要用 FetchWeb。" % scheme)
    elif re.match(r"^(javascript|data|file|mailto|tel|about|blob|chrome|ftp):", lowered):
        scheme = lowered.split(":", 1)[0]
        raise FetchError(
            "只支持 http/https 网址，收到的是「%s:」。"
            "本地文件请用 readFile 工具读，不要用 FetchWeb。" % scheme)
    else:
        text = "https://" + text.lstrip("/")

    parts = urllib.parse.urlsplit(text)
    if not parts.hostname:
        raise FetchError("网址里没有主机名：%s" % text)

    # 中文域名/路径要转成百分号编码，curl 才认
    return urllib.parse.quote(text, safe=":/?#[]@!$&'()*+,;=%~")


def sniff_charset(raw: bytes, content_type: str) -> str:
    """定编码。先看响应头，再看 <meta charset>，都没有就交给 decode_bytes 猜。"""
    match = _CHARSET_HEADER.search(content_type or "")
    if match:
        return match.group(1).lower()
    match = _CHARSET_META.search(raw[:4096])
    if match:
        return match.group(1).decode("ascii", "ignore").lower()
    return ""


def decode_html(raw: bytes, content_type: str) -> str:
    """按 charset 解码，失败就依次退到 utf-8 / gbk。"""
    charset = sniff_charset(raw, content_type)
    if charset:
        try:
            return raw.decode(charset, errors="strict")
        except (LookupError, UnicodeDecodeError):
            pass        # 站点把 gbk 写成 gb2312 之类，退回下面的通用猜测
    return decode_bytes(raw)


def run_curl(url: str, timeout: float) -> Tuple[str, str, str, int]:
    """执行 curl，返回 (最终URL, content_type, HTML文本, HTTP状态码)。

    失败一律抛 FetchError，消息里带 curl 的原始报错，方便定位。
    """
    curl = find_curl()
    if not curl:
        raise FetchError(
            "本机找不到 curl。Windows 10 1803 以后的系统自带 curl.exe；"
            "如果没有，去 https://curl.se/windows/ 下一个放进 PATH。")

    command = build_curl_command(url, timeout, curl)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.run(
            command,                       # 列表，不加 shell=True
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 10,          # 比 curl 自己的 --max-time 松一点，留收尾时间
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        raise FetchError(
            "抓取超时：%.0f 秒内这个页面没有下载完。可能是站点太慢，或文件太大。"
            % timeout)
    except (OSError, ValueError) as e:
        raise FetchError("调用 curl 失败：%s" % e)
    except subprocess.SubprocessError as e:
        raise FetchError("curl 进程异常：%s" % e)

    if proc.returncode != 0:
        detail = decode_bytes(proc.stderr or b"").strip()
        reason = CURL_ERRORS.get(proc.returncode, "curl 退出码 %s" % proc.returncode)
        raise FetchError("网页访问失败（%s）%s" % (reason, ("：" + detail) if detail else ""))

    raw = proc.stdout or b""
    marker = _META_SEP.encode("utf-8")
    index = raw.rfind(marker)
    if index < 0:
        # curl 成功但拿不到 -w 尾巴，说明输出被什么截断了
        body_bytes, meta = raw, ""
    else:
        body_bytes, meta = raw[:index], raw[index + len(marker):].decode("utf-8", "replace")

    fields = meta.strip().split(_FIELD_SEP)
    status_text = fields[0].strip() if fields else ""
    content_type = fields[1].strip() if len(fields) > 1 else ""
    effective_url = fields[2].strip() if len(fields) > 2 else url

    try:
        status = int(status_text)
    except (TypeError, ValueError):
        status = 0

    if status >= 400:
        hint = ""
        if status in (401, 403):
            hint = "（这个页面要登录，或者拒绝了爬取）"
        elif status == 404:
            hint = "（页面不存在，链接可能过期了）"
        elif status == 429:
            hint = "（被限流，稍后再试）"
        raise FetchError("网页返回 HTTP %d%s，没有正文可读。" % (status, hint))

    if len(body_bytes) > MAX_BYTES:
        body_bytes = body_bytes[:MAX_BYTES]

    if not body_bytes.strip():
        raise FetchError(
            "服务器返回了空内容（HTTP %d）。可能是纯前端渲染的页面——"
            "正文由 JavaScript 现场生成，curl 拿到的只是个空壳。" % status)

    return effective_url, content_type, decode_html(body_bytes, content_type), status


# --------------------------------------------------------------------- 2. 解析


def _tokens(value: str) -> List[str]:
    return [t for t in _TOKEN_SPLIT.split((value or "").lower()) if t]


def _looks_noisy(attrs: Dict[str, str]) -> bool:
    """按 class / id / role 判断这块是不是页面架子。"""
    for key in ("class", "id", "role"):
        for token in _tokens(attrs.get(key, "")):
            if token in NOISE_TOKENS:
                return True
    label = (attrs.get("aria-label") or "").lower()
    return any(word in label for word in ("导航", "nav", "menu", "广告", "ad"))


def _looks_main(attrs: Dict[str, str]) -> bool:
    for key in ("class", "id"):
        for token in _tokens(attrs.get(key, "")):
            if token in MAIN_TOKENS:
                return True
    return False


class MarkdownExtractor(HTMLParser):
    """HTML → Markdown。只用标准库，不引第三方解析器。

    设计要点：
      - 跳过的内容用**标签栈**管，比计数器稳：遇到没闭合的标签也能靠同名匹配退出
      - 正文容器（article/main/class=content 之类）收集到独立 buffer，
        最后和整页文本比长度——长得多的那个才是正文
      - 所有块级标记只管往 buffer 里塞 \n\n，最后统一压缩空行，
        不在过程中纠结"这里该不该换行"
    """

    def __init__(self, base_url: str, use_noise_filter: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        # False = 只砍标签级噪音（script/nav/footer/form），不碰 class 启发式。
        # 过滤过头时用它兜底，保证"有字可读"优先于"一点噪音都没有"。
        self.use_noise_filter = use_noise_filter
        self.buf: List[str] = []
        self.best_main = ""
        self.main_stack: List[Tuple[List[str], str]] = []
        # 跳过区用「同名标签计数」退出：不靠所有标签配对，只数同名嵌套。
        # 好处是即使里面混着七八层 div，也不会把后续正文一起吞掉。
        self.skip_tag = ""
        self.skip_depth = 0
        self.list_stack: List[Tuple[bool, int]] = []
        self.tables: List[Dict[str, Any]] = []
        self.pre_depth = 0
        self.quote_stack: List[int] = []
        self.link_stack: List[str] = []
        self.title_parts: List[str] = []
        self._in_title = False

    @property
    def skipping(self) -> bool:
        return self.skip_depth > 0

    # -------------------------------------------------- 输出

    def _emit(self, text: str) -> None:
        if not text:
            return
        if self.tables:
            table = self.tables[-1]
            if table["cell"] is not None:
                table["cell"].append(text)
                return
        self.buf.append(text)

    def _break_block(self) -> None:
        self._emit("\n\n")

    def _text(self) -> str:
        return "".join(self.buf)

    def _begin_skip(self, tag: str) -> None:
        self.skip_tag = tag
        self.skip_depth = 1

    def _end_skip(self) -> None:
        self.skip_tag = ""
        self.skip_depth = 0

    # -------------------------------------------------- 解析

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        data = {k.lower(): (v or "") for k, v in attrs}

        if tag == "title":
            self._in_title = True
            return

        if self.skipping:
            # 只在同名标签上累加，保证"关掉的是外面那一层"
            if tag == self.skip_tag and self.skip_tag not in OPTIONAL_END_TAGS:
                self.skip_depth += 1
            return

        if tag in VOID_TAGS:
            self._handle_void(tag, data)
            return

        if tag in BLOCK_SKIP_TAGS or (self.use_noise_filter and _looks_noisy(data)):
            self._begin_skip(tag)
            return

        if self.use_noise_filter and (
                tag in ("article", "main") or _looks_main(data) or data.get("role") == "main"):
            self.main_stack.append((self.buf, tag))
            self.buf = []
            self._start_block(tag, data)
            return

        self._start_block(tag, data)

    def _start_block(self, tag: str, data: Dict[str, str]) -> None:
        if tag in ("p", "div", "section", "article", "main", "header", "figure",
                   "figcaption", "address", "center"):
            self._break_block()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._break_block()
            self._emit("#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            self._break_block()
            self.list_stack.append((tag == "ol", 0))
        elif tag == "li":
            ordered, count = self.list_stack.pop() if self.list_stack else (False, 0)
            indent = "  " * max(len(self.list_stack), 0)
            count += 1
            self.list_stack.append((ordered, count))
            self._emit("\n%s%s " % (indent, ("%d." % count) if ordered else "-"))
        elif tag == "table":
            self._break_block()
            self.tables.append({"rows": [], "row": None, "cell": None})
        elif tag == "tr":
            if self.tables:
                self.tables[-1]["row"] = []
        elif tag in ("td", "th"):
            if self.tables:
                self.tables[-1]["cell"] = []
        elif tag == "blockquote":
            self._break_block()
            self.quote_stack.append(len(self.buf))
        elif tag == "pre":
            self._break_block()
            self._emit("```\n")
            self.pre_depth += 1
        elif tag == "code" and not self.pre_depth:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "del":
            self._emit("~~")
        elif tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._break_block()
            self._emit("---")
            self._break_block()
        elif tag == "a":
            href = self._absolute(data.get("href", ""))
            self.link_stack.append(href)
            if href:
                self._emit("[")
        elif tag == "dt":
            self._break_block()
            self._emit("**")
        elif tag == "dd":
            self._emit("\n  ")

    def _handle_void(self, tag: str, data: Dict[str, str]) -> None:
        if self.skipping:
            return
        if tag == "img":
            src = data.get("src") or data.get("data-src") or ""
            alt = (data.get("alt") or "").strip()
            if src:
                self._emit("![%s](%s)" % (alt, self._absolute(src)))
        elif tag == "br":
            self._emit("\n" if not self.pre_depth else "\n")
        elif tag == "hr":
            self._break_block()
            self._emit("---")
            self._break_block()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag == "title":
            self._in_title = False
            return

        if self.skipping:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth <= 0:
                    self._end_skip()
            return

        if self.main_stack and tag == self.main_stack[-1][1]:
            chunk = self._text()
            self.buf, _ = self.main_stack.pop()
            if len(chunk) > len(self.best_main):
                self.best_main = chunk
            return

        if tag == "p" or tag in ("div", "section", "article", "main", "header", "figure",
                                 "figcaption", "address"):
            self._break_block()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._break_block()
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            self._break_block()
        elif tag == "li":
            pass
        elif tag == "table":
            self._finish_table()
        elif tag == "tr":
            self._finish_row()
        elif tag in ("td", "th"):
            self._finish_cell()
        elif tag == "blockquote":
            self._finish_quote()
        elif tag == "pre":
            self.pre_depth = max(0, self.pre_depth - 1)
            self._emit("\n```")
            self._break_block()
        elif tag == "code" and not self.pre_depth:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "del":
            self._emit("~~")
        elif tag == "a":
            href = self.link_stack.pop() if self.link_stack else ""
            if href:
                self._emit("](%s)" % href)
        elif tag == "dt":
            self._emit("**")
            self._break_block()
        elif tag == "dd":
            self._break_block()

    # -------------------------------------------------- 表格

    def _finish_cell(self) -> None:
        if not self.tables:
            return
        table = self.tables[-1]
        cell = table["cell"]
        if cell is None:
            return
        table["cell"] = None
        if table["row"] is None:
            table["row"] = []
        table["row"].append("".join(cell))
        self._emit("\n")

    def _finish_row(self) -> None:
        if not self.tables:
            return
        table = self.tables[-1]
        if table["cell"] is not None:
            self._finish_cell()
        if table["row"]:
            table["rows"].append(table["row"])
        table["row"] = None

    def _finish_table(self) -> None:
        if not self.tables:
            return
        table = self.tables[-1]
        # 必须先收干净单元格和行，再出栈——出栈后再 _finish_cell 会打到外层表格上
        if table["cell"] is not None:
            self._finish_cell()
        if table["row"]:
            table["rows"].append(table["row"])
            table["row"] = None
        self.tables.pop()
        rendered = render_table(table["rows"])
        if rendered:
            self._break_block()
            self._emit(rendered)
            self._break_block()

    # -------------------------------------------------- 引用

    def _finish_quote(self) -> None:
        if not self.quote_stack:
            return
        start = self.quote_stack.pop()
        chunk = "".join(self.buf[start:])
        del self.buf[start:]
        lines = [line for line in chunk.splitlines() if line.strip()]
        if lines:
            self._emit("\n\n" + "\n".join("> " + line.strip() for line in lines) + "\n\n")

    # -------------------------------------------------- 文本

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self.skipping:
            return

        if self.pre_depth:
            self._emit(data)
            return

        text = _WS.sub(" ", data)
        if not text.strip():
            # 纯空白：留一个空格，免得中文和行内标签粘成一坨
            if text and self.buf and not self.buf[-1][-1:].isspace():
                self.buf.append(" ")
            return
        self._emit(text)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        data = {k.lower(): (v or "") for k, v in attrs}
        if tag in VOID_TAGS:
            self._handle_void(tag, data)
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    # -------------------------------------------------- 收尾

    def _absolute(self, href: str) -> str:
        href = (href or "").strip()
        if not href or href.lower().startswith(("javascript:", "data:", "#")):
            return ""
        try:
            return urllib.parse.urljoin(self.base_url, href)
        except ValueError:
            return href

    def to_markdown(self) -> Tuple[str, str]:
        """返回 (标题, 正文 Markdown)。"""
        # 页面写得残缺（候选容器没闭合）时，从这里把还没结算的 buffer 收掉，
        # 否则最内层那段正文会凭空消失
        while self.main_stack:
            chunk = self._text()
            self.buf, _ = self.main_stack.pop()
            if len(chunk) > len(self.best_main):
                self.best_main = chunk

        body = cleanup_markdown(self._text())
        main = cleanup_markdown(self.best_main)

        # 正文容器要"够长、且占整页可读文本的相当比例"才敢只用它。
        # 比例这一关很关键：有的站点 class="content" 只包了个侧栏，
        # 光看长度会误判，把真正的内容整段丢掉。
        if main and len(main) >= MIN_BODY_CHARS and len(main) >= len(body) * MIN_MAIN_RATIO:
            body = main

        title = cleanup_markdown(" ".join(self.title_parts))
        return title, body


def cleanup_markdown(text: str) -> str:
    """统一收尾：压空行、去行尾空白、丢空链接。

    按行处理而不是整段正则，原因只有一个：**代码块里的缩进不能动**。
    整段跑 `[ \\t]{2,}` → ` `，Python 代码会变成一行一行的顶格，
    那种输出对模型来说比没有还糟。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines: List[str] = []
    in_fence = False
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append(line.strip())
            continue
        if in_fence:
            lines.append(line)                  # 代码块：原样保留缩进
            continue
        # 只压缩行内连续空格，行首缩进（嵌套列表）要留着
        line = re.sub(r"(?<=\S)[ \t]{2,}", " ", line)
        # 行首多出来的一个空格（HTML 里的缩进空白）挡在列表符号前面，去掉
        line = re.sub(r"^ (?=[-*] |\d+\. )", "", line)
        lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 文档站标题末尾的锚点符号 ¶ 对模型没意义。
    # 注意只能用 [ \t]*，用 \s* 会把标题后的空行一起吃掉，标题就和正文粘在一行了。
    text = re.sub(r"¶[ \t]*$", "", text, flags=re.M)
    # 行内标签两边各带一个空格时，链接文字会变成 "[ 标题 ](url)"
    text = re.sub(r"\[\s+([^\]\n]*?)\s*\]\(", r"[\1](", text)
    # <a><img></a> 这类会产出 [](url)，链接本身还有意义，留下 URL
    text = re.sub(r"\[\s*\]\(([^)\s]+)\)", r"\1", text)
    return text.strip()


def render_table(rows: List[List[str]]) -> str:
    """把单元格二维数组渲染成 Markdown 表格。第一行当表头。"""
    cleaned: List[List[str]] = []
    for row in rows:
        cells = [_clean_cell(c) for c in row]
        if any(cells):
            cleaned.append(cells)
    if not cleaned:
        return ""

    width = max(len(r) for r in cleaned)
    cleaned = [r + [""] * (width - len(r)) for r in cleaned]
    header, body = cleaned[0], cleaned[1:]

    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _clean_cell(text: str) -> str:
    flat = _WS.sub(" ", (text or "")).strip()
    return flat.replace("|", "\\|")


def _extract_once(html: str, base_url: str, use_noise_filter: bool) -> Tuple[str, str]:
    parser = MarkdownExtractor(base_url, use_noise_filter=use_noise_filter)
    try:
        parser.feed(html)
        parser.close()
    except Exception as e:     # noqa: BLE001 —— HTMLParser 对各种畸形输入都可能炸
        raise FetchError("HTML 解析失败：%s（页面结构可能被截断或不是 HTML）"
                         % (str(e) or type(e).__name__))
    return parser.to_markdown()


def html_to_markdown(html: str, base_url: str) -> Tuple[str, str]:
    """HTML → (标题, Markdown)。

    走两遍：
      第一遍按 class/id 狠狠过滤（导航、广告、侧栏全砍掉）；
      第二遍只砍标签级噪音，保留全部容器。
    如果第一遍砍得只剩一点渣（说明关键词误伤了正文，python.org 的
    class="small-widget" 就是活例子），就用第二遍的结果。
    判断"只剩一点渣"用的是**相对比例**而不是绝对长度——短页面本来就短。
    """
    title, filtered = _extract_once(html, base_url, True)
    _, loose = _extract_once(html, base_url, False)

    if not filtered:
        return title, loose
    # 过滤后不足 15%，且宽松版明显更多内容 → 认定误杀，退回宽松版
    if len(filtered) < len(loose) * 0.15 and len(loose) - len(filtered) > 300:
        return title, loose
    return title, filtered


def looks_like_html(text: str) -> bool:
    head = text[:2048].lower()
    return "<html" in head or "<!doctype html" in head or "<body" in head \
        or "<div" in head or "<p" in head


# --------------------------------------------------------------------- 3. 串联


def fetch_web(url: str, timeout: float = DEFAULT_TIMEOUT,
              max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """抓一页并转成 Markdown。失败抛 FetchError。"""
    target = normalize_url(url)
    effective_url, content_type, html, status = run_curl(target, timeout)

    lowered = (content_type or "").lower()
    if lowered and "html" not in lowered and "xml" not in lowered and "text/plain" not in lowered:
        raise FetchError(
            "这个地址返回的不是网页，而是 %s。FetchWeb 只处理 HTML 页面，"
            "PDF/图片/压缩包这类请让用户自己下载。" % content_type)

    if not looks_like_html(html):
        raise FetchError(
            "抓回来的内容不像 HTML（前 120 字：%s…）。"
            "可能是纯前端渲染的页面，正文由 JavaScript 生成，curl 拿不到。"
            % _WS.sub(" ", html[:120]).strip())

    title, markdown = html_to_markdown(html, effective_url)
    markdown = markdown.strip()

    if len(markdown) < MIN_BODY_CHARS:
        raise FetchError(
            "网页拿到了，但正文提取失败：只解析出 %d 个字符"
            "（可能是登录墙、纯脚本渲染，或整页内容都在 iframe 里）。"
            % len(markdown))

    truncated = False
    if max_chars and len(markdown) > max_chars:
        markdown = markdown[:max_chars].rstrip()
        truncated = True

    lines = [
        "【网页标题】%s" % (title or "（无标题）"),
        "【地址】%s" % effective_url,
    ]
    # 只在真的跳转过（域名或路径变了）时才多打一行，免得补个斜杠也占一行
    if _bare_url(effective_url) != _bare_url(target):
        lines.append("【原始链接】%s" % target)
    lines += [
        "【状态】HTTP %d ｜ 正文 %d 字符%s"
        % (status, len(markdown), "（已截断）" if truncated else ""),
        "",
        "【正文 Markdown】",
        markdown,
    ]
    if truncated:
        lines += ["", "…（正文超过 %d 字符，后面已省略。需要更多内容请分段抓取别的页面。）"
                       % max_chars]

    return "\n".join(lines)


# --------------------------------------------------------------------- 工具声明


def _arg_text(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    return str(value).strip() if value is not None else ""


def _bare_url(url: str) -> str:
    """去掉协议和末尾斜杠，用来判断"是不是真的跳转了"。"""
    return url.replace("https://", "").replace("http://", "").rstrip("/")


def _arg_int(args: Dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    raw = args.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


@tool(
    name="FetchWeb",
    description=(
        "抓取指定网页的正文，转成 Markdown 返回。会去掉导航栏、广告、页脚这些噪音，"
        "只保留标题、段落、列表、表格和链接。\n"
        "典型用法：先用 searchWe 搜到 URL，再用 FetchWeb 读全文核对细节。"
        "用户直接给链接时也用它。\n"
        "不适合：PDF、图片、视频，以及正文靠 JavaScript 现场渲染的页面（会返回明确报错）。"
    ),
    parameters=params(
        {
            "url": string("网页完整地址，必须带 http:// 或 https://，例如 https://example.com/post/123"),
            "max_chars": integer("正文最多返回多少字符，默认 12000。页面很长时可以调到 30000。"),
        },
        required=["url"],
    ),
    danger=DangerLevel.SAFE,
    tags=["web", "net", "fetch"],
)
def fetch_we(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    url = _arg_text(args, "url")
    if not url:
        return ToolResult(
            "FetchWeb 需要一个 url 参数，例如 {\"url\": \"https://example.com\"}。请重新调用。",
            ok=False)

    max_chars = _arg_int(args, "max_chars", ctx.fetch_max_chars or DEFAULT_MAX_CHARS,
                         1000, 60000)

    try:
        text = fetch_web(url, timeout=float(ctx.fetch_timeout or DEFAULT_TIMEOUT),
                         max_chars=max_chars)
    except FetchError as e:
        return ToolResult("抓取网页失败：%s" % e, ok=False)
    except Exception as e:      # noqa: BLE001 —— 兜底，不让整轮对话炸掉
        return ToolResult("抓取网页时出现未预期的错误（%s）：%s"
                          % (type(e).__name__, e), ok=False)
    return ToolResult(text, ok=True)


ALL_TOOLS = [fetch_we]

__all__ = [
    "ALL_TOOLS",
    "fetch_we",
    "fetch_web",
    "run_curl",
    "build_curl_command",
    "normalize_url",
    "html_to_markdown",
    "cleanup_markdown",
    "render_table",
    "sniff_charset",
    "find_curl",
    "FetchError",
    "MarkdownExtractor",
    "DEFAULT_MAX_CHARS",
]
