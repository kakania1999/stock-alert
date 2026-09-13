#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宗申动力(001696) 股价跌到目标价 -> 微信提醒

设计要点
--------
1. 多源兜底取价：腾讯 -> 新浪 -> 东财，任一可用即可。GitHub 的服务器在海外，
   国内行情接口不一定都通，所以不能只依赖一个源。
2. 非交易日自动跳过：拿到的行情日期不是"今天"就说明今天没开市，直接退出，
   避免节假日拿着上周的收盘价反复报警。
3. 状态机去重：跌破触发线只提醒一次；价格回到"复位线"以上后，才允许下次再提醒。
   状态存 state.json，由 workflow 提交回仓库，这样跨次运行能记住。
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ============ 你可以改这里 ============
CODE = "001696"          # 股票代码（不带市场前缀）
NAME = "宗申动力"
TRIGGER = 14.55          # 触发线：现价 <= 这个数就提醒
RESET = 14.80            # 复位线：价格回到这个数以上，下次跌破才会再提醒
# =====================================

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def log(msg):
    print(msg, flush=True)


def http_get(url, referer=None, enc="utf-8", tries=3, timeout=20):
    """带重试的 GET。海外访问国内接口偶发超时，退避重试几次。"""
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    last_err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode(enc, "ignore")
        except Exception as exc:                      # noqa: BLE001
            last_err = exc
            if i < tries - 1:
                time.sleep(0.8 * (i + 1))
    log("[warn] 请求失败 %s -> %s" % (url, last_err))
    return ""


def quote_tencent():
    """腾讯行情。字段用 ~ 分隔，GBK 编码。[3]现价 [4]昨收 [30]时间 [33]最高 [34]最低"""
    text = http_get("https://qt.gtimg.cn/q=sz%s" % CODE, enc="gbk")
    if not text or '"' not in text:
        return None
    parts = text.split('"')[1].split("~")
    if len(parts) < 35 or not parts[3]:
        return None
    digits = "".join(c for c in parts[30] if c.isdigit())   # 形如 20260913153000
    return {
        "price": float(parts[3]),
        "prev": float(parts[4]) if parts[4] else None,
        "date": digits[:8],                                  # YYYYMMDD
        "clock": digits[8:14],
        "high": float(parts[33]) if parts[33] else None,
        "low": float(parts[34]) if parts[34] else None,
        "src": "腾讯",
    }


def quote_sina():
    """新浪行情。需要 Referer，GBK 编码。[3]现价 [2]昨收 [30]日期 [31]时间"""
    text = http_get("https://hq.sinajs.cn/list=sz%s" % CODE,
                    referer="https://finance.sina.com.cn", enc="gbk")
    if not text or '"' not in text:
        return None
    parts = text.split('"')[1].split(",")
    if len(parts) < 32 or not parts[3] or float(parts[3]) == 0:
        return None
    return {
        "price": float(parts[3]),
        "prev": float(parts[2]) if parts[2] else None,
        "date": parts[30].replace("-", ""),
        "clock": parts[31].replace(":", ""),
        "high": float(parts[4]) if parts[4] else None,
        "low": float(parts[5]) if parts[5] else None,
        "src": "新浪",
    }


def quote_eastmoney():
    """东财延时行情（push2 实时域名有风控，用 push2delay）。"""
    url = ("https://push2delay.eastmoney.com/api/qt/stock/get"
           "?secid=0.%s&fields=f43,f44,f45,f46,f60,f86&fltt=2&invt=2" % CODE)
    text = http_get(url)
    if not text:
        return None
    try:
        data = json.loads(text).get("data") or {}
    except Exception:                                    # noqa: BLE001
        return None
    if not data.get("f43"):
        return None
    ts = data.get("f86")                                  # 东财返回的是时间戳(秒)
    stamp = datetime.fromtimestamp(ts, CST) if ts else datetime.now(CST)
    return {
        "price": float(data["f43"]),
        "prev": float(data["f60"]) if data.get("f60") else None,
        "date": stamp.strftime("%Y%m%d"),
        "clock": stamp.strftime("%H%M%S"),
        "high": data.get("f44"),
        "low": data.get("f45"),
        "src": "东财",
    }


def get_quote():
    """依次尝试三个数据源，返回第一个成功的。"""
    for fn in (quote_tencent, quote_sina, quote_eastmoney):
        try:
            q = fn()
        except Exception as exc:                          # noqa: BLE001
            log("[warn] %s 解析异常: %s" % (fn.__name__, exc))
            continue
        if q and q.get("price"):
            return q
        log("[info] %s 无数据，换下一个源" % fn.__name__)
    return None


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:                                 # noqa: BLE001
            log("[warn] state.json 读取失败，按全新状态处理")
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


