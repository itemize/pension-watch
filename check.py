"""숙소 취소분(빈 날짜) 감시 → 텔레그램 알림.

지원 대상 (config.json의 targets[].type):
  naver       네이버 예약 (m.booking.naver.com)
  kensington  켄싱턴 호텔·리조트 (kensington.co.kr 빠른예약)

사용법:
  python check.py           # 확인 후, 새로 열린 날짜만 알림
  python check.py --test    # 현재 상태를 텔레그램으로 전송 (연결 확인용)
  python check.py --dry     # 텔레그램 없이 콘솔 출력만 (다른 모드와 함께 써도 됨)
  python check.py --demo 2026-12-05,2026-12-19
                            # 지정 날짜로 🎉/🔒 알림을 실제 전송 (감시 상태는 그대로)
  python check.py --summary # 월간 예약 현황을 지금 바로 전송 (일반 실행 시엔 하루 1회 자동)

환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
ERROR_ALERT_THRESHOLD = 3  # 연속 실패가 이 횟수에 도달하면 한 번 알림

# ── 네이버 예약 ────────────────────────────────────────────────
NAVER_GRAPHQL_URL = "https://m.booking.naver.com/graphql"

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
        f"{NAVER_GRAPHQL_URL}?opName={op}",
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


def fetch_items(t):
    items = graphql("bizItems", BIZ_ITEMS_QUERY,
                    {"input": {"businessId": t["businessId"], "lang": "ko"}})["bizItems"]
    wanted = t["bizItemIds"]
    if wanted != "all":
        items = [i for i in items if i["bizItemId"] in wanted]
    return items


def fetch_daily(t, biz_item_id, start, end):
    params = {
        "businessId": t["businessId"],
        "businessTypeId": t["businessTypeId"],
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


def naver_url(t, room_id, checkin, checkout):
    q = urllib.parse.urlencode({"startDate": checkin, "endDate": checkout})
    return (f"https://m.booking.naver.com/booking/{t['businessTypeId']}/bizes/"
            f"{t['businessId']}/items/{room_id}?{q}")


def naver_check(t, checkins, nights):
    """{(객실ID, 객실명, 체크인): 가능여부}"""
    last = date.fromisoformat(checkins[-1]) + timedelta(days=nights - 1)
    out = {}
    for item in fetch_items(t):
        daily = fetch_daily(t, item["bizItemId"], checkins[0], last.isoformat())
        for ci in checkins:
            ci_date = date.fromisoformat(ci)
            stay = [(ci_date + timedelta(days=n)).isoformat() for n in range(nights)]
            ok = not item.get("isClosedBooking") and all(night_available(daily.get(d)) for d in stay)
            out[(item["bizItemId"], item["name"], ci)] = ok
    return out


def naver_open_days(t, start, end):
    """[(객실명, {예약 가능한 날짜})] — start~end 1박 기준."""
    result = []
    for item in fetch_items(t):
        daily = fetch_daily(t, item["bizItemId"], start.isoformat(), end.isoformat())
        days = iso_range(start, end)
        result.append((item["name"], {d for d in days
                                      if not item.get("isClosedBooking") and night_available(daily.get(d))}))
    return result


# ── 켄싱턴 호텔·리조트 ──────────────────────────────────────────
KENSINGTON_ROOMDATA_URL = "https://kensington.co.kr/reservation/quick_roomdata/"
KENSINGTON_REQUEST_GAP = 0.3  # 초. 하루씩 여러 번 조회할 때 요청 간격


def kensington_open_rooms(t, checkin, checkout):
    """해당 기간에 예약 가능한 객실 코드 집합. (마감된 객실은 응답에서 빠짐)"""
    q = urllib.parse.urlencode({
        "search_bran_cd": t["branCd"],
        "search_stay_start_dt": checkin,
        "search_stay_end_dt": checkout,
        "search_room_cnt": 1,
        "search_people_adult_cnt": t.get("adults", 2),
        "search_people_adult_cnts": t.get("adults", 2),
        "search_people_child_cnt": 0,
        "search_people_child_cnts": 0,
        "pay_type": "P",
    })
    req = urllib.request.Request(
        f"{KENSINGTON_ROOMDATA_URL}?{q}",
        headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                 "Referer": f"https://kensington.co.kr/reservation/quick?search_bran_cd={t['branCd']}"},
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        body = res.read().decode("utf-8", "replace")
    if body.strip() == "N" or "item_list" not in body:
        raise RuntimeError(f"켄싱턴 응답 형식이 예상과 다릅니다 ({checkin}): {body.strip()[:80]!r}")
    return set(re.findall(r'id="btn_first_([^"]+)"', body))


def kensington_url(t, room_id, checkin, checkout):
    return f"https://kensington.co.kr/reservation/quick?search_bran_cd={t['branCd']}"


def kensington_check(t, checkins, nights):
    out = {}
    for i, ci in enumerate(checkins):
        if i:
            time.sleep(KENSINGTON_REQUEST_GAP)
        co = (date.fromisoformat(ci) + timedelta(days=nights)).isoformat()
        open_codes = kensington_open_rooms(t, ci, co)
        for code, name in t["rooms"].items():
            out[(code, name, ci)] = code in open_codes
    return out


def kensington_open_days(t, start, end):
    days = iso_range(start, end)
    opened = {code: set() for code in t["rooms"]}
    for i, d in enumerate(days):
        if i:
            time.sleep(KENSINGTON_REQUEST_GAP)
        co = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
        for code in kensington_open_rooms(t, d, co) & opened.keys():
            opened[code].add(d)
    return [(name, opened[code]) for code, name in t["rooms"].items()]


PROVIDERS = {
    "naver": {"check": naver_check, "open_days": naver_open_days, "url": naver_url},
    "kensington": {"check": kensington_check, "open_days": kensington_open_days, "url": kensington_url},
}


# ── 공통 ──────────────────────────────────────────────────────
def iso_range(start, end):
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def provider(t):
    return PROVIDERS[t.get("type", "naver")]


def check(t):
    """{"객실ID|체크인": {...}} 형태로 객실×체크인 날짜별 예약 가능 여부를 반환."""
    today = datetime.now(KST).date()
    nights = t.get("nights", 1)
    checkins = sorted(d for d in t["checkinDates"] if date.fromisoformat(d) >= today)
    if not checkins:
        return {}
    p = provider(t)
    result = {}
    for (room_id, room, ci), ok in p["check"](t, checkins, nights).items():
        co = (date.fromisoformat(ci) + timedelta(days=nights)).isoformat()
        result[f"{room_id}|{ci}"] = {
            "room": room, "checkin": ci, "checkout": co, "available": ok,
            "url": p["url"](t, room_id, ci, co),
        }
    return result


def send_telegram(text, parse_mode=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없습니다.")
    fields = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    if parse_mode:
        fields["parse_mode"] = parse_mode
    body = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20) as res:
        json.load(res)


def fmt_date(iso):
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}({'월화수목금토일'[d.weekday()]})"


def star(t, iso):
    """가장 원하는 날짜(priorityDates)면 ⭐."""
    return "⭐" if iso in t.get("priorityDates", []) else ""


def room_label(result, r):
    """객실이 여러 개일 때만 ' · 객실명'을 붙임."""
    return f" · {r['room']}" if len({x["room"] for x in result.values()}) > 1 else ""


def single_room_line(t, result):
    """객실이 하나뿐이면 알림에 객실명을 따로 한 줄 표시."""
    rooms = {x["room"] for x in result.values()}
    return f"\n{next(iter(rooms))}" if len(rooms) == 1 and t.get("type") == "kensington" else ""


def notify_changes(t, prev, result, notify):
    """이전 상태와 비교해 열림(🎉)/다시 마감(🔒)을 알림."""
    for k, r in result.items():
        stay = f"{fmt_date(r['checkin'])} → {fmt_date(r['checkout'])}{room_label(result, r)}"
        room = single_room_line(t, result)
        if r["available"] and not prev.get(k):
            if star(t, r["checkin"]):
                head = f"🎉⭐ [{t['name']}] 원하던 {fmt_date(r['checkin'])} 예약 가능!"
            else:
                head = f"🎉 [{t['name']}] 예약 가능!"
            notify(f"{head}{room}\n{stay}\n{r['url']}")
        elif not r["available"] and prev.get(k):
            notify(f"🔒 [{t['name']}] 다시 마감됐어요{room}\n{star(t, r['checkin'])}{stay}")


def summary_months(t):
    m = t.get("summaryMonths") or t.get("summaryMonth") or []
    return [m] if isinstance(m, str) else m


def month_summary(t):
    """summaryMonths(YYYY-MM 목록)의 남은 날짜별 1박 예약 현황 메시지. 모두 지났으면 None."""
    now = datetime.now(KST)
    months = []
    for ym in summary_months(t):
        year, month = map(int, ym.split("-"))
        first = date(year, month, 1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        start = max(first, now.date())
        if start <= last:
            months.append((first, last, start))
    if not months:
        return None
    watched = set(t["checkinDates"])
    esc = html.escape
    title = "·".join(f"{f.month}월" for f, _, _ in months)
    lines = [f"📅 [{esc(t['name'])}] {title} 예약 현황",
             f"{now.month}/{now.day} {now:%H:%M} 기준 · <b>[숫자]</b> = 예약 가능"]
    rooms = provider(t)["open_days"](t, months[0][2], months[-1][1])
    for name, open_all in rooms:
        if len(rooms) > 1 or t.get("type") == "kensington":
            lines.append(f"\n<b>{esc(name)}</b>")
        for first, last, start in months:
            days = iso_range(start, last)
            open_days = open_all & set(days)
            lines.append(f"\n<b>{first.month}월</b> 🟢 예약 가능 {len(open_days)}일 / 마감 {len(days) - len(open_days)}일")
            lines.append("<pre>" + calendar_grid(first, last, open_days) + "</pre>")
        mine = [f"{'🟢' if d in open_all else '⚪'} {star(t, d)}{fmt_date(d)}"
                for d in sorted(watched) if d >= months[0][2].isoformat()]
        if mine:
            lines.append("감시 중인 날짜:\n" + "  ".join(mine))
    return "\n".join(lines)


def calendar_grid(first, last, open_days):
    """일~토 달력. 칸 너비 4, 날짜 오른쪽 정렬, 예약 가능일은 [n]."""
    rows = [" 일  월  화  수  목  금  토"]
    cells = ["    "] * ((first.weekday() + 1) % 7)  # 일요일=0
    d = first
    while d <= last:
        n = str(d.day)
        cells.append((f"[{n}]" if d.isoformat() in open_days else f"{n} ").rjust(4))
        d += timedelta(days=1)
    for i in range(0, len(cells), 7):
        rows.append("".join(cells[i:i + 7]).rstrip())
    return "\n".join(rows)


def load_state(targets):
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    if "targets" not in state:
        # 예전(대상 하나) 형식 → 첫 번째 대상의 상태로 옮김. 키 형식("객실ID|날짜")은 같음
        old = {k: state[k] for k in ("available", "errors", "checkedAt") if k in state}
        state = {"targets": {targets[0]["id"]: old} if old else {},
                 "lastSummary": state.get("lastSummary")}
    return state


def load_targets():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return cfg, cfg["targets"]


def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    args = [a for a in args if a != "--dry"]
    mode = args[0] if args else ""
    notify = (lambda text, parse_mode=None: print(text + "\n")) if dry else send_telegram
    cfg, targets = load_targets()

    if mode == "--demo":
        # 지정한 날짜의 현재 상태와 반대였다고 가정해 실제 알림을 보냄 (state.json은 건드리지 않음)
        if len(args) < 2 or not args[1].strip():
            sys.exit("사용법: python check.py --demo 2026-12-05,2026-12-19")
        dates = [d.strip() for d in args[1].split(",") if d.strip()]
        # 해당 날짜를 감시하는 대상에만 적용, 아무 대상도 감시하지 않는 날짜면 모든 대상에 적용
        picked = [(t, [d for d in dates if d in t["checkinDates"]]) for t in targets]
        picked = [(t, ds) for t, ds in picked if ds] or [(t, dates) for t in targets]
        for t, ds in picked:
            t = {**t, "checkinDates": ds, "name": f"테스트·{t['name']}"}
            result = check(t)
            notify_changes(t, {k: not r["available"] for k, r in result.items()}, result, notify)
        return

    if mode == "--summary":
        sent = False
        for t in targets:
            msg = month_summary(t)
            if msg:
                notify(msg, "HTML")
                sent = True
        if not sent:
            notify("현황을 보낼 달이 모두 지났습니다.")
        return

    state = load_state(targets)
    failed = False
    results = {}

    for t in targets:
        ts = state["targets"].setdefault(t["id"], {})
        try:
            result = check(t)
        except Exception as e:  # 네트워크/API 변경 등
            failed = True
            ts["errors"] = ts.get("errors", 0) + 1
            print(f"[{t['name']}] 확인 실패 ({ts['errors']}회 연속): {e}", file=sys.stderr)
            if ts["errors"] == ERROR_ALERT_THRESHOLD and mode != "--test":
                notify(f"⚠️ [{t['name']}] 예약 확인이 {ERROR_ALERT_THRESHOLD}회 연속 실패했습니다.\n{e}")
            continue

        results[t["id"]] = result
        for r in result.values():
            print(f"[{t['name']}] {r['checkin']} {r['room']}: {'가능' if r['available'] else '마감'}")

        if mode == "--test":
            continue  # 상태를 저장하지 않아야 다음 정기 실행에서 알림이 빠지지 않음

        if ts.get("errors", 0) >= ERROR_ALERT_THRESHOLD:
            notify(f"✅ [{t['name']}] 예약 확인이 다시 정상 동작합니다.")
        ts["errors"] = 0
        notify_changes(t, ts.get("available", {}), result, notify)
        ts["available"] = {k: r["available"] for k, r in result.items()}
        ts["checkedAt"] = datetime.now(KST).isoformat(timespec="seconds")

    if mode == "--test" or dry:
        lines = []
        for t in targets:
            if t["id"] not in results:
                continue
            result = results[t["id"]]
            lines.append(f"🔎 [{t['name']}] 현재 상태{single_room_line(t, result)}")
            lines += [f"{'🟢' if r['available'] else '⚪'} {star(t, r['checkin'])}{fmt_date(r['checkin'])}"
                      f"{room_label(result, r)}" for r in result.values()]
            if not result:
                lines.append("감시할 날짜가 남아있지 않습니다.")
            lines.append("")
        if lines:
            notify("\n".join(lines).rstrip())

    if mode == "--test":
        sys.exit(1 if failed else 0)

    # 하루 한 번, summaryHour 이후 첫 실행에서 월간 현황 전송 (실패해도 감시는 계속)
    now = datetime.now(KST)
    today = now.date().isoformat()
    if any(summary_months(t) for t in targets) and now.hour >= cfg.get("summaryHour", 9) \
            and state.get("lastSummary") != today:
        for t in targets:
            try:
                msg = month_summary(t)
                if msg:
                    notify(msg, "HTML")
            except Exception as e:
                print(f"[{t['name']}] 월간 현황 전송 실패: {e}", file=sys.stderr)
        state["lastSummary"] = today

    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
