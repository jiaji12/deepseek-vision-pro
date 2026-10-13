#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看图工具 (see.py) — 通用版
================================
支持采用 Chat Completions 多模态消息格式的 OpenAI 兼容视觉模型（GPT-4o、
gpt-4.1-mini、DeepSeek-VL、通义千问、智谱GLM、各种中转站的视觉模型等）。

用法:
  python see.py <图片路径> [图片2 ...]   # 看指定图片
  python see.py --clip                    # 看剪贴板里的截图
  python see.py --dir <文件夹>            # 看文件夹里所有图片
  python see.py --help                    # 帮助

第一次使用：
  1. 运行 python see.py --setup  生成配置文件
     如果脚本目录不可写，可运行 python see.py --setup --global
  2. 用记事本打开 config.json，填上你的密钥、接口地址、模型名
  3. 再运行上面的命令即可

没有任何参数时会自动尝试：剪贴板截图 → 若没有则提示用法。
"""

import argparse, base64, io, json, os, shutil, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request


def configure_console_encoding():
    """让 Windows 非 UTF-8 控制台也能输出中文和 JSON。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


configure_console_encoding()


class SeeError(Exception):
    """可直接展示给用户的错误。"""


DEFAULT_CONFIG = {
    "base_url": "https://api.openai.com/v1",
    "api_key": "在这里填你的密钥",
    "model": "gpt-4o",
    "max_tokens": 2048,
    "max_image_dimension": 4096,
    "max_upload_mb": 8,
    "prompt": "请仔细描述这张(些)图片的内容。如果是截图，请逐条提取上面所有文字（报错信息、聊天记录、表格、界面内容等），不要总结、不要省略，用中文回答。",
}

