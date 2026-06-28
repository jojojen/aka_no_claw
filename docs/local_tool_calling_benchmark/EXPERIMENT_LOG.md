# Local Tool Calling Experiment Log

Last reviewed: 2026-06-24
Status: Current
Owner area: agent-maintenance

## Question

Can OpenClaw use local models as stable tool selectors for chat, instead of
depending on the cloud Big Pickle path?

This experiment focuses on Ollama native `/api/chat` tool calling because the
OpenCode + local Ollama experiment failed before tool ability could be measured:
the 7B model printed tool JSON as normal text, and `qwen3:14b` timed out through
the OpenAI-compatible route.

## Setup

- Machine: local Mac mini class environment, 16 GB memory constraint.
- Endpoint: `http://127.0.0.1:11434/api/chat`
- Harness: [run_benchmark.py](run_benchmark.py)
- Cases: [cases.json](cases.json)
- Latest result: [latest_results.md](latest_results.md)
- Baseline run: [results_20260624T030509Z.md](results_20260624T030509Z.md)
- Subgoal-gate run: [results_20260624T031051Z.md](results_20260624T031051Z.md)
- Gemma tool-support check: [results_20260624T032416Z.md](results_20260624T032416Z.md)
- Strict dependency-validation run: [results_20260624T142241Z.md](results_20260624T142241Z.md)
- Realistic music probe harness: [run_realistic_probe.py](run_realistic_probe.py)
- Latest realistic probe: [latest_realistic_probe.md](latest_realistic_probe.md)
- Realistic fixture-web run: [realistic_probe_20260624T032155Z.md](realistic_probe_20260624T032155Z.md)
- Realistic live-web run: [realistic_probe_20260624T032222Z.md](realistic_probe_20260624T032222Z.md)

Models tested:

- `qwen3:4b`
- `qwen2.5:0.5b`
- `qwen2.5-coder:7b`
- `qwen3:14b`
- `gemma3:12b`

The harness sends deterministic fixture tools to Ollama:

- `get_weather(city)`
- `search_music(artist, limit)`
- `get_song_detail(title)`

The harness then runs an application-owned agent loop:

1. Send user prompt, history, and tool schemas.
2. If `message.tool_calls` exists, execute fixture tools.
3. Append tool results as `role=tool`.
4. Ask the model again until it returns final content or reaches max rounds.

Quality failures are counted even when the task partially succeeds:

- `leaked_thinking`: the final answer exposes `<think>` / `</think>`.
- `raw_json_tool_text`: the model prints a tool call JSON as normal text instead
  of returning structured `message.tool_calls`.
- `invalid_tool_dependency`: a later tool did not use required data from an
  earlier tool result, for example calling `get_song_detail("第一首歌")` instead
  of `get_song_detail("Lemon")` after `search_music` returned `Lemon` as rank 1.

## Results

### Baseline: no subgoal gate

| Model | Passed | Total | Pass rate | Mean seconds | Main failure |
| --- | ---: | ---: | ---: | ---: | --- |
| `qwen3:4b` | 0 | 4 | 0% | 16.1 | thinking leaked; multi-tool calls not emitted |
| `qwen2.5-coder:7b` | 1 | 4 | 25% | 2.6 | prints raw tool JSON as text |
| `qwen3:14b` | 3 | 4 | 75% | 7.2 | stops after first tool in a two-step task |

### With unfinished-subgoal gate

| Model | Passed | Total | Pass rate | Mean seconds | Main observation |
| --- | ---: | ---: | ---: | ---: | --- |
| `qwen3:14b` | 4 | 4 | 100% | 12.2 | one corrective intervention fixed the two-step case |

The gate detected that `get_song_detail` was still missing after the model tried
to finalize the two-step music answer. It sent this corrective message:

```text
你尚未完成必要工具 `get_song_detail`。不要編造缺少的資料，請先呼叫下一個必要工具；工具結果回來後再給最終答案。
```

The model then called `get_song_detail` and produced a grounded final answer.

### Tool-support check

| Model | Passed | Total | Pass rate | Mean seconds | Main observation |
| --- | ---: | ---: | ---: | ---: | --- |
| `gemma3:12b` | 0 | 4 | 0% | 0.1 | Ollama rejects `tools`: model does not support tools |

`gemma3:12b` is installed locally, but Ollama returns this error for every
request that includes tool schemas:

```text
HTTPError 400: {"error":"registry.ollama.ai/library/gemma3:12b does not support tools"}
```

This rules it out as a direct local tool router even before quality or latency
can be measured.

### Strict dependency validation

| Model | Passed | Total | Pass rate | Mean seconds | Main observation |
| --- | ---: | ---: | ---: | ---: | --- |
| `qwen2.5:0.5b` | 3 | 4 | 75% | 1.1 | fast but failed cross-tool dependency |
| `qwen3:14b` | 4 | 4 | 100% | 10.3 | passed with one subgoal-gate intervention |

This run added `invalid_tool_dependency` to catch a subtle false positive. The
small `qwen2.5:0.5b` model called the right tool names in the right order, but
called:

```text
get_song_detail(title="第一首歌")
```

instead of using the first title returned by `search_music`:

```text
get_song_detail(title="Lemon")
```

So it produced a superficially plausible answer but could not ground the
release detail. This is a strong signal that production validation must inspect
tool arguments and tool results, not just count tool calls.

### Realistic music-selection probe

