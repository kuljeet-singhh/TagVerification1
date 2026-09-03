"""
Data only. No code runs from here.

This directory held the inference tier: a SigLIP 2 detector deployed to a Hugging Face Space,
git-subtree-pushed and run flat there. All of it was removed once a model that reads its prompt
per request made the Space, the phrase packs and the publish step between them unnecessary. The
detector is preserved on `main`.

What is left is the measurements, which outlive the code that produced them: `eval/` is the
labelled corpus `dooh gate` scores against, `gate_cache.json` banks the SigLIP baseline the
removal was judged against, and `phrase_packs_archive.json` holds the packs themselves, dumped
the moment before migration 0003 dropped their columns.

This file exists only so `inference` remains an installable package — pyproject declares
`inference*` under packages.find. Keep it free of imports.
"""
