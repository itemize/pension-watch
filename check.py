"""네이버 예약 펜션 취소분 감시 → 텔레그램 알림.

사용법:
  python check.py           # 확인 후, 새로 열린 날짜만 알림
  python check.py --test    # 현재 상태를 텔레그램으로 전송 (연결 확인용)
  python check.py --dry     # 텔레그램 없이 콘솔 출력만
  python check.py --demo 2026-11-03,2026-11-07
                            # 지정 날짜로 🎉/🔒 알림을 실제 전송 (감시 상태는 그대로)

환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
KST = timezone(timedelta(hours=9))
GRAPHQL_URL = "https://m.booking.naver.com/graphql"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
ERROR_ALERT_THRESHOLD = 3  # 연속 실패가 이 횟수에 도달하면 한 번 알림

BIZ_ITEMS_QUERY = """
query bizItems($input: BizItemsParams) {
  bizItems(input: $input) { bizItemId name isClosedBooking }
}"""

SCHEDULE_QUERY = """
query schedule($scheduleParams: ScheduleParams) {
  schedule(input: $scheduleParams) { bizItemSchedule { daily { date } } }
}"""


def graphql(op, query, variables):
    body = json.dumps({"operationName": op, "variables": variables, "query": query}).encode()
    req = urllib.request.Request(
        f"{GRAPHQL_URL}?opName={op}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Referer": "https://m.booking.naver.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        data = json.load(res)
    if data.get("errors"):
        raise RuntimeError(f"{op} 오류: {data['errors']}")
    return data["data"]


def fetch_items(cfg):
    items = graphql("bizItems", BIZ_ITEMS_QUERY,
                    {"input": {"businessId": cfg["businessId"], "lang": "ko"}})["bizItems"]
    wanted = cfg["bizItemIds"]
    if wanted != "all":
        items = [i for i in items if i["bizItemId"] in wanted]
    return items


def fetch_daily(cfg, biz_item_id, start, end):
    params = {
        "businessId": cfg["businessId"],
        "businessTypeId": cfg["businessTypeId"],
        "bizItemId": biz_item_id,
        "startDateTime": f"{start}T00:00:00",
        "endDateTime": f"{end}T23:59:59",
    }
    data = graphql("schedule", SCHEDULE_QUERY, {"scheduleParams": params})
    return data["schedule"]["bizItemSchedule"]["daily"]["date"] or {}


def night_available(day):
    """하루 숙박이 예약 가능한지: 영업일 + 판매일 + 남은 재고."""
    if not day:
        return False
    return bool(day.get("isBusinessDay") and day.get("isSaleDay")
                and (day.get("stock") or 0) - (day.get("bookingCount") or 0) > 0)


def booking_url(cfg, biz_item_id, checkin, checkout):
    q = urllib.parse.urlencode({"startDate": checkin, "endDate": checkout})
    return (f"https://m.booking.naver.com/booking/{cfg['businessTypeId']}/bizes/"
            f"{cfg['businessId']}/items/{biz_item_id}?{q}")


def check(cfg):
    """{key: {...}} 형태로 객실×체크인 날짜별 예약 가능 여부를 반환."""
    today = datetime.now(KST).date()
    nights = cfg.get("nights", 1)
    checkins = sorted(d for d in cfg["checkinDates"] if date.fromisoformat(d) >= today)
    if not checkins:
        return {}
    last = date.fromisoformat(checkins[-1]) + timedelta(days=nights - 1)
    result = {}
    for item in fetch_items(cfg):
        daily = fetch_daily(cfg, item["bizItemId"], checkins[0], last.isoformat())
        for ci in checkins:
            ci_date = date.fromisoformat(ci)
            stay = [(ci_date + timedelta(days=n)).isoformat() for n in range(nights)]
            co = (ci_date + timedelta(days=nights)).isoformat()
            ok = not item.get("isClosedBooking") and all(night_available(daily.get(d)) for d in stay)
            result[f"{item['bizItemId']}|{ci}"] = {
                "room": item["name"], "checkin": ci, "checkout": co, "available": ok,
                "url": booking_url(cfg, item["bizItemId"], ci, co),
            }
    return result


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없습니다.")
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "disable_web_page_preview": "true",
    }).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20) as res:
        json.load(res)


def fmt_date(iso):
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}({'월화수목금토일'[d.weekday()]})"


def notify_changes(cfg, prev, result, notify):
    """이전 상태와 비교해 열림(🎉)/다시 마감(🔒)을 알림."""
    for k, r in result.items():
        stay = f"{fmt_date(r['checkin'])} → {fmt_date(r['checkout'])} · {r['room']}"
        if r["available"] and not prev.get(k):
            notify(f"🎉 [{cfg['name']}] 예약 가능!\n{stay}\n{r['url']}")
        elif not r["available"] and prev.get(k):
            notify(f"🔒 [{cfg['name']}] 다시 마감됐어요\n{stay}")


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"available": {}, "errors": 0}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    notify = print if mode == "--dry" else send_telegram
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    if mode == "--demo":
        # 지정한 날짜의 현재 상태와 반대였다고 가정해 실제 알림을 보냄 (state.json은 건드리지 않음)
        if len(sys.argv) < 3 or not sys.argv[2].strip():
            sys.exit("사용법: python check.py --demo 2026-11-03,2026-11-07")
        cfg["checkinDates"] = [d.strip() for d in sys.argv[2].split(",") if d.strip()]
        cfg["name"] = f"테스트·{cfg['name']}"
        result = check(cfg)
        notify_changes(cfg, {k: not r["available"] for k, r in result.items()}, result, send_telegram)
        return

    state = load_state()

    try:
        result = check(cfg)
    except Exception as e:  # 네트워크/API 변경 등
        state["errors"] = state.get("errors", 0) + 1
        print(f"확인 실패 ({state['errors']}회 연속): {e}", file=sys.stderr)
        if state["errors"] == ERROR_ALERT_THRESHOLD:
            notify(f"⚠️ [{cfg['name']}] 예약 확인이 {ERROR_ALERT_THRESHOLD}회 연속 실패했습니다.\n{e}")
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        sys.exit(1)

    if state.get("errors", 0) >= ERROR_ALERT_THRESHOLD:
        notify(f"✅ [{cfg['name']}] 예약 확인이 다시 정상 동작합니다.")
    state["errors"] = 0

    for r in result.values():
        print(f"{r['checkin']} {r['room']}: {'가능' if r['available'] else '마감'}")

    if mode in ("--test", "--dry"):
        lines = [f"🔎 [{cfg['name']}] 현재 상태"]
        lines += [f"{'🟢' if r['available'] else '⚪'} {fmt_date(r['checkin'])} {r['room']}"
                  for r in result.values()]
        if not result:
            lines.append("감시할 날짜가 남아있지 않습니다.")
        notify("\n".join(lines))

    if mode == "--test":
        return  # 상태를 저장하지 않아야 다음 정기 실행에서 알림이 빠지지 않음

    notify_changes(cfg, state.get("available", {}), result, notify)

    state["available"] = {k: r["available"] for k, r in result.items()}
    state["checkedAt"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