This probe replaced deterministic fixture music tools with the real OpenClaw
music index while explicitly preventing audio playback. It also ran once with a
fixture web-search result and once with the real web-search backend.

Tool chain under test:

1. `list_local_music(query)` reads the real music index and returns sanitized
   candidate paths.
2. `web_search_top_songs(artist)` returns either a fixture result or live search
   results.
3. `select_local_song(title)` uses the real music search function and returns a
   selected local track, but never starts `afplay` or `/music`.

| Run | Live web | Passed | Duration | Music entries | Observed tools | Selected song |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `realistic_probe_20260624T032155Z` | No | Yes | 13.6s | 672 | `list_local_music -> web_search_top_songs -> select_local_song` | `Lemon` |
| `realistic_probe_20260624T032222Z` | Yes | Yes | 26.9s | 672 | `list_local_music -> web_search_top_songs -> select_local_song` | `IRIS OUT` |

Important observations:

- `qwen3:14b` successfully executed the required three-tool plan in order.
- No playback command was available to the model, so this test is safe for
  repeated CI/manual runs.
- Result files sanitize local paths as `<music_root>/...`; they do not record
  absolute local filesystem paths.
- The real music index currently contains filename/path data, not reliable
  normalized artist/album/title metadata. In this local library, searching the
  artist name found only files whose folder happened to include that artist
  string. A popular title such as `Lemon` was still selectable, but this relied
  on the second step producing the title.
- Live web-search quality varied across runs. In the retained live run, the
  first result snippet ranked `IRIS OUT` first and `Lemon` second, so the model
  selected a locally available first-ranked song. This is acceptable for "choose
  one available popular song" but still not enough for "top 3 by current
  popularity" without a deterministic extraction/ranking step.

### qwen3:14b

This is the only model that currently looks viable.

Passes:

- Direct no-tool answer.
- Single weather tool call.
- Pronoun follow-up: history lets it resolve `他` as `米津玄師` and call
  `search_music`.

Failure:

- In the two-step music task it called `search_music`, then produced a final
  answer without calling `get_song_detail`. It also fabricated a release date
  instead of using the fixture detail tool. This is the exact production risk:
  the model can start a tool workflow but stop too early.

The subgoal gate mitigated this failure in the second run, raising `qwen3:14b`
from 3/4 to 4/4. The tradeoff is latency: mean runtime increased from 7.2s to
12.2s, and the two-step case took 18.7s.

### qwen2.5-coder:7b

Not suitable as a direct native tool selector. It understood the desired tool
names and arguments but returned them as plain JSON text, so Ollama did not
surface `message.tool_calls` and the application could not safely execute them.

This model may still be useful for code generation or as a fallback summarizer,
but not as the primary chat tool router.

### qwen3:4b

Not suitable for web chat. It sometimes emitted native tool calls, but final
answers leaked thinking text and it failed music tool-routing cases. The latency
was also worse than expected for the quality level.

## Interpretation

The local path is not blocked by Ollama. Native `/api/chat` tool calling works
well enough to prove feasibility, especially with `qwen3:14b`.

The missing production piece is not "use OpenCode with a local model". The more
promising architecture is:

```text
web chat request
  -> OpenClaw local planner/router
  -> Ollama native tool-capable model proposes tool calls
  -> OpenClaw validates and executes tools
  -> OpenClaw checks unfinished subgoals
  -> model continues or final answer is accepted
```

The important addition is the `unfinished subgoals` gate. The two-step failure
shows that relying on the model alone to continue after the first tool is not
stable enough; the second run shows that a simple corrective gate can recover
the workflow.

## Recommended Next Experiment

The realistic probe is enough to justify a focused architecture issue, but not
enough to implement production chat blindly. The next production design should
include these constraints:

1. Use Ollama native tool calls, not OpenCode, for local tool routing.
2. Keep the expected-tool/subgoal gate.
3. Add timeout handling and a user-visible "still working" progress event.
4. Add a tool-result validator that rejects final answers when required tool
   facts are missing.
5. Improve the music index with normalized metadata so artist queries do not
   depend on folder names.
6. For "recent top songs", add a deterministic extraction/ranking layer over
   web-search results instead of expecting the model to infer rankings from
   arbitrary snippets.
7. Optionally install and test `qwen3:8b` if memory allows; Docker's benchmark
   suggests Qwen3 8B can be a better speed/accuracy compromise.

Acceptance before implementing the production chat path:

- `qwen3:14b` or another installed local model passes at least 4/4 current cases
  with subgoal gate enabled.
- The two-step case must call `search_music` then `get_song_detail`.
- No final answer may contain leaked thinking or raw tool JSON.
- Mean latency should stay tolerable for the web chat surface, or the UI must
  emit explicit progress events for longer live-search runs.
- Realistic probes must pass without exposing absolute local paths or invoking
  playback.

## External References

- Ollama native tool calling docs: https://docs.ollama.com/capabilities/tool-calling
- Ollama API docs for `message.tool_calls`: https://github.com/ollama/ollama/blob/main/docs/api.md
- Ollama streaming tool support model list: https://ollama.com/blog/streaming-tool
- Qwen function calling docs: https://qwen.readthedocs.io/en/latest/framework/function_call.html
- Qwen-Agent implementation: https://github.com/QwenLM/Qwen-Agent
- Docker local tool-calling benchmark: https://www.docker.com/blog/local-llm-tool-calling-a-practical-evaluation/
- OpenCode local Ollama failure pattern: https://github.com/anomalyco/opencode/issues/1034
