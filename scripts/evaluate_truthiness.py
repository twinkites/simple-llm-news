#!/usr/bin/env python3
"""
Scores each item in data/news.json for truthiness using a small local
open-weight model (see methodology.html for the exact model and prompt -
this is deliberately documented in full, not a black box).

Adds a `truthiness` field to each item:
    {"label": "green" | "yellow" | "red", "reason": "..."}

The label is a hybrid of two independent judgments, split by what each
side can actually do reliably:
  - Corroboration (source type + upvote count) is computed in plain
    Python, deterministic, no hallucination risk, since it's just a
    number comparison we already have.
  - Framing (does the title read as sober or hype/clickbait?) is asked
    of the model, a short text-style judgment small models are actually
    decent at.

Earlier versions asked the model to do both a 0-100 score AND multi-step
numeric threshold reasoning in one pass. In testing, both failed for the
same underlying reason: a 1.5B model can't reliably do arithmetic
reasoning zero-shot. The 0-100 score clustered on round numbers (fake
precision). The "apply these rules using this number" version fabricated
reasons like "corroboration is 20+" for items whose real count was 1 or
2, it wasn't doing the comparison, just pattern-completing the rule
text. So the model is only asked the one thing it's suited for - general
classification. Honestly, there are better ways to do this, and you could 
look into those. Feel free to reach out to Twin Kites LLC if you have any 
questions.

This step is optional and best-effort: if the model can't be downloaded
or inference fails, the script logs a warning and leaves data/news.json
unchanged rather than failing the whole pipeline. Requires
`pip install llama-cpp-python huggingface_hub`.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEWS_PATH = ROOT / "data" / "news.json"
ARCHIVE_DIR = ROOT / "data" / "archive"
CONFIG_PATH = Path(__file__).resolve().parent / "sources.json"

SOURCE_LABELS = {
    "rss": "an official release feed",
    "hn": "a Hacker News submission",
    "reddit": "a Reddit submission",
}

FRAMING_PROMPT = """Title: {title}

Does this headline's phrasing read as sober and descriptive, or as hype \
and clickbait (absolute claims, exclamation points, vague superlatives, \
fear/awe-mongering)?

