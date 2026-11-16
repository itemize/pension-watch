# pension-watch

숙소의 취소분(빈 날짜)을 5분마다 확인해 텔레그램으로 알려줍니다.
GitHub Actions에서 돌아가므로 PC를 꺼도 동작합니다. 추가 패키지 없이 Python 표준 라이브러리만 사용합니다.

- 오픈세서미 (네이버 플레이스 1404202432 / 예약 bizId 885041) — 2026년 11~12월 토요일 체크인 1박
- 켄싱턴 리조트 경주 (`RKT02`) 그랜드 스위트 마운틴뷰(클린룸) (`62DMV`) — 2026년 12월 토요일 체크인 1박, ⭐12/19 우선

## 동작
- 네이버: `m.booking.naver.com/graphql`의 `schedule` 쿼리로 날짜별 재고(`stock - bookingCount`)와 영업/판매 여부를 확인
- 켄싱턴: `kensington.co.kr/reservation/quick_roomdata/`(빠른예약 객실 조회)에 날짜를 넣어 조회. 예약 가능한 객실만 응답에 나오므로 객실 코드가 있으면 가능
- `priorityDates`에 넣은 날짜는 🎉⭐ "원하던 날짜" 알림과 ⭐ 표시
- 상태가 바뀔 때만 알림 (같은 알림 반복 없음)
  - 마감 → 예약 가능: 🎉 예약 링크와 함께 알림
  - 예약 가능 → 마감: 🔒 다시 마감 알림
- 하루 한 번(`summaryHour`시 이후 첫 실행) 📅 월간 예약 현황 전송: 예약 가능한 날짜 목록 + 감시 중인 날짜 상태
- 3회 연속 확인 실패 시 경고 알림, 복구되면 복구 알림
- 지난 날짜는 자동으로 제외

## 설정
`config.json` — `targets` 배열에 감시 대상을 나열합니다. 상태는 대상의 `id`별로 따로 저장됩니다.

| 키 | 설명 |
|---|---|
| `summaryHour` | (최상위) 월간 현황을 보내기 시작하는 시각 (한국 시간) |
| `id` | 대상 고유 이름 (상태 저장용, 바꾸면 알림 상태가 초기화됨) |
| `type` | `naver` 또는 `kensington` |
| `name` | 알림에 표시할 이름 |
| `checkinDates` | 감시할 체크인 날짜 목록 |
| `priorityDates` | 가장 원하는 날짜 (⭐ 표시, 선택) |
| `nights` | 숙박일 수 (2 이상이면 그 기간 전체가 비어야 알림) |
| `summaryMonths` | 월간 현황을 보낼 달 목록 (`["YYYY-MM", ...]`). 빼면 월간 현황을 보내지 않음 |

`naver` 전용: `businessId`, `businessTypeId`, `bizItemIds` (`"all"` 또는 객실 ID 목록, 예: `["4999614"]`)

`kensington` 전용: `branCd` (지점 코드, 예약 URL의 `search_bran_cd`), `rooms` (`{"객실코드": "객실명"}`), `adults` (선택, 기본 2)
- 객실 코드는 빠른예약 페이지에서 객실이 남아 있는 날짜를 검색한 뒤, 개발자도구에서 객실 카드의 `data-room-type-cd` 값으로 확인

## GitHub에 올리기
1. **공개(public)** 저장소를 만들고 이 폴더를 push
   - 비공개 저장소는 Actions 무료 한도(월 2,000분)를 5분 주기로는 초과합니다. 토큰은 Secrets에 들어가므로 공개해도 노출되지 않습니다.
2. Settings → Secrets and variables → Actions → New repository secret
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Actions 탭 → `pension-watch` → **Run workflow**, mode에서 `--test` 선택 → 텔레그램에 현재 상태가 오면 성공
   - mode `--demo` + dates(예: `2026-12-05,2026-12-19`)로 실행하면 지정 날짜 중 열린 날은 🎉, 마감된 날은 🔒 알림을 실제로 보내봅니다 (감시 상태는 그대로)
   - mode `--summary`로 실행하면 📅 월간 예약 현황을 바로 보냅니다
4. 이후 5분마다 자동 실행 (GitHub 사정에 따라 5~15분 지연될 수 있음)

12월이 끝나면 Actions 탭에서 workflow를 **Disable** 하세요.

## 로컬 실행
```
python check.py --dry    # 텔레그램 없이 콘솔 출력 (--summary, --demo 와 함께 써도 됨)
python check.py --test   # 현재 상태를 텔레그램으로 전송 (환경변수 필요)
python check.py          # 일반 실행
```
