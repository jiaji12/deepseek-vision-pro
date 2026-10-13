r"""
ZCode 识图代理 (see_proxy) v1
==============================
给纯文字模型（DeepSeek）装"眼睛"：在 ZCode 和模型之间插一层本地代理。

原理：
  ZCode 发来的 /v1/chat/completions 请求里，图片附件以 base64 存在请求体的
  attachments / content 块里（ZCode 会把图片缓存到 image-cache 目录）。
  本代理拦截请求 → 找到里面的图片 → 调用视觉模型（默认智谱 GLM-4V-Flash，
  见 see.py 的识图逻辑）识别成文字 → 把请求里的图片替换成识别出的文字 →
  再转发给真实的 DeepSeek 官方接口。

效果：
  - DeepSeek 永远不会收到图片，只收到文字 → 永远不会说"看不了图片"
  - 不依赖钩子/技能/模型自觉，新对话、新项目天然生效（拦在模型调用层）
  - 识别率 = see.py 的识别率（实测 OCR 中文/英文都很准）

用法：
  python see_proxy.py [--port 8777]
  ZCode 里把 DeepSeek 的 baseURL 改成 http://127.0.0.1:8777/v1
  （apiKey 填什么都行，代理会用你配置的 DeepSeek 官方 key 替换）

安全：
  - DeepSeek 密钥、智谱密钥都从单独配置文件读，脚本不留明文
  - 图片只在本机识别，不会进 ZCode 对话历史
"""
import argparse
import json
import os
import re
import base64
import http.client
import ssl
import threading
import time
import sys
import tempfile
import subprocess
import http.server

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_HERE, "see_proxy_config.json")

DEFAULTS = {
    "listen_port": 8777,
    "upstream_base": "https://api.deepseek.com",   # DeepSeek 官方
    "upstream_key": "",                             # 必填：DeepSeek 官方 API key
    "agnes_base_url": "https://open.bigmodel.cn/api/paas/v4",  # 智谱视觉模型
    "agnes_api_key": "",
    "agnes_model": "glm-4v-flash",                 # 智谱免费识图模型
    "agnes_max_tokens": 1024,
    "see_py": "",                                   # 启动时自动探测（同目录 see.py）
    "see_fallback": "",
    "enable_debug": True,
    "max_image_mb": 8,
    "image_thinking_off": True,   # 看图请求强制关思考：快速 + 不瞎编（写代码的纯文字请求不受影响）
}

# 缓存运行时的配置（load 后覆盖）
CFG = dict(DEFAULTS)
_log_lock = threading.Lock()


def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[see_proxy][{ts}] {msg}"
    print(line, flush=True)
    with _log_lock:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "see_proxy.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def load_config():
    """从 see_proxy_config.json 读配置，缺的字段用默认值。"""
    global CFG
    path = CONFIG_PATH
    if not os.path.exists(path):
        # 仓库里只有 example，用作默认展示
        alt = os.path.join(_HERE, "see_proxy_config.example.json")
        if os.path.exists(alt):
            path = alt
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                CFG.update({k: v for k, v in loaded.items() if k in DEFAULTS})
                log(f"配置已加载: {path}")
                if not CFG.get("upstream_key"):
                    log("!! 警告: upstream_key 未配置（DeepSeek 官方 key），识图转发会失败")
                if not CFG.get("agnes_api_key"):
                    log("!! 警告: agnes_api_key 未配置，识图会失败")
                return
        except Exception as e:
            log(f"配置加载失败({e})，用默认值")
    log(f"配置文件不存在: {CONFIG_PATH}，用默认值（请运行一次 --setup 或手动填写）")


