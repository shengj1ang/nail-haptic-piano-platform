"""Validation experiments - small hardware-validation / calibration
experiments that inform the main user study's design constants but are
not part of the main protocol (launcher section "Validation
Experiments").

One subfolder per experiment (e.g. lra_resonance_intensity_calibration/),
with the shared serial-rig helpers in rig.py at this level. Each
experiment is a standalone script that can still be run directly from
its folder; the GUI windows in app/gui/validation_experiment_window.py
are thin wrappers (progress bar + log) around the same run_experiment()
functions, so both paths produce byte-identical outputs under
data/validation_experiments/<experiment>/.
"""
