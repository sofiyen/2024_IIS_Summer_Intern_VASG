# Full-log summary generation

This directory implements Phase B of `eval/VALIDATION_SPEC.md`: convert all Log2Feat chunk features for one log into a grounded chronological summary.

The pipeline core does not know which model generated the chunk features and does not import a particular summarization model. Its two hot-plug boundaries are:

- `FeatureSource`: lists expected chunks, loads feature objects, and optionally provides raw chunk paths for identity-only overlap matching.
- `SummaryBackend`: receives a stage prompt plus JSON payload and returns one structured JSON result.

The first feature adapter is `DirectoryFeatureSource`. Switching Log2Feat from fine-tuned GPT-OSS to a commercial model therefore only changes `--features-dir` and `--feature-model`.

## Plan without a model

Planning validates coverage, sorts chunk IDs numerically, normalizes adjacent overlap, builds the chosen representation, and computes direct versus hierarchical leaf groups. It makes no model calls:

```bash
python tools/scripts/summary_generation/gen_summary.py \
  --log-id 242 \
  --features-dir data/chunk_features/finetuned_v3 \
  --feature-model finetuned_v3 \
  --backend plan
```

Without `--tokenizer`, planning uses a clearly labeled approximate character counter. Official GPT-OSS runs should use its tokenizer:

```bash
conda activate unsloth
python tools/scripts/summary_generation/gen_summary.py \
  --log-id 242 \
  --features-dir data/chunk_features/finetuned_v3 \
  --feature-model finetuned_v3 \
  --backend plan \
  --tokenizer train/unsloth/outputs_dpo_curated_v3/checkpoint-32 \
  --overwrite
```

Outputs are written to `data/summaries/<feature-model>/<log_id>.json`.
Use `--run-name` when comparing summarizers or representations over the same feature source. Results then go to `data/summaries/<run-name>/<log_id>.json`.

## Fine-tuned GPT-OSS baseline

The native `unsloth` backend loads the checkpoint once and reuses it for every leaf, merge, and final request. Start with complete teacher-generated features so the first experiment isolates summarization quality from Log2Feat feature-generation failures:

```bash
conda activate unsloth
python tools/scripts/summary_generation/gen_summary.py \
  --log-id 1 \
  --features-dir data/chunk_features/gpt \
  --feature-model gpt \
  --run-name gpt-features__gpt-oss-v3__entries-and-summary \
  --backend unsloth \
  --adapter-path train/unsloth/outputs_dpo_curated_v3/checkpoint-32 \
  --representation entries_and_summary \
  --context-limit 8192 \
  --input-token-budget 5800 \
  --max-output-tokens 1400 \
  --reasoning-effort medium \
  --overwrite
```

The result is `data/summaries/gpt-features__gpt-oss-v3__entries-and-summary/1.json`.

Reasoning effort `medium` matches the checkpoint's training template. If GPT-OSS uses the entire generation budget before reaching its final JSON channel, the pipeline records `PIPELINE_ERROR`; it never saves reasoning text as a summary.

## OpenAI API comparison

The OpenAI backend reuses the credential convention used by `eval/scripts/run_eval.py`:
`OPENAI_API_KEY` (or `GPT_API_KEY`) and optional `OPENAI_BASE_URL` are loaded from
`tools/scripts/feature_generation/.env`. The key is never copied into an output artifact.

To compare GPT-5.4 mini with GPT-OSS on the same features, prompts, tokenizer-based
packing, context limit, and reduction budgets:

```bash
python tools/scripts/summary_generation/gen_summary.py \
  --log-id 1 \
  --features-dir data/chunk_features/gpt \
  --feature-model gpt \
  --run-name summary_test_gpt54mini \
  --backend openai \
  --openai-model gpt-5.4-mini \
  --tokenizer train/unsloth/outputs_dpo_curated_v3/checkpoint-32 \
  --representation entries_and_summary \
  --context-limit 8192 \
  --input-token-budget 4500 \
  --max-output-tokens 3000 \
  --reasoning-effort medium \
  --overwrite
```

The artifact records response IDs, finish reasons, and token usage for each API call.
For GPT-5 reasoning models, `--temperature 0` means that no API temperature override
is sent because these models accept only their default temperature.

If the tokenizer environment does not contain the OpenAI SDK, keep token packing in
that environment and invoke the supplied adapter with a Python environment that does:

```bash
conda run -n unsloth python tools/scripts/summary_generation/gen_summary.py \
  --log-id 1 \
  --features-dir data/chunk_features/gpt \
  --feature-model gpt \
  --run-name summary_test_gpt54mini \
  --backend command \
  --backend-name openai:gpt-5.4-mini \
  --backend-command "/home/sophie940104/anaconda3/bin/python tools/scripts/summary_generation/openai_adapter.py --model gpt-5.4-mini" \
  --tokenizer train/unsloth/outputs_dpo_curated_v3/checkpoint-32 \
  --representation entries_and_summary \
  --context-limit 8192 \
  --input-token-budget 4500 \
  --max-output-tokens 3000 \
  --reasoning-effort medium \
  --overwrite
```

## Attach a model through the command adapter

`--backend command` launches an adapter process once per leaf, merge, or final call. The adapter reads one JSON object from stdin:

```json
{
  "stage": "leaf|merge|final",
  "system_prompt": "...",
  "payload": {"timeline": []},
  "max_output_tokens": 1400,
  "temperature": 0.0
}
```

It must print one JSON summary object to stdout. It may instead wrap the result as `{"output": {...}}`.

```bash
python tools/scripts/summary_generation/gen_summary.py \
  --log-id 242 \
  --features-dir data/chunk_features/finetuned_v3 \
  --feature-model finetuned_v3 \
  --backend command \
  --backend-command "python path/to/model_adapter.py" \
  --backend-name gpt-oss-20b-dpo-curated-v3 \
  --tokenizer train/unsloth/outputs_dpo_curated_v3/checkpoint-32
```

The same contract can wrap a local checkpoint or an API. Provider-specific loading, authentication, retry logic, and generation stay outside the pipeline core.

## Input modes and coverage

`--representation` supports the planned ablation:

- `entries_only`
- `entries_and_summary` (default)
- `summary_only`

Strict mode is the default. Missing or invalid chunk features produce an `INCOMPLETE_INPUT` artifact and no model calls. `--allow-incomplete` continues diagnostically while keeping that status so the result cannot be mistaken for an official complete run.

## Tests

```bash
python -m unittest discover \
  -s tools/scripts/summary_generation/tests \
  -p 'test_*.py' -v
```

The built-in `deterministic-test` backend exists only for offline wiring tests; it is not a summarization baseline.
