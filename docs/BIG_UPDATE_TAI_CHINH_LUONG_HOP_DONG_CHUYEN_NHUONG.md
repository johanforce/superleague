> **Ghi chú kiến trúc hiện tại (03-11-2026):** các mục lưu trữ JSON/CSV ở phần dưới mô tả thiết kế lịch sử tại thời điểm tài liệu được viết. Phiên bản hiện tại đã chuyển sang Single Database: danh mục cầu thủ, reference names, career và archive mùa đều nằm trong `memory/super_league.db`; không còn các file dữ liệu sidecar trong `memory/`.

# BIG UPDATE: Tài chính CLB – Lương – Hợp đồng – Chuyển nhượng

> **Tài liệu lịch sử:** Đây là bản mô tả thiết kế/tiến độ Economy ở nhánh phát triển trước, không phải hướng dẫn cài đặt hay mô tả chính sách lưu trữ hiện hành. Với gói Clean Start, hãy theo `README.md` ở thư mục gốc; reset/khởi tạo mới không lưu backup cục bộ.

> **Mục tiêu:** phát triển Super League từ trình mô phỏng giải đấu thành hệ thống quản lý bóng đá kiểu Football Manager. Mỗi CLB có tài chính riêng; cầu thủ có lương, giá trị thị trường và hợp đồng; thị trường chuyển nhượng, vận hành đội bóng, doanh thu, chi phí, sân vận động và Financial Fair Play hoạt động theo mùa.
>
> **Đơn vị tiền:** triệu euro (M€). Lưu đến 0,001 M€ (tương đương 1.000 €) để hạn chế sai số số thực. Tất cả con số trong cấu hình là giá trị khởi điểm, phải được cân bằng bằng mô phỏng dài hạn.
>
> **Giải đấu:** C1, Premier League (PL), FC1-Cup và FC2. Trong code hiện tại, Cup quốc nội là `Cup`; Football Championship 1/2 được lưu dưới tên giải hiện hành của project.

---

## 0. Nền tảng & quy ước chung

### Kiến trúc đề xuất

Tạo package `src/economy/`, tách biệt khỏi engine trận đấu, gồm:

- `FinanceService`: ví CLB, số dư và ledger.
- `WageService`: lương năm, quỹ lương, cập nhật market value.
- `ContractService`: hợp đồng, gia hạn, hết hạn, free agent.
- `TransferWindowService`: lịch và trạng thái cửa sổ chuyển nhượng.
- `TransferMarketService`: danh sách rao bán, mục tiêu, offer và lịch sử.
- `NegotiationService`: thương lượng CLB–CLB và CLB–cầu thủ.
- `SquadRulesService`: giới hạn quân số, vị trí và kiểm tra đội hình.
- `PrizeMoneyService`: tiền thưởng theo kết quả và vòng đấu.
- `StadiumService` và `MatchdayService`: sân, khán giả, trận tâm điểm và doanh thu sân.

Sử dụng event bus/hook để phát các sự kiện `MATCH_FINISHED`, `ROUND_FINISHED`, `COMPETITION_FINISHED`, `SEASON_FINISHED`, `DATE_ADVANCED`, `TRANSFER_WINDOW_OPENED`, `TRANSFER_WINDOW_CLOSED`. Service đăng ký lắng nghe thay vì cài logic tiền trực tiếp vào engine trận đấu.

### Ledger là nguồn sự thật

Mọi thay đổi tiền mặt phải qua một hàm ghi giao dịch; không tự gán `balance` ở bất kỳ UI hoặc service nào. Một giao dịch tối thiểu có:

```text
Transaction {
  id, date, season, month, teamId,
  type, amount, description, relatedId
}
```

`amount` dương là thu, âm là chi. Nhóm giao dịch: `PRIZE`, `MATCHDAY`, `BROADCAST`, `SPONSOR`, `COMMERCIAL`, `WAGE`, `TRANSFER_IN`, `TRANSFER_OUT`, `SIGNING_BONUS`, `AGENT_FEE`, `TAMPERING_COMPENSATION`, `OTHER`.

Dùng random seed xác định để cùng seed và cùng save tạo ra cùng kết quả tài chính, thuận tiện tái hiện lỗi.

### Quy tắc vận hành

- Tách logic kinh tế khỏi logic mô phỏng trận.
- Các hằng số phải nằm trong file cấu hình, không hard-code trong GUI.
- Mỗi migration phải idempotent: chạy lại không tạo ví, hợp đồng hoặc ledger trùng.
- Ghi chú lịch sử của nhánh phát triển cũ: migration cần được bảo vệ trước khi áp dụng. Chính sách này không áp dụng cho gói Clean Start hiện tại; xem README.md.
- Dùng feature flags: `ENABLE_ECONOMY`, `ENABLE_TRANSFERS`, `ENABLE_FFP`, `ENABLE_YOUTH_INTAKE`, `ENABLE_STADIUMS`, `ENABLE_HOME_ADVANTAGE`.

## 1. Quỹ tài chính ban đầu theo phân hạng đội

Mỗi CLB có `ClubFinance`:

```text
ClubFinance {
  teamId, balance, transferBudget, wageBudget, debt,
  reputation, fanBase, stadiumId, ticketPrice, history
}
```

Cấu hình cơ sở:

| Phân hạng | Balance | Transfer budget | Wage budget/năm | Reputation | Sức chứa sân mục tiêu |
|---|---:|---:|---:|---:|---:|
| Đội lớn | 300 M€ | 110 M€ | 230 M€ | 85–95 | 55.000–85.000 |
| Đội trung bình | 200 M€ | 80 M€ | 170 M€ | 60–75 | 35.000–55.000 |
| Đội vừa | 150 M€ | 55 M€ | 125 M€ | 35–55 | 12.000–35.000 |

Mỗi CLB nhận biến thiên xác định theo seed ±10%. Khoảng cách giữa các hạng phải đủ để giữ thứ tự lớn > trung bình > vừa. Kiểm tra tổng lương khởi tạo; nếu vượt quỹ lương hơn 10%, tăng ngân sách lương để đội không khởi đầu trong tình trạng mất khả năng chi trả.

`club_finance_overrides.json` cho phép ghi đè từng CLB (tier, balance, ngân sách, reputation, fan base, giá vé). Khởi tạo ví đúng một lần ở mùa đầu hoặc migration save cũ.

## 2. Lương mặc định & market value

### Lương năm

Công thức nền đề xuất:

```text
base       = 0.10 * exp(0.17 * (OVR - 60))
ageFactor  = <=21: 0.85 | 22–28: 1.00 | 29–31: 1.05 | 32–34: 0.85 | >=35: 0.65
potFactor  = 1 + max(0, POT - OVR) * 0.015
posFactor  = GK: 0.85 | DEF: 0.95 | MID: 1.00 | FWD: 1.10
clubFactor = BIG: 1.10 | MEDIUM: 1.00 | SMALL: 0.92
starFactor = 1.10 nếu thuộc top 3 OVR của CLB
wage       = base * ageFactor * potFactor * posFactor * clubFactor * starFactor
```

