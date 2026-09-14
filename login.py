# -*- coding: utf-8 -*-
"""
雀魂（Majsoul）每日自动登录 —— GitHub Actions + Selenium

适配 2026-05 起上线的 Unity WebGL 客户端。
旧版客户端是 LayaAir 引擎，登录框是真实 DOM（#layaCanvas / input[name=input]），
可以直接 find_element 操作；现在的客户端整屏只有一块 <canvas id="unity-canvas">，
所有 UI 都由 Unity 绘制，DOM 里没有任何输入框，因此必须：
  1) 用固定的相对比例在 canvas 上做鼠标点击；
  2) 用键盘事件把账号密码送进 Unity；
  3) 用「截图像素」判断界面到底渲染到哪一步了。

用法（与旧版保持一致，workflow 无需改动）：
    python login.py <email1> <email2> ... <passwd1> <passwd2> ...

可选环境变量：
    MAJ_URL            游戏地址，默认 https://game.maj-soul.net/1/
    MAJ_FORM_TIMEOUT   等待登录界面出现的最长秒数，默认 600
    MAJ_LOGIN_TIMEOUT  点击登录后等待离开登录页的最长秒数，默认 150
    MAJ_ATTEMPTS       提交登录的最大重试次数，默认 3
    MAJ_LOBBY_SETTLE   登录成功后停留秒数（等大厅加载完），默认 45
    MAJ_CLAIM_CLICKS   登录后在画面中央点几下收掉每日奖励弹窗，默认 3，设 0 关闭
    MAJ_DEBUG          设为 0 可关闭逐步调试截图，默认开启

log.txt 格式（在旧版三字段基础上，失败时追加原因）：
    2026-09-12 21:17:30-Success-183.42s
    2026-09-12 21:20:55-Failed-486.10s-等待登录界面超时（600s）
"""

import os
import sys
import time
from datetime import datetime
from io import BytesIO

from PIL import Image

from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

GAME_URL = os.environ.get("MAJ_URL", "https://game.maj-soul.net/1/")

# ---- 相对 canvas 尺寸的点击位置（实测于 Unity WebGL 客户端 v0.16.275.W.4.0.46）----
# 数值 = (相对 canvas 中心的横向比例, 相对 canvas 中心的纵向比例)
EMAIL_RATIO = (0.24182, -0.21206)
PASSWD_RATIO = (0.24182, -0.10426)
LOGIN_RATIO = (0.24102, 0.08865)

# ---- 登录按钮检测窗口（金色「進入遊戲」按钮，按相对比例划定的紧包围盒）----
# 用「金色像素占比」而不是「有没有金色像素」来判断，才能把登录界面和
# 加载界面区分开：加载界面中央那坨金色「雀魂」logo 落在这个窗口里是 0 像素。
BTN_HALF_W = 0.105
BTN_HALF_H = 0.045
BTN_READY_FILL = 0.30  # 金色占比 >= 30% → 登录界面已渲染完成（实测登录页 70%，加载页 0%）
BTN_GONE_FILL = 0.15   # 金色占比 < 15%  → 已经离开登录界面

# 敲完键后输入框区域至少要有 2% 的像素变化，才算“键盘输入确实被 Unity 收到了”
MIN_INPUT_CHANGE = 0.02

FORM_TIMEOUT = int(os.environ.get("MAJ_FORM_TIMEOUT", "600"))
LOGIN_TIMEOUT = int(os.environ.get("MAJ_LOGIN_TIMEOUT", "150"))
MAX_ATTEMPTS = int(os.environ.get("MAJ_ATTEMPTS", "3"))
LOBBY_SETTLE = int(os.environ.get("MAJ_LOBBY_SETTLE", "45"))
CLAIM_CLICKS = int(os.environ.get("MAJ_CLAIM_CLICKS", "3"))
DEBUG = os.environ.get("MAJ_DEBUG", "1") != "0"

_here = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(_here, "debug")


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------- #
# 截图 / 像素分析
# --------------------------------------------------------------------------- #
def shot(driver, name, always=False):
    """截图。always=True 时即使关闭了 DEBUG 也会存盘（用于失败取证）。"""
    png = driver.get_screenshot_as_png()
    if DEBUG or always:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, name + ".png"), "wb") as f:
            f.write(png)
    return png


