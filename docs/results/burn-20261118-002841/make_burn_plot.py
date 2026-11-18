#!/usr/bin/env python3
"""Regenerate burn.png from burn_series.json (Matplotlib; no raw session needed)."""
import json
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
data = json.loads((HERE / 'burn_series.json').read_text())

series = data['series']
floors = data['floor_proxy']
trials = []
for row in series:
    if row['trial'] not in trials:
        trials.append(row['trial'])

by = {name: {'t': [], 'temp': [], 'ft': [], 'f': []} for name in trials}
for row in series:
    by[row['trial']]['t'].append(row['t'])
    by[row['trial']]['temp'].append(row['temp'])
for row in floors:
    by[row['trial']]['ft'].append(row['t'])
    by[row['trial']]['f'].append(row['floor_db'])

colors = ['#4cc9f0', '#f72585', '#ffd166', '#06d6a0']
fig, axes = plt.subplots(2, 1, figsize=(12.5, 7.6), sharex=True, dpi=110)
fig.patch.set_facecolor('#0b1220')
for ax in axes:
    ax.set_facecolor('#0b1220')
    ax.tick_params(colors='#c7d3e8')
    ax.grid(color='#22314a', lw=.6, alpha=.8)
    for spine in ax.spines.values():
        spine.set_color('#2b3c58')
    ax.xaxis.label.set_color('#c7d3e8')
    ax.yaxis.label.set_color('#c7d3e8')
    ax.title.set_color('#eaf2ff')

for i, name in enumerate(trials):
    color = colors[i % len(colors)]
    d = by[name]
    axes[0].plot(np.array(d['t']) / 60.0, d['temp'], color=color, lw=.8, alpha=.9, label=name)
    axes[1].plot(np.array(d['ft']) / 60.0, d['f'], color=color, lw=1.1, marker='.', ms=2.4,
                 alpha=.9, label=name)

axes[0].set_ylabel('RFIC temperature (°C)')
axes[1].set_ylabel('130 MHz floor proxy (dB, relative)')
axes[1].set_xlabel('elapsed time (min)')
axes[0].legend(frameon=False, fontsize=8, labelcolor='#c7d3e8', ncol=4, loc='upper right')

all_f = np.array([row['floor_db'] for row in floors])
all_t = np.array([row['temp'] for row in series])
fig.text(.012, .972, 'B200mini 30B2A32 burn test · 4 × 300 s · RX2, 20 dB, antenna',
         color='#eaf2ff', fontsize=13, ha='left', va='top')
fig.text(.012, .937,
         'T = {:.2f}–{:.2f} °C · repeatability σ = 0.032 dB · holdout RMS = 0.051 dB · '
         'floor p5–p95 = {:.2f} dB (span {:.2f} dB)'.format(
             all_t.min(), all_t.max(),
             np.percentile(all_f, 95) - np.percentile(all_f, 5),
             all_f.max() - all_f.min()),
         color='#8be28b', fontsize=8.8, ha='left', va='top')

fig.text(.012, .018,
         'Relative and uncalibrated. Floor proxy = median over ±100 kHz at 130 MHz when that RF is '
         'within 5 MHz of the tuned center; residual IF shape and environment are included. '
         'No automatic notch or correction.',
         color='#8ba0bf', fontsize=7.8)
fig.tight_layout(rect=[0, 0.035, 1, 0.925])
fig.savefig(HERE / 'burn.png', facecolor=fig.get_facecolor())
print('wrote', HERE / 'burn.png')