Làm tròn đến 0,05 M€; lương tối thiểu lớn hơn 0 và trần khởi tạo 35 M€/năm (cấu hình được). Nếu tổng lương >85% wage budget thì scale về khoảng 80%; nếu dưới 50%, chỉ scale lên tối đa 15%. Sau khi cân bằng phải thống kê min/average/max theo tier.

### Market value

Công thức khởi điểm theo tài liệu:

```text
marketValue = multiplier * exp(0.20 * (OVR - 60)) * ageValueFactor * potentialFactor
ageValueFactor: <=20: 1.5 | 21–23: 1.3 | 24–27: 1.0
                28–30: 0.75 | 31–33: 0.45 | >=34: 0.2
```

Hiệu chỉnh hệ số/cap trong config để các mốc tham khảo hợp lý: OVR 90 trẻ khoảng 150–200 M€, OVR 75 khoảng 15–25 M€, OVR 65 khoảng 1–3 M€. Cập nhật giá trị cuối mùa dựa trên OVR và tuổi mới; không thay đổi giá chuyển nhượng lịch sử đã chốt.

## 3. Hợp đồng cầu thủ

```text
Contract {
  id, playerId, teamId, startSeason, endSeason, yearsTotal,
  wagePerYear, signingBonus, releaseClause,
  appearanceBonus, goalBonus, squadRole, status
}
```

- Cầu thủ hiện có nhận hợp đồng tổng 3 năm; số năm còn lại lúc khởi tạo phân bố 1/2/3 năm với trọng số 30/35/35. Có cờ `UNIFORM_INITIAL_CONTRACT` nếu muốn tất cả còn đủ 3 năm.
- Vai trò theo thứ hạng OVR: top 3 `STAR`, 4–11 `FIRST_TEAM`, 12–18 `ROTATION`, còn lại `BACKUP`; cầu thủ trẻ tiềm năng cao có thể là `PROSPECT`.
- Cuối mùa giảm `yearsRemaining`; còn 1 năm chuyển sang `EXPIRING`; hết hạn mà chưa gia hạn thì thành free agent.
- Lương được trừ hàng tháng theo `wagePerYear / 12`, ghi ledger `WAGE`.
- Mỗi cầu thủ tối đa một hợp đồng `ACTIVE`. Không ký làm vượt wage budget, trừ khi chính sách FFP cho phép.
- Chấm dứt hợp đồng phải tính khoản bồi thường lương còn lại (mặc định 50%, config được).

## 4. Doanh thu & chi phí CLB

### Doanh thu

1. **Matchday:** vé + hospitality + concessions + sell-out bonus, dựa trên sân, sức chứa, fill rate và độ tâm điểm trận (mục 17).
2. **Broadcast:** 50% chia đều, 25% theo thứ hạng cuối mùa, 25% theo số trận được chiếu/facility fee (có thể gán theo reputation). Giải hạng dưới dùng quỹ nhỏ hơn.
3. **Sponsor:** base theo tier × reputation/70; tăng khi vô địch/đá châu Âu, giảm khi xuống hạng.
4. **Merchandise:** phụ thuộc fan base, thành tích; có cú hích khi ký ngôi sao OVR ≥88.
5. **Prize money:** theo mục 5.
6. **Player sales:** ghi nhận khi thương vụ hoàn tất.

Mốc doanh thu năm trước prize money:

| Tier | Matchday | Broadcast | Sponsor + merch |
|---|---:|---:|---:|
| Lớn | 80–120 M€ | 150–220 M€ | 150–250 M€ |
| Trung bình | 35–55 M€ | 120–170 M€ | 70–120 M€ |
| Vừa | 12–25 M€ | 90–130 M€ | 35–60 M€ |

### Chi phí

- Lương cầu thủ hằng tháng.
- Vận hành/ban huấn luyện mỗi năm: lớn khoảng 60 M€, trung bình 42 M€, vừa 30 M€.
- Agent fee 5–10% phí chuyển nhượng/lót tay khi ký.
- Hạ tầng, học viện và bảo trì sân (khi bật module sân).
- Trả nợ và lãi 3–6%/năm nếu có debt.

### Mốc thời gian hạch toán

- `MATCH_FINISHED`: doanh thu sân nhà.
- Cuối tháng: lương, vận hành; sponsor/merch chia theo tháng.
- `ROUND_FINISHED`/`COMPETITION_FINISHED`: prize money, broadcast theo điều kiện.
- `SEASON_FINISHED`: rollup báo cáo tài chính mùa.

Không được để đội trung bình/vừa tự phá sản khi giữ nguyên đội hình và không có hành động người chơi.

## 5. Prize money

Tất cả cấu hình nằm trong `config/prize_money.json`. C1 lớn nhất, sau đó PL, FC1-Cup, FC2. Mốc khởi điểm:

- **C1:** tham dự 18,6 M€; thắng league phase 2,1; hòa 0,7; playoff 1; vòng 16 đội 11; tứ kết 12,5; bán kết 15; chung kết 18,5; vô địch thêm 6,5. Tổng vô địch mục tiêu khoảng 130–150 M€ trước các nguồn thu khác.
- **PL:** thắng 0,8; hòa 0,25; thưởng vị trí từ khoảng 45 M€ cho hạng nhất đến 7 M€ cho hạng cuối trong thể thức 20 đội thực tế. Với project hiện có 8 CLB mỗi hạng, phải cân chỉnh bảng vị trí theo 8 đội, không dùng trực tiếp mốc 20 đội.
- **FC1-Cup:** quy mô khoảng 40% C1; tham dự 4,3; thắng 0,8; hòa 0,27; thưởng các vòng tăng dần: R16 1,5; QF 2,5; SF 4; Final 6; winner bonus 8.
- **FC2:** quy mô khoảng 15% C1; tham dự 3; thắng 0,4; hòa 0,13; vòng giữa tăng dần; vô địch thêm khoảng 3.

`onMatchFinished`, `onStageReached`, `onCompetitionFinished` chỉ trả khoản đội đủ điều kiện đạt được; không trả thưởng vòng chưa vào. Kiểm thử tổng thưởng vô địch theo thứ tự C1 > PL > FC1 > FC2.

## 6. Lịch & cửa sổ chuyển nhượng

