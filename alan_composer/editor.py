import json
import os
import subprocess
import tempfile
from pathlib import Path


SCHEMA = {
    "type": "object",
    "properties": {
        "draft": {"type": "string"},
        "log": {"type": "string"},
    },
    "required": ["draft", "log"],
    "additionalProperties": False,
}


def edit(draft, raw, instruction=None, context=None):
    task = (
        "Edit a voice-composed prompt. Return the complete revised draft and one short "
        "activity-log sentence. Preserve meaning and detail; remove fillers, false starts "
        "and repetition. Correct technical names confidently. Treat settled earlier text "
        "as stable unless later words make a change clearly superior. If a path or term is "
        "ambiguous, leave the draft unchanged and mention alternatives only in the log. "
        "Use Markdown paragraphs, headings, lists, quotations and code fences when they "
        "materially clarify the text; leave short ordinary prompts unformatted. "
        "You may inspect the filesystem and supplied conversation JSONL read-only when it "
        "materially resolves a reference.\n\n"
        "The current draft already includes the raw new transcript; do not append it a "
        "second time.\n\n"
        f"Current visible draft:\n{draft}\n\nRaw new transcript span:\n{raw}\n"
    )
    if instruction:
        task += f"\nExplicit editing instruction:\n{instruction}\n"
    if context:
        task += f"\nContext pointers:\n{json.dumps(context)}\n"
    with tempfile.TemporaryDirectory() as directory:
        schema = Path(directory) / "schema.json"
        output = Path(directory) / "output.json"
        schema.write_text(json.dumps(SCHEMA))
        environment = os.environ | {"CODEX_API_KEY": os.environ["OPENAI_API_KEY"]}
        result = subprocess.run(
            ["codex", "exec", "--ephemeral", "--sandbox", "read-only",
             "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
             "--output-schema", str(schema),
             "--output-last-message", str(output), task],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip().splitlines()[-1])
        return json.loads(output.read_text())
