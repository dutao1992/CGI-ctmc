"""Bounded low-frequency vibration features for a 10 Hz navigation stream.

This module deliberately does not claim machine-condition or ISO 2631 analysis.
At 10 Hz the useful spectrum is limited to 0-4 Hz, so the result is a short,
read-only carrier/body vibration observation derived from filtered GPCHC(X)
specific-force samples.  No additional raw data is stored.
"""
import math
from statistics import median


EXPECTED_HZ = 10.0
MAX_WINDOW_S = 60.0
LOOKBACK_S = 180.0
MIN_SAMPLES = 100
MAX_FREQUENCY_HZ = 4.0
MAX_GAP_S = 0.16


def unavailable(reason, samples=0):
    return {
        'available': False,
        'reason': reason,
        'samples': samples,
        'capability': '10 Hz 仅用于 0-4 Hz 低频载体振动观察；设备端抗混叠特性未核验，频谱仅作趋势，不用于轴承、齿轮等高频故障诊断',
    }


def _segments(samples):
    ordered = []
    for sample in sorted(samples, key=lambda item: item['t']):
        values = [sample.get(key) for key in ('t', 'ax', 'ay', 'az')]
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
            continue
        item = {key: float(sample[key]) for key in ('t', 'ax', 'ay', 'az')}
        if ordered and item['t'] == ordered[-1]['t']:
            ordered[-1] = item
        else:
            ordered.append(item)
    result = []
    current = []
    for sample in ordered:
        if current and sample['t'] - current[-1]['t'] > MAX_GAP_S:
            result.append(current)
            current = []
        current.append(sample)
    if current:
        result.append(current)
    return result


def _linear_residual(values, times):
    mean_t = sum(times) / len(times)
    mean_v = sum(values) / len(values)
    denominator = sum((value - mean_t) ** 2 for value in times)
    slope = sum((t - mean_t) * (value - mean_v) for t, value in zip(times, values)) / denominator if denominator else 0.0
    intercept = mean_v - slope * mean_t
    return [value - (intercept + slope * t) for t, value in zip(times, values)]


def _spectrum(values, sample_hz, usable_max_hz):
    size = len(values)
    window = [0.5 - 0.5 * math.cos(2 * math.pi * index / (size - 1)) for index in range(size)]
    window_sum = sum(window)
    maximum_bin = min(size // 2, int(math.floor(usable_max_hz * size / sample_hz)))
    rows = []
    for frequency_bin in range(1, maximum_bin + 1):
        real = imaginary = 0.0
        for index, (value, weight) in enumerate(zip(values, window)):
            angle = 2 * math.pi * frequency_bin * index / size
            weighted = value * weight
            real += weighted * math.cos(angle)
            imaginary -= weighted * math.sin(angle)
        amplitude = 2 * math.hypot(real, imaginary) / window_sum
        rows.append([round(frequency_bin * sample_hz / size, 4), round(amplitude, 7)])
    return rows


def analyze(samples):
    """Return a compact time waveform, 1 s RMS envelope and Hann FFT."""
    candidates = [segment for segment in _segments(samples) if len(segment) >= MIN_SAMPLES]
    if not candidates:
        return unavailable('没有不少于 10 秒的连续三轴比力原始窗；不对缺测数据插值或补零', len(samples))
    segment = candidates[-1]
    intervals = [right['t'] - left['t'] for left, right in zip(segment, segment[1:]) if right['t'] > left['t']]
    sample_hz = 1.0 / median(intervals) if intervals else 0.0
    if not 8.0 <= sample_hz <= 10.5:
        return unavailable(f'连续窗采样率约 {sample_hz:.2f} Hz，不满足 10 Hz 低频分析前提', len(segment))
    maximum_samples = max(MIN_SAMPLES, int(round(sample_hz * MAX_WINDOW_S)) + 1)
    segment = segment[-maximum_samples:]
    times = [item['t'] - segment[0]['t'] for item in segment]
    magnitudes = [math.sqrt(item['ax'] ** 2 + item['ay'] ** 2 + item['az'] ** 2) for item in segment]
    residual = _linear_residual(magnitudes, times)
    rms_samples = max(2, int(round(sample_hz)))
    rolling_sum = 0.0
    envelope = []
    for index, value in enumerate(residual):
        rolling_sum += value * value
        if index >= rms_samples:
            rolling_sum -= residual[index - rms_samples] ** 2
        envelope.append(math.sqrt(rolling_sum / rms_samples) if index >= rms_samples - 1 else None)
    rms = math.sqrt(sum(value * value for value in residual) / len(residual))
    peak = max(abs(value) for value in residual)
    usable_max_hz = min(MAX_FREQUENCY_HZ, sample_hz * 0.4)
    spectrum = _spectrum(residual, sample_hz, usable_max_hz)
    dominant_candidates = [row for row in spectrum if row[0] >= 0.2]
    dominant = max(dominant_candidates, key=lambda row: row[1]) if dominant_candidates else [None, None]
    time_rows = [[round(item['t'] * 1000), round(value, 7), None if env is None else round(env, 7)]
                 for item, value, env in zip(segment, residual, envelope)]
    duration_s = segment[-1]['t'] - segment[0]['t']
    return {
        'available': True,
        'start': segment[0]['t'],
        'end': segment[-1]['t'],
        'samples': len(segment),
        'sample_hz': round(sample_hz, 4),
        'duration_s': round(duration_s, 3),
        'frequency_resolution_hz': round(sample_hz / len(segment), 4),
        'usable_frequency_hz': [0.2, round(usable_max_hz, 3)],
        'time': time_rows,
        'spectrum': spectrum,
        'metrics': {
            'rms_g': round(rms, 7),
            'peak_g': round(peak, 7),
            'peak_to_peak_g': round(max(residual) - min(residual), 7),
            'crest_factor': round(peak / rms, 4) if rms else 0.0,
            'dominant_hz': dominant[0],
            'dominant_amplitude_g': dominant[1],
        },
        'method': '三轴比力合成模长 -> 线性去趋势 -> 1 秒滑动 RMS；频谱使用 Hann 窗单边幅值 FFT',
        'source': '过滤后的连续 GPCHC(X) 原始采样；无插值、无补零、无新增落盘',
        'capability': '10 Hz 仅用于 0-4 Hz 低频载体振动观察；设备端抗混叠特性未核验，频谱仅作趋势，不用于轴承、齿轮等高频故障诊断',
    }