- Cửa sổ mùa đông: tháng 11–12.
- Cửa sổ mùa hè: tháng 6–7–8.
- Lưu trong `config/transfer_windows.json`; deadline là ngày cuối mỗi cửa sổ, có thể tăng xác suất AI panic-buy.
- Lịch hiện tại chủ yếu có fixture từ tháng 8 đến tháng 5; không có tick mô phỏng tháng 6–7. Vì vậy P3 phải bổ sung bước advance thời gian giữa mùa để cửa sổ mùa hè tồn tại đầy đủ, không chỉ dựa vào ngày trận đấu.
- Ngoài cửa sổ cấm chuyển nhượng giữa CLB; free agent có thể được phép theo flag; gia hạn vẫn được.
- Offer chưa hoàn tất khi đóng cửa sổ tự hết hạn.
- Giới hạn mặc định 8 thương vụ/CLB/cửa sổ; có transfer list và loan list (loan triển khai giai đoạn sau).
- Project hiện đã lưu ngày theo từng calendar slot; có thể map tháng từ ngày slot, chưa có tick mỗi ngày. Deal tính theo ngày phải được phát triển theo chênh lệch ngày giữa các slot hoặc mở thêm bước tiến thời gian.

## 7. Giới hạn đội hình 23–30

`SquadRulesService.validate(team)` kiểm tra:

- Tổng 23–30 cầu thủ.
- Tối thiểu GK 3, DEF 7, MID 7, FWD 4 (config được).
- Mỗi nhóm vị trí chính tối thiểu 2 cầu thủ.
- Chặn bán khiến đội dưới 23 hoặc mua làm đội trên 30, trừ giao dịch thay thế đồng thời.
- Deadline auto-fix: ưu tiên free agent/trẻ học viện cho vị trí thiếu; giải phóng backup OVR thấp nhất khi vượt 30.
- Cuối mùa kiểm tra sau khi hợp đồng hết hạn/retirement.
- Youth intake 1–3 người/mùa (OVR 45–62, potential cao), nếu bật `ENABLE_YOUTH_INTAKE`.

## 8. AI nhu cầu đội hình & mua bán

`SquadAnalyzer` tìm vị trí yếu/thiếu, cầu thủ dư, cầu thủ già suy giảm và hợp đồng sắp hết. Mỗi `Need` có position, priority 1–5, minOVR, maxAge, maxPrice, maxWage.

- Đội lớn săn OVR cao, có thể mua 3–6 người/mùa hè và bán backup giá tốt.
- Đội trung bình cân bằng tiềm năng/tuổi 22–28; có thể bán sao nếu giá >130% market value.
- Đội vừa ưu tiên free agent/trẻ/giá rẻ và chấp nhận bán sao nếu được trả giá cao.
- `targetScore` xét độ phù hợp vị trí/OVR, value, affordability, tuổi và chi phí. Không đề xuất mục tiêu vượt ngân sách.
- AI hành động rải theo tick, xác suất khởi tạo 10–25%; mọi hành động ghi log lý do.

## 9. Đàm phán chuyển nhượng CLB–CLB

`TransferOffer` gồm player, đội bán/mua, fee, add-ons, sell-on, status, round, created/expiry date.

Giá yêu cầu ban đầu:

```text
askingPrice = marketValue * roleFactor * contractFactor * needFactor * clubStatusFactor
roleFactor: STAR 1.4 | FIRST_TEAM 1.15 | ROTATION 1.0 | BACKUP 0.8
contractFactor: 1 năm 0.7 | 2 năm 0.9 | 3+ năm 1.0+
needFactor: 1.2 nếu đội bán thiếu người cùng vị trí
clubStatusFactor: 0.9 nếu đội bán cần tiền
```

- Offer ≥ asking price: chấp nhận.
- Offer ≥85%: phản giá.
- Offer thấp hơn 85%: từ chối.
- Tối đa 3 vòng, cooldown 7 ngày cho cầu thủ vừa đàm phán thất bại.
- STAR ở đội lớn có thể bị khóa bán trừ offer ≥1,8× market value.
- Giao dịch hoàn tất khi CLB bán chấp nhận và cầu thủ đồng ý hợp đồng cá nhân.
- Kiểm tra balance, transfer budget và đội hình trước khi ghi ledger hai chiều.
- Free agent không mất phí chuyển nhượng; vẫn cần phí lót tay/agent fee.

## 10. Đàm phán hợp đồng cá nhân & thời gian deal

`ContractNegotiation` lưu type (`NEW_SIGNING`, `RENEWAL`, `TAPPING_UP`), ngày bắt đầu, thời gian dự kiến, vòng, demand/offer, mood, trạng thái.

- Sao/OVR cao: 7–14 ngày; cầu thủ thường: 3–7; trẻ: 1–3.
- Mỗi vòng đàm phán tiêu tốn 2–4 ngày mô phỏng.
- Demand wage dựa trên wage cơ sở, form/OVR, vai trò và sức hút CLB; demand years theo tuổi; signing bonus khoảng 5–15% tổng lương hợp đồng.
- CLB đá C1/reputation cao có thể trả thấp hơn 5–10%; CLB yếu/không đá cúp có thể phải trả cao hơn 5–15%.
- Offer wage ≥95% demand và số năm phù hợp: agreed; 80–95%: counter; dưới 80% hoặc mood <20: failed, cooldown 30 ngày.
- Không vượt wage budget và khả năng trả lót tay.
- UI hiển thị ngày đã qua, yêu cầu cầu thủ, offer CLB và lựa chọn để trợ lý xử lý hoặc người dùng tự deal.

## 11. Gia hạn 3–5 năm

Kích hoạt khi hợp đồng còn ≤1 năm; thông báo người dùng. `retentionScore` 0–100 đề xuất trọng số:

- OVR trong đội 35%.
- Form 10 trận 25%.
- Tầm ảnh hưởng 20%.
- Potential/tuổi 10%.
- Khan hiếm vị trí 10%.

- ≥75: ưu tiên gia hạn 5 năm (tuổi ≤28) hoặc 4 năm (29–31).
- 55–75: 4 năm (≤29) hoặc 3 năm.
- 40–55: 3 năm nếu dưới 32 và cần vị trí; nếu không có thể để hết hạn.
- <40: không ưu tiên gia hạn.
- Tuổi ≥33 thường 1–2 năm, tối đa 3.
- Score ≥75 có thể tăng lương 10–25%; giữ/giảm phong độ tăng 0–5% hoặc giữ nguyên.
- Gia hạn đi qua `ContractNegotiation`, ưu tiên nhanh hơn 30%.
- Cho phép pre-contract từ mốc ≤6 tháng theo luật mô phỏng.

## 12. Phí tiếp cận cầu thủ còn hợp đồng (tapping up)

Hành động xin phép đàm phán chỉ có trong cửa sổ chuyển nhượng (trừ pre-contract). Phí tiếp cận trả cho đội chủ khi được chấp thuận:

