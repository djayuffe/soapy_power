# Signal/spur analysis demo

Synthetic only. No radio was opened; no automatic exclusion was produced.

| scenario | tp | fp | missed | kind errors |
|---|---:|---:|---:|---:|
| T1 no signal (50 ohm) | 0 | 0 | 0 | 0 |
| T2 200 MHz-band in-band tone | 1 | 0 | 0 | 0 |
| T3 800 MHz out-of-band tone | 1 | 0 | 0 | 0 |
| T4 two in-band tones | 2 | 0 | 0 | 0 |
| T5 strong near-band neighbour (FM surrogate) | 2 | 0 | 0 | 0 |

## Drive diversity

| track | attribution | drive kind | drive slope (dB/dB) | source (MHz) |
|---|---|---|---:|---:|
| tone | signal | linear | 1.10 | 265.999 |
| intermod product | intermod | nonlinear-3 | 2.84 | 268.001 |

## Figures

- spur_slope: `docs/spurs-demo/spur_slope.png`
- before_after: `docs/spurs-demo/before_after.png`
- decision_flow: `docs/spurs-demo/decision_flow.png`
- mixing_diagram: `docs/spurs-demo/mixing_diagram.png`
- floor_vs_temperature: `docs/spurs-demo/floor_vs_temperature.png`
- drive_order: `docs/spurs-demo/drive_order.png`

## Scope

The scores are software verification against a known truth. They are not a
claim of hardware performance. The controlled T1-T5 programme and the
before/after hardware comparison still require a lab session.
