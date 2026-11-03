# Loaded automatically when this directory is on PYTHONPATH (see probe.py).
# Does nothing unless TEST_LAYERS_PROBE_DIR is set.
import os

if os.environ.get('TEST_LAYERS_PROBE_DIR'):
    import layer_probe
    layer_probe.install()