```text
approachFee = max(0.1 M€, 5–10% * marketValue * yearsRemainingFactor * roleFactor)
yearsRemainingFactor: 1 năm 0.6 | 2 năm 0.9 | 3+ năm 1.2
roleFactor: STAR 1.5 | FIRST_TEAM 1.2 | ROTATION 1.0 | BACKUP 0.7
```

Đội chủ có thể từ chối khi cầu thủ sao đang đua danh hiệu, khi mất người làm đội dưới 23 hoặc người mua là đối thủ trực tiếp; nếu từ chối thì không thu phí/hoàn phí. Hợp đồng ≤6 tháng được giảm mạnh hoặc miễn phí. Ledger `TAMPERING_COMPENSATION` ghi hai đầu; hiển thị tổng phí tiếp cận + chuyển nhượng + signing bonus + agent fee + lương/năm. Giới hạn ví dụ 5 cầu thủ/đội/cửa sổ, cooldown 14 ngày.

## 13. Financial Fair Play / PSR

- Wage + amortisation ≤70–80% doanh thu (config).
- Theo dõi lỗ 3 mùa liên tiếp; ngưỡng tham khảo: lớn 90 M€, trung bình 65 M€, vừa 50 M€.
- Cấp phạt: cấm mua có phí trong một cửa sổ; nặng hơn có thể trừ 3 điểm hoặc phạt tiền.
- Transfer amortisation = fee / số năm hợp đồng.
- Owner injection (nếu bật) có giới hạn và ghi ledger.
- Nợ kéo dài có thể kích hoạt cấm chuyển nhượng/fire sale.
- Cuối mùa có báo cáo FFP, lỗ lũy kế, trạng thái phạt và hành động AI giảm chi.

## 14. Giao diện & báo cáo trong trang CLB

Điều hướng mục tiêu:

```text
CLB
├── Tổng quan
├── Đội hình
├── Tài chính
├── Chuyển nhượng
│   ├── Tổng quan thị trường / Watchlist / Mục tiêu
│   ├── Offer & đàm phán / Rao bán / Free agents
│   └── Lịch sử chuyển nhượng
├── Hợp đồng
│   ├── Tất cả / Đến hạn / Cần gia hạn
│   ├── Đang đàm phán / Mới ký
├── Sân vận động
├── Báo cáo & thống kê
└── Thông báo (Inbox)
```

Header CLB luôn hiện balance, transfer budget, wage room, số cầu thủ x/30 và trạng thái cửa sổ. Badge hiển thị offer chưa trả lời, hợp đồng sắp hết hạn và thông báo chưa đọc. Khi xem CLB AI thì chỉ hiện thông tin công khai, ẩn thao tác nội bộ.

### Tài chính

KPI: balance, transfer budget, wage budget đã dùng/tổng, debt, revenue, cost, profit/loss, wage/revenue, FFP status. Biểu đồ: cashflow theo tháng; revenue breakdown; cost breakdown; prize theo giải; wages/revenue qua mùa; cash balance nhiều mùa; so sánh với các đội cùng tier. Ledger có filter, tìm kiếm, sort, pagination và xuất CSV/JSON. Cho phép phân bổ lại ngân sách và điều chỉnh giá vé khi quyền người dùng cho phép.

### Chuyển nhượng

Hiện trạng thị trường, nhu cầu theo vị trí, watchlist qua nhiều mùa, mục tiêu có trần giá/lương và tổng chi phí, inbox offer đi/đến, rao bán, free agent, lịch sử chi/thu. Mỗi thương vụ có trạng thái stepper, vòng thương lượng, hạn trả lời và cảnh báo vượt ngân sách/quân số/FFP.

### Hợp đồng

Danh sách lương, năm bắt đầu/kết thúc, thời gian còn lại, role, signing bonus, release clause, status. Tab đến hạn/cần gia hạn ưu tiên theo retention score. Màn hình đàm phán hai cột: cầu thủ yêu cầu vs CLB đề nghị, mood, thời gian, vòng, tác động ngân sách và FFP.

### Sân, báo cáo, Inbox và đội hình

- Sân: sân/ thành phố/ sức chứa, khán giả từng trận, % fill, doanh thu, forecast, nâng cấp.
- Báo cáo: tài chính, chuyển nhượng, hợp đồng, sân, FFP; chọn mùa và xuất PDF/CSV/JSON.
- Inbox: thông báo theo loại/độ ưu tiên, có thao tác nhanh và trạng thái đã đọc.
- Đội hình: thêm lương/năm, hợp đồng, role, market value, mood, trạng thái; hỗ trợ xem hợp đồng, gia hạn, rao bán, theo dõi.

UI chỉ đọc qua selector/query; mọi thay đổi ghi qua service. Cần có trạng thái loading/empty/error, confirm trước thao tác tốn tiền, tooltip lý do khóa, responsive, dark/light, i18n nếu ứng dụng hỗ trợ. Ledger/historic data cần rollup theo tháng/mùa để tránh render chậm.

## 15. Lưu trữ & migration dữ liệu cũ

- Bổ sung state schema version cho economy.
- Migration khởi tạo finance, wages, contract cho save cũ; không ghi trùng khi chạy lại.
- Các model gồm ClubFinance, Transaction, Contract, TransferOffer, ContractNegotiation, TransferHistory, SeasonFinancialReport.
- Ledger index theo team/season; rollup theo tháng.
- Cấu hình tập trung dưới `config/`; giữ feature flags để bật từng hệ thống.
- Lưu các chỉ số cầu thủ hiện tại và lịch sử giá/OVR theo mùa, không sửa archive quá khứ.

## 16. Kiểm thử & cân bằng

Unit tests:

- Wage/market value dương, có cap, tăng theo OVR trong cùng điều kiện.
- Squad 23–30 và vị trí tối thiểu.
- Cửa sổ tháng 6–8 và 11–12.
- Hợp đồng/renewal/free agent đúng thời hạn.
- Ledger hai chiều cho transfer, approach fee, signing bonus.
- Prize order C1 > PL > FC1 > FC2.

Soak test 10–20 mùa không can thiệp:

- Không đội phá sản mãi mãi hoặc giàu vô hạn.
- Đội lớn có lợi thế nhưng đội trung bình có thể vượt lên.
- Mọi đội có 23–30 cầu thủ, đủ vị trí.
- Lương trung bình tăng khoảng 3–5%/mùa trở xuống sau hiệu chỉnh.
- Số thương vụ mỗi cửa sổ hợp lý.

Kịch bản mẫu: đội nhỏ bán sao cho đội lớn; cầu thủ hết hạn/từ chối gia hạn; FFP cấm mua; đội thiếu người trước deadline.

## 17. Sân vận động & trận tâm điểm

### Model và gán sân

```text
Stadium {
  id, name, city, country, capacity, baseCapacity, tierFit,
  atmosphere, hospitalityRatio, roofed, homeTeamId,
  upgradeLevel, underConstruction
}
```

