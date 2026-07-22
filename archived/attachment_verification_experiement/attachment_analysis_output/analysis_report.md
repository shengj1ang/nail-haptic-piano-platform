# Attachment Verification Analysis Report

This report evaluates whether attachment method changes the measured vibration transmission.

## Data structure
- Condition-level rows: 6
- Participants: 1
- Actuators: ERM, LRA

## Descriptive statistics
### ERM
**Peak delta**
- tape: mean=417.98, sd=0.00, median=417.98, n=1
- putty: mean=427.86, sd=0.00, median=427.86, n=1
- eyelash_glue: mean=534.18, sd=0.00, median=534.18, n=1
**RMS delta**
- tape: mean=220.27, sd=0.00, median=220.27, n=1
- putty: mean=226.17, sd=0.00, median=226.17, n=1
- eyelash_glue: mean=280.23, sd=0.00, median=280.23, n=1
**Onset delay (ms)**
- tape: mean=12.04, sd=0.00, median=12.04, n=1
- putty: mean=12.51, sd=0.00, median=12.51, n=1
- eyelash_glue: mean=12.79, sd=0.00, median=12.79, n=1

### LRA
**Peak delta**
- tape: mean=353.72, sd=0.00, median=353.72, n=1
- putty: mean=315.07, sd=0.00, median=315.07, n=1
- eyelash_glue: mean=290.05, sd=0.00, median=290.05, n=1
**RMS delta**
- tape: mean=183.80, sd=0.00, median=183.80, n=1
- putty: mean=168.16, sd=0.00, median=168.16, n=1
- eyelash_glue: mean=160.90, sd=0.00, median=160.90, n=1
**Onset delay (ms)**
- tape: mean=12.51, sd=0.00, median=12.51, n=1
- putty: mean=12.93, sd=0.00, median=12.93, n=1
- eyelash_glue: mean=12.91, sd=0.00, median=12.91, n=1

## Friedman tests by actuator
Friedman tests could not be computed.

## Pooled Friedman tests
- mean_peak_delta: chi-square=0.000, p=1.0000, n blocks=2
- mean_rms_delta: chi-square=0.000, p=1.0000, n blocks=2
- mean_onset_delay_ms: chi-square=3.000, p=0.2231, n blocks=2

## Pairwise Wilcoxon tests
Pairwise tests could not be computed.

## Reliability summary
### ERM
- tape: mean failure cycle=0.00, median=0.00, never fell off=1/1
- putty: mean failure cycle=0.00, median=0.00, never fell off=1/1
- eyelash_glue: mean failure cycle=0.00, median=0.00, never fell off=1/1
### LRA
- tape: mean failure cycle=0.00, median=0.00, never fell off=1/1
- putty: mean failure cycle=0.00, median=0.00, never fell off=1/1
- eyelash_glue: mean failure cycle=0.00, median=0.00, never fell off=1/1

## Interpretation guide
- Start with the Friedman test for each actuator and metric.
- If p < 0.05, attachment method likely affected that vibration metric.
- Use the pairwise Wilcoxon results to see which attachment methods differed.
- For onset delay, missing values can reduce the number of analyzable blocks.
- Reliability is descriptive here unless you want a separate survival-style analysis later.