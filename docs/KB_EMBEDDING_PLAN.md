# KB Embedding 檢索 — 落地計畫

狀態：設計確定，實作中。最後更新 2026-06-16。
Spike：`spikes/embedding_retrieval/`（可獨立重跑，`rm -rf` 即移除）。

## 1. 動機與實測

龍蝦知識庫目前的檢索是**字元層級 lexical 比對**（substring / char-bigram），
抓不到跨語近義（「彩虹社」↔「にじさんじ」、「藍色牢籠」↔「藍色監獄」、
「滿分」↔「PSA10 完美品相」）。在 10 題實測（149 entries）上：

| 檢索方式 | hit@1 | hit@3 | MRR | 查詢延遲 |
|---|---|---|---|---|
| lexical（現況） | 6/10 | 8/10 | 0.703 | 7 ms |
| nomic-embed-text（英文為主） | 3/10 | 4/10 | 0.378 | 229 ms |
| **bge-m3（多語）** | **9/10** | **10/10** | **0.950** | 63 ms |

結論：**模型選對（多語 bge-m3）才有意義**；英文模型反而比 lexical 差。
採用 `bge-m3`（維度 1024，向量常駐約 0.6 KB/筆）。

## 2. 範圍與策略（已與使用者確認）

- **範圍**：實體 KB（`knowledge_entries`+`entity_aliases`）**與** Codegen KB
  （`codegen_knowledge`，`/new` 用）兩張表都加 embedding。
- **消費端策略**：**exact 先行、embedding fallback**。既有精確/substring 命中
  完全不動；只在命中不足時用向量 top-k 補位。純加分，不破壞現況。
- **寫入端失敗策略**：**best-effort + log**。embedding 算失敗（Ollama 掛/逾時）
  **不可**擋住 KB 寫入；記 warning，靠 backfill 補。KB 寫入永遠成功。

## 3. 寫入端盤點（全部收斂到 3 個 chokepoint）

`knowledge_db.py`：`upsert_entry`(L183)、`add_alias`(L251)、`delete_entry`(L339)，
另加 codegen 的 `upsert_codegen_knowledge`(L423)、`delete_codegen`(L347)。
上游 6 模組只呼叫這些方法，**本身零改動**：

| 模組 | 動作 |
|---|---|
| `entity_researcher.py` | upsert ×3 |
| `research_command.py` | upsert |
| `knowledge_command.py` | upsert / add_alias / delete |
| `yuyutei_code_resolver.py` | upsert |
| `sns_monitor_service.py` | upsert / add_alias / delete |
| `rag_daily_digest.py` | delete |

## 4. 消費端盤點

實體 KB：
| 模組 | 方法 | embedding 介入 |
|---|---|---|
| `research_command._lookup_appreciation_entries`(L940) | all_aliases→substring→get_entry | ⭐ 主要：substring 不足時 embedding fallback |
| `sns_tools.py`(L113) / `yuyutei_code_resolver.py`(L132) / `entity_researcher.py`(L188) | get_entry | PK 精確查，**不動** |
| `knowledge_command.py` | recent/get/lookup | 顯示+增刪，**不動** |
| `rag_daily_digest.py`(L114) | entries_since | 時間掃描，**不動** |

Codegen KB：
| 模組 | 方法 | embedding 介入 |
|---|---|---|
| `dynamic_tools.py`(L1508/1525) | retrieve_codegen_knowledge | lexical 不足 k 筆時，embedding 補滿剩餘名額 |

## 5. 設計

### 5.1 注入式 embedder（可拔關鍵）

`KnowledgeDatabase(path, embedder=None)`。`embedder` 是
`Callable[[str], list[float] | None]`，**預設 None = embedding 全關**，行為與今日
完全一致。production 由組裝層注入一個包了 Ollama `bge-m3` 的 embedder；
測試注入假 embedder（不依賴 Ollama）。embedder 自帶 `model` 與 `dim` 屬性。

### 5.2 統一向量表（additive，可整張 DROP）

```sql
CREATE TABLE IF NOT EXISTS embeddings (
    kind       TEXT NOT NULL,   -- 'entry' | 'codegen'
    ref_id     TEXT NOT NULL,   -- entity_canonical 或 knowledge_id
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,    -- float32 little-endian bytes
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, ref_id)
);
```

讀取時 `model`/`dim` 與當前 embedder 不符的列視為 miss（換模型只需重跑 backfill）。

### 5.3 待 embedding 的文字

- entry：`canonical | 別名... | summary`（與 spike 一致；故 **add_alias 也要 re-embed**）。
- codegen：`title | technique | keywords...`。

### 5.4 寫入掛勾（best-effort）

- `upsert_entry` / `add_alias` 成功 commit 後 → 重算該 entry 向量 → upsert `embeddings`。
- `delete_entry` → 連帶刪該 entry 向量列。
- `upsert_codegen_knowledge` → 重算 codegen 向量；`delete_codegen` → 刪列。
- 任一步 embedder 回 None 或丟例外 → log warning、跳過、**不影響主寫入**。

### 5.5 消費掛勾（exact 先行 + fallback）

- 新增 `search_semantic(kind, query, k)`：對該 kind 的向量做 cosine top-k。
- `_lookup_appreciation_entries`：先跑現有 substring；結果 < 3 才用
  `search_semantic('entry', …)` 補到 3，去重、排在精確命中之後。
- `retrieve_codegen_knowledge`：先跑現有 lexical；不足 k 筆才用
  `search_semantic('codegen', …)` 補滿剩餘名額。

### 5.6 Backfill

維護腳本 `spikes/embedding_retrieval/backfill.py`（或 `scripts/`）：對現有
149 entries + 55 codegen 全量補 embedding；冪等（model/dim 相符即跳過）。

## 6. 成本

每次寫入多一次 Ollama 呼叫（bge-m3 約 60 ms/筆），寫入低頻、可接受。
查詢端 fallback 只在命中不足時觸發，多一次 query embed（約 60 ms）+ numpy cosine。

## 7. 測試計畫

- 單元（不依賴 Ollama，用假 embedder）：
  - upsert_entry/add_alias 後 `embeddings` 有對應列且 model/dim 正確。
  - delete_entry 後向量列消失。
  - embedder 丟例外時 upsert 仍成功（best-effort）。
  - `search_semantic` 回傳依 cosine 排序；model 不符的列被忽略。
  - 消費端 fallback：substring/lexical 命中時不觸發向量；不足時才補位、且排序在後。
- 環境閘控的 live 測試（需本機 bge-m3）：重跑 spike 的 10 題確認 hit@1 ≥ 8。

## 8. Rollback

1. 建構時 `embedder=None` → 立即停用，回到純 lexical。
2. `DROP TABLE embeddings` → 完全移除，主資料不受影響（向量表 additive）。
3. `rm -rf spikes/embedding_retrieval/` → 移除 spike 與 backfill。