Đội thật dùng sân thật qua `stadiums_real.json`; đội hư cấu lấy sân chưa sử dụng cùng tier; hết kho thì tạo `<Tên đội> Arena`. Không gán trùng trừ khi có cấu hình chia sân. Khoảng gợi ý:

| Tier | Capacity | Giá vé | Hospitality |
|---|---:|---:|---:|
| Lớn | 55k–85k | €60–90 | 8–12% |
| Trung bình | 35k–55k | €40–60 | 5–8% |
| Vừa | 12k–35k | €25–40 | 2–5% |

Danh sách sân gợi ý trong yêu cầu gốc cần được kiểm chứng sức chứa và trạng thái cải tạo trước khi phát hành; không lấy con số ước lượng làm dữ liệu cố định.

### Độ tâm điểm

Tính `I` trong [0,1] từ derby, sức hút hai đội, stakes (đua vô địch/top 4/trụ hạng), vòng cúp và form. Công thức khởi điểm:

```text
I = clamp(max(derbyScore * 0.9,
    0.40*bigClash + 0.30*stakes + 0.20*stageFactor + 0.10*formFactor), 0, 1)
```

`I ≥0.75`: tâm điểm; 0.5–0.75: hấp dẫn; thấp hơn: thường. Rivalries nằm trong `rivalries.json`.

### Khán giả & doanh thu sân

```text
baseFill = clamp(0.50 + reputation/200 + atmosphere/500 + formBonus, 0.45, 0.95)
fillRate = clamp(baseFill + 0.35*I, 0.45, 1.0)
attendance = round(capacity * fillRate)
isSoldOut = fillRate >= 0.98

ticketPrice = tierBasePrice * (1 + 0.6*I) * competitionMultiplier
matchdayRevenue = ticketRevenue + hospitality + concessions + sellOutBonus
```

Competition multiplier gợi ý: PL 1.0, C1 1.8, FC1 1.3, FC2 1.1, cúp quốc nội 0.9. Trận C1/Cup trung lập chia doanh thu theo cấu hình; vé khách khoảng 5–8% capacity nhưng vẫn thuộc doanh thu chủ nhà. Lưu attendance/fillRate/importance vào match record.

Tùy chọn: home advantage tối đa khoảng +2% OVR hiệu dụng. Nâng cấp sân theo 3 cấp, phí 25–250 M€, thời gian 1–3 mùa; chỉ xây khi đủ balance/FFP và lấp đầy >92% trong 2 mùa. Tính bảo trì hằng năm; sân dưới chuẩn C1 phải thuê sân phù hợp.

## 18. Lộ trình triển khai

| Giai đoạn | Nội dung | Phạm vi |
|---|---|---|
| P1 – Nền tảng | economy package, ledger, config, feature flags, migration, event bus | 0, 15 |
| P2 – Tài chính cơ bản | ví, lương, hợp đồng, doanh thu/chi phí, prize | 1–5 |
| P2b – Sân | sân thật, trận tâm điểm, attendance, matchday | 17 |
| P3 – Đội hình & lịch | ngày/tháng, cửa sổ, squad 23–30 | 6–7 |
| P4 – Chuyển nhượng AI | squad needs, AI, offer CLB–CLB | 8–9 |
| P5 – Hợp đồng nâng cao | đàm phán theo ngày, gia hạn, pre-contract, approach fee | 10–12 |
| P6 – Cân bằng & hoàn thiện | FFP, UI, báo cáo, stadium upgrade, soak test | 13–14, 16, 17.7 |

---

## Đối chiếu với code Super League hiện tại

Đây là phần bắt buộc phải đọc trước khi phát triển; các tên trong spec không được giả định là class đã tồn tại.

| Khái niệm trong spec | Cấu trúc code hiện tại |
|---|---|
| Team/Club | Không có `Team` class riêng; tên CLB là key của `state['rosters']`, `team_tiers`, `season_tiers` |
| Player | Dictionary theo `player_id` trong `state['players']`, khởi tạo từ `memory/super_league.csv` |
| Tuổi/Potential | `state['player_meta'][player_id]` |
| Đội hình | `state['rosters'][team]`; hiện tại cố định 23 cầu thủ và kiểm tra đúng `POSITION_TEMPLATE` |
| Phân hạng giải | `premier`, `fc1`, `fc2`; mỗi hạng 8 CLB. Đây không tự động đồng nghĩa BIG/MEDIUM/SMALL về tài chính |
| Phân loại năng lực học viện | `academy_talent_profile()` có nhóm `elite`, `middle`, `standard`; đây là phân loại riêng cho cầu thủ trẻ, không phải tier tài chính mặc định |
| Mô phỏng trận | `Game._record_match()`, `advance_slot()`, `_complete_slot()` |
| Lịch | `state['slots']` lưu ngày ISO cụ thể; season hiện được thiết kế theo lịch từ tháng 8 đến các tháng cuối mùa |
| Kết thúc mùa | `Game.finalize_season()`; khởi tạo mùa mới trong `Game.next_season()` |
| Save/migration | JSON trong `memory/save.json`, `Game.SAVE_VERSION = 1`; state cho phép bổ sung field mới mà không cần đổi schema save cấp cao |
| GUI | Tkinter trong `src/sl_gui.py`, các trang CLB và live match nằm trong ứng dụng desktop |

### Những điểm phải xử lý trước khi bật toàn bộ hệ thống

1. Project có 24 CLB, 8 đội mỗi hạng, không phải PL 20 đội. Prize/TV allocation phải hiệu chỉnh cho 8 đội.
2. CSV nguồn ban đầu vẫn yêu cầu đúng 23 người theo khuôn vị trí; save sau khi khởi tạo đã hỗ trợ 23–30 và kiểm tra nhóm vị trí ở `_check_state`. Transfer service có nhánh đôn prospect thay thế khi bán từ đội đủ 23, nhưng các trường hợp tự động sửa đội hình sau retirement/expiry hoặc đội vượt 30 vẫn cần hoàn thiện.
3. Project vẫn mô phỏng trận theo calendar slot, nhưng daily tick hiện đã chạy từng ngày giữa các slot và trong off-season để kiểm tra deadline, offer và AI market. Fitness/training/morale theo ngày vẫn chưa có.
4. C1 hiện là thể thức riêng với play-in, 2 bảng và bán kết; không giả định có toàn bộ các stage UEFA league-phase chuẩn.
5. Các file archive trong `memory/season/` là lịch sử đã compact; không được sửa ngược ledger/hợp đồng mới vào những mùa cũ nếu không có migration minh bạch.
6. Phân loại finance `big/medium/small` cần tách khỏi academy profile. P1 map theo sức mạnh cạnh tranh hiện tại: `premier → big`, `fc1 → medium`, `fc2 → small`; tên CLB có thể ghi đè bằng `club_finance_overrides.json`. Khi có hệ thống thăng/hạng và tài chính nhiều mùa, cần quy tắc điều chỉnh tier theo mùa, không tự động reset ví.
7. Monthly accounting hiện vẫn chạy ở hook cấp slot và ledger có idempotency theo tháng. Daily tick riêng xử lý deadline và AI, không làm đổi nhịp hạch toán. Doanh thu sponsor/merchandise, operating cost, wage, lãi nợ, matchday và prize/broadcast đã có hooks. Cân bằng kinh tế dài hạn vẫn cần tiếp tục vì tiền mặt còn tích lũy đáng kể.

