# product_research 比價 comp 篩選：能不能用 BM25 改善？(討論稿)

> 這份是給外部 LLM（GPT）討論用的自包含說明。讀者不需要 repo 存取權，
> 相關程式碼片段已內嵌。目標：評估「把市場比價 comp 的相似度篩選換成
> BM25 / 其他技術」能否解決目前觀察到的兩個失敗模式。

## 背景

`/research`（深度商品研究）會對一個 Mercari 商品，抓「現在在賣(active)」與
「已售出(sold)」的同類商品當作比價樣本(comp)，據此判斷「這個開價合不合理」。

抓回來的 comp 是雜的：同關鍵字會混進不同物件（單卡 vs 整盒、不同版本、
受損品、評級卡 PSA/BGS…）。所以進到價格結論前，會先用一個**標題相似度**
過濾，把「不是同一個可賣單位」的樣本丟掉。

目前這個過濾用的是一個**靜態詞彙相似度門檻 0.32**。

---

## ★ 問題陳述（先讀這段）

### 我們想改善的問題（What）
> **/research 的價格結論不可靠,因為「拿來比價的 comp 樣本」不乾淨。**
> 過濾器要嘛放太鬆讓「不是同一個可賣單位」的東西混進來(把均價拉歪),
> 要嘛收太緊把「其實是同一款、只是寫法/版本不同」的樣本誤殺(樣本不足無法判讀)。
> 目標:**讓進入價格判斷的 comp,盡量都是『同一個可賣單位』** —— 既不混雜訊、
> 也不誤殺同物。

### Root Cause（Why，根因，單一句）
> **comp 是否「同一個可賣單位」本質是『語意等價/實體比對』問題,
> 但目前只用『純詞彙、所有詞等權重、且帶 coverage 灌水』的字串相似度去近似它。**

根因拆解成三個可獨立修的子因:
- **R1 詞彙非語意**:用 token/字元重疊判斷,無法跨書寫系統或改寫對齊
  （「THE BOOK」≠「ザ・ブック」、「完全生産限定盤」≠「限定版」）→ 造成**誤殺(Mode 2)**。
- **R2 所有詞等權重**:高鑑別力詞（BOX / シュリンク / 完全生産限定盤）和
  通用詞（CD / 未開封）一樣重,單卡靠通用詞就能拿到分 → 造成**雜訊混入(Mode 1)**。
- **R3 coverage-max 灌水**:`score = max(jaccard, overlap/|cand_tokens|)`,
  短的「reference 子集」標題(單卡)coverage 很高 → 直接過關 → 放大**雜訊混入(Mode 1)**。

> 補充:後加的 MAD 數值離群過濾是**下游補救**,治不到根因 —— comp 一旦在
> 上游就選錯,數值層只能在被污染的分布裡硬剔極端值,門檻一保守就漏(如
> ¥3,900/¥5,500 未被剔除)。

### 希望達成的成功標準（Done 看什麼）
- **Mode 1**:黒炎 BOX 案,sold 樣本不再混入 ¥3,900/¥5,500 這類單卡 →
  sold 均價貼近真實整盒成交帶,結論不再偏空。
- **Mode 2**:YOASOBI CD 案,同專輯不同版/不同寫法的 comp 不被誤殺 →
  active/sold 樣本數足以判讀(>1)。
- **不變的硬約束**:不增加對外查詢次數(不可封 IP);過濾失敗要走安全網
  (沿用原樣本、不丟光);符合 Rule G(純統計或 LLM,不硬編碼關鍵字/別名表)。

> 對應關係:本文評估的各方案,實際上是在問「**用什麼技術去近似 R1/R2/R3
> 這三個根因**」—— BM25/IDF 主要打 R2/R3,語意層(embedding/LLM)才打 R1。

---

## 觀察到的兩個失敗模式（來自兩次真實實跑）

### Mode 1 — 雜訊混入（門檻太鬆 → 結論偏掉）
真實案例：寶可夢卡「黒炎の支配者 BOX 未開封 シュリンク付き」(開價 ¥14,800)。
- sold 樣本裡混進 **¥3,900 / ¥5,500** 兩筆，幾乎不可能是同款未開封整盒
  （active 全部在 ¥19,000+），比較可能是單卡或誤標。
- 這兩筆把 sold 均價拉低到 ¥11,832 → 結論變成「開價高於 sold 均價約 28%」
  （偏空、可能誤導）。
