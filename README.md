# 螢幕側錄專案
```
本專案用於差異式電腦螢幕側錄，並可將電腦螢幕畫面儲存於NAS
```
## 側錄系統安裝
### 1.下載專案
```
git clone git@github.com:alexchenys/record_project.git
```
### 2. 執行安裝腳本(請使用最高權限)
```
cd C:\Users\users\Desktop\record_project
powershell -ep bypass
./install.ps1
```
### 3. 注意事項
- 預設NAS的ip位置為`\\192.168.7.90\vidoes`，請依據個人需求修正
- NAS儲存路徑不可使用密碼

## 側錄系統使用
- 安裝成功則會有一個畫面放置於右上方
- 可以到NAS查看側錄畫面，預設於設備關機時自動存入nas
- 如需即時影像可瀏覽`http://127.0.0.1:8000/videos`
"# record_project" 