def canvas_geo(driver):
    """canvas 在视口中的位置尺寸 + 视口宽度（用于换算截图坐标系）。"""
    g = driver.execute_script(
        """
        var c = document.querySelector('canvas');
        if (!c) return null;
        var r = c.getBoundingClientRect();
        return {l: r.left, t: r.top, w: r.width, h: r.height,
                cx: r.left + r.width / 2, cy: r.top + r.height / 2,
                iw: window.innerWidth, ih: window.innerHeight};
        """
    )
    if not g or not g["w"] or not g["h"]:
        return None
    return g


def _is_gold(r, g, b):
    # 「進入遊戲」按钮的金色：亮、暖、蓝通道低
    return r > 150 and g > 115 and b < 140 and (r - b) > 60 and (g - b) > 25


def _open(png):
    return Image.open(BytesIO(png)).convert("RGB")


def _scale_of(im, geo):
    return im.width / float(geo["iw"] or im.width)


def button_fill(png, geo):
    """登录按钮紧包围盒里金色像素的占比，0.0 ~ 1.0。"""
    im = _open(png)
    s = _scale_of(im, geo)
    bx = (geo["cx"] + LOGIN_RATIO[0] * geo["w"]) * s
    by = (geo["cy"] + LOGIN_RATIO[1] * geo["h"]) * s
    hw = BTN_HALF_W * geo["w"] * s
    hh = BTN_HALF_H * geo["h"] * s
    x0, y0 = max(0, int(bx - hw)), max(0, int(by - hh))
    x1, y1 = min(im.width, int(bx + hw)), min(im.height, int(by + hh))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    px = im.load()
    total = hit = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            total += 1
            if _is_gold(*px[x, y]):
                hit += 1
    return hit / float(total) if total else 0.0


def region_changed(png_a, png_b, geo, ratio, half_w=0.10, half_h=0.035):
    """两次截图里某个输入框区域的变化像素占比，用于确认键盘输入真的进去了。"""
    a = _open(png_a).convert("L")
    b = _open(png_b).convert("L")
    s = _scale_of(a, geo)
    cx = (geo["cx"] + ratio[0] * geo["w"]) * s
    cy = (geo["cy"] + ratio[1] * geo["h"]) * s
    hw, hh = half_w * geo["w"] * s, half_h * geo["h"] * s
    x0, y0 = max(0, int(cx - hw)), max(0, int(cy - hh))
    x1 = min(a.width, int(cx + hw))
    y1 = min(a.height, int(cy + hh))
    pa, pb = a.load(), b.load()
    total = diff = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            total += 1
            if abs(pa[x, y] - pb[x, y]) > 40:
                diff += 1
    return diff / float(total) if total else 0.0


# --------------------------------------------------------------------------- #
# 交互
# --------------------------------------------------------------------------- #
def click_ratio(driver, canvas, geo, ratio):
    ActionChains(driver).move_to_element_with_offset(
        canvas, ratio[0] * geo["w"], ratio[1] * geo["h"]).click().perform()


def wait_for_form(driver, canvas, deadline):
    """轮询直到登录界面渲染完成，返回 canvas 几何信息（超时返回 None）。"""
    while time.time() < deadline:
        geo = canvas_geo(driver)
        if not geo:
            time.sleep(5)
            continue
        try:
            fill = button_fill(shot(driver, "wait_form"), geo)
        except Exception as exc:  # noqa: BLE001
            log("  截图像素分析失败：%s" % exc)
            fill = 0.0
        log("  等待登录界面… 按钮金色占比=%.1f%%（阈值 %.0f%%）"
            % (fill * 100, BTN_READY_FILL * 100))
        if fill >= BTN_READY_FILL:
            return geo
        time.sleep(8)
    return None


def left_login_screen(driver, geo):
    try:
        return button_fill(shot(driver, "check_left"), geo) < BTN_GONE_FILL
    except Exception:  # noqa: BLE001
        return False


def fill_field(driver, canvas, geo, ratio, text, label, tag):
    """点中输入框 → 清空 → 键入，返回输入框区域的像素变化比例。"""
    click_ratio(driver, canvas, geo, ratio)
    time.sleep(1.2)
    # 先清空，避免重试时和旧内容叠加（Unity InputField 支持 Ctrl+A）
    ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL) \
        .send_keys(Keys.DELETE).perform()
    time.sleep(1.0)
    before = shot(driver, "%s_before" % tag)
    ActionChains(driver).send_keys(text).perform()
    time.sleep(1.5)
    after = shot(driver, "%s_after" % tag)
    changed = region_changed(before, after, geo, ratio)
    log("  %s输入框像素变化=%.1f%%" % (label, changed * 100))
    return changed