- 我們後加的數值離群過濾(MAD, 修正 z-score, 門檻 3.5)在這種真實寬幅分布下
  **太保守**，只剔掉 1 筆最極端 active，沒擋住低價 sold。
  → 治標不治本，真正的洞在**上游詞彙相似度讓非整盒混進來**。

### Mode 2 — 同款不同版被誤殺（門檻太嚴 → 樣本不足）
真實案例：J-pop CD「YOASOBI THE BOOK II 完全生産限定盤 バインダー入CD」
(開價 ¥4,400)。
- 查詢太細 + 0.32 純詞彙相似度，把同專輯其他版本/寫法都濾掉
  → active 只剩 **1 筆**、sold **0 筆**，比價段幾乎無法判讀。
- 跨書寫系統/改寫是死角：「THE BOOK」vs「ザ・ブック」、
  「完全生産限定盤」vs「限定版」詞彙零重疊。

**兩個模式方向相反，但都源自上面的根因：** Mode 1 來自 R2(等權重)+R3
(coverage 灌水);Mode 2 來自 R1(純詞彙非語意)。下游 MAD 只是補救、治不到根因。

## 目前的相似度實作（已內嵌，供精確評估）

過濾主函式（簡化重點）：

```python
def _filter_market_items_for_price(*, reference_title, items, min_similarity):
    specific_tokens = _specific_reference_tokens(reference_title)
    anchor_tokens = set(specific_tokens[1:] if len(specific_tokens) >= 2 else specific_tokens)
    for item in items:
        title = item["title"]
        if _looks_graded_title(title) and not _looks_graded_title(reference_title):
            drop; continue                      # 評級卡(PSA/BGS) vs 生品 直接丟
        candidate_tokens = set(_market_title_tokens(_normalize_market_title(title)))
        if anchor_tokens and not (anchor_tokens & candidate_tokens):
            drop; continue                      # anchor-token AND 閘門
        if _title_similarity_score(reference_title, title) < min_similarity:  # 0.32
            drop; continue
        keep
```

相似度分數：

```python
def _title_similarity_score(reference, candidate):
    ref, cand = normalize(reference), normalize(candidate)
    if ref == cand: return 1.0
    ref_tokens, cand_tokens = set(tokens(ref)), set(tokens(cand))
    token_score    = |ref∩cand| / |ref∪cand|          # token Jaccard
    token_coverage = |ref∩cand| / |cand_tokens|        # 不對稱 coverage
    bigram_score    = char_bigram Jaccard(ref, cand)    # 日文無空格用字元 bigram
    bigram_coverage = char_bigram overlap / |cand_bigrams|
    containment_bonus = 0.15 if (ref in cand or cand in ref) else 0
    score = max(
        token_score*0.55    + bigram_score*0.45,
        token_coverage*0.55 + bigram_coverage*0.45,    # ← coverage 路徑
    )
    return min(1.0, score + containment_bonus)
```

**目前公式的兩個關鍵弱點：**
1. **所有 token 等權重** —— 「CD / BOX / 未開封」與「黒炎の支配者 /
   完全生産限定盤」一樣重，鑑別力高的詞沒有被加重。
2. **`max(jaccard, coverage)` 的 coverage 路徑會灌水** —— 一個「短的、
   是 reference 子集」的標題（如單卡）coverage 很高 → 過關。這是 Mode 1
   雜訊混入的直接元兇。

## BM25 分析（核心結論）

BM25 = **IDF 加權 + TF 飽和 + 文件長度正規化**，三件事。

### 對短標題而言，BM25 只有「IDF」真正在做事
marketplace 標題都很短、同一詞極少在標題內重複出現：
- **TF 飽和**幾乎不作用（詞頻幾乎都是 1）。
- **長度正規化**幾乎不作用（所有標題長度相近）。
- 只剩 **IDF（逆文件頻率）加權**是有效成分。

→ 在這個場景，「引入 BM25」實際上 ≈「**給現有 token 重疊加 IDF 權重，
並拿掉 coverage-max 灌水**」。與其拉一個 BM25 套件，不如直接改公式。

### BM25/IDF 能治 Mode 1（雜訊混入）
單卡標題缺了「BOX / シュリンク / 完全生産限定盤」這些**高 IDF 鑑別詞**。
BM25 只加總「文件中出現的 query 詞」，缺高 IDF 詞 → 總分低，不像現在的
coverage-max 會被短標題灌高 → **能把單卡這類雜訊壓下去。**