## Trạng thái triển khai trong bản code kèm tài liệu (02/11/2026)

> Quy ước: `[x]` chỉ dùng cho phần đã có code và có test/kiểm tra tương ứng; `[~]` là đã có một phần nhưng còn thiếu hành vi trong đặc tả; `[ ]` là chưa triển khai. Các đoạn mô tả P1 cũ ở những phiên bản trước được thay bằng tình trạng hiện tại dưới đây.

### Đối chiếu cập nhật với cấu trúc project

- `Team` vẫn không phải class độc lập: CLB là key trong `state['rosters']`, `team_tiers`, `club_finance`.
- Player vẫn là dictionary trong `state['players']`; tuổi/tiềm năng nằm trong `state['player_meta']`.
- Mỗi hạng giải hiện có 8 CLB (24 CLB tổng), nên prize/broadcast config được hiệu chỉnh theo quy mô này, không áp dụng trực tiếp số đội Premier League 20 đội thật.
- Đội hình save hiện cho phép 23–30 người; CSV nguồn ban đầu vẫn yêu cầu đúng 23 người/CLB để khởi tạo career sạch.
- Lịch game mô phỏng trận theo slot nhưng phát `DAY_ADVANCED` cho từng ngày giữa các slot và off-season; `DATE_ADVANCED` vẫn chỉ là hook slot cho hạch toán tháng.

### Checklist theo mục 0–18

#### 0. Nền tảng & quy ước chung
- [x] Package `src/economy/` tách `FinanceService`, `WageService`, `ContractService`, `SquadRulesService`, `TransferWindowService`, runtime/stadium/prize services và `TransferMarketService`.
- [x] Ledger là đường ghi nhận số dư cho các giao dịch kinh tế; số tiền lưu theo M€ với độ chính xác 0.001 M€; transaction hỗ trợ idempotency key để retry không cộng/trừ tiền lần hai và từ chối tái sử dụng key với payload khác.
- [x] Event bus và hook `DAY_ADVANCED`, `DATE_ADVANCED`, `ROUND_FINISHED`, `MATCH_FINISHED`, `COMPETITION_FINISHED`, `SEASON_FINISHED`, `TRANSFER_WINDOW_OPENED/CLOSED`.
- [x] Có phát `ROUND_FINISHED` và `TRANSFER_WINDOW_OPENED/CLOSED` từ vòng đời slot.
- [~] Đã có daily calendar tick cho từng ngày giữa hai slot và cả quãng nghỉ mùa; dùng để expire đàm phán/offer và chạy AI transfer. Chưa mô phỏng daily fitness/training/morale.
- [x] Seed xác định cho khởi tạo tài chính, tiền lương và sân vận động.

#### 1. Quỹ tài chính ban đầu
- [x] Ví, transfer budget, wage budget, debt, reputation, fan base cho tất cả CLB.
- [x] Phân hạng mặc định theo Premier/FC1/FC2, hệ số dao động seed và override theo tên CLB.
- [x] Migration schema economy v2 có idempotency; giới hạn tăng wage budget ban đầu tối đa 10% và chuẩn hóa wage để tránh save cũ âm tiền ngay khi khởi tạo.

#### 2. Lương và market value
- [x] Công thức lương năm xét OVR, tuổi, potential, vị trí, tier và vai trò ngôi sao; round theo cấu hình, có cap.
- [x] Wage/market value được ghi vào player; market value được cập nhật khi chốt mùa.
- [x] Lương đội hình khởi tạo được scale về xấp xỉ 80% wage budget trong migration schema v2.
- [ ] Chưa có CLI độc lập `seed:wages`; seed hiện chạy qua migration/bootstrap của game.

#### 3. Hợp đồng
- [x] Cầu thủ đội một được tạo hợp đồng 3 năm, thời hạn còn lại seed theo trọng số 1/2/3; squad role được gán.
- [x] Hợp đồng được trừ thời hạn ở cuối mùa; xử lý hợp đồng hết hạn/free agent khi đội vẫn thỏa quy tắc đội hình.
- [x] Chi trả wage hằng tháng qua ledger.
- [x] Có gia hạn dựa retention score và luồng đàm phán; có lưu contract history.
- [x] Có `terminate_contract`: chấm dứt hợp đồng ACTIVE/EXPIRING với bồi thường mặc định 50% tổng lương năm còn lại; ghi ledger có idempotency, giải phóng cầu thủ thành free agent và hủy các offer/đàm phán/pre-contract còn treo. Cầu thủ đang mượn bị chặn; nếu đội hình sau khi giải phóng thiếu 23 người/vị trí, service phải staging và đôn prospect học viện trong cùng giao dịch.

#### 4. Doanh thu và chi phí
- [x] Matchday revenue: vé, hospitality, concessions và sell-out bonus cho sân nhà.
- [x] Hạch toán tháng: wage, operating cost, sponsor, merchandise và lãi nợ; từng khoản có idempotency key riêng, tránh ghi trùng kể cả khi retry giữa chừng.
- [x] Catch-up hạch toán được khoảng nghỉ nhiều mùa (tối đa 100 năm có kiểm soát); tháng được gán đúng season theo lịch tháng 8–7, không gán nhầm tất cả vào mùa đang mở save.
- [x] Hợp đồng đã hết hạn không bị tính lương lại qua fallback wage của save cũ; fallback chỉ dùng khi roster chưa có bất kỳ contract record nào.
- [x] Doanh thu/chi phí được ghi vào ledger và rollup tháng.
- [x] Có owner emergency injection theo tier, có cap số lần/tổng tiền mỗi mùa; ghi `OWNER_INJECTION` riêng và không tính thành doanh thu P&L/FFP. Debt principal/interest được tách riêng.

#### 5. Tiền thưởng giải
- [x] Bảng thưởng tách config cho C1, PL, FC1-Cup/FC1 league và FC2; trả thưởng trận thắng/hòa, một số mốc giải hiện có và thưởng thứ hạng cuối mùa.
- [x] Chia broadcast 50/25/25 (đều/thứ hạng/facility) theo pool config đã hiệu chỉnh cho 8 CLB mỗi hạng.
- [~] Cấu trúc giải C1/Cup trong simulator khác format UEFA ngoài đời; các khoản stage bonus chỉ gắn vào các stage mà code giải hiện tại cung cấp, chưa bao phủ mọi mốc trong tài liệu.

