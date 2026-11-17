"""Figures for the signal/spur analysis.

Matplotlib is an optional dependency.  Every public function imports it lazily
and raises a clear ``RuntimeError`` if it is unavailable, so the analysis and
its tests remain usable on a headless machine or in a minimal virtualenv.
"""
import math
from pathlib import Path

import numpy as np

DARK = {
    'figure.facecolor': '#070b14',
    'axes.facecolor': '#0b1222',
    'text.color': '#c9d4e6',
    'axes.labelcolor': '#c9d4e6',
    'xtick.color': '#c9d4e6',
    'ytick.color': '#c9d4e6',
    'axes.edgecolor': '#2b3a55',
    'axes.grid': True,
    'grid.color': '#24324f',
    'grid.alpha': 0.55,
    'axes.spines.top': False,
    'axes.spines.right': False,
    'axes.titleweight': 'bold',
    'axes.titlelocation': 'left',
}


def _plt():
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError('matplotlib is required for figures: {}'.format(exc)) from exc
    plt.rcParams.update(DARK)
    return plt


def _save(fig, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt = _plt()
    plt.close(fig)
    return str(path)


def plot_spur_slope(report, path):
    """Observed IF versus LO for every track, with the fitted mixing laws.

    A real signal has slope -1; a 3rd-order response has slope +/-3; a 5th-order
    response has slope +/-5.  The figure makes the LO-diversity test visible.
    """
    plt = _plt()
    tracks = [t for t in report.get('tracks', []) if len(t.get('points', [])) >= 2]
    if not tracks:
        raise ValueError('report has no tracks with at least two LO points')
    colors = {'signal': '#00d1ff', 'image': '#ffb000', 'lo_harmonic': '#ff5d8f'}
    markers = {'signal': 'o', 'image': 's', 'lo_harmonic': '^'}
    fig, ax = plt.subplots(figsize=(10.5, 6.5), dpi=110)
    for track in tracks:
        points = track['points']
        lo = np.array([p['lo_hz'] for p in points]) / 1e6
        f = np.array([p['if_hz'] for p in points]) / 1e6
        kind = track.get('kind') or 'unconfirmed'
        color = colors.get(kind, '#9aa7bd')
        marker = markers.get(kind, 'x')
        label = '{} n={} s={:+d}'.format(kind, track.get('order'), int(track.get('sign') or 0))
        ax.plot(lo, f, marker + '-', color=color, lw=1.3, ms=6, label=label)
        slope = track.get('lo_slope', {}).get('slope_hz_per_hz')
        if slope is not None:
            x = np.linspace(lo.min(), lo.max(), 20)
            ref = points[0]
            y = ref['if_hz'] / 1e6 + slope * (x - ref['lo_hz'] / 1e6)
            ax.plot(x, y, '--', color=color, lw=0.8, alpha=0.55)
    ax.axhline(0, color='#52617a', lw=0.8)
    ax.set_xlabel('LO (MHz)')
    ax.set_ylabel('observed IF (MHz)')
    ax.set_title('LO diversity: slope 1 = signal/image, slope 3 or 5 = LO harmonic')
    ax.legend(frameon=False, fontsize=8, loc='best')
    fig.tight_layout()
    return _save(fig, path)


def plot_mixing_diagram(lo_hz, rf_sources, path, orders=(1, 3, 5, 7),
                        image_rejection_db=35.0, rate_hz=None):
    """RF-to-IF lines for the square-wave LO and the marked input sources."""
    plt = _plt()
    sources = [float(s[0] if isinstance(s, (tuple, list)) else s) for s in rf_sources]
    if not sources:
        raise ValueError('rf_sources must not be empty')
    lo_hz = float(lo_hz)
    span = max(3.0 * lo_hz, max(sources) - min(sources))
    rf = np.linspace(max(0.0, min(sources) - 0.2 * span), max(sources) + 0.2 * span, 1200)
    fig, ax = plt.subplots(figsize=(10.5, 6.5), dpi=110)
    palette = ['#00d1ff', '#ffb000', '#ff5d8f', '#8be28b', '#c792ea']
    for i, n in enumerate(orders):
        for sign in (1, -1):
            if_hz = sign * (rf - n * lo_hz)
            if rate_hz is not None and np.all(np.abs(if_hz) > rate_hz / 2):
                continue
            ax.plot(rf / 1e6, if_hz / 1e6, color=palette[i % len(palette)],
                    lw=1.1, alpha=0.8 if sign == 1 else 0.45,
                    label='n={} sign={:+d}'.format(n, sign) if i < 3 else None)
    for source in sources:
        ax.axvline(source / 1e6, color='#c9d4e6', ls=':', lw=0.8)
        ax.text(source / 1e6, ax.get_ylim()[1] * 0.92, '{:.0f} MHz'.format(source / 1e6),
                rotation=90, va='top', ha='right', fontsize=8, color='#c9d4e6')
    if rate_hz is not None:
        ax.axhspan(-rate_hz / 2e6, rate_hz / 2e6, color='#16304a', alpha=0.25,
                   label='baseband +/- rate/2')
    ax.axhline(0, color='#52617a', lw=0.8)
    ax.set_xlabel('RF (MHz)')
    ax.set_ylabel('IF = sign * (RF - n*LO) (MHz)')
    ax.set_title('Mixing diagram for square-wave LO = {:.1f} MHz'.format(lo_hz / 1e6))
    ax.legend(frameon=False, fontsize=8, loc='best')
    fig.tight_layout()
    return _save(fig, path)


def plot_floor_vs_temperature(times_s, temps_c, floor_db, fit, path):
    """Floor versus time and versus temperature, with the fitted slope."""
    plt = _plt()
    t = np.asarray(times_s, dtype=float)
    temp = np.asarray(temps_c, dtype=float)
    y = np.asarray(floor_db, dtype=float)
    ok = np.isfinite(t) & np.isfinite(temp) & np.isfinite(y)
    if ok.sum() < 3:
        raise ValueError('need at least three finite floor/temperature records')
    t, temp, y = t[ok], temp[ok], y[ok]
    fig, axs = plt.subplots(1, 2, figsize=(13, 5.2), dpi=110)
    axs[0].plot(t, y, '.', color='#00d1ff', ms=4, label='measured floor')
    axs[0].set_xlabel('time (s)')
    axs[0].set_ylabel('floor (dB relative)')
    axs[0].set_title('Floor versus time')
    axs[0].legend(frameon=False, fontsize=8)
    axs[1].plot(temp, y, '.', color='#ffb000', ms=4, label='measured floor')
    if fit and fit.get('status') == 'fit':
        slope = float(fit['slope_db_per_c'])
        ref = float(fit.get('reference_temperature_c', np.mean(temp)))
        intercept = float(np.mean(y) - slope * (np.mean(temp) - ref))
        grid = np.linspace(temp.min(), temp.max(), 100)
        line = intercept + slope * (grid - ref)
        axs[1].plot(grid, line, '-', color='#ff5d8f', lw=2,
                    label='{:+.4f} dB/°C'.format(slope))
        if fit.get('slope_sd_db') is not None:
            sd = float(fit['slope_sd_db'])
            axs[1].fill_between(grid, line - 1.96 * sd * (grid - temp.mean()),
                                line + 1.96 * sd * (grid - temp.mean()),
                                color='#ff5d8f', alpha=0.16, label='95 % slope band')
        axs[1].set_title('Floor versus temperature{}'.format(
            ' (confounded)' if fit.get('confounded') else ''))
    else:
        axs[1].set_title('Floor versus temperature (not fitted)')
    axs[1].set_xlabel('temperature (°C)')
    axs[1].set_ylabel('floor (dB relative)')
    axs[1].legend(frameon=False, fontsize=8)
    fig.tight_layout()
    return _save(fig, path)


def plot_drive_order(report, path):
    """Observed level versus input drive for each classified track.

    A linear signal follows 1 dB/dB; an n-th order intermodulation product
    follows approximately n dB/dB.  The fitted drive slope is annotated from
    the report's ``drive`` block.
    """
    plt = _plt()
    tracks = [t for t in report.get('tracks', [])
              if t.get('drive', {}).get('status') == 'measured']
    if not tracks:
        raise ValueError('report has no tracks with a measured drive response')
    colors = {'signal': '#00d1ff', 'image': '#ffb000', 'lo_harmonic': '#ff5d8f',
              'intermod': '#c792ea'}
    markers = {'signal': 'o', 'image': 's', 'lo_harmonic': '^', 'intermod': 'D'}
    fig, ax = plt.subplots(figsize=(9.5, 6.0), dpi=110)
    for track in tracks:
        points = track.get('points', [])
        drive = np.array([p['drive_db'] for p in points], dtype=float)
        level = np.array([p['power_db'] for p in points], dtype=float)
        attribution = track.get('attribution') or track.get('kind') or 'unconfirmed'
        slope = track['drive'].get('slope_db_per_db')
        nonlinear = track['drive'].get('order')
        if attribution == 'intermod':
            label = 'intermod (drive order {})'.format(nonlinear)
        elif attribution == 'lo_harmonic':
            label = 'LO harmonic n={}'.format(track.get('order'))
        else:
            label = attribution
        label += ': {:.2f} dB/dB'.format(slope if slope is not None else float('nan'))
        ax.plot(drive, level, markers.get(attribution, 'x'), color=colors.get(attribution, '#9aa7bd'),
                ls='none', ms=6, label=label)
        if slope is not None and drive.size >= 2:
            xm, ym = float(drive.mean()), float(level.mean())
            x = np.array([drive.min(), drive.max()])
            ax.plot(x, ym + slope * (x - xm), '-', color=colors.get(attribution, '#9aa7bd'),
                    lw=1.0, alpha=0.8)
    ax.set_xlabel('input drive (dB, relative)')
    ax.set_ylabel('observed level (dB above floor)')
    ax.set_title('Drive order: linear signal = 1 dB/dB, n-th order product = n dB/dB')
    ax.legend(frameon=False, fontsize=8, loc='best')
    fig.tight_layout()
    return _save(fig, path)


def plot_decision_flow(path):
    """A compact decision-flow diagram for the implemented pipeline."""
    plt = _plt()
    from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
    fig, ax = plt.subplots(figsize=(11, 6.4), dpi=110)
    ax.set_axis_off()
    boxes = [
        (0.04, 0.76, 0.27, 0.14, 'captures\nLO, drive, temperature'),
        (0.365, 0.76, 0.27, 0.14, 'bias-corrected floor\n+ Gamma mixture'),
        (0.69, 0.76, 0.27, 0.14, 'FDR peak detection\n(BH / Bonferroni)'),
        (0.04, 0.45, 0.27, 0.14, 'associate tracks\nall mixing laws'),
        (0.365, 0.45, 0.27, 0.14, 'LO slope + drive order\n+ Bayesian posterior'),
        (0.69, 0.45, 0.27, 0.14, 'signal / image /\nLO harmonic / unresolved'),
        (0.69, 0.12, 0.27, 0.14, 'report + figures\nfloor before/after, JSON, PNG'),
    ]
    for x, y, w, h, text in boxes:
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle='round,pad=0.012,rounding_size=0.02',
                                    facecolor='#10203a', edgecolor='#3e5b82', lw=1.2))
        ax.text(x + w / 2, y + h / 2, text, ha='center', va='center', fontsize=9.2)
    arrows = [((0.31, 0.83), (0.365, 0.83)), ((0.635, 0.83), (0.69, 0.83)),
              ((0.175, 0.76), (0.175, 0.59)), ((0.31, 0.52), (0.365, 0.52)),
              ((0.635, 0.52), (0.69, 0.52)), ((0.825, 0.45), (0.825, 0.26))]
    for start, end in arrows:
        ax.add_patch(FancyArrowPatch(start, end, arrowstyle='-|>', mutation_scale=12,
                                     color='#6d86ad', lw=1.2))
    ax.text(0.04, 0.30, 'guardrail: no automatic notch,\nno exclusion list, relative levels only',
            fontsize=8.4, color='#8be28b', va='center', ha='left')
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_title('Signal/spur pipeline: evidence first, exclusion last (and never automatic)',
                 fontsize=13, pad=12)
    return _save(fig, path)


