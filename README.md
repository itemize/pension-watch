# pension-watch

네이버 예약 펜션의 취소분(빈 날짜)을 5분마다 확인해 텔레그램으로 알려줍니다.
GitHub Actions에서 돌아가므로 PC를 꺼도 동작합니다. 추가 패키지 없이 Python 표준 라이브러리만 사용합니다.

- 대상: 오픈세서미 (네이버 플레이스 1404202432 / 예약 bizId 885041)
- 날짜: 2026년 11월 토요일 체크인 1박 (11/7, 11/14, 11/21, 11/28)

## 동작
- `m.booking.naver.com/graphql`의 `schedule` 쿼리로 날짜별 재고(`stock - bookingCount`)와 영업/판매 여부를 확인
- 상태가 바뀔 때만 알림 (같은 알림 반복 없음)
  - 마감 → 예약 가능: 🎉 예약 링크와 함께 알림
  - 예약 가능 → 마감: 🔒 다시 마감 알림
- 3회 연속 확인 실패 시 경고 알림, 복구되면 복구 알림
- 지난 날짜는 자동으로 제외

## 설정
`config.json`
| 키 | 설명 |
|---|---|
| `checkinDates` | 감시할 체크인 날짜 목록 |
| `nights` | 숙박일 수 (2 이상이면 연속된 모든 밤이 비어야 알림) |
| `bizItemIds` | `"all"` 또는 객실 ID 목록 (예: `["4999614"]`) |

## GitHub에 올리기
1. **공개(public)** 저장소를 만들고 이 폴더를 push
   - 비공개 저장소는 Actions 무료 한도(월 2,000분)를 5분 주기로는 초과합니다. 토큰은 Secrets에 들어가므로 공개해도 노출되지 않습니다.
2. Settings → Secrets and variables → Actions → New repository secret
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Actions 탭 → `pension-watch` → **Run workflow**, mode에서 `--test` 선택 → 텔레그램에 현재 상태가 오면 성공
   - mode `--demo` + dates(예: `2026-11-03,2026-11-07`)로 실행하면 지정 날짜 중 열린 날은 🎉, 마감된 날은 🔒 알림을 실제로 보내봅니다 (감시 상태는 그대로)
4. 이후 5분마다 자동 실행 (GitHub 사정에 따라 5~15분 지연될 수 있음)

11월이 끝나면 Actions 탭에서 workflow를 **Disable** 하세요.

## 로컬 실행
```
python check.py --dry    # 텔레그램 없이 콘솔 출력
python check.py --test   # 현재 상태를 텔레그램으로 전송 (환경변수 필요)
python check.py          # 일반 실행
```