> IDF 需要 document frequency。8 筆候選算 IDF 太不穩，建議用一張
> **背景 DF 表**（從歷次市場爬蟲累積），純統計、可持久化、確定性。

### BM25/IDF 治不到 Mode 2（同款不同版誤殺）
BM25 仍是 **bag-of-words 詞彙比對**。跨書寫系統/改寫零詞彙重疊 → 零分，
跟現在同一個盲點，而且**更嚴格只會讓 Mode 2 更糟**。
這個模式只有**語意**能解（embedding 相似度 或 LLM 閘門）。

## 建議架構：retrieve-then-rerank（不是二選一）

1. **粗排 = BM25/IDF（其實是 IDF 加權 token 重疊 + 去掉 coverage 灌水）**
   - 便宜、確定性、可單測、無 LLM 延遲。
   - 負責 Mode 1：把明顯雜訊（單卡、受損、明顯不同物）壓到低分砍掉。
2. **細排 = LLM 語意閘門**
   - 重用既有 `filter_relevant_sources_with_ollama`（本地 Ollama qwen3:14b,
     `format:"json"`, 回 `{"keep":[...]}`, 失敗/空 → 安全網全留）。
   - 對粗排後 top-K 做最終 keep/drop，判「單卡 vs 整盒」「不同版同物」。
   - 負責 Mode 2：語意等價判斷。

這樣**品質交給 LLM、省 token 靠 BM25 先縮池**，符合既有優先序
（①正確/品質 ②不被封 ③省 token ④速度）。

## 候選做法比較（給 GPT 評估用）

| 方案 | 治 Mode 1 | 治 Mode 2 | 成本/延遲 | 確定性 | 備註 |
|---|---|---|---|---|---|
| A. 現況(0.32 詞彙) | 差 | 差 | 極低 | 高 | baseline |
| B. 只調門檻/MAD | 微 | 微 | 極低 | 高 | 治標 |
| C. BM25/IDF 粗排取代相似度 | 好 | 差(或更糟) | 低 | 高 | 只動詞彙層 |
| D. 純 embedding 相似度 | 中 | 好 | 中 | 中 | 需向量模型/門檻校準 |
| E. 純 LLM 語意閘門 | 好 | 好 | 高(每筆 LLM) | 低 | 品質最佳、貴 |
| **F. BM25/IDF 粗排 + LLM 細排** | **好** | **好** | 中 | 中 | **建議** |

## 需要和 GPT 釐清的開放問題

1. 短標題場景，BM25 的 TF 飽和/長度正規化是否真的可忽略？是否有反例
   （例如標題堆關鍵字 spam，TF 飽和反而有用）？
2. IDF 的 DF 來源：用「當批候選池」vs「歷史背景語料」哪個更穩？冷啟動怎麼辦？
3. 日文 tokenization：現在用字元 bigram。BM25 該配形態素分析(MeCab/Sudachi)
   還是 n-gram？對「完全生産限定盤」這種複合詞影響多大？
4. Mode 2 的跨書寫系統(漢字/片假名/羅馬字)等價，embedding 夠不夠？還是一定
   要 LLM？有沒有便宜的中間方案（如 reading 正規化 / 別名表）？
   （限制：本專案 **Rule G** —— 不准維護硬編碼關鍵字/別名清單，辨識一律走
   LLM+RAG；所以「手刻別名表」這條被排除。）
5. retrieve-then-rerank 的 K 該多大？LLM 細排每次 /research 多打幾次本地模型
   可接受（速度是最低優先），但要避免把「同款不同版」在粗排階段就誤殺
   （粗排要刻意放寬、把判斷權留給語意層）。

## 專案約束（讓 GPT 的建議落地用）

- 本地 Ollama qwen3:14b @ `http://127.0.0.1:11434`，已有 JSON 模式呼叫。
- **Rule G**：開放世界辨識,不准硬編碼關鍵字/別名清單(BM25/IDF/MAD 這類
  純統計方法是乾淨的；手刻「BOX 必含シュリンク」這種規則則違規)。
- 比價資料是 Mercari 爬蟲，**每日自動查詢必須維持個位數,不可觸發 IP 封鎖** —— 
  任何方案都不該增加對外查詢次數（BM25/LLM 都在本地、不增查詢，OK）。