def plot_before_after(report, path):
    """Raw spectrum, legacy floor, model floor and classified peak bins."""
    plt = _plt()
    data = report.get('before_after')
    if not data:
        raise ValueError('report has no before_after section')
    if_hz = np.asarray(data['if_hz'], dtype=float) / 1e6
    power = np.asarray(data['power_db'], dtype=float)
    spur = np.asarray(data.get('spur_mask', []), dtype=bool)
    signal = np.asarray(data.get('signal_mask', []), dtype=bool)
    fig, ax = plt.subplots(figsize=(13, 6.2), dpi=110)
    ax.plot(if_hz, power, color='#6d86ad', lw=0.55, label='capture (floor-normalized)')
    if spur.size == power.size and spur.any():
        ax.plot(if_hz[spur], power[spur], '.', color='#ff5d8f', ms=3.5, label='classified spur bins')
    if signal.size == power.size and signal.any():
        ax.plot(if_hz[signal], power[signal], '.', color='#00d1ff', ms=3.5, label='classified signal bins')
    ax.axhline(data['median_before_db'], color='#ffb000', lw=1.4, ls='--',
               label='legacy dB-median floor')
    ax.axhline(data['floor_before_db'], color='#8be28b', lw=1.4, ls='-.',
               label='bias-corrected floor')
    ax.axhline(data['floor_after_db'], color='#c792ea', lw=1.6,
               label='Gamma-mixture floor')
    ax.set_xlabel('IF (MHz)')
    ax.set_ylabel('power (dB relative)')
    ax.set_title('Before/after: floor preservation with explicit spur bins')
    ax.legend(frameon=False, fontsize=8, loc='best')
    fig.tight_layout()
    return _save(fig, path)


def save_report_figures(report, outdir, mixing=None, temperature=None):
    """Write the standard figure set and return the paths that were created."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, function in (('spur_slope', lambda p: plot_spur_slope(report, p)),
                           ('before_after', lambda p: plot_before_after(report, p)),
                           ('decision_flow', lambda p: plot_decision_flow(p))):
        try:
            paths[name] = function(outdir / (name + '.png'))
        except ValueError:
            pass
    if mixing:
        try:
            paths['mixing_diagram'] = plot_mixing_diagram(
                mixing['lo_hz'], mixing['rf_sources'], outdir / 'mixing_diagram.png',
                orders=mixing.get('orders', (1, 3, 5, 7)),
                rate_hz=mixing.get('rate_hz'))
        except (ValueError, KeyError):
            pass
    if temperature:
        try:
            paths['floor_vs_temperature'] = plot_floor_vs_temperature(
                temperature['times_s'], temperature['temps_c'], temperature['floor_db'],
                temperature.get('fit'), outdir / 'floor_vs_temperature.png')
        except (ValueError, KeyError):
            pass
    return paths
