# Liquidity / Trend Methodology

Last updated: 2026-04-17

這份文件說明 dashboard 上 `Pokemon Liquidity Board` 與 `WS Liquidity Board` 目前怎麼排序，以及為什麼這次把邏輯從「單一店家買盤 proxy」改成「近期交易熱度 + 買方承接 + SNS 注意力」的混合模型。

## 1. 先講結論

目前榜單不再把 `listing_count` / `在庫数` / `出品数` 當主分數。

原因很直接：

- 供給很多，不代表真的好賣
- 很多掛單可能只是價格偏離、長時間沒成交、或不同品況重複上架
- 如果榜上完全沒有 `ピカチュウ` 或 `リザードン` 這類明顯有持續交易熱度的卡，代表模型太偏單一店家觀點

所以現在的排序改成三層：

1. `recent market activity`
2. `buy-side support`
3. `SNS attention`

## 2. 為什麼要改

前一版最大的問題有兩個：

1. 候選池太窄  
Pokemon 候選主要來自單一店家的高稀有單卡頁，容易偏向當期或特定分類。

2. 分數太依賴單一店家的買盤  
如果只看某一家店的 bid / ask，很容易把「店家很好收」誤當成「全市場最熱」。

這會導致：

- 一些最近實際交易很活躍的卡沒進前列
- `ピカチュウ`、`リザードン` 這種跨期、跨系列都長期有需求的卡被低估

## 3. 現行資料來源

### 3.1 Pokemon

Pokemon 現在會同時看：

- SNKRDUNK 月交易數排名  
  `https://snkrdunk.com/articles/31649/`
- SNKRDUNK UR 類別交易排名  
  `https://snkrdunk.com/articles/31962/`
- SNKRDUNK SA 類別交易排名  
  `https://snkrdunk.com/articles/31708/`
- Cardrush Pokemon 類別頁  
  `https://www.cardrush-pokemon.jp/product-group/22?sort=rank&num=100`
- magi Pokemon 頁面  
  `https://magi.camp/brands/3/items`
- 遊々亭賣價 / 買取頁
- Yahoo!リアルタイム検索

### 3.2 WS

WS 的資料條件仍然比 Pokemon 差，因為還沒有像 Pokemon 月交易榜那樣穩定、廣域、且可長期固定沿用的單一排行榜。

但 WS 現在已經不再只是 `magi + 遊々亭 + SNS`：

- SNKRDUNK WS 年度賣上排行  
  `https://snkrdunk.com/articles/26509/`
- SNKRDUNK 近期 WS 初動相場文章  
  目前已接入：
  - `https://snkrdunk.com/articles/31956/`
  - `https://snkrdunk.com/articles/31830/`
- magi Weiss Schwarz 頁面  
  `https://magi.camp/series/7/products`
- 遊々亭賣價 / 買取頁
- Yahoo!リアルタイム検索

這代表：

- Pokemon 榜單仍然更偏向「最近真實交易熱度 + 流動性」
- WS 榜單已經開始往「多來源市場活動 + 買盤承接 + 社群注意力」靠攏，不再只是單一店家買盤 proxy

## 4. 候選池怎麼建立

### 4.1 Pokemon

Pokemon 候選池會把下列來源合併後再去重：

- SNKRDUNK 月交易數榜單
- SNKRDUNK 類別交易榜單
- Cardrush 類別頁
- magi 頁面

這一步的目的是先回答：

- 最近市場上哪些卡真的有人交易
- 哪些卡不是只有某單一頁面短暫冒出來

### 4.2 WS

WS 候選池現在會把下列來源合併後再去重：

- SNKRDUNK WS 年度賣上排行
- SNKRDUNK 近期 WS 初動相場文章
- magi Weiss Schwarz 頁面

之後再用遊々亭買盤與 SNS 做承接驗證，而不是只靠 magi 單頁面起候選。

## 5. 三層分數

### 5.1 Recent Market Activity

這是現在最重要的一層。

Pokemon 會把近期交易榜單視為高可信度信號：

