"""
partial_ai_resolver.py
Evaluates each pair (wl_home↔fb_home, wl_away↔fb_away) ONE BY ONE via local Qwen.
Not batched, not chunked — every pair is its own model call.
Skips records that already have result=true/false (resumable).
Saves progress to disk every 50 records.
"""
import json
import os
import time
import torch
from transformers import (
    AutoTokenizer,
    BitsAndBytesConfig,
    Qwen2_5_VLForConditionalGeneration,
)

os.environ['HF_HOME'] = 'M:/hf_cache'
os.environ['HF_HUB_CACHE'] = 'M:/hf_cache/hub'

MODEL_ID = "Qwen/Qwen2.5-VL-7B-Instruct"
INPUT_FILE = 'data/event_partial_matches.json'


def build_model():
    quant = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
    )
    print(f"Loading {MODEL_ID} (4-bit nf4)...")
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        MODEL_ID,
        torch_dtype=torch.float16,
        quantization_config=quant,
        device_map="auto",
    )
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    print(f"Model ready on {model.device}")
    return model, tokenizer


def ask_same_team(model, tokenizer, name1, name2):
    """Send a single pair to the model. Returns True if same team, else False."""
    prompt = (
        f'Task: determine if two sports team names refer to the SAME team.\n'
        f'Name A: "{name1}"\n'
        f'Name B: "{name2}"\n'
        f'Are these the same team? Consider: same city, same mascot/nickname, '
        f'just different transliteration (e.g. "Рэд" = "Ред") or abbreviation '
        f'(e.g. "Сент" = "Ст."). But DIFFERENT city or DIFFERENT mascot = NO.\n'
        f'Answer with a single word: YES or NO.'
    )
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(text, return_tensors="pt").to(model.device)

    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=5,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )

    # Decode only newly generated tokens (skip input prompt)
    input_len = inputs['input_ids'].shape[1]
    response = tokenizer.decode(outputs[0][input_len:], skip_special_tokens=True)
    response_up = response.strip().upper()

    if response_up.startswith("YES"):
        return True
    elif response_up.startswith("NO"):
        return False
    else:
        # Fallback for unexpected output
        return name1.strip().lower() == name2.strip().lower()


def _is_resolved(rec):
    """True if result is already a boolean (previously processed)."""
    return isinstance(rec.get('result'), bool)


def _save(data):
    with open(INPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    print(f"Reading {INPUT_FILE}...")
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    partials = data['partials']
    total = len(partials)
    already = sum(1 for p in partials if _is_resolved(p))
    todo = total - already
    print(f"Loaded {total} records ({already} already resolved, {todo} to process)")

    model, tokenizer = build_model()

    true_count = 0
    false_count = 0
    skipped_count = 0
    t_start = time.time()

    for i in range(total):
        rec = partials[i]

        if _is_resolved(rec):
            skipped_count += 1
            done = i + 1
            if done % 50 == 0:
                elapsed = time.time() - t_start
                rate = elapsed / max(done - skipped_count, 1)
                eta_sec = rate * (todo - (true_count + false_count))
                print(f"  [{done}/{total}] true={true_count} false={false_count} "
                      f"skipped={skipped_count} | {rate:.1f}s/rec | ETA {eta_sec/60:.0f}min")
            continue

        # Pair 1: home teams
        home_same = ask_same_team(model, tokenizer, rec['wl_home'], rec['fb_home'])

        # Pair 2: away teams
        away_same = ask_same_team(model, tokenizer, rec['wl_away'], rec['fb_away'])

        rec['result'] = home_same and away_same
        rec.pop('_applied_at', None)

        if rec['result']:
            true_count += 1
        else:
            false_count += 1

        done = i + 1
        if done % 50 == 0:
            _save(data)
            elapsed = time.time() - t_start
            processed = true_count + false_count
            rate = elapsed / max(processed, 1)
            eta_sec = rate * (todo - processed)
            print(f"  [{done}/{total}] true={true_count} false={false_count} "
                  f"skipped={skipped_count} | {rate:.1f}s/rec | ETA {eta_sec/60:.0f}min | saved")

    # Final save
    print(f"\nWriting final results to {INPUT_FILE}...")
    _save(data)

    elapsed_min = (time.time() - t_start) / 60
    print(f"\n{'='*60}")
    print(f"DONE: {total} records in {elapsed_min:.1f} min")
    print(f"  result=true:  {true_count}")
    print(f"  result=false: {false_count}")
    print(f"  skipped:      {skipped_count}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()