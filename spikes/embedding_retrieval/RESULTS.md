# Embedding 檢索 spike — 修改前後效能比較

資料：龍蝦知識庫 `knowledge.sqlite3`（10 題測試 / 149 entries，含中日英別名）。
本 spike 只讀庫、未接 production，可隨時 `rm -rf spikes/embedding_retrieval/` 拔掉。

## 總分（lexical = 現況；embedding = 提案）

| 指標 | lexical | nomic-embed-text | bge-m3 |
|---|---|---|---|
| hit@1 | 6/10 | 3/10 | 9/10 |
| hit@3 | 8/10 | 4/10 | 10/10 |
| MRR | 0.703 | 0.378 | 0.950 |
| 查詢延遲 | 7.0ms | 229.2ms | 63.1ms |

- `nomic-embed-text`：維度 768，建索引 0.0s，向量常駐 447 KB
- `bge-m3`：維度 1024，建索引 24.8s，向量常駐 596 KB

## 逐題名次（數字=正解排第幾名，— = 前 149 名外）

| # | 類型 | 查詢 | 正解 | lexical | nomic-embed-text | bge-m3 |
|---|---|---|---|---|---|---|
| 1 | exact | ブルーロック | blue_lock | 1 | 47（top1=r） | 1 |
| 2 | semantic | 彩虹社那位扮成年輕教授的VTuber是誰 | オリバー・エバンス | 14（top1=r） | 1 | 1 |
| 3 | semantic | 足球題材 名字叫藍色牢籠的那部漫畫 | blue_lock | 1 | 37（top1=1-108巻） | 1 |
| 4 | semantic | 獨自一人變強的那部韓國網漫 | sololeveling | 1 | 2（top1=全国予約店舗一覧） | 1 |
| 5 | exact | 咒術迴戰 | jujutsu | 1 | 28（top1=ブラックボルト） | 1 |
| 6 | semantic | 賣模型手辦的萬代官方限定通販網站 | プレミアムバンダイ | 3（top1=instagram） | 1 | 1 |
| 7 | variant | 怪獸八號 動畫第二季 | kaiju8 | 1 | 6（top1=エナジーマーカー:ゴジータ…） | 1 |
| 8 | semantic | PSA評級拿到滿分的卡 | psa10 | 8（top1=ピカチュウex sar […） | 64（top1=ビーデル） | 1 |
| 9 | variant | 東北地區限定的皮卡丘卡盒 | トウホクのピカチュウ p [s… | 2（top1=フクオカのピカチュウ p …） | 1 | 2（top1=フクオカのピカチュウ p …） |
| 10 | exact | ドラゴンスター | ドラゴンスター | 1 | 88（top1=r） | 1 |
