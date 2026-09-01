"""Process-wide figure display preference: whether to overlay one faint
line per participant behind the group summaries.

The Group Analysis window flips this from its "Hide participant lines"
checkbox and rebuilds.  Every plotting site that would draw a
per-participant overlay trace reads ``show_participant_traces`` first;
when it is ``False`` the panel shows the group mean with a 95% CI band
(or its existing error bars) and no individual-trace cloud.

Kept as a module global rather than a constructor argument because the
overlay is drawn from many independent figure builders across the app
(the window's own tabs plus ``condition_a_strategy_tab`` and
``session_progression_figures``); one switch they all read at draw time
avoids threading the same boolean through every function signature.

Read it as ``figure_prefs.show_participant_traces`` at the moment of
drawing (import the module, not the name) so the current value is seen.
"""

# Default False: the checkbox ships ticked, and ticked means "hide".
show_participant_traces = False


def set_show_participant_traces(value: bool) -> None:
    """Set the shared overlay preference (called from the window's checkbox)."""
    global show_participant_traces
    show_participant_traces = bool(value)
