# Development

How it works inside, and the rules for not knocking GoldHEN over: [PROJECT.md](PROJECT.md). What changed since
Tech-Morph's 0.9.0: [CHANGELOG.md](CHANGELOG.md).

Tests are plain scripts, no HA install needed. They need Python 3.13+ with `cryptography` (HA ships it; the pairing
code imports it) and Node 24:

```sh
python3 -m venv .venv && . .venv/bin/activate && pip install cryptography
for t in tests/test_*.py;  do python -B "$t" || break; done
for t in tests/test_*.cjs; do node "$t"      || break; done
```

Issues and pull requests are welcome. For bug reports, include the console model, firmware, GoldHEN version, HA version
and the relevant HA log lines (remove keys and account IDs first). Taking this further, a fork, a rewrite, a "third
stack" on top: please do, that's why it's MIT.

Most of this fork's code was written by Claude Code (Anthropic's AI) from prompts, and tested on real consoles. Review
it like any other code. Much of the time spent was correcting issues it created (I swear it's by design). If you spot
anything that's redundant or a result of corrections, please point it out.

Full disclosure: I'm not a prompt engineer, I'm not a dev, I'm not even a professional. I'm just a person with an
Anthropic sub; anyone could have done this with some time, patience and the right attitude. Better or worse? At this
point the variables are the calendar date and the model. If you want to build on it with the same method, by all means,
please do so. Just make sure you prompt it to credit the ones who went before you, the real ones. Without their source
data, this isn't possible. So thanks, genuinely. GG.

[Back to the main README](../README.md)
