# 推廣派貨 Portal

每月一次：後台設定店舖生意佔比（A／B／C 三個版本），前台提供兩份 Excel
（SKU × Target Qty、該批 SKU 的每店庫存），系統按各版本把 Target 拆到每間店，
算出**需要派貨量**與**是否夠貨**，三個版本並排比較後匯出。

只計算推廣期內的派貨，不預測推廣期後的銷售。

---

## 本機執行

```bash
cd promo-portal
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt

set ADMIN_PASSWORD=adminpw
set USER_PASSWORD=userpw
set SECRET_KEY=dev-only-key
set COOKIE_SECURE=0
uvicorn app.main:app --reload --port 8000
```

開 http://127.0.0.1:8000 ，用 `adminpw` 登入。

`samples/` 有現成的範例檔，可以直接走一次完整流程：

| 檔案 | 用途 |
|------|------|
| `sample_shares.xlsx` | 20 間店舖的 A／B／C 佔比 |
| `sample_target.xlsx` | 10 個 SKU 的 Target 與 MOQ |
| `sample_stock_wide.xlsx` | 寬表格式庫存（每欄一店） |
| `sample_stock_long.xlsx` | 長表格式庫存（SKU／店舖／數量） |

重新產生：`python samples/make_samples.py`

---

## Railway 部署

### 1. 建 Volume（先做，順序很重要）

Railway 的容器是拋棄式的，沒有 volume 的話每次重新部署 SQLite 資料庫就會消失。

1. 專案 → **New** → **Volume**
2. Mount path 填 **`/data`**
3. 掛到這個 service

### 2. 設定環境變數

Service → **Variables**：

| 變數 | 值 | 說明 |
|------|-----|------|
| `SECRET_KEY` | 一串長隨機字元 | 簽 cookie 用，外洩等於可偽造登入 |
| `ADMIN_PASSWORD` | 你決定 | 後台：店舖、佔比 |
| `USER_PASSWORD` | 你決定 | 前台：上載、計算、匯出 |
| `DATA_DIR` | `/data` | SQLite 存放位置，必須對應 volume |
| `COOKIE_SECURE` | `1` | 強制 HTTPS cookie；本機測試才設 0 |

產生隨機字串：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 3. 部署

1. **New Project** → **Deploy from GitHub repo**
2. 選 repo → **Settings → Root Directory** 填 `promo-portal`
3. Railway 會讀 `railway.json` 與 `Procfile`，自動用 uvicorn 啟動
4. **Settings → Networking → Generate Domain**

健康檢查路徑是 `/health`，回 `{"ok":true}` 即正常。

### 資源

FastAPI + openpyxl 常駐約 100–150 MB，$5 Hobby plan 的 512 MB 相當充裕。
SQLite 單檔，每月一次計算的資料量在幾 MB 以內。

---

## 計算邏輯

### 拆 Target

```
每店分配數量 = Target × 該店佔比
```

用**最大餘數法**分配，確保每間店的分配數量加起來**剛好等於 Target**。
普通四捨五入會留下幾件的尾差，沒人解釋得到。

佔比總和不等於 100% 時，會按比例正規化並在結果頁提示。

### 派貨量

```
差距   = 分配數量 − 該店現有庫存
需派貨 = 差距 > 0 ? ceil(差距 / MOQ) × MOQ : 0
```

MOQ 留空或 1 表示不進位。

### 夠不夠貨

```
虧欠 = max(Target − 所有店舖庫存加總, 0)
夠貨 = 虧欠 == 0
```

**注意：夠貨不代表不用派貨。** 貨可能都在錯的店，需要調撥。
所以表上同時有「需派貨」與「可調出」兩欄。

### 三個版本的關係

A／B／C 用的是同一份 Target 與同一份庫存，只有店舖佔比不同。所以：

- **虧欠在三個版本必然相同**（總庫存與總 Target 沒變）
- **派貨量與派貨分布會不同**

結果頁的 KPI 直接把這件事寫出來，避免誤讀成「B 餐缺貨比較少」。

---

## 輸入格式

### 店舖佔比（後台匯入）

| Shop | 店舖名稱 | Region | A | B | C |
|------|---------|--------|---|---|---|
| HA19 | 銅鑼灣店 | HK | 5.58% | 5.94% | 6.12% |

- A／B／C 各自要加起來等於 100%
- 填 `0.0558` 或 `5.58` 都接受（大於 1.5 自動當百分比）
- 店舖代碼不存在會自動建立
- 沒有這個檔也可以線上逐間填

### SKU × Target

| SKU | 貨品名稱 | Target Qty | MOQ |
|-----|---------|-----------|-----|
| 109991605010 | 柔潤潔面乳 120ml | 2400 | 12 |

- SKU 不可重複（重複會直接擋下來，不會靜默翻倍）
- MOQ 選填

### 店舖庫存

兩種格式都接受，系統自動辨識：

**寬表**（每欄一店）

| SKU | HA19 | HA21 | HB38 | ... |
|-----|------|------|------|-----|
| 109991605010 | 132 | 122 | 151 | |

**長表**（每行一筆）

| SKU | 店舖 | 庫存 |
|-----|------|------|
| 109991605010 | HA19 | 132 |

數字可以是 `1,234`、`(20)`（負數）等常見 Excel 寫法，會自動解析。

---

## 專案結構

```
promo-portal/
├── app/
│   ├── calc.py          純計算核心（無 DB、無框架，30 個單元測試）
│   ├── db.py            SQLite schema 與查詢
│   ├── auth.py          簽名 cookie，admin / user 兩種角色
│   ├── excel_io.py      Excel 解析與匯出（含格式自動辨識）
│   ├── main.py          FastAPI 路由
│   ├── templates/       Jinja2 頁面
│   └── static/app.css   樣式
├── samples/             範例 Excel 與產生腳本
├── tests/               計算單元測試 + 端到端流程測試
└── tools/screens.py     開發用：各頁面截圖
```

## 測試

```bash
python -m pytest tests/ -q
```

37 個測試，約 2 秒。涵蓋分配演算法、MOQ 進位、夠貨邊界、
完整上載到匯出流程、角色權限、重複 SKU 攔截。

---

## 已知假設（如與實際業務不符請告知）

1. **夠貨的定義**是「該 SKU 所有店舖庫存加總 ≥ Target」。
   如果實際上有 DC／倉庫存貨需要一併計入，要再加一個庫存來源。
2. **派貨來源**假設是倉庫，所以派貨量只看店舖缺口，不從其他店扣。
   「可調出」只是參考值，不會自動從盈餘店扣減。
3. **推廣期長度不影響計算** —— 你提到不關將來的 Forecast Sale 事，
   所以沒有引入日銷率或覆蓋天數。
4. **A／B／C 是同一份 Target 的三種鋪貨假設**，不是三份不同的 Target。
