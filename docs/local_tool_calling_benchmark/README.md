# Local Tool Calling Benchmark

Purpose: test whether local Ollama models can reliably act as OpenClaw's tool
selector before changing the production chat architecture.

This benchmark deliberately uses Ollama's native `/api/chat` tool-calling path,
not OpenCode and not the OpenAI-compatible proxy. The prior OpenCode experiment
showed that provider/tool-call compatibility can fail before the model's actual
tool ability is measured.

## What It Tests

- zero-tool chat: the model should answer directly.
- single tool: the model should call one deterministic fixture tool.
- two-step tool flow: the model should call search first, then detail lookup.
- pronoun follow-up: the model should use prior chat context when selecting
  search arguments.

The fixture tools are deterministic and offline:

- `get_weather(city)`
- `search_music(artist, limit)`
- `get_song_detail(title)`

## Run

```bash
cd /Users/jen/ai_work_space/related_to_claw/aka_no_claw
python3 docs/local_tool_calling_benchmark/run_benchmark.py
```

For a faster smoke test:

```bash
python3 docs/local_tool_calling_benchmark/run_benchmark.py --models qwen3:4b qwen2.5-coder:7b
```

To test whether OpenClaw can force unfinished multi-step tasks to continue:

```bash
python3 docs/local_tool_calling_benchmark/run_benchmark.py \
  --models qwen3:14b \
  --timeout 120 \
  --max-rounds 6 \
  --subgoal-gate
```

Outputs:

- `latest_results.json`
- `latest_results.md`
- timestamped `results_*.json` / `results_*.md`

## Acceptance Bar Before Filing An Architecture Issue

A local model should meet all of these before it is worth proposing a production
chat backend change:

- At least 3/4 benchmark cases pass.
- No-tool case must not call tools.
- Multi-step case must call tools in the expected order.
- Mean latency should be tolerable for web chat on the Mac mini target.
- Failure mode must be structured and diagnosable, not empty content or raw JSON.
- Multi-step tasks should pass both with correct tool order and no fabricated
  detail after a subgoal gate intervention.

## Reference Notes

- Ollama native `/api/chat` supports `tools` and returns `message.tool_calls`.
- Ollama's docs describe the application-owned agent loop: model emits tool
  calls, the application executes tools, then sends tool results back.
- Qwen docs emphasize templates and inference backend details for function
  calling; Qwen3 may require thinking-mode control.
- Docker's practical local model benchmark found Qwen models strong for tool
  calling, but with latency tradeoffs.
