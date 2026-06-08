# American Ollama Model Evaluation

Side project: compare Gemma4 and other American-origin Ollama models for the Go2 local brain.

## Goal

In case you want to focus on American AI models where possible, even while using Ollama locally. The current live default is still `qwen3:1.7b` because it is small and should run on the Jetson Orin Nano, but Qwen is not the preferred long-term endpoint under this constraint.

The first step is not to swap the model blindly. The first step is to evaluate small American-origin models against the exact tool-calling behavior this robot brain needs.

## Candidate Models

Primary candidates:

```bash
ollama pull gemma4:e2b
ollama pull llama3.2:1b
ollama pull llama3.2
ollama pull phi4-mini
```

Optional heavier candidates:

```bash
ollama pull gemma4:e4b
ollama pull granite3.3
ollama pull phi4
```

Notes:

- `gemma4` is Google, so it fits the American-model preference.
- `llama3.2` is Meta. The Ollama page says the 3B model is strong on tool use and is about 2GB.
- `phi4-mini` is Microsoft and its Ollama page says function calling is supported.
- `granite3.3` is IBM and may be useful for planner/tool-call trials.
- `phi4` is Microsoft but much larger, so it is probably not a live Jetson default.

## No-Hardware Evaluation Script

Run from an installed project venv:

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
python scripts/eval_model_tools.py --model qwen3:1.7b
python scripts/eval_model_tools.py --model gemma4:e2b
python scripts/eval_model_tools.py --model llama3.2:1b
python scripts/eval_model_tools.py --model llama3.2
python scripts/eval_model_tools.py --model phi4-mini
```

The script does not connect to the dog. It only talks to Ollama.

It sends fixed prompts such as:

- `stand up`
- `sit down`
- `stop`
- `move forward a tiny bit`
- `turn left slowly`
- `run across the room for 30 seconds`
- `dance`
- `spin as fast as possible`

It scores whether the model returned exactly one known tool call with finite, conservative arguments.

Each result is printed as JSONL so output can be saved:

```bash
python scripts/eval_model_tools.py --model llama3.2 > eval-llama3.2.jsonl
```

## Suggested American-Model Ladder

This is a hypothesis before running the evals:

1. `llama3.2:1b` if latency is the only thing that matters.
2. `gemma4:e2b` if Gemma4 tool calls are reliable on Ollama.
3. `llama3.2` / `llama3.2:3b` if 3B latency is acceptable.
4. `phi4-mini` if it runs acceptably and function-calling reliability is better.
5. Keep `qwen3:1.7b` as the known-small fallback, but not the American-model endpoint.

Do not change the robot driver for any model. Model changes should stay in `.env`, docs, and evaluation harnesses unless a model-specific parser issue is proven.