# ---- 找配置文件 ----
def config_candidates():
    """返回配置候选：优先使用有效的 skill 本地配置，再回退到用户配置。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.join(script_dir, "config.json"),
        os.path.join(os.path.expanduser("~"), ".see_config.json"),
    ]


def find_config():
    """选择配置文件；无效的本地模板不会遮蔽有效的全局配置。"""
    existing = []
    for path in config_candidates():
        if not os.path.isfile(path):
            continue
        existing.append(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not config_errors(cfg):
                return path
        except (OSError, ValueError, TypeError):
            continue
    return existing[0] if existing else None

def load_config():
    cfg_path = find_config()
    if not cfg_path:
        raise SeeError("没有找到配置文件。请先运行 python see.py --setup；如果脚本目录不可写，运行 python see.py --setup --global。")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        raise SeeError(f"读取配置文件失败: {e}\n配置文件位置: {cfg_path}") from e
    if not isinstance(cfg, dict):
        raise SeeError(f"配置文件格式错误：顶层必须是 JSON 对象。\n配置文件位置: {cfg_path}")
    return cfg, cfg_path

def config_errors(cfg):
    """返回配置问题，不直接退出，供 doctor 和正常调用共用。"""
    bad = []
    if not isinstance(cfg, dict):
        return ["顶层必须是 JSON 对象"]
    if not isinstance(cfg.get("api_key"), str) or not cfg["api_key"].strip() or "在这里填" in cfg["api_key"]:
        bad.append("api_key (密钥)")
    if not isinstance(cfg.get("model"), str) or not cfg["model"].strip():
        bad.append("model (模型名)")
    if not isinstance(cfg.get("base_url"), str) or not cfg["base_url"].strip():
        bad.append("base_url (接口地址)")
    else:
        parsed = urllib.parse.urlparse(cfg["base_url"].strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            bad.append("base_url (必须是 http/https 地址)")
    headers = cfg.get("headers")
    if headers is not None and not isinstance(headers, dict):
        bad.append("headers (必须是 JSON 对象)")
    if cfg.get("detail") is not None and cfg.get("detail") not in ("auto", "low", "high"):
        bad.append("detail (只能是 auto/low/high)")
    for key in ("max_tokens", "max_image_dimension", "max_upload_mb"):
        if key in cfg:
            try:
                if int(cfg[key]) <= 0:
                    bad.append(f"{key} (必须是正数)")
            except (TypeError, ValueError):
                bad.append(f"{key} (必须是数字)")
    return bad


def check_config(cfg, cfg_path):
    """检查配置是否真的填了密钥。"""
    bad = config_errors(cfg)
    if bad:
        raise SeeError(
            "配置还没有填好，缺少: " + "、".join(bad)
            + f"\n请修改配置文件: {cfg_path}"
        )

# ---- 剪贴板截图 ----
def save_clipboard_png(data):
    if not data:
        return None
    path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="see_clip_", suffix=".png", delete=False) as f:
            path = f.name
            f.write(data)
    except OSError as e:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        raise SeeError(f"保存剪贴板截图失败: {e}") from e
    return path


def grab_external_clipboard():
    """在没有 Pillow 时尝试调用系统剪贴板工具。"""
    if sys.platform == "darwin":
        commands = [["pngpaste", "-"]]
    elif sys.platform.startswith("linux"):
        commands = [
            ["wl-paste", "--no-newline", "--type", "image/png"],
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
        ]
    else:
        commands = []
    for command in commands:
        if not shutil.which(command[0]):
            continue
        try:
            result = subprocess.run(command, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout:
            return result.stdout
    return None


def grab_clipboard_image(verbose=True):
    """从剪贴板取出图片，返回文件路径；剪贴板无图片返回 None。"""
    image = None
    try:
        from PIL import Image, ImageGrab
        try:
            image = ImageGrab.grabclipboard()
        except Exception:
            image = None
        if isinstance(image, Image.Image):
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
            return save_clipboard_png(buffer.getvalue())
    except ImportError:
        pass

    path = save_clipboard_png(grab_external_clipboard())
    if path:
        return path
    if verbose:
        print("缺少可用的剪贴板图片读取方式。Windows/macOS/Linux 可安装 pillow；Linux 也可使用 wl-paste 或 xclip。")
        print("看文件路径不需要剪贴板依赖，可以直接运行: python see.py 图片路径")
    return None

# ---- 调用视觉模型 ----
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
API_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
MAX_IMAGES_PER_REQUEST = 20
MAX_SOURCE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_UPLOAD_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_IMAGE_DIMENSION = 4096


def list_images(folder):
    """列出文件夹中的图片，统一处理大小写和非法路径。"""
    if not os.path.isdir(folder):
        raise SeeError(f"文件夹不存在: {folder}")
    try:
        with os.scandir(folder) as entries:
            paths = [entry.path for entry in entries
                     if entry.is_file() and os.path.splitext(entry.name)[1].lower() in IMAGE_EXTS]
    except OSError as e:
        raise SeeError(f"读取文件夹失败: {folder} ({e})") from e
    return sorted(paths)

def mime_of(ext):
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
    }.get(ext, "image/png")


def prepare_image(path, cfg):
    """读取图片；必要时缩放并转成 JPEG，降低请求体积并统一格式。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        source_size = os.path.getsize(path)
        if source_size > MAX_SOURCE_BYTES:
            raise SeeError(f"图片过大: {path}（原文件不能超过 100 MB）")
        with open(path, "rb") as f:
            raw = f.read()
    except SeeError:
        raise
    except OSError as e:
        raise SeeError(f"读取图片失败: {path} ({e})") from e

    try:
        max_upload_bytes = int(cfg.get("max_upload_mb", 8)) * 1024 * 1024
        max_dimension = int(cfg.get("max_image_dimension", DEFAULT_MAX_IMAGE_DIMENSION))
    except (TypeError, ValueError) as e:
        raise SeeError("图片压缩配置必须是正整数。") from e
    if max_upload_bytes <= 0 or max_dimension <= 0:
        raise SeeError("图片压缩配置必须是正整数。")

    needs_conversion = ext not in API_IMAGE_EXTS or len(raw) > max_upload_bytes
    try:
        from PIL import Image, ImageOps
    except ImportError:
        if needs_conversion:
            raise SeeError("图片需要压缩或转换格式，请先运行: python -m pip install pillow")
        return raw, mime_of(ext)

    try:
        with Image.open(io.BytesIO(raw)) as source:
            width, height = source.size
            needs_conversion = needs_conversion or max(width, height) > max_dimension
            if not needs_conversion:
                return raw, mime_of(ext)
            image = ImageOps.exif_transpose(source).copy()
    except SeeError:
        raise
    except Exception as e:
        if ext in API_IMAGE_EXTS and len(raw) <= max_upload_bytes:
            return raw, mime_of(ext)
        raise SeeError(f"无法读取图片: {path} ({e})") from e

    resampling = getattr(Image, "Resampling", Image).LANCZOS
    image.thumbnail((max_dimension, max_dimension), resampling)
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")

    for quality in (85, 70, 50, 30):
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True)
        if buffer.tell() <= max_upload_bytes:
            return buffer.getvalue(), "image/jpeg"

    raise SeeError(f"图片压缩后仍超过 {max_upload_bytes // (1024 * 1024)} MB: {path}")


