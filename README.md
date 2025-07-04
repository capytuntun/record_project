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
如果出現powershell無法辨識pip3的狀況，請執行
```
pip3 install -r C:\Users\users\Desktop\record_project\install\requirements.txt
```
### 3. 注意事項
- 預設NAS的ip位置為`\\192.168.63.200\Ret`，請依據個人需求修正
- NAS儲存路徑不可使用密碼

### 4. 可以將啟動腳本`record.bat`及停止腳本`stop_record.bat`放在桌面
- 啟動腳本: record_project\record.bat
- 停止腳本: record_project\stop_record.bat
## 側錄系統使用
- 安裝成功則會有一個畫面放置於右上方
- 可以到NAS查看側錄畫面，預設於設備關機時自動存入nas
- 如需即時影像可瀏覽`http://127.0.0.1:8000/videos`

