# HKTE 通告簽署可行性研究

## 結論

技術上可行：只安裝 `hass-hkte-smart-school` integration 和 notices card，
由 integration 使用既有 HKTE 登入 session 直接呼叫 HKTE，毋須部署 Message
Hub 或填寫它的 API key。現有 Message Hub 原始碼已直接實作 `SignNotice`，
它是本次研究的參考來源，而非未來執行時的必要依賴。

目前 HA integration 已按以下研究結果移植直接 client、表格正規化、使用者
確認及不確定結果處理。研究文件仍保留歷史參考，以便追溯欄位來源；它不是
執行時依賴，也不代表本文件曾使用真實憑證或代替使用者提交。

## 證據

- integration 的 API client 只呼叫 `Login`、`GetChildren`、`GetAllNotices`、
  `GetNoticeData`、`GetMessageList`、`GetHomeworks` 及附件所需的 `uHubSid`。
  這些呼叫位於 `custom_components/hkte_smart_school/api.py` 及
  `attachments.py`；背景同步明確保持唯讀。
- 現有簽署程式 `custom_components/hkte_smart_school/signing.py` 是
  **Message Hub bridge**，只呼叫 `/api/integrations/hkte/.../reply-form`、
  `/sign` 及 `/notice-operations/...`，並不是 HKTE 原始 API。
- Message Hub 的 API 指南明確說明：只有其 `/read` 和 `/sign` POST 會改變
  真實 HKTE 帳戶，而且簽署需要先取得 reply form、展示所有問題、收集使用者
  親自選擇並以 `confirmed=true` 提交（參見
  `/Users/wfchan/App_Dev/message-hub/docs/hkte-api-agent-guide.md` 的
  “Notice read/sign and user-selected reply answers”）。這是 Message Hub 提供
  的安全代理契約，不能推導成 HKTE 直接 endpoint 契約。
- `message-hub/api/app/hkte.py:193` 的 `_call` 直接 POST 到 HKTE，multipart
  欄位 `data` 裝 JSON，與 HA `api.py:239` 的既有 transport 相同。
  `hkte.py:261` 呼叫 `SignNotice` 並加入 `user_id` 和 `nid`；`hkte.py:265`
  以相同 ID 呼叫唯讀 `GetNoticeReply` 並驗證通告 ID。
- `message-hub/api/app/hkte_forms.py:147` 將明確選擇編碼成 `reply`、
  `amount`、`amount_without_extra_subsidy`，並按題型加入 `sub_notices`、
  `section_reply`、`comment` 及 `subsidy_status`。此模組源碼標示依據 Android
  APK；`api/tests/test_hkte_actions.py:192` 對原始呼叫參數有模擬測試。
- `hkte_forms.py:14` 支援 `ReadOnly`、`Choice`、`Choices`、`Comment`、
  `Feedback`、`ItemQuantities`、`ItemSet`。付款、上傳、特殊 ECA、巢狀或
  未識別表格須使用官方 App，不能猜測答案或以零元代替付款。
- 指南第 359–361 行記錄 2026-09-12 曾有一份經使用者同意的真實回覆成功，
  並由 `GetNoticeReply` 回讀確認。這是既有專案的驗證記錄，本次未重做實際
  簽署，亦未獨立檢查私人回覆資料。

## 獨立整合所需改動

1. 以既有 HA client 增加明確的 `sign_notice`、`notice_reply` 方法；HKTE 密碼
   及 cookies 維持後端管理，不新增中介服務 key。
2. 從 `GetAllNotices` 保留完整回條欄位供後端表格處理，目前 `GetNoticeData`
   enrichment 只補附件 metadata，亦沒有保留完整表格。不能只用目前 sensor 的
   標題、正文、回覆狀態推導表格。遷移 `hkte_forms.py` 的驗證規則，去掉 FastAPI
   依賴，轉成 HA 專用錯誤及 typed models。
3. Card 顯示全部適用題目、使用者自行選擇、最後預覽並明確確認。開啟或下載
   通告不標記已讀、不簽署；AI 不選答案。
4. 每次提交前重新確認 child/notice 歸屬、系統期限、是否已回覆及 form hash。
   HA 後端驗證 entity 讀取及控制權限。
5. 提交前寫入私人 Store 操作紀錄、加鎖去重。逾時/斷線不能自動重送
   `SignNotice`；保留 pending/unknown，僅以唯讀查詢確認結果。此模式可參考
   `message-hub/api/app/hkte_actions.py:136–218`，但需以 HA Store 取代 PostgreSQL。
6. 用 `GetNoticeReply` 確認實際保存答案，分清「已簽署」與「本次所選答案已保存」。
   對於服務端沒有已驗證 idempotency key 的情況，不能把本地 UUID 當成 HKTE 去重保證。

建議先提供非付款及已識別題型，對未支援題型清楚提示使用官方 App；再以
mock 測試確認 payload、條件題、期限、重複點擊、HA 重啟及未知結果。
實際簽署驗收仍需由使用者逐份選答案及最後確認；本次實作與 mock 測試沒有
使用真實憑證，也沒有自動提交任何真實通告。
