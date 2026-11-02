"""Every Anthropic and OpenAI model name in ccusage's pricing snapshots, one per line."""
import json
import re
import sys

pattern = re.compile(r"(?:^|[/.])((?:claude|gpt|o\d|codex|chatgpt)[\w.\-:@]*)")
names = {"codex-auto-review"}
for path in sys.argv[1:]:
    for key in json.load(open(path)):
        for name in pattern.findall(key):
            names.add(name)
            names.add(re.sub(r"-v\d+:\d+$", "", name))
print("\n".join(sorted(names)))
