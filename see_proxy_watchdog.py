"""
see_proxy 看门狗 (watchdog)
==========================
每 60 秒检查一次识图代理(8777端口)是否活着：
  - 活着：什么都不做
  - 死了：自动用 pythonw 无窗口拉起 see_proxy.py

开机自启由 see-proxy-watchdog.vbs 负责，随系统启动。
"""
import socket
import subprocess
import sys
import os
import time

PORT = 8777

# 相对脚本自身定位，便于分发到任何机器
_HERE = os.path.dirname(os.path.abspath(__file__))
PROXY_SCRIPT = os.path.join(_HERE, "see_proxy.py")
LOG_PATH = os.path.join(_HERE, "see_proxy_watchdog.log")

# 无窗口运行 python 的可执行文件：优先同目录 pythonw，其次当前解释器
PYTHONW = os.path.join(_HERE, "pythonw.exe")
if not os.path.exists(PYTHONW):
    # 回退到当前解释器
    PYTHONW = sys.executable


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[watchdog][{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def port_alive(port=PORT):
    """尝试连一下端口，能连上说明代理活着。"""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.close()
        return True
    except Exception:
        return False


def start_proxy():
    log("代理未运行，正在拉起 see_proxy.py ...")
    try:
        subprocess.Popen(
            [PYTHONW, PROXY_SCRIPT],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log("已发出启动命令")
    except Exception as e:
        log(f"启动失败: {e}")


def main():
    log("看门狗启动，监控端口 " + str(PORT))
    # 首次检查前等 30 秒，给开机自启的代理留足启动时间，避免误判
    time.sleep(30)
    while True:
        try:
            if not port_alive():
                start_proxy()
        except Exception as e:
            log(f"检查异常: {e}")
        time.sleep(60)


if __name__ == "__main__":
    main()