- 既有安全網語意：閘門回空 / 例外 → 沿用原樣本，不可因過濾失敗而丟光。

---

## 附錄：真實 log 摘錄（關鍵證據，標註對應根因）

> 以下為兩次真實 `/research` 端到端實跑（打真實 Mercari）的原始輸出節錄。
> 這是「問題真的存在」的一手證據,GPT 可直接據此推理。

### A. Mode 1 證據 — 黒炎 BOX（門檻太鬆 → 單卡混入 → 結論偏空）

商品頁（reference）：
```
未開封 シュリンク付き 1 BOX / ¥14,800 / 狀態 未使用に近い
標題：【新品】ポケモンカードゲーム 黒炎の支配者 未開封 シュリンク付き 1 BOX
```

合理市價分析輸出（注意被選為 comp 的最低兩筆）：
```
目前開價高於同條件（新品） sold 均價約 28%；
結論依據 7 筆 sold comp：
  ¥3,900  https://jp.mercari.com/item/m40574829670   ← 疑似單卡/非整盒
  ¥5,500  https://jp.mercari.com/item/m44243846283   ← 疑似單卡/非整盒
  ¥10,200 https://jp.mercari.com/item/m68380420635 …(+4)
賣家開價 ¥14,800；
Mercari sold 樣本 8 筆，均價約 ¥11,832；
  ・新品 sold 7 筆，中位數 ¥10,800，區間 ¥3,900–¥23,800   ← 區間寬到 6 倍
active 樣本 7 筆，中位數 ¥20,000，區間 ¥19,000–¥22,100     ← active 全在 ¥19k+
```

warnings：
```
- active 候選再排除了 1 筆價格離群樣本（MAD）。   ← MAD 只剔到 active 1 筆
- sold 候選排除了 1 筆低相關樣本。
- active 候選排除了 1 筆低相關樣本。
```

**判讀**：active 全部 ¥19,000+,但 sold 卻收到 ¥3,900/¥5,500 —— 這兩筆幾乎
不可能是同款未開封整盒,卻通過 0.32 詞彙門檻進到 sold,把均價從整盒實價
(~¥14k 帶)拉到 ¥11,832,結論變「開價偏高 28%」。
→ **直接對應 R2（高鑑別詞「BOX/シュリンク」沒被加重,單卡靠共同詞過關）+
R3（短的單卡標題 coverage 高 → 過關）。** MAD(下游)門檻 3.5 太保守,沒救回來。

### B. Mode 2 證據 — YOASOBI CD（門檻太嚴 → 同款不同版誤殺 → 樣本不足）

商品頁（reference）：
```
未開封 YOASOBI THE BOOK II 完全生産限定盤 バインダー入CD / ¥4,400 / 新品、未使用
```

合理市價分析輸出：
```
[partial] confidence=0.35 sample=1
賣家開價 ¥4,400；active 樣本 1 筆（mercari 1筆 ¥4,400），中位數 ¥4,400，區間 ¥4,400–¥4,400
```

warnings：
```
- Mercari sold 價目前只拿到平均值接口；此查詢未回傳可用 sold avg。
- active 樣本少於 3 筆，市價判讀可信度有限。
- sold 樣本少於 2 筆，流動性判讀可信度有限。
```

**判讀**：這張專輯在 Mercari 明明有大量掛單,但「完全生産限定盤 バインダー入CD」
這種很細的寫法 + 0.32 純詞彙門檻,把同專輯其他寫法/版本全濾掉,active 只剩
商品自己 1 筆、sold 0 筆,比價段幾乎無法判讀。
→ **直接對應 R1（跨書寫系統/改寫零詞彙重疊:「THE BOOK」vs「ザ・ブック」、
「完全生産限定盤」vs「限定版」無法對齊）。** 純詞彙手段(含 BM25)在此只會更嚴格。

### C. 對照組（證明搜尋/語意路徑可行,給架構參考）

同一次 YOASOBI 跑,**增值潛力分析**走的是「web 搜尋 + LLM 語意摘要」路徑,
即使知識庫完全沒命中,仍從真實零售頁(tower.jp / sonymusicshop / amazon.co.jp)
回出對的、on-topic 的摘要。→ 佐證:**語意層在這個資料上是有效的**,所以
「BM25 粗排 + LLM 語意細排」的細排端有現成可行性。