def fill_until_ok(driver, canvas, geo, ratio, text, label, tag, retries=2):
    changed = 0.0
    for i in range(retries):
        changed = fill_field(driver, canvas, geo, ratio, text, label, tag)
        if changed >= MIN_INPUT_CHANGE:
            return changed
        if i + 1 < retries:
            log("  %s输入框没变化，重试填入…" % label)
            time.sleep(2)
    return changed


def submit_login(driver, canvas, geo, email, passwd):
    """填入账号密码并提交。成功直接返回，失败抛异常。"""
    canvas.click()  # 让 canvas 拿到焦点，Unity 才收得到键盘事件
    time.sleep(1.5)

    d = fill_until_ok(driver, canvas, geo, EMAIL_RATIO, email, "账号", "01_email")
    if d < MIN_INPUT_CHANGE:
        raise RuntimeError("账号未能填入输入框（像素变化 %.1f%%，点击可能落空）" % (d * 100))

    d = fill_until_ok(driver, canvas, geo, PASSWD_RATIO, passwd, "密码", "02_passwd")
    if d < MIN_INPUT_CHANGE:
        raise RuntimeError("密码未能填入输入框（像素变化 %.1f%%，点击可能落空）" % (d * 100))

    for attempt in range(1, MAX_ATTEMPTS + 1):
        click_ratio(driver, canvas, geo, LOGIN_RATIO)
        log("  已点击「進入遊戲」（第 %d/%d 次）" % (attempt, MAX_ATTEMPTS))
        hits = 0
        deadline = time.time() + LOGIN_TIMEOUT
        while time.time() < deadline:
            time.sleep(10)
            if left_login_screen(driver, geo):
                hits += 1
                if hits >= 2:  # 连续两次确认，避免被瞬时黑屏骗到
                    return
                log("  已离开登录界面，再确认一次…")
            else:
                hits = 0
                log("  仍在登录界面，继续等待…")
        if attempt < MAX_ATTEMPTS:
            log("  未进入游戏，重新点击登录按钮")
            time.sleep(3)
    raise RuntimeError("连续 %d 次提交后仍停留在登录界面（账号密码或网络问题）" % MAX_ATTEMPTS)


# --------------------------------------------------------------------------- #
# 单账号流程
# --------------------------------------------------------------------------- #
def login_one(index, email, passwd):
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # 无桌面环境靠 SwiftShader 软件渲染跑 WebGL
    options.add_argument("--enable-unsafe-swiftshader")
    options.add_argument("--use-gl=angle")
    options.add_argument("--use-angle=swiftshader")
    options.add_argument("--mute-audio")
    options.add_argument("--window-size=1280,800")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(180)
        driver.get(GAME_URL)
        canvas = WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.TAG_NAME, "canvas")))
        log("账号 %d：客户端开始联网下载资源，Unity WebGL 加载较慢，耐心等待" % index)

        geo = wait_for_form(driver, canvas, time.time() + FORM_TIMEOUT)
        if not geo:
            raise RuntimeError("等待登录界面超时（%ds）" % FORM_TIMEOUT)
        log("账号 %d：登录界面已就绪" % index)

        submit_login(driver, canvas, geo, email, passwd)
        log("账号 %d：登录成功，等待大厅加载…" % index)

        time.sleep(LOBBY_SETTLE)
        shot(driver, "05_lobby")
        # 每日奖励是「点击任意处领取」的居中弹窗；画面中央在段位场对局按钮
        # 左侧，点这里不会误触发匹配，只是把弹窗收掉。
        for _ in range(CLAIM_CLICKS):
            click_ratio(driver, canvas, geo, (0.0, 0.0))
            time.sleep(3)
        shot(driver, "06_lobby_final")
        log("账号 %d：完成" % index)
    except Exception:
        shot(driver, "fail_last", always=True)
        raise
    finally:
        driver.quit()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main():
    args = sys.argv[1:]
    count = len(args) // 2
    print("Config %d accounts" % count)
    if count == 0:
        print("用法：python login.py <email...> <passwd...>")
        return 0

    start = time.time()
    success = True
    reason = ""
    for i in range(count):
        email = args[i]
        passwd = args[count + i]
        print("-" * 28)
        try:
            login_one(i + 1, email, passwd)
        except Exception as exc:  # noqa: BLE001
            success = False
            reason = str(exc).replace("\n", " ")[:200]
            log("账号 %d 登录失败：%s" % (i + 1, exc))

    elapsed = time.time() - start
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "%s-%s-%.2fs" % (now, "Success" if success else "Failed", elapsed)
    if not success and reason:
        line += "-%s" % reason
    with open(os.path.join(os.getcwd(), "log.txt"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