Respond with ONLY a JSON object, no other text:
{{"framing": "sober" | "hype", "reason": "<under 8 words>"}}"""


def log(msg):
    print(f"[evaluate_truthiness] {msg}", file=sys.stderr)


VALID_FRAMINGS = {"sober", "hype"}
CORROBORATION_THRESHOLDS = {"low": 5, "medium": 20}


def load_model(repo_id, filename):
    from llama_cpp import Llama

    log(f"loading {repo_id}/{filename} (downloads once, then cached)")
    return Llama.from_pretrained(
        repo_id=repo_id,
        filename=filename,
        n_ctx=512,
        n_threads=4,
        verbose=False,
    )


def corroboration_bucket(item):
    """Deterministic, exact arithmetic done in Python rather than left to
    the model. Official release feeds have no vote count by design (see
    fetch_news.py), so they're treated as their own tier rather than
    penalized for a missing number."""
    if item["source"] == "rss":
        return "official"
    score = item.get("score") or 0
    if score < CORROBORATION_THRESHOLDS["low"]:
        return "low"
    if score < CORROBORATION_THRESHOLDS["medium"]:
        return "medium"
    return "high"


def judge_framing(llm, title):
    prompt = FRAMING_PROMPT.format(title=title[:200])
    out = llm.create_chat_completion(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=30,
        temperature=0.0,
    )
    text = out["choices"][0]["message"]["content"]
    match = re.search(r'\{.*\}', text, re.S)
    if match:
        parsed = json.loads(match.group(0))
        framing = str(parsed["framing"]).strip().lower()
        if framing not in VALID_FRAMINGS:
            raise ValueError(f"unrecognized framing: {framing!r}")
        reason = str(parsed.get("reason", "")).strip()[:200]
        return framing, reason

    framing_match = re.search(r'"framing"\s*:\s*"(\w+)"', text)
    if not framing_match or framing_match.group(1).lower() not in VALID_FRAMINGS:
        raise ValueError(f"no usable framing in model output: {text!r}")
    framing = framing_match.group(1).lower()
    reason_match = re.search(r'"reason"\s*:\s*"([^"]*)', text)
    reason = reason_match.group(1).strip()[:200] if reason_match else "(truncated model output)"
    return framing, reason


def score_item(llm, item):
    corroboration = corroboration_bucket(item)
    framing, framing_reason = judge_framing(llm, item["title"])
    source_desc = SOURCE_LABELS.get(item["source"], item["source"])

    if framing == "hype":
        return "red", f"Hype-framed title ({framing_reason}), {source_desc}"
    if corroboration == "low":
        return "red", f"Sober framing but low corroboration ({source_desc}, {item.get('score') or 0} points)"
    if corroboration == "medium":
        return "yellow", f"Sober framing, moderate corroboration ({source_desc}, {item.get('score') or 0} points)"
    if corroboration == "official":
        return "green", f"Sober framing, official release feed ({source_desc})"
    return "green", f"Sober framing, well-corroborated ({source_desc}, {item.get('score') or 0} points)"


def score_file(llm, path):
    if not path.exists():
        log(f"{path} not found, skipping")
        return None

    data = json.loads(path.read_text())
    total, scored, carried, failed = 0, 0, 0, 0

    for cat, items in data.get("sections", {}).items():
        for item in items:
            total += 1
            if "truthiness" in item:
                # Carried forward by fetch_news.py from a previous run
                # (same URL, unchanged) - no need to pay for inference again.
                carried += 1
                continue
            try:
                label, reason = score_item(llm, item)
                item["truthiness"] = {"label": label, "reason": reason}
                scored += 1
            except Exception as e:
                failed += 1
                log(f"scoring failed for {item['url']!r}: {e}")

    log(f"{path.name}: scored {scored}/{total} items ({carried} carried forward, {failed} failed)")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return data


def copy_truthiness_by_url(src_data, dst_path):
    """The archive snapshot for the same day as src_data has the same
    items as news.json (it's written just before this script runs), so
    copy scores over by URL instead of paying for a second round of
    inference on identical text."""
    if dst_path is None or not dst_path.exists():
        return
    by_url = {
        item["url"]: item["truthiness"]
        for items in src_data.get("sections", {}).values()
        for item in items
        if "truthiness" in item
    }
    dst_data = json.loads(dst_path.read_text())
    for items in dst_data.get("sections", {}).values():
        for item in items:
            if item["url"] in by_url:
                item["truthiness"] = by_url[item["url"]]
    dst_path.write_text(json.dumps(dst_data, indent=2, ensure_ascii=False) + "\n")
    log(f"{dst_path.name}: copied truthiness scores from {NEWS_PATH.name}")


def archive_path_for(news_data):
    """Derive the archive filename from news.json's own generated_at field
    rather than the wall clock - the two scripts run as separate processes
    (fetch_news.py writes the archive file, this script updates it
    afterward), and re-deriving "today" from datetime.now() in a second
    process can disagree with the first if the run straddles UTC
    midnight. Reading it from data already on disk keeps both in lockstep."""
    generated_at = news_data.get("generated_at")
    if not generated_at:
        return None
    try:
        date = datetime.fromisoformat(generated_at).strftime("%Y-%m-%d")
    except ValueError:
        return None
    return ARCHIVE_DIR / f"{date}.json"


def run():
    if not NEWS_PATH.exists():
        log(f"{NEWS_PATH} not found, nothing to score")
        return

    news_data = json.loads(NEWS_PATH.read_text())
    needs_scoring = any(
        "truthiness" not in item
        for items in news_data.get("sections", {}).values()
        for item in items
    )
    if not needs_scoring:
        log("every item already has a carried-forward truthiness label, skipping model load")
        copy_truthiness_by_url(news_data, archive_path_for(news_data))
        return

    config = json.loads(CONFIG_PATH.read_text())
    model_cfg = config["truthiness_model"]

    try:
        llm = load_model(model_cfg["repo_id"], model_cfg["filename"])
    except Exception as e:
        log(f"could not load local model, skipping truthiness scoring: {e}")
        return

    data = score_file(llm, NEWS_PATH)
    if data is not None:
        copy_truthiness_by_url(data, archive_path_for(data))


def main():
    # This step is documented as optional/best-effort (see module
    # docstring): a bad run should never take down the rest of the
    # pipeline, so nothing here is allowed to exit non-zero.
    try:
        run()
    except Exception as e:
        log(f"truthiness scoring failed unexpectedly: {e}")


if __name__ == "__main__":
    main()