- 月交易數榜單權重最高
- 類別交易榜單次之
- 店家頁順序只算低權重輔助

同一張卡如果同時出現在多個獨立來源，會得到 `source diversity bonus`。

這層想回答的是：

- 最近是不是很多人真的在交易這張卡
- 它是不是不只出現在單一來源

### 5.2 Buy-Side Support

這層主要看遊々亭：

- 有沒有可見買取價
- 最佳賣價和最佳買取價有多接近
- `bid / ask` 比例
- 是否有 `priceup` 之類的明確加價訊號

`buy_support_ratio` 目前仍然沿用舊公式：

```text
0.35 if a bid exists
+ 0.50 * min(1.0, bid / ask) when both bid and ask exist
+ 0.15 when both bid and ask exist
+ small momentum boost when the store explicitly marks the bid as raised
```

這層想回答的是：

- 如果今天要賣，有沒有像樣的承接
- 市場是不是只剩賣方，還是買方也在

### 5.3 SNS Attention

這層目前用：

- Yahoo!リアルタイム検索 的匹配貼文數
- 可見互動量

SNS 只當輔助，不允許單獨主導排序。

原因是：

- 社群聲量常常有雜訊
- 話題很大不代表成交也很大

## 6. 最終分數

目前最終 `hot_score` 是：

```text
hot_score =
  market_activity_score * 0.50
+ buy_support_score   * 0.45
+ attention_score     * 0.05
+ small raw-card fungibility bonus
```

幾個重點：

- `market_activity_score` 現在權重最高
- `buy_support_score` 很高，但不再單獨主導整個榜
- `attention_score` 只剩 5%，避免 SNS 把結果帶偏
- raw 卡會有小幅加分，graded 不會

## 7. 排序順序

目前實作上的排序優先順序是：

1. `hot_score`
2. `market_activity_score`
3. `buy_support_score`
4. `attention_score`
5. raw 優先於 graded
6. 來源內原始排名作為最後 tie-breaker

## 8. 這樣修正後，為什麼會更合理

這次改動的核心是：

- 不再把「單一店家很好收」誤當成「全市場最熱」
- 把「最近真有交易」拉回主分數
- 保留買盤承接，避免排行榜只剩純話題卡
- 把 SNS 壓到輔助層，避免社群雜訊主導

以 2026-04-17 的 live 檢查為例，新榜單前十已經重新出現：

- `ピカチュウex SAR [M2a 234/193]`
- `メガリザードンXex MA [M2a 223/193]`
- `メガリザードンXex SAR [M2 110/080]`

這比前一版完全看不到 `ピカチュウ` / `リザードン` 明顯合理得多。

## 9. 目前限制

還是有幾個限制要誠實寫清楚：

- WS 目前缺少像 Pokemon 那樣強的近期交易榜資料
- SNKRDUNK 文章頁不是正式 API，HTML 結構未來可能變
- 遊々亭買盤仍然只代表單一店家的買方需求
- Yahoo!リアルタイム検索 只能當注意力 proxy，不是成交證據

## 10. 下一步最值得做的事

如果之後要再把合理性往上推，優先順序會是：

1. 接更多公開且穩定的 `recent sold` / `recent trades` 來源
2. 把多來源交易訊號做時間衰減
3. 對 Pokemon / WS 分別做更專門的權重，而不是共用同一套
4. 讓 dashboard 顯示每張卡命中的交易來源摘要

## 11. 本次參考來源

- SNKRDUNK 月交易數榜單  
  https://snkrdunk.com/articles/31649/
- SNKRDUNK UR 交易榜單  
  https://snkrdunk.com/articles/31962/
- SNKRDUNK SA 交易榜單  
  https://snkrdunk.com/articles/31708/
- 遊々亭買取說明  
  https://img.yuyu-tei.jp/sp/info/buy_10.php
- Yahoo!リアルタイム検索  
  https://search.yahoo.co.jp/realtime
- IMF liquidity measurement discussion  
  https://www.imf.org/en/Publications/WP/Issues/2016/12/30/Measuring-Liquidity-in-Financial-Markets-16211
