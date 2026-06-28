# Local Tool Calling Benchmark Results

Last reviewed: 2026-06-24
Status: Generated
Owner area: agent-maintenance

- Timestamp: `2026-06-24T14:22:41.293961+00:00`
- Endpoint: `http://127.0.0.1:11434`
- Models: `qwen2.5:0.5b, qwen3:14b`

| Model | Passed | Total | Pass rate | Mean seconds |
| --- | ---: | ---: | ---: | ---: |
| `qwen2.5:0.5b` | 3 | 4 | 75% | 1.1 |
| `qwen3:14b` | 4 | 4 | 100% | 10.3 |

## Case Details

### qwen2.5:0.5b / zero_tool_greeting / PASS
- Duration: `2.831s`
- Expected tools: `[]`
- Observed tools: `[]`
- Final content:

```text
你好！
```

### qwen2.5:0.5b / single_weather_lookup / PASS
- Duration: `0.402s`
- Expected tools: `['get_weather']`
- Observed tools: `['get_weather']`
- Final content:

```text
東京現在是晴天，气温為28度。
```

### qwen2.5:0.5b / two_step_music_search_then_detail / FAIL
- Duration: `0.931s`
- Expected tools: `['search_music', 'get_song_detail']`
- Observed tools: `['search_music', 'get_song_detail']`
- Quality flags: `['invalid_tool_dependency']`
- Final content:

```text
米津玄師的最近熱門前三首歌分別是「Lemon」、「KICK BACK」和「感電」。第一名是「Lemon」，排名1；第二名是「KICK BACK」，排名2；第三名是「感電」，排名3。第一首歌的發行資訊尚未確定。
```

### qwen2.5:0.5b / tool_after_context_pronoun / PASS
- Duration: `0.4s`
- Expected tools: `['search_music']`
- Observed tools: `['search_music']`
- Final content:

```text
1. Lemon
2. Kick Back
3.感電
```

### qwen3:14b / zero_tool_greeting / PASS
- Duration: `11.073s`
- Expected tools: `[]`
- Observed tools: `[]`
- Final content:

```text
您好，很高興為您服務！
```

### qwen3:14b / single_weather_lookup / PASS
- Duration: `4.532s`
- Expected tools: `['get_weather']`
- Observed tools: `['get_weather']`
- Final content:

```text
東京現在天氣晴朗，氣溫28攝氏度。
```

### qwen3:14b / two_step_music_search_then_detail / PASS
- Duration: `18.74s`
- Expected tools: `['search_music', 'get_song_detail']`
- Observed tools: `['search_music', 'get_song_detail']`
- Gate interventions: `1`
- Final content:

```text
1. Lemon  
2. KICK BACK  
3. 感電  

Lemon 於 2018 年 3 月 14 日發行，並作為電視劇《Unnatural》的主題曲。
```

### qwen3:14b / tool_after_context_pronoun / PASS
- Duration: `6.96s`
- Expected tools: `['search_music']`
- Observed tools: `['search_music']`
- Final content:

```text
1. Lemon  
2. KICK BACK  
3. 感電
```