#### 6. Lịch và cửa sổ chuyển nhượng
- [x] Cửa sổ 6–8 và 11–12 nằm trong config; chặn giao dịch phí ngoài cửa sổ.
- [x] Có giới hạn lượt tiếp cận/lượt deal, cooldown 14 ngày, free agent ngoài cửa sổ theo config, expiry offer/negotiation.
- [x] Deadline offer/đàm phán được rà mỗi ngày giữa các slot và cả quãng nghỉ mùa; không mô phỏng fitness/training/morale theo ngày.
- [ ] Chưa có màn hình lịch deadline day và panic-buy riêng.

#### 7. Đội hình 23–30
- [x] Validation cho 23–30 và tối thiểu GK/DEF/MID/FWD.
- [x] Chặn thương vụ làm sai squad; khi bán cầu thủ từ đội đúng 23 người, service chuẩn bị/đôn prospect học viện thay thế trong cùng giao dịch nếu có thể.
- [ ] Chưa có auto-fix toàn giải khi đóng cửa sổ cho đội >30 hoặc thiếu vị trí do các nguyên nhân ngoài transfer service.
- [~] Học viện hiện duy trì prospect pool và có đường đôn người thay thế; chưa có youth intake 1–3 người/CLB/mùa theo cấu hình riêng của tài liệu.

#### 8. AI nhu cầu đội hình & mua bán
- [x] `analyze_squad`, phát hiện thiếu/yếu/thừa, hợp đồng ngắn, wage room và target scoring.
- [x] Có đề xuất mục tiêu và danh sách rao bán thủ công.
- [x] AI director chạy theo từng cửa sổ, mua/bán qua transfer service; có policy BIG/MEDIUM/SMALL (quy mô mục tiêu 27/25/24, target OVR 96/92/86, share ngân sách 50%/35%/20%, dự trữ 25/15/10 M€). Tổng tối đa 12 deal, một deal/CLB/cửa sổ và player uniqueness.
- [x] AI fire-sale khi tiền mặt xuống dưới ngưỡng theo tier; vẫn cần buyer thật sự, kiểm tra squad replacement và dùng transfer pipeline đầy đủ.
- [x] AI development loan cho prospect tối đa 23 tuổi, potential cao hơn OVR, nhóm vị trí dư ở CLB cha và CLB mượn có need; chia wage share theo tier, tối đa 4 khoản mượn/cửa sổ.
- [x] AI swap khi hai CLB có nhu cầu vị trí đối ứng và cầu thủ dư tương ứng; kiểm tra OVR, mức cân bằng giá, quỹ lương/đội hình bằng `complete_swap`; tối đa 2 swap/cửa sổ.
- [~] Vẫn thiếu mô hình xác suất 10–25%, profile tuổi/tiềm năng/personality chuyên sâu theo tier, panic-buy và đánh giá thanh lý nhiều mùa; chưa chứng minh wealth equilibrium.

#### 9. Offer chuyển nhượng CLB–CLB
- [x] Xin phép tiếp cận có approach fee, yêu cầu giá, gửi offer, phản giá, chấp nhận/từ chối và hoàn tất; cập nhật roster hai bên và ledger.
- [x] Có asking price theo market value, role và năm hợp đồng; có chặn giao dịch vi phạm ngân sách/đội hình.
- [x] Có trả phí theo 1–6 đợt (trả sau tự retry 7 ngày nếu thiếu tiền), sell-on 0–30% được kích hoạt ở lần bán tiếp theo, tối đa 3 add-on theo số lần ra sân/bàn thắng (dự phòng ngân sách, tự thanh toán khi đạt mốc, tự giải phóng khi hết hạn hoặc cầu thủ rời CLB). Có swap hai cầu thủ với tiền cân bằng, hợp đồng mới, FFP amortisation theo notional market value và idempotency. Có loan với chia tỷ lệ lương, auto-return khi đủ điều kiện, RETURN_PENDING nếu đội hình chưa hợp lệ và option-to-buy. Inbox vẫn chưa bao phủ toàn bộ accept/reject/counter.

#### 10. Đàm phán hợp đồng cá nhân
- [x] Lưu player demand/club offer, mood, due date, trạng thái và kết quả counter/fail.
- [~] GUI có bước nhập đề nghị và phản hồi cơ bản; chưa có màn hình timeline hai cột và tiến trình nhiều vòng tiêu tốn từng tick 2–4 ngày như đặc tả.

#### 11. Gia hạn 3–5 năm
- [x] Retention score, thời hạn khuyến nghị theo tuổi, lương đề xuất, gia hạn qua negotiation service, lịch sử hợp đồng.
- [x] Pre-contract từ tháng 12 khi hợp đồng còn tối đa một năm; gia nhập ở mùa tiếp theo nếu điều kiện đội hình cho phép.
- [~] AI renewal campaign chạy ở summer window, xếp hạng bằng retention score và tự gia hạn tối đa 2 cầu thủ/CLB/mùa theo khả năng quỹ lương/ngân sách; chưa bao phủ mọi cửa sổ và mọi trường hợp hết hạn.

#### 12. Phí tiếp cận cầu thủ còn hợp đồng
- [x] Approach fee dựa market value, years remaining, role và reputation; ghi hai chiều `TAMPERING_COMPENSATION`.
- [x] Nếu CLB chủ từ chối, không trừ phí; pre-contract đủ điều kiện không mất phí tiếp cận.
- [~] Chưa có toàn bộ rule từ chối đối thủ trực tiếp và cấu hình N yêu cầu theo từng loại CLB.

#### 13. FFP/PSR
- [x] Tính wage + amortisation/revenue, rolling three-season aggregate loss, báo cáo trạng thái và ban chuyển nhượng theo config. Nếu doanh thu bằng 0 nhưng vẫn có chi phí lương/khấu hao, FFP vẫn phát cảnh báo thay vì bỏ qua tỷ lệ.
- [x] Transfer ban được kiểm tra trong transfer service và đếm cửa sổ.
- [x] Trừ điểm FFP có cấu hình và chỉ áp dụng từ mùa kế tiếp vào bảng domestic (Premier phase 1/phase 2, FC1, FC2) và bảng trực tiếp trong lúc xem trận, tránh sửa raw match points; không trừ điểm vào bảng vòng bảng C1.
- [x] Owner support có cap theo tier/mùa, không tính vào doanh thu vận hành; debt restructuring tạo lịch trả nợ gốc, chỉ thu khi vẫn giữ cash floor và hoãn kỳ thiếu thanh khoản.
- [x] Fire-sale AI dùng ngưỡng cash theo tier, nhưng chỉ bán khi có CLB mua thật sự cần cầu thủ và giao dịch vẫn hợp lệ.
- [~] Wealth balancing dài hạn vẫn chưa hoàn thành; soak clean-start 5 mùa chưa kích hoạt FFP point deduction hoặc debt restructuring và tiền mặt vẫn tích lũy mạnh.

