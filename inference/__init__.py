"""
The inference service: a SigLIP 2 tag detector deployed to a Hugging Face Space.

This directory is git-subtree-pushed to the Space and runs FLAT there (app.py, detector.py
and banding.py sit side by side, imported without a package). This __init__.py exists only so
the web application in dooh/ can `from inference.banding import band_of` — sharing the one
piece of logic that must never diverge between the two.

Keep this file free of imports. Importing detector.py from here would drag torch and
transformers into the web application's startup path, which is exactly what the split is
meant to avoid.
"""
