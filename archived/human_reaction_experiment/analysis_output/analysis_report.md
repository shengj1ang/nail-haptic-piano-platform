# User Reaction Experiment Statistical Report

## Overview
This report summarizes the user reaction experiment comparing **LRA** and **ERM** vibrotactile actuators. The analysis includes **5 participants** and **150 total trials** across both actuator conditions.
The script computes descriptive statistics, participant-level paired comparisons, subjective rating summaries, preference counts, and figure outputs suitable for inclusion in a thesis or paper.

## Main quantitative findings
- **Reaction time (valid responses only):** LRA = 276.98 ± 44.92 ms, ERM = 281.65 ± 49.92 ms. The participant-level paired comparison used Wilcoxon signed-rank and yielded p = 0.625.
- **Accuracy:** LRA = 100.0% ± 0.0%, ERM = 100.0% ± 0.0%. The paired comparison yielded p = 1.000.
- **Miss rate:** LRA = 0.0% ± 0.0%, ERM = 0.0% ± 0.0%. The paired comparison yielded p = 1.000.
- **False starts per trial:** LRA = 0.00 ± 0.00, ERM = 0.03 ± 0.04. The paired comparison yielded p = 0.500.

## Subjective ratings
- **Clarity:** LRA = 4.20, ERM = 3.60; paired comparison (Wilcoxon signed-rank) p = 0.250.
- **Comfort:** LRA = 4.60, ERM = 3.00; paired comparison (Wilcoxon signed-rank) p = 0.062.
- **Responsiveness:** LRA = 4.20, ERM = 3.20; paired comparison (Wilcoxon signed-rank) p = 0.062.
- **Satisfaction:** LRA = 4.40, ERM = 3.00; paired comparison (Wilcoxon signed-rank) p = 0.062.
- **Overall Mean Rating:** LRA = 4.35, ERM = 3.20; paired comparison (Wilcoxon signed-rank) p = 0.062.

## Participant preference
- **LRA** was preferred by 5/5 participants (100.0%, 95% CI [56.6%, 100.0%]).

### Qualitative preference notes
- Participant 1: preferred **LRA** because more clear, comfort

## Suggested Results-section wording
In the user reaction experiment, five participants completed single-finger response blocks for both the LRA and ERM actuator conditions. For each participant, block-level averages were computed and compared using paired tests. Because the sample size was small, non-parametric paired statistics (Wilcoxon signed-rank or sign test where necessary) are the most appropriate primary inferential results, while the paired t-test and Cohen's dz are included as supplementary effect-size-oriented references in the exported tables.
The descriptive summaries and plots can be used directly in the thesis to support claims about relative speed, reliability, perceived clarity, comfort, responsiveness, and overall user preference between the two actuator types.

## Generated figures
- `figures/mean_reaction_time_valid.png`
- `figures/mean_accuracy.png`
- `figures/mean_miss_rate.png`
- `figures/paired_reaction_time_by_participant.png`
- `figures/paired_accuracy_by_participant.png`
- `figures/subjective_ratings.png`
- `figures/preferred_actuator.png`