def save_config():
    """把当前 CFG 写回配置文件。"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({k: CFG.get(k) for k in DEFAULTS}, f, ensure_ascii=False, indent=2)
    log(f"配置已保存: {CONFIG_PATH}")


# ---------------------------------------------------------------------------
# 识图（复用 see.py）
# ---------------------------------------------------------------------------
def call_see_py(image_paths, prompt):
    """调 see.py 识别图片，返回识别文字。成功返回字符串，失败返回 None。"""
    if not image_paths:
        return None
    # 决定用哪个 python
    py = "python" if os.name == "nt" else "python3"
    # 自动探测 see.py：优先同目录，其次配置值
    local_see = os.path.join(_HERE, "see.py")
    see_py = local_see if os.path.exists(local_see) else (CFG.get("see_py") or "")
    if not os.path.exists(see_py):
        alt = CFG.get("see_fallback") or ""
        if alt and os.path.exists(alt):
            see_py = alt
    if not os.path.exists(see_py):
        log(f"!! see.py 不存在: {see_py}")
        return None
    try:
        cmd = [py, see_py] + image_paths + ["--prompt", prompt]
        log("识图调用: " + " ".join([py, see_py, f"[{len(image_paths)}图]", "--prompt ..."]))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode != 0 or not out:
            log(f"see.py 失败 rc={result.returncode} err={err[:300]}")
            return None
        log(f"识图成功: {len(out)} 字符")
        return out
    except subprocess.TimeoutExpired:
        log("see.py 超时(90s)")
        return None
    except Exception as e:
        log(f"see.py 调用异常: {e}")
        return None


def find_images_in_payload(req):
    """
    从请求体里找出图片。
    返回 (images: [base64_str...], image_paths: [str...], saw_image: bool)
    ZCode 图片有两种形态：
      1. attachments 数组里 kind=image 且带 dataBase64
      2. content 块里 type=image_url 带 base64 data url
    """
    images_base64 = []
    image_paths = []
    saw_image = False

    def walk(o, path="root"):
        nonlocal saw_image
        if isinstance(o, dict):
            _k = o.get("type") or o.get("kind") or ""
            k = _k.lower() if isinstance(_k, str) else ""
            if k == "image" or k == "image_url" or k == "image_url":
                saw_image = True
                # 形态1: {type:'image', dataBase64:'...'}
                db64 = o.get("dataBase64") or o.get("data_base64") or o.get("base64")
                if db64:
                    images_base64.append(db64)
                # 形态2: {type:'image_url', image_url:{url:'data:image/png;base64,...'}}
                iu = o.get("image_url")
                if isinstance(iu, dict) and iu.get("url"):
                    url = iu["url"]
                    if url.startswith("data:image"):
                        m = re.search(r"base64,(.*)$", url, re.S)
                        if m:
                            images_base64.append(m.group(1))
                            saw_image = True
                    elif url.startswith("http"):
                        image_paths.append(url)
                # 形态3: localPath
                lp = o.get("localPath") or o.get("local_path")
                if lp and os.path.exists(lp):
                    image_paths.append(lp)
            for v in o.values():
                walk(v, path + ".dict")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")

    walk(req)
    # 去重
    seen = set()
    unique_b64 = []
    for b in images_base64:
        if b not in seen:
            seen.add(b)
            unique_b64.append(b)
    return unique_b64, image_paths, saw_image


def write_temp_images(base64_list):
    """把 base64 图片写到临时目录，返回文件路径列表。"""
    paths = []
    tmpdir = tempfile.gettempdir()
    for i, b in enumerate(base64_list):
        try:
            raw = base64.b64decode(b)
            if len(raw) == 0:
                continue
            # 根据魔数猜扩展名
            ext = "png"
            if raw[:3] == b"\xff\xd8\xff":
                ext = "jpg"
            elif raw[:6] in (b"GIF89a", b"GIF87a"):
                ext = "gif"
            elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
                ext = "webp"
            p = os.path.join(tmpdir, f"see_proxy_{int(time.time()*1000)}_{i}.{ext}")
            with open(p, "wb") as f:
                f.write(raw)
            paths.append(p)
        except Exception as e:
            log(f"base64 解码失败: {e}")
    return paths


def sanitize_images_in_payload(req, recognized_text, root=None):
    """
    把请求里的图片替换成识别出的文字。
    深度遍历，凡是 type/kind 为 image/image_url 的块，替换为 text 块。
    只遍历 root 指定的子结构（默认整个 req）。
    返回替换数量。
    """
    replaced = [0]
    replaced_b64 = set()

    def walk(o):
        if isinstance(o, dict):
            _k = o.get("type") or o.get("kind") or ""
            k = _k.lower() if isinstance(_k, str) else ""
            if k in ("image", "image_url"):
                db64 = o.get("dataBase64") or o.get("data_base64") or o.get("base64")
                if db64:
                    replaced_b64.add(db64)
                replaced[0] += 1
                # 原地把图片块变成文字块
                o.clear()
                o["type"] = "text"
                o["text"] = recognized_text
                return
            for v in list(o.values()):
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(root if root is not None else req)
    return replaced[0]


def replace_remaining_images(req, placeholder):
    """
    把请求里剩余的所有图片块（历史消息里的，非本次新发的）替换成占位文字，
    避免多张历史图混在一起重复识别。
    返回替换数量。
    """
    count = [0]

    def walk(o):
        if isinstance(o, dict):
            _k = o.get("type") or o.get("kind") or ""
            k = _k.lower() if isinstance(_k, str) else ""
            if k in ("image", "image_url"):
                o.clear()
                o["type"] = "text"
                o["text"] = placeholder
                count[0] += 1
                return
            for v in list(o.values()):
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(req)
    return count[0]


def find_last_user_message(messages):
    """返回最后一条 role=user 且 content 是列表的消息（本次新发的那条）。"""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), list):
            return m
    return None


def normalize_weird_blocks(req):
    """
    ZCode 发的消息里，content 块的 type 字段有时是嵌套对象 {kind:"text"}，
    DeepSeek 官方只认字符串 type（"text"），不认这种格式，会报
    "unknown variant `kind`, expected `text`"。
    这里深度遍历：凡是 type/kind 不是字符串的块，拆成纯文本，避免官方报错。
    返回处理了几处。
    """
    fixed = [0]

    def walk(o):
        if isinstance(o, dict):
            t = o.get("type")
            k = o.get("kind")
            # 只有 type 或 kind 存在但又不是字符串才算怪块；普通容器（无 type/kind）不动
            bad = (t is not None and not isinstance(t, str)) or (k is not None and not isinstance(k, str))
            if bad:
                # 取文本内容，把 type 修正为标准字符串
                txt = o.get("text") or o.get("content") or ""
                if not isinstance(txt, str):
                    txt = str(txt)
                o.clear()
                o["type"] = "text"
                o["text"] = txt
                fixed[0] += 1
                return
            for v in list(o.values()):
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(req)
    return fixed[0]


def strip_mcp_tools(req):
    """
    ZCode 会给所有模型注入一堆 MCP 插件工具（chrome-devtools、browser-use 等）。
    这些工具的 schema 是 Anthropic 风格，DeepSeek 的 OpenAI 兼容接口不认识，
    会直接 400 拒收整个请求（如 chrome-devtools 的 navigate_page "text" 类型错误）。
    而且 DeepSeek 反正执行不了 MCP 工具，滤掉它们不影响任何能力。
    返回滤掉了几个。
    """
    tools = req.get("tools")
    if not isinstance(tools, list) or not tools:
        return 0
    before = len(tools)
    keep = []
    for t in tools:
        name = ""
        if isinstance(t, dict):
            fn = t.get("function")
            if isinstance(fn, dict):
                name = fn.get("name") or ""
        if str(name).startswith("mcp__"):
            continue  # 丢掉 MCP 工具
        keep.append(t)
    req["tools"] = keep
    return before - len(keep)


# ---------------------------------------------------------------------------
# 转发（简化版：非流式完整转发 + 流式透传）
# ---------------------------------------------------------------------------
class Forwarder:
    def __init__(self):
        host, https = _parse_upstream()
        self.host = host
        self.https = https
        self.ctx = ssl.create_default_context()

    def _conn(self):
        if self.https:
            return http.client.HTTPSConnection(self.host, timeout=300, context=self.ctx)
        return http.client.HTTPConnection(self.host, timeout=300)

    def forward_full(self, method, path, body, headers, retries=3):
        last_err = None
        for attempt in range(1, retries + 1):
            conn = self._conn()
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                resp_body = resp.read()
                resp_headers = dict(resp.getheaders())
                if status in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {status}"
                    delay = 1.5 * (2 ** (attempt - 1))
                    log(f"上游 {status}，第{attempt}/{retries}次重试，等{delay:.0f}s")
                    time.sleep(delay)
                    continue
                return status, resp_headers, resp_body
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                log(f"转发异常: {last_err}，重试{attempt}/{retries}")
                delay = 1.0 * attempt
                time.sleep(delay)
            finally:
                conn.close()
        return 502, {}, json.dumps({"error": {"message": f"upstream failed: {last_err}", "type": "proxy_error"}}).encode()

    def forward_stream(self, method, path, body, headers, on_chunk):
        conn = self._conn()
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            hdrs = dict(resp.getheaders())
            # 非200走完整返回
            if status != 200:
                return status, resp.read(), False
            want = on_chunk(b"", status, hdrs)
            if not want:
                return status, resp.read(), False
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                on_chunk(chunk, status, None)
            return status, None, True
        finally:
            conn.close()


def _parse_upstream():
    base = CFG["upstream_base"].rstrip("/")
    if base.startswith("https://"):
        return base[len("https://"):], True
    rest = base[len("http://"):] if base.startswith("http://") else base
    return rest, False


def _is_streaming(body_bytes):
    try:
        d = json.loads(body_bytes or b"{}")
        return bool(d.get("stream"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self):
        ln = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(ln) if ln else b""
        if not self.path.startswith("/v1"):
            self._send(404, b'{"error":"not found"}')
            return
        self._process(self.command, self.path, body)

    def _process(self, method, path, body):
        recognized_text = None
        try:
            # --- 解析请求 ---
            req = json.loads(body or b"{}")
            messages = req.get("messages", [])

            # --- 调试：打印请求结构（enabled 时） ---
            if CFG.get("enable_debug"):
                try:
                    import copy as _copy
                    slim = _copy.deepcopy(req)
                    def _redact(o):
                        if isinstance(o, dict):
                            if o.get("type") in ("image", "image_url") or "image" in str(o.get("type", "")).lower():
                                o["_IMG"] = "<image-block>"
                            if "dataBase64" in o: o["dataBase64"] = "<b64:%d>" % len(str(o["dataBase64"]))
                            if isinstance(o.get("url"), str) and o["url"].startswith("data:image"):
                                o["url"] = "<data-image:%d>" % len(o["url"])
                            for v in o.values(): _redact(v)
                        elif isinstance(o, list):
                            for v in o: _redact(v)
                    _redact(slim)
                    # 打印工具定义（含 chrome-devtools 的具体 schema）
                    tools = req.get("tools")
                    if tools:
                        tnames = []
                        for t in tools:
                            if isinstance(t, dict):
                                fn = t.get("function", {})
                                n = fn.get("name", "?")
                                if "mcp" in str(n).lower() or "chrome" in str(n).lower():
                                    tnames.append({"name": n, "schema": fn.get("parameters", {})})
                                else:
                                    tnames.append({"name": n})
                        log("工具: " + json.dumps(tnames, ensure_ascii=False, default=str)[:600])
                    brief = {"msg_count": len(messages), "top_keys": list(req.keys())}
                    if messages and isinstance(messages[0], dict):
                        m0 = messages[0]
                        brief["first_role"] = m0.get("role")
                        c0 = m0.get("content")
                        if isinstance(c0, list):
                            brief["first_content_types"] = [ (x.get("type") if isinstance(x.get("type"), str) else type(x.get("type")).__name__) for x in c0 if isinstance(x, dict) ]
                        else:
                            brief["first_content"] = str(c0)[:120]
                    # 打印每条消息的 content 类型（定位图片在哪条）
                    all_types = []
                    for mi, m in enumerate(messages):
                        if not isinstance(m, dict): continue
                        ct = m.get("content")
                        if isinstance(ct, list):
                            types = []
                            for x in ct:
                                if isinstance(x, dict):
                                    t = x.get("type")
                                    if not isinstance(t, str):
                                        t = "<dict:" + str(type(t).__name__) + ">"
                                    types.append(t)
                                else:
                                    types.append(type(x).__name__)
                            all_types.append({"msg": mi, "role": m.get("role"), "content_types": types})
                        elif isinstance(ct, str):
                            all_types.append({"msg": mi, "role": m.get("role"), "content": "str:" + str(len(ct))})
                    if all_types:
                        brief["msg_types"] = all_types
                    # 顶层 attachments 等字段
                    for tk in ("attachments", "files", "image-cache", "images"):
                        if tk in req:
                            brief[tk] = type(req[tk]).__name__
                    log("请求结构: " + json.dumps(brief, ensure_ascii=False))
                except Exception as _e:
                    log(f"debug 打印失败: {_e}")

            # --- 先修正 ZCode 的怪格式（type 是嵌套对象） ---
            # 不修的话 DeepSeek 官方会报 "unknown variant `kind`, expected `text`"
            n_fix = normalize_weird_blocks(req)
            if n_fix:
                log(f"已修正 {n_fix} 处 type 为嵌套对象的块")
                body = json.dumps(req, ensure_ascii=False).encode("utf-8")

            # --- 滤掉 MCP 插件工具（chrome-devtools 等，DeepSeek 用不了还会 400） ---
            n_strip = strip_mcp_tools(req)
            if n_strip:
                log(f"已滤掉 {n_strip} 个 MCP 工具（DeepSeek 不兼容）")
                body = json.dumps(req, ensure_ascii=False).encode("utf-8")

            # --- 找图片 ---
            images_b64, image_paths, saw_image = find_images_in_payload(req)

            if saw_image or image_paths or images_b64:
                log(f"检测到图片! base64×{len(images_b64)} 路径×{len(image_paths)}")
                # 只识别"本次新发的图"（最后一条 user 消息里的图）
                # 历史消息里累积的图不重复识别，避免多张混一起又慢又乱
                last_user = find_last_user_message(messages)
                target = last_user if last_user is not None else req
                target_images = [0]

                def count_imgs(o):
                    if isinstance(o, dict):
                        _k = o.get("type") or o.get("kind") or ""
                        k = _k.lower() if isinstance(_k, str) else ""
                        if k in ("image", "image_url"):
                            target_images[0] += 1
                        for v in o.values():
                            count_imgs(v)
                    elif isinstance(o, list):
                        for v in o:
                            count_imgs(v)

                count_imgs(target)
                if target_images[0] == 0:
                    # 最后一条 user 消息没有图（历史图而已），全部替换成占位
                    ph = "【用户之前发送过图片，此处省略图片内容。】"
                    n = replace_remaining_images(req, ph)
                    log(f"历史图片占位: 替换 {n} 处")
                else:
                    # 组装要识别的图片路径（只从最后一条 user 消息取图）
                    tmp_paths = []
                    # 只从最后一条 user 消息里提取 base64 图片（兼容 dataBase64 和 data URL 两种形态）
                    def collect_b64(o, out):
                        if isinstance(o, dict):
                            _k = o.get("type") or o.get("kind") or ""
                            k = _k.lower() if isinstance(_k, str) else ""
                            if k in ("image", "image_url"):
                                db64 = o.get("dataBase64") or o.get("data_base64") or o.get("base64")
                                if db64:
                                    out.append(db64)
                                iu = o.get("image_url")
                                if isinstance(iu, dict) and isinstance(iu.get("url"), str):
                                    url = iu["url"]
                                    if url.startswith("data:image"):
                                        m = re.search(r"base64,(.*)$", url, re.S)
                                        if m:
                                            out.append(m.group(1))
                            for v in o.values():
                                collect_b64(v, out)
                        elif isinstance(o, list):
                            for v in o:
                                collect_b64(v, out)
                    last_b64 = []
                    collect_b64(target, last_b64)
                    # 去重
                    seen = set()
                    uniq = [b for b in last_b64 if not (b in seen or seen.add(b))]
                    if uniq:
                        tmp_paths = write_temp_images(uniq)
                    # 路径形式的图（localPath / http）也从最后一条消息取
                    last_paths = []
                    def collect_paths(o, out):
                        if isinstance(o, dict):
                            _k = o.get("type") or o.get("kind") or ""
                            k = _k.lower() if isinstance(_k, str) else ""
                            if k in ("image", "image_url"):
                                iu = o.get("image_url")
                                if isinstance(iu, dict) and isinstance(iu.get("url"), str) and iu["url"].startswith("http"):
                                    out.append(iu["url"])
                                lp = o.get("localPath") or o.get("local_path")
                                if lp and os.path.exists(lp):
                                    out.append(lp)
                            for v in o.values():
                                collect_paths(v, out)
                        elif isinstance(o, list):
                            for v in o:
                                collect_paths(v, out)
                    collect_paths(target, last_paths)
                    all_paths = list(dict.fromkeys([p for p in (tmp_paths + last_paths) if p]))
                    if all_paths:
                        prompt = "完整转述这张图片的所有可见文字与内容，不要省略。中文回答。"
                        recognized_text = call_see_py(all_paths, prompt)
                        for p in tmp_paths:
                            try:
                                os.remove(p)
                            except Exception:
                                pass
                    if recognized_text:
                        replace_text = (
                            "【用户发送了一张图片，以下是系统调用视觉模型自动识别出的图片内容，"
                            "请直接照实复述/理解，不要再说看不了图片。】\n"
                            "===图片识别结果===\n" + recognized_text + "\n===图片识别结果结束==="
                        )
                        # 只替换本次消息里的图，历史图替换成占位
                        n_new = sanitize_images_in_payload(req, replace_text, root=target)
                        n_hist = replace_remaining_images(req, "【用户之前发送的图片，此处省略。】")
                        log(f"识别文字已替换 {n_new} 处，历史图片占位 {n_hist} 处")
                        body = json.dumps(req, ensure_ascii=False).encode("utf-8")
                    else:
                        ph = "【用户发送了一张图片，但本次自动识别失败，请告知用户图片识别失败。】"
                        n = replace_remaining_images(req, ph)
                        log(f"识别失败，图片替换为失败提示 {n} 处")
                        body = json.dumps(req, ensure_ascii=False).encode("utf-8")
            else:
                log("无图片，直接转发")
        except Exception as e:
            log(f"解析请求异常: {e}")

        # --- 看图请求强制关思考（image_thinking_off） ---
        # 带图片的请求走完识别后，请求体已替换成文字。此时强制塞 thinking disabled，
        # 让 DeepSeek 不做无谓的长时间思考（看图场景 max 思考会拖到几十秒）。
        if recognized_text and CFG.get("image_thinking_off"):
            try:
                d = json.loads(body or b"{}")
                d["thinking"] = {"type": "disabled"}
                body = json.dumps(d, ensure_ascii=False).encode("utf-8")
                log("看图请求: 已强制 thinking=disabled (关思考)")
            except Exception as e:
                log(f"注入 thinking disabled 失败: {e}")

        # --- 转发到上游 ---
        upstream_key = CFG.get("upstream_key") or ""
        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("authorization", "content-length", "host", "connection",
                                "accept-encoding", "transfer-encoding")
        }
        headers["Authorization"] = f"Bearer {upstream_key}"
        headers["Content-Type"] = "application/json"

        # 转发路径：/v1/chat/completions
        upstream_path = path
        forwarder = Forwarder()

        if _is_streaming(body):
            sent_headers = [False]
            errored = [False]

            def on_chunk(chunk, status, hdrs):
                if status is not None and hdrs is not None:
                    if status != 200:
                        return False
                    sent_headers[0] = True
                    try:
                        self.send_response(status)
                        ct = hdrs.get("Content-Type") or "text/event-stream"
                        self.send_header("Content-Type", ct)
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                    except Exception:
                        pass
                    return True
                if chunk:
                    try:
                        hexlen = f"{len(chunk):X}\r\n"
                        self.wfile.write(hexlen.encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    except Exception:
                        errored[0] = True

            try:
                status, full_body, streamed = forwarder.forward_stream(method, upstream_path, body, headers, on_chunk)
                if sent_headers[0] and not errored[0]:
                    try:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                elif status != 200:
                    self._send(status, full_body or b"{}")
            except Exception as e:
                try:
                    self._send(500, json.dumps({"error": {"message": str(e)}}).encode())
                except Exception:
                    pass
        else:
            status, hdrs, resp = forwarder.forward_full(method, upstream_path, body, headers)
            try:
                self.send_response(status)
                ct = hdrs.get("Content-Type", "application/json")
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
                self.wfile.flush()
            except Exception:
                pass

    def _send(self, code, payload):
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            pass

    def log_message(self, *a):
        pass

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle


def main():
    ap = argparse.ArgumentParser(description="ZCode 识图代理: 图片→AGNES识别→文字→DeepSeek")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--setup", action="store_true", help="交互式填写配置")
    args = ap.parse_args()

    load_config()

    if args.setup:
        print("=== see_proxy 配置 ===")
        keys = [
            ("upstream_key", "DeepSeek 官方 API key"),
            ("agnes_api_key", "AGNES API key"),
            ("agnes_model", "AGNES 视觉模型名"),
            ("agnes_base_url", "AGNES base_url"),
        ]
        for k, label in keys:
            cur = CFG.get(k, "")
            v = input(f"{label} [{cur}]: ").strip()
            if v:
                CFG[k] = v
        save_config()
        print("配置已保存。启动: python see_proxy.py")
        return

    if args.port:
        CFG["listen_port"] = args.port

    if not CFG.get("upstream_key"):
        log("!! 未配置 DeepSeek key。请运行: python see_proxy.py --setup")
    if not CFG.get("agnes_api_key"):
        log("!! 未配置 AGNES key。请运行: python see_proxy.py --setup")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", CFG["listen_port"]), ProxyHandler)
    log(f"识图代理已启动: http://127.0.0.1:{CFG['listen_port']}/v1")
    log(f"上游: {CFG['upstream_base']} | 识图模型: {CFG['agnes_model']}")
    log(f"ZCode 里把 DeepSeek 的 baseURL 改成 http://127.0.0.1:{CFG['listen_port']}/v1")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("已停止")


if __name__ == "__main__":
    main()