def build_headers(cfg):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.get('api_key')}",
    }
    custom = cfg.get("headers") or {}
    if not isinstance(custom, dict):
        raise SeeError("配置项 headers 必须是 JSON 对象。")
    for key, value in custom.items():
        if not isinstance(value, (str, int, float)):
            raise SeeError(f"配置项 headers.{key} 必须是字符串或数字。")
        headers[str(key)] = str(value)
    return headers


def content_to_text(content):
    """兼容字符串和部分服务商返回的内容数组。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        return content_to_text(content.get("text") or content.get("content") or "")
    if isinstance(content, list):
        pieces = [content_to_text(item) for item in content]
        return "\n".join(piece for piece in pieces if piece).strip()
    return ""


def response_text(resp):
    choices = resp.get("choices") if isinstance(resp, dict) else None
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or choice.get("delta") or {}
        if not isinstance(message, dict):
            message = {}
        text = content_to_text(message.get("content"))
        if not text:
            text = content_to_text(message.get("reasoning_content"))
        if not text:
            text = content_to_text(choice.get("text"))
        return text
    return content_to_text(resp.get("output_text") or resp.get("response")) if isinstance(resp, dict) else ""


def describe_images(cfg, paths, max_retries=3, prompt=None, detail=None):
    if not paths:
        return "（没有可用的图片）"
    if len(paths) > MAX_IMAGES_PER_REQUEST:
        raise SeeError(f"一次最多识别 {MAX_IMAGES_PER_REQUEST} 张图片，请分批运行。")

    parts = []
    for p in paths:
        if not os.path.isfile(p):
            raise SeeError(f"图片不存在: {p}")
        ext = os.path.splitext(p)[1].lower()
        if ext not in IMAGE_EXTS:
            raise SeeError(f"不支持的图片格式: {p}（支持 png/jpg/jpeg/gif/bmp/webp）")
        image_data, image_mime = prepare_image(p, cfg)
        b64 = base64.b64encode(image_data).decode()
        image_url = {"url": f"data:{image_mime};base64,{b64}"}
        selected_detail = detail or cfg.get("detail")
        if selected_detail in ("auto", "low", "high"):
            image_url["detail"] = selected_detail
        parts.append({"type": "image_url",
                      "image_url": image_url})

    user_text = prompt if prompt is not None else cfg.get("prompt")
    if not isinstance(user_text, str) or not user_text.strip():
        user_text = "请描述这张图片的内容，如果是截图请提取全部文字。"
    parts.append({"type": "text", "text": user_text})

    try:
        max_tokens = int(cfg.get("max_tokens", 2048))
    except (TypeError, ValueError) as e:
        raise SeeError("配置项 max_tokens 必须是正整数。") from e
    if max_tokens <= 0:
        raise SeeError("配置项 max_tokens 必须是正整数。")

    payload = {
        "model": cfg.get("model"),
        "messages": [{"role": "user", "content": parts}],
        "max_tokens": max_tokens,
    }

    base_url = cfg.get("base_url", "").rstrip("/")
    if not base_url.endswith("/chat/completions"):
        base_url += "/chat/completions"
    req = urllib.request.Request(base_url,
        data=json.dumps(payload).encode(),
        headers=build_headers(cfg))

    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                resp = json.load(response)
            text = response_text(resp)
            return text or "（模型返回了空内容）"
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            raise SeeError(f"接口返回格式异常: {e}") from e
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:500]
            except Exception:
                pass
            if e.code in (401, 403):
                raise SeeError(f"认证失败（{e.code}）。请检查 config.json 里的密钥/api_key 是否正确。\n接口返回: {body}")
            if e.code == 429 or e.code >= 500:
                last_err = f"HTTP {e.code}: {body}"
                if attempt + 1 < max_retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            raise SeeError(f"调用接口失败（HTTP {e.code}）: {body}\n请检查 config.json 里的 base_url（接口地址）和 model（模型名）是否填对。")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt + 1 < max_retries:
                time.sleep(1.5 * (attempt + 1))
                continue

    raise SeeError(f"调用视觉模型失败: {last_err}\n（网络问题，可重试一次；或检查 base_url 是否可访问）")


def print_description(cfg, paths, json_output=False, prompt=None, detail=None, max_retries=3):
    try:
        text = describe_images(cfg, paths, max_retries=max_retries, prompt=prompt, detail=detail)
        if json_output:
            print(json.dumps({"ok": True, "text": text, "images": paths}, ensure_ascii=False))
        else:
            print(text)
        return 0
    except SeeError as e:
        if json_output:
            print(json.dumps({"ok": False, "error": str(e), "images": paths}, ensure_ascii=False))
        else:
            print(f"错误: {e}", file=sys.stderr)
        return 1


def process_clipboard_image(cfg, path, json_output=False, prompt=None, detail=None, max_retries=3):
    try:
        if not json_output:
            print(f"已从剪贴板取到截图: {path}\n识别中...")
        return print_description(cfg, [path], json_output, prompt, detail, max_retries)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def find_example_config(script_dir):
    """优先找 skill 内模板，再兼容工具包根目录模板。"""
    candidates = [
        os.path.join(script_dir, "config.example.json"),
        os.path.join(os.path.dirname(script_dir), "config.example.json"),
    ]
    return next((p for p in candidates if os.path.isfile(p)), None)

# ---- 诊断与主流程 ----
def setup_config(use_global=False, json_output=False):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(
        os.path.expanduser("~") if use_global else script_dir,
        ".see_config.json" if use_global else "config.json",
    )
    if os.path.exists(target):
        result = {"ok": True, "config": target, "existing": True}
        if json_output:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"配置文件已经存在: {target}\n直接编辑它即可。")
        return 0
    try:
        example = find_example_config(script_dir)
        if example:
            shutil.copyfile(example, target)
        else:
            with open(target, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    except OSError as e:
        result = {"ok": False, "error": f"生成配置文件失败: {e}", "config": target}
        if json_output:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result["error"], file=sys.stderr)
        return 1
    result = {"ok": True, "config": target, "existing": False}
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"已生成配置文件: {target}\n用记事本打开，填上你的密钥、接口地址、模型名。")
    return 0


def run_doctor(json_output=False):
    checks = []

    def add(name, status, detail, required=True):
        checks.append({"name": name, "status": status, "detail": detail, "required": required})

    add("python", "ok", sys.version.split()[0])
    cfg_path = find_config()
    cfg = None
    if not cfg_path:
        add("config", "error", "没有找到配置文件")
    else:
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            errors = config_errors(cfg)
            add("config", "ok" if not errors else "error", cfg_path if not errors else ", ".join(errors))
        except Exception as e:
            add("config", "error", f"读取失败: {e}")

    try:
        import PIL
        add("pillow", "ok", getattr(PIL, "__version__", "已安装"), required=False)
    except ImportError:
        add("pillow", "warning", "未安装；读取文件仍可用，剪贴板和自动压缩不可用", required=False)

    if isinstance(cfg, dict) and not config_errors(cfg):
        try:
            req = urllib.request.Request(cfg["base_url"].rstrip("/"), headers=build_headers(cfg))
            with urllib.request.urlopen(req, timeout=10) as response:
                add("endpoint", "ok", f"网络可达（HTTP {response.status}），未验证视觉接口")
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                add("endpoint", "error", f"服务端错误（HTTP {e.code}）")
            else:
                add("endpoint", "warning", f"服务已响应 HTTP {e.code}，仅确认网络可达，未验证视觉接口", required=False)
        except (urllib.error.URLError, TimeoutError) as e:
            add("endpoint", "error", f"无法连接: {e}")
    else:
        add("endpoint", "warning", "配置无效，跳过接口检查")

    ok = not any(check["status"] == "error" and check["required"] for check in checks)
    if json_output:
        print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False))
    else:
        for check in checks:
            label = {"ok": "OK", "warning": "WARN", "error": "ERROR"}[check["status"]]
            print(f"[{label}] {check['name']}: {check['detail']}")
    return 0 if ok else 1


class SeeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, json_requested=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.json_requested = json_requested

    def error(self, message):
        if self.json_requested:
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
            raise SystemExit(2)
        super().error(message)


def build_parser(json_requested=None):
    if json_requested is None:
        json_requested = "--json" in sys.argv[1:]
    parser = SeeArgumentParser(
        description="读取图片并调用 OpenAI 兼容视觉模型识别。",
        json_requested=json_requested,
    )
    parser.add_argument("paths", nargs="*", help="一张或多张图片路径，也可传入文件夹")
    parser.add_argument("--clip", action="store_true", help="读取剪贴板截图")
    parser.add_argument("--dir", metavar="FOLDER", help="读取文件夹中的直接子项图片")
    parser.add_argument("--setup", action="store_true", help="生成配置文件")
    parser.add_argument("--global", dest="global_config", action="store_true", help="将配置写入用户目录")
    parser.add_argument("--doctor", action="store_true", help="检查 Python、配置、依赖和接口连通性")
    parser.add_argument("--prompt", help="覆盖配置中的识别提示词")
    parser.add_argument("--detail", choices=("auto", "low", "high"), help="设置图片细节级别")
    parser.add_argument("--json", dest="json_output", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--retries", type=int, default=3, help="网络重试次数，默认 3")
    return parser


def main():
    raw_args = sys.argv[1:]
    parser = build_parser(json_requested="--json" in raw_args)
    if raw_args == ["help"]:
        parser.print_help()
        return 0
    args = parser.parse_args(raw_args)

    if args.retries <= 0:
        parser.error("--retries 必须是正整数")
    if args.global_config and not args.setup:
        parser.error("--global 只能和 --setup 一起使用")
    if args.setup:
        if args.paths or args.clip or args.dir or args.doctor or args.prompt or args.detail:
            parser.error("--setup 不能和图片识别参数一起使用")
        return setup_config(args.global_config, args.json_output)
    if args.doctor:
        if args.paths or args.clip or args.dir or args.prompt or args.detail:
            parser.error("--doctor 不能和图片识别参数一起使用")
        return run_doctor(args.json_output)
    if args.clip and (args.paths or args.dir):
        parser.error("--clip 不能和图片路径或 --dir 一起使用")
    if args.dir and args.paths:
        parser.error("--dir 不能和普通图片路径一起使用")

    try:
        cfg, cfg_path = load_config()
        check_config(cfg, cfg_path)
    except SeeError as e:
        if args.json_output:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"错误: {e}", file=sys.stderr)
        return 1

    if args.clip:
        path = grab_clipboard_image(verbose=not args.json_output)
        if not path:
            message = "剪贴板里没有图片，请先截图并复制。"
            if args.json_output:
                print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
            else:
                print(message, file=sys.stderr)
            return 1
        return process_clipboard_image(cfg, path, args.json_output, args.prompt, args.detail, args.retries)

    try:
        if args.dir:
            paths = list_images(args.dir)
            if not paths:
                raise SeeError(f"文件夹里没找到图片: {args.dir}")
        elif args.paths:
            paths = []
            for path in args.paths:
                paths.extend(list_images(path) if os.path.isdir(path) else [path])
            paths = sorted(set(paths))
            if not paths:
                raise SeeError("没有找到图片。")
        else:
            path = grab_clipboard_image(verbose=not args.json_output)
            if path:
                return process_clipboard_image(cfg, path, args.json_output, args.prompt, args.detail, args.retries)
            if args.json_output:
                print(json.dumps({"ok": False, "error": "剪贴板里没有图片"}, ensure_ascii=False))
                return 1
            parser.print_help()
            return 0
    except SeeError as e:
        if args.json_output:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"错误: {e}", file=sys.stderr)
        return 1

    return print_description(cfg, paths, args.json_output, args.prompt, args.detail, args.retries)

if __name__ == "__main__":
    sys.exit(main())
