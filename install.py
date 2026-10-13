#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
see-vision 一键安装脚本
=======================
把"DeepSeek 识图方案"装到本机 ZCode：
  1. 探测环境（python、~/.zcode/skills 目录）
  2. 把包内容复制到 ~/.zcode/skills/see-vision/
  3. 引导填写 智谱 key（免费识图） 和 DeepSeek key
  4. 自动改 ZCode 配置：DeepSeek baseURL 指向本地代理 + 支持图片
  5. 设置开机自启（看门狗 vbs）
  6. 启动代理 + 验证 8777 端口

用法：双击 安装.bat 或运行 python install.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PORT = 8777
_HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_NAME = "see-vision"
ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_MODEL = "glm-4v-flash"          # 智谱免费识图模型
DEEPSEEK_BASE = "https://api.deepseek.com"


def cprint(msg, color=None):
    """彩色打印。"""
    c = {"green": "32", "red": "31", "yellow": "33", "cyan": "36", "bold": "1"}.get(color, "")
    if c:
        print(f"\033[{c}m{msg}\033[0m", flush=True)
    else:
        print(msg, flush=True)


def find_pythonw():
    """找一个可用的 pythonw.exe（无窗口运行）。"""
    # 1. 当前解释器同目录的 pythonw
    cur = os.path.dirname(sys.executable)
    cand = os.path.join(cur, "pythonw.exe")
    if os.path.exists(cand):
        return cand
    # 2. 常见 uv 安装路径
    for base in (os.path.expanduser("~"),):
        uv = os.path.join(base, "AppData", "Roaming", "uv", "python")
        if os.path.isdir(uv):
            for d in sorted(os.listdir(uv), reverse=True):
                p = os.path.join(uv, d, "pythonw.exe")
                if os.path.exists(p):
                    return p
    # 3. PATH 里的 pythonw
    for p in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(p, "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return sys.executable  # 兜底


def find_skills_dir():
    """找 ZCode 用户级 skills 目录：~/.zcode/skills。"""
    home = os.path.expanduser("~")
    cand = os.path.join(home, ".zcode", "skills")
    if os.path.isdir(cand):
        return cand
    os.makedirs(cand, exist_ok=True)
    return cand


def find_zcode_config():
    """
    找 ZCode 真正生效的 config.json。
    优先：读 ~/.zcode/v2/setting.json 的 dataBaseDir → 拼 config.json
    兜底：扫常见位置。
    """
    home = os.path.expanduser("~")
    # 方式1：setting.json 里 dataBaseDir
    for setting_path in (os.path.join(home, ".zcode", "v2", "setting.json"),):
        if os.path.isfile(setting_path):
            try:
                with open(setting_path, encoding="utf-8") as f:
                    st = json.load(f)
                db = st.get("dataBaseDir")
                if db:
                    cand = os.path.join(db, ".zcode", "v2", "config.json")
                    if os.path.isfile(cand):
                        return cand
                    cand2 = os.path.join(db, "config.json")
                    if os.path.isfile(cand2):
                        return cand2
            except Exception:
                pass
    # 方式2：常见位置扫一遍
    for cand in (
        os.path.join(home, ".zcode", "v2", "config.json"),
        os.path.join(home, ".zcode", "config.json"),
    ):
        if os.path.isfile(cand):
            return cand
    # 方式3：全盘找 v2/config.json（限定常见盘符）
    for drive in ("D:", "E:", "C:"):
        p = os.path.join(drive, "zcodebaocun", ".zcode", "v2", "config.json")
        if os.path.isfile(p):
            return p
    return None


def patch_zcode_config(config_path, proxy_url):
    """
    改 ZCode config.json：
      - deepseek provider 的 baseURL → proxy_url
      - deepseek-v4-flash 的 modalities.input 加 "image"
      - reasoning.defaultVariant → "high"
    改前备份。返回 True/False。
    """
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        cprint(f"  读取配置失败: {e}", "red")
        return False

    prov = cfg.get("provider", {})
    ds = prov.get("deepseek")
    if not isinstance(ds, dict):
        # 兼容 provider 直接叫 deepseek 或在别处的结构
        ds = None
        for k, v in prov.items():
            if isinstance(v, dict) and (v.get("name") == "DeepSeek" or "deepseek" in v.get("models", {})):
                ds = v
                break
    if not isinstance(ds, dict):
        cprint("  未找到 DeepSeek provider，跳过配置修改（可稍后手动改）", "yellow")
        return False

    # baseURL
    opts = ds.setdefault("options", {})
    opts["baseURL"] = proxy_url

    # 所有 deepseek 模型：支持图片 + 推理 high
    models = ds.get("models", {})
    patched = 0
    for mname, m in models.items():
        if not isinstance(m, dict):
            continue
        mod = m.setdefault("modalities", {})
        inp = mod.setdefault("input", [])
        if "image" not in inp:
            inp.append("image")
        if "text" not in inp:
            inp.insert(0, "text")
        r = m.setdefault("reasoning", {})
        if isinstance(r, dict) and "defaultVariant" in r:
            r["defaultVariant"] = "high"
        patched += 1

    # 备份 + 写入
    bak = config_path + ".bak-see-vision-" + time.strftime("%Y%m%d-%H%M%S")
    try:
        shutil.copy2(config_path, bak)
    except Exception:
        pass
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        cprint(f"  写入配置失败: {e}", "red")
        return False
    cprint(f"  已修改 {patched} 个 deepseek 模型：baseURL→代理、支持图片、推理high", "green")
    cprint(f"  备份在: {os.path.basename(bak)}", "gray" if hasattr(cprint, "__self__") else "cyan")
    return True


def make_startup_vbs(pythonw, script_dir):
    """在 Windows 启动目录生成看门狗 vbs。返回 vbs 路径。"""
    startup = os.path.join(os.environ.get("APPDATA", ""),
                           "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    watchdog = os.path.join(script_dir, "see_proxy_watchdog.py")
    vbs_path = os.path.join(startup, "see-vision-watchdog.vbs")
    vbs = (
        f'set shell = CreateObject("WScript.Shell")\n'
        f'shell.Run """{pythonw}"" ""{watchdog}""", 0, False\n'
    )
    try:
        with open(vbs_path, "w", encoding="utf-8") as f:
            f.write(vbs)
        return vbs_path
    except Exception as e:
        cprint(f"  写开机自启失败: {e}", "red")
        return None


def start_proxy(script_dir, pythonw):
    """后台拉起 see_proxy.py。"""
    proxy = os.path.join(script_dir, "see_proxy.py")
    try:
        subprocess.Popen(
            [pythonw, proxy],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        cprint(f"  启动代理失败: {e}", "red")
        return False


def port_alive(port=PORT, timeout=2):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def ask_key(label, hint=""):
    """引导输入 key，去空白。"""
    print(f"  {label}")
    if hint:
        print(f"    （{hint}）")
    val = input("  请输入: ").strip()
    return val


def main():
    cprint("=" * 56, "cyan")
    cprint("  see-vision 安装器：给 DeepSeek 装上识图眼睛", "bold")
    cprint("=" * 56, "cyan")

    # 1. 探测环境
    cprint("\n[1/6] 探测环境 ...", "bold")
    skills_dir = find_skills_dir()
    cprint(f"  ZCode skills 目录: {skills_dir}")
    pythonw = find_pythonw()
    cprint(f"  Python: {sys.executable}")
    cprint(f"  pythonw: {pythonw}")

    # 2. 复制文件到 skills/see-vision
    target = os.path.join(skills_dir, SKILL_NAME)
    cprint(f"\n[2/6] 安装到 {target} ...", "bold")
    if os.path.isdir(target):
        cprint("  目标已存在，备份旧版 ...", "yellow")
        old = target + ".old-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.move(target, old)
    os.makedirs(target, exist_ok=True)
    for fname in ("SKILL.md", "see_proxy.py", "see_proxy_watchdog.py",
                  "see.py", "see_proxy_config.example.json", "config.example.json"):
        src = os.path.join(_HERE, fname)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(target, fname))
            cprint(f"  ✓ {fname}")
    # 生成 see.py 用的 config.json（占位）
    see_cfg = {
        "base_url": ZHIPU_BASE,
        "api_key": "",
        "model": ZHIPU_MODEL,
        "max_tokens": 1024,
        "max_image_dimension": 2048,
        "max_upload_mb": 5,
        "headers": {},
        "prompt": "请仔细描述这张(些)图片的内容。如果是截图，请逐条提取上面所有文字（报错信息、聊天记录、表格、界面内容等），不要总结、不要省略，用中文回答。",
    }
    with open(os.path.join(target, "config.json"), "w", encoding="utf-8") as f:
        json.dump(see_cfg, f, ensure_ascii=False, indent=2)
    cprint("  ✓ config.json（识图配置占位）")

    # 3. 引导填 key
    cprint(f"\n[3/6] 填写密钥 ...", "bold")
    zhipu_key = ask_key("智谱 API key（免费识图用）", "注册 https://open.bigmodel.cn 控制台获取；免费模型 " + ZHIPU_MODEL)
    ds_key = ask_key("DeepSeek API key", "注册 https://platform.deepseek.com 获取")
    if not zhipu_key:
        cprint("  ⚠ 智谱 key 为空，识图将不可用，可之后在配置里补", "yellow")
    if not ds_key:
        cprint("  ⚠ DeepSeek key 为空，代理将无法转发，可之后在配置里补", "yellow")

    # 写入 see_proxy_config.json
    proxy_cfg = {
        "listen_port": PORT,
        "upstream_base": DEEPSEEK_BASE,
        "upstream_key": ds_key,
        "agnes_base_url": ZHIPU_BASE,
        "agnes_api_key": zhipu_key,
        "agnes_model": ZHIPU_MODEL,
        "agnes_max_tokens": 1024,
        "see_py": os.path.join(target, "see.py"),
        "see_fallback": "",
        "enable_debug": True,
        "max_image_mb": 8,
        "image_thinking_off": True,
    }
    with open(os.path.join(target, "see_proxy_config.json"), "w", encoding="utf-8") as f:
        json.dump(proxy_cfg, f, ensure_ascii=False, indent=2)
    # 写入 see.py 的 config.json
    see_cfg["api_key"] = zhipu_key
    with open(os.path.join(target, "config.json"), "w", encoding="utf-8") as f:
        json.dump(see_cfg, f, ensure_ascii=False, indent=2)
    cprint("  密钥已写入配置（本地文件，不外传）")

    # 4. 改 ZCode 配置
    cprint(f"\n[4/6] 修改 ZCode 配置（DeepSeek 指向代理）...", "bold")
    config_path = find_zcode_config()
    if config_path:
        cprint(f"  找到生效配置: {config_path}")
        proxy_url = f"http://127.0.0.1:{PORT}/v1"
        patch_zcode_config(config_path, proxy_url)
    else:
        cprint("  未找到 ZCode 生效配置，跳过。请手动把 DeepSeek baseURL 改成 "
               f"http://127.0.0.1:{PORT}/v1", "yellow")

    # 5. 开机自启
    cprint(f"\n[5/6] 设置开机自启 ...", "bold")
    vbs = make_startup_vbs(pythonw, target)
    if vbs:
        cprint(f"  ✓ 已写入: {vbs}")

    # 6. 启动代理 + 验证
    cprint(f"\n[6/6] 启动识图代理 ...", "bold")
    if port_alive(PORT):
        cprint(f"  端口 {PORT} 已在运行，跳过启动", "green")
    else:
        start_proxy(target, pythonw)
        time.sleep(3)
    if port_alive(PORT):
        cprint(f"  ✓ 代理已就绪: http://127.0.0.1:{PORT}/v1", "green")
    else:
        cprint("  ✗ 代理未能启动，请检查 Python 和密钥后重试", "red")

    cprint("\n" + "=" * 56, "cyan")
    cprint("  安装完成！", "bold")
    cprint("  1. 重启 ZCode（让它重新加载技能和配置）", "cyan")
    cprint("  2. 开新对话，发一张图 + 提问，DeepSeek 就能看图了", "cyan")
    cprint("  3. 识图用智谱免费模型 " + ZHIPU_MODEL + "，永久免费", "cyan")
    cprint("=" * 56, "cyan")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(1)