#### 14. Giao diện CLB
- [x] Có tab Tài chính, Chuyển nhượng, Hợp đồng, Sân vận động, Báo cáo, Thông báo.
- [x] Tài chính hiển thị ví, ngân sách, wage, ledger gần nhất và xuất CSV/JSON.
- [x] Chuyển nhượng có xin tiếp cận, ký tự do/ký trước, rao bán, cho mượn, thực hiện mua đứt loan option và swap. UI nhập instalments, sell-on và add-ons ở luồng mua; tab Hợp đồng có gia hạn và chấm dứt với preview khoản bồi thường.
- [x] Sân vận động hiển thị capacity, attendance/fill/importance và matchday revenue; báo cáo hiển thị tài chính/FFP.
- [~] Inbox hiện thiên về thông báo/offer read-only; chưa có toàn bộ thao tác accept/reject/counter trong inbox.
- [ ] Chưa có watchlist bền vững, target board đầy đủ, chart library, dashboard nhiều biểu đồ, stadium upgrade/ticket-price controls, PDF export, responsive/i18n và lưu trạng thái UI theo CLB.

#### 15. Lưu trữ và migration
- [x] State economy có schema version riêng (hiện v2), bootstrap idempotent và giữ nguyên player records cũ.
- [x] Ledger/contract/transfer/report được lưu trong save; financial report được thêm vào archive mùa.
- [~] Monthly rollup có nhưng chưa có index/phân trang tối ưu cho ledger rất lớn hoặc materialized rollup đa mùa.

#### 16. Kiểm thử và cân bằng
- [x] 52 test Economy đạt; gồm AI mua/bán/fire-sale, AI loan, AI swap, cap hai phía, player uniqueness, loan/return/option, swap settlement, add-on, sell-on, instalments, termination/replacement, ledger idempotency, daily expiry, debt restructuring, owner support và FFP point deduction ở cả BXH đã lưu và BXH trực tiếp.
- [x] 49 test nhóm archive/Google Sheets/data exchange/human-data/award history đạt.
- [x] 5 test simulator hồi quy trọng yếu đạt (save-resume, calendar, live table, phase 2 live table và academy migration giữ RNG).
- [x] Migration trên bản save gốc trong sandbox giữ 24 CLB, 552 cầu thủ đội hình và 563 player records; tạo 24 ví, 552 hợp đồng; mở lại không tạo bản ghi khởi tạo trùng.
- [x] Soak 5 mùa seed `20261102`: 24 CLB giữ đội hình 23–30, không số dư âm. AI market deals 8/6/2/3/2; permanent buys/sales 3/4/2/2/0; loans 2/0/0/0/0; swaps 3/2/0/1/2; renewals 48/48/48/23/17. Owner support 0/8.300/9.162/0/0 M€. Tiền mặt cao nhất tăng 626.652 → 1,309.893 M€; không có FFP point deduction/debt restructure được kích hoạt. Kết quả xác nhận guardrails và workflow hoạt động, không chứng minh wealth equilibrium.
- [ ] Soak 10–20 mùa chưa hoàn tất trong môi trường kiểm tra; lượt chạy 10 mùa vượt giới hạn thời gian 300 giây.
- [x] 5 test simulator trọng yếu (save/resume, calendar, live league table, live phase-2 table, academy migration/RNG) chạy đạt.
- [ ] Toàn bộ `test.test_simulator` chưa chạy xong trong một lượt; lượt chạy kết hợp các test dài vượt giới hạn 300 giây, không coi toàn bộ suite là pass.

#### 17. Sân vận động & trận tâm điểm
- [x] Gán sân ổn định theo seed, sức chứa/giá vé/hospitality theo tier.
- [x] Tính match importance, attendance, fill rate, sell-out, doanh thu sân và lưu metadata trận.
- [~] Tên sân hiện tạo dạng `<Tên CLB> Arena`; chưa có mapping đáng tin cậy CLB thật–sân thật và danh sách rivalries được curate.
- [ ] Chưa có mở rộng/nâng cấp sân, thời gian thi công, bảo trì, naming rights, thuê sân trung lập theo chuẩn C1 và home advantage (đang tắt).

#### 18. Lộ trình
- [x] P1 nền tảng, P2 hạch toán/tiền thưởng, P2b mô phỏng sân cơ bản, P3 cửa sổ và squad rule cơ bản, P5 một phần đàm phán/hợp đồng, P6 một phần FFP/UI đã có code.
- [~] P4 có AI mua/bán/loan/swap bounded và giao dịch nâng cao, nhưng thiếu probabilistic decision, panic-buy, profiles sâu và cân bằng wealth; P5 có daily deadline tick nhưng chưa có daily fitness/training/morale và negotiation timeline GUI; P6 đã có FFP point deduction/owner support/debt restructuring cơ bản, còn stadium upgrades, inbox actions và analytics.

### Kiểm tra đã chạy trong bản hiện tại

| Nhóm | Kết quả |
|---|---:|
| `test.test_economy` | 52/52 pass |
| Archive, Google Sheets, hub data exchange, human data, award history | 49/49 pass |
| Simulator hồi quy trọng yếu | 5/5 pass |
| Python compile (`sl_game`, `sl_gui`, `season_archive`, `economy/*.py`) | pass |
| Full simulator suite | Chưa hoàn tất; timeout 300 giây |
| Soak mô phỏng | 5 mùa pass theo seed `20261102`; 8/6/2/3/2 AI deals, có loan/swap; cash không âm, squad hợp lệ, nhưng cash vẫn tích lũy mạnh |

### Ghi chú về bản Clean Start

Tài liệu này là ghi chú tiến độ của nhánh Economy trước đó, không phải hướng dẫn cài đặt cho gói Clean Start hiện tại. Hãy dùng `README.md` ở thư mục gốc để chạy project. Clean Start khởi tạo career mới từ dữ liệu nền, không mang theo save/archive cũ và không giữ backup sau thao tác reset hoặc cập nhật dữ liệu.

**Kết luận:** đây là một bản triển khai lớn đã có luồng tài chính cơ bản và transfer/contract workflow sử dụng được ở mức nền tảng, nhưng chưa phải bản mô phỏng đầy đủ mọi hệ thống trong spec. Các mục `[ ]` và `[~]` là backlog còn lại, không được hiểu là đã hoàn thành.
