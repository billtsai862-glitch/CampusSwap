# CampusSwap 校園智慧二手交易平台

這是 CampusSwap 第二週網站初版，使用 Python Flask 與 SQLite 製作。

## 目前功能
- 會員註冊、登入、登出
- 密碼雜湊儲存
- 商品刊登、商品列表及商品詳細資訊
- 依商品名稱或分類搜尋
- SQLite 儲存會員及商品資料

## 本機執行
1. 安裝 Python 3。
2. 在此資料夾開啟終端機。
3. 執行 `python -m pip install -r requirements.txt`
4. 執行 `python app.py`
5. 瀏覽器開啟 `http://127.0.0.1:5000`

## Render 部署
- Build Command：`pip install -r requirements.txt`
- Start Command：`gunicorn app:app`
- Environment Variable：新增 `SECRET_KEY`，值請設定成一組自己的長隨機字串。

目前使用 SQLite。Render 免費服務的檔案系統可能在重新部署或重啟後重置，因此目前資料適合課堂展示，不保證永久保存。若要長期保存資料，之後需改用持久化資料庫。

## 尚未完成
目前尚未加入商品圖片上傳、收藏、站內訊息、付款及真實交易流程。