def push_wxpusher(spt, title, html):
    """WxPusher 极简推送（SPT）。永久免费、无需注册、无需实名。
    接口：POST /api/send/message/simple-push，contentType=2 表示 HTML。"""
    payload = json.dumps({
        "spt": spt,
        "content": html,
        "summary": title,
        "contentType": 2,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://wxpusher.zjiecode.com/api/send/message/simple-push",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "ignore")


def push_serverchan(send_key, title, markdown):
    """Server酱。免费版每天 5 条，够用（本任务触发后一天最多 1 条）。"""
    url = "https://sctapi.ftqq.com/%s.send" % send_key
    payload = urllib.parse.urlencode({"title": title, "desp": markdown}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "ignore")


def push_pushplus(token, title, html):
    """pushplus。2026-09 起免费用户要付费实名，保留作为可选渠道。"""
    payload = json.dumps({
        "token": token,
        "title": title,
        "content": html,
        "template": "html",
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "ignore")


def send_notification(title, quote, now):
    """按环境变量自动选渠道，返回 (渠道名, 服务端响应)。用户用哪个就配哪个。

    优先级：Server酱 > pushplus > WxPusher。
    原因（2026-09 实测）：WxPusher 的 SPT 极简推送只投递到它自家的客户端 App，
    微信渠道已降级为补充通道，没装 App 时消息会永久卡在「等待发送」。
    而 Server酱 关注「方糖服务号」即可在微信直接收到，无需装任何 App。
    """
    key = (os.environ.get("SERVERCHAN_KEY") or "").strip()
    if key:
        return "Server酱", push_serverchan(key, title, build_markdown(quote, now))
    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    if token:
        return "pushplus", push_pushplus(token, title, build_message(quote, now))
    spt = (os.environ.get("WXPUSHER_SPT") or "").strip()
    if spt:
        return "WxPusher", push_wxpusher(spt, title, build_message(quote, now))
    raise RuntimeError("三个渠道的凭据都没配（SERVERCHAN_KEY / PUSHPLUS_TOKEN / WXPUSHER_SPT）")


def push_succeeded(channel, resp):
    """各家成功码不同，统一判断，避免出现"HTTP 通了但消息没真发出去"的静默失败。"""
    try:
        data = json.loads(resp)
    except Exception:                                     # noqa: BLE001
        return False
    if channel == "WxPusher":
        return data.get("code") == 1000
    if channel == "Server酱":
        return data.get("code") == 0
    return data.get("code") == 200


def build_markdown(quote, now):
    """Server酱 用的 Markdown 版消息。"""
    price = quote["price"]
    prev = quote.get("prev")
    if prev:
        chg = price - prev
        chg_text = "%s%.2f 元 (%.2f%%)" % ("跌 " if chg < 0 else "涨 ", abs(chg),
                                          abs(chg / prev * 100))
    else:
        chg_text = "—"
    return (
        "## %s(%s) 现价 %.2f 元\n\n"
        "- 涨跌：%s\n"
        "- 触发线：≤ %.2f 元\n"
        "- 今日区间：%s ~ %s\n"
        "- 数据源：%s\n"
        "- 检查时间：%s\n\n"
        "> 价格回到 %.2f 元以上后，再次跌破才会重新提醒。"
    ) % (
        NAME, CODE, price, chg_text, TRIGGER,
        ("%.2f" % quote["low"]) if quote.get("low") else "—",
        ("%.2f" % quote["high"]) if quote.get("high") else "—",
        quote["src"], now.strftime("%Y-%m-%d %H:%M:%S"), RESET,
    )


def build_message(quote, now):
    price = quote["price"]
    prev = quote.get("prev")
    if prev:
        chg = price - prev
        pct = chg / prev * 100
        arrow = "▼" if chg < 0 else ("▲" if chg > 0 else "—")
        color = "#1aad19" if chg < 0 else "#e64545"     # A股惯例：绿跌红涨
        chg_text = "%s %.2f (%.2f%%)" % (arrow, abs(chg), pct)
    else:
        color, chg_text = "#666", "—"

    return """
<div style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;line-height:1.7">
  <div style="font-size:16px;font-weight:600;margin-bottom:8px">🔔 {name} 触及目标价</div>
  <div style="font-size:32px;font-weight:700;color:{color};margin:6px 0">{price:.2f}
    <span style="font-size:15px;font-weight:400">元</span></div>
  <div style="font-size:14px;color:{color};margin-bottom:12px">{chg}</div>
  <table style="font-size:13px;color:#555;border-collapse:collapse">
    <tr><td style="padding:2px 10px 2px 0">触发线</td><td>≤ {trigger:.2f}</td></tr>
    <tr><td style="padding:2px 10px 2px 0">今日区间</td><td>{low} ~ {high}</td></tr>
    <tr><td style="padding:2px 10px 2px 0">数据源</td><td>{src}</td></tr>
    <tr><td style="padding:2px 10px 2px 0">检查时间</td><td>{ts}</td></tr>
  </table>
  <div style="font-size:12px;color:#999;margin-top:12px">
    价格回到 {reset:.2f} 以上后，再次跌破才会重新提醒。
  </div>
</div>
""".format(
        name=NAME, price=price, color=color, chg=chg_text,
        trigger=TRIGGER,
        low=("%.2f" % quote["low"]) if quote.get("low") else "—",
        high=("%.2f" % quote["high"]) if quote.get("high") else "—",
        src=quote["src"], ts=now.strftime("%Y-%m-%d %H:%M:%S"), reset=RESET,
    )


def main():
    if not any((os.environ.get(k) or "").strip()
               for k in ("WXPUSHER_SPT", "SERVERCHAN_KEY", "PUSHPLUS_TOKEN")):
        log("[error] 未配置任何推送凭据")
        log("        需要设置 WXPUSHER_SPT / SERVERCHAN_KEY / PUSHPLUS_TOKEN 其中之一")
        return 1

    now = datetime.now(CST)
    today = now.strftime("%Y%m%d")

    quote = get_quote()
    if not quote:
        log("[error] 三个行情源都取不到数据")
        return 1

    log("[info] %s(%s) 现价 %.2f | 昨收 %s | 行情日期 %s | 源 %s"
        % (NAME, CODE, quote["price"], quote.get("prev"), quote.get("date"), quote["src"]))

    # 非交易日：行情日期不是今天 -> 今天没开市，不判断也不推送
    if quote["date"] != today:
        log("[info] 行情日期 %s != 今天 %s，今日休市，跳过" % (quote["date"], today))
        return 0

    state = load_state()
    alerted = bool(state.get("alerted"))
    price = quote["price"]

    if price <= TRIGGER and not alerted:
        log("[action] 现价 %.2f <= %.2f，触发提醒" % (price, TRIGGER))
        prev = quote.get("prev")
        # Server酱 免费版的卡片只显示标题，正文要展开才看得到。
        # 所以把最关键的信息（现价 + 涨跌幅）压进标题，保证一眼可见。
        if prev:
            title = "%s 跌到 %.2f 元 (%.2f%%)" % (NAME, price, (price - prev) / prev * 100)
        else:
            title = "%s 跌到 %.2f 元" % (NAME, price)
        try:
            channel, resp = send_notification(title, quote, now)
        except Exception as exc:                          # noqa: BLE001
            log("[error] 推送请求异常: %s" % exc)
            return 1
        log("[push] 渠道=%s 响应=%s" % (channel, resp))
        if not push_succeeded(channel, resp):
            log("[error] %s 返回失败，不标记已提醒，下次运行会自动重试" % channel)
            return 1
        alerted = True

    elif price >= RESET and alerted:
        log("[action] 现价 %.2f >= %.2f，复位，等待下次跌破" % (price, RESET))
        alerted = False

    else:
        log("[info] 现价 %.2f，未触发（触发线 %.2f / 复位线 %.2f / 当前已提醒=%s）"
            % (price, TRIGGER, RESET, alerted))

    # 只在关键时刻写盘：状态翻转、首次运行、或每月保活。
    # 否则每次都改 last_check，会让仓库每天多出 6 条无意义的提交记录。
    #
    # 保活的原因：GitHub 会在仓库连续 60 天没有提交后，自动停掉定时任务。
    # 如果股价一直没跌到目标价，就不会有提交，监控会被静默关掉——
    # 等到真跌破那天反而收不到消息。所以每月强制写一次。
    heartbeat_month = now.strftime("%Y-%m")
    need_write = (
        alerted != bool(state.get("alerted"))
        or not os.path.exists(STATE_FILE)
        or state.get("heartbeat_month") != heartbeat_month
    )
    if need_write:
        save_state({
            "alerted": alerted,
            "last_price": price,
            "last_check": now.strftime("%Y-%m-%d %H:%M:%S"),
            "heartbeat_month": heartbeat_month,
        })
        log("[state] 状态已更新: alerted=%s -> 已写入 state.json" % alerted)
    else:
        log("[state] 状态无变化，不写文件")

    return 0


def run_test_push():
    """发一条测试推送，验证接收链路是否真的通。
    不连行情源、不写状态文件，可以随时重复执行。"""
    now = datetime.now(CST)
    quote = {"price": 14.45, "prev": 15.24, "low": 14.35, "high": 15.20, "src": "链路测试"}
    title = "【测试】%s 监控已就绪" % NAME
    try:
        channel, resp = send_notification(title, quote, now)
    except Exception as exc:                              # noqa: BLE001
        log("[error] 推送请求异常: %s" % exc)
        return 1
    log("[test] 使用渠道: %s" % channel)
    log("[test] 服务端响应: %s" % resp)
    ok = push_succeeded(channel, resp)
    log("[test] 判定: %s" % ("成功，请查看手机" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(run_test_push())
    sys.exit(main())
