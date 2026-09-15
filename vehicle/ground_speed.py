"""Versioned projection of the device-reported GPCHCX V_2D ground speed.

The effective speed is never reconstructed from Ve/Vn, coordinates, IMU
integration, smoothing, interpolation, or a previous sample. The IMU is used
only to classify the current sample: vibration-stationary samples become zero;
otherwise the original GPCHCX ``speed`` value is retained. Quality quarantine
in :mod:`vehicle.quality` can still reject an obvious navigation drift point.
"""
from collections import deque
import math


WINDOW_S = 0.0
MAX_SPEED_MS = 130 / 3.6
MOTION_IMPACT_THRESHOLD_G = .01
METHOD = 'gpchcx_v2d_vibration_zero_v1'


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def motion_state(p):
    """Classify one sample only from three-axis resultant deviation."""
    values = [p.get(key) for key in ('ax', 'ay', 'az')]
    if not all(finite(value) for value in values):
        return 'unknown'
    deviation = abs(math.sqrt(sum(value * value for value in values)) - 1.0)
    return 'moving' if deviation > MOTION_IMPACT_THRESHOLD_G else 'stationary'


def valid_reference(estimate):
    """Reject the removed v9 reference-speed channel in imported metadata."""
    return isinstance(estimate, dict) and estimate.get('reference') is None


class Estimator:
    """Pointwise GPCHCX.speed projection; retained as a migration interface."""

    def __init__(self):
        # Backfill uses this bounded cursor only to detect discontinuous repair
        # batches. No value in the deque influences another sample.
        self.window = deque(maxlen=1)

    def observe(self, p):
        self.window.append({'t': p.get('t')})
        result = dict(value=None, state='unknown', reason='motion_unknown',
                      sigma_ms=None, source='GPCHCX.speed', window_s=0,
                      method=METHOD)
        if p.get('protocol') != 'GPCHCX':
            result['reason'] = 'non_gpchcx'
            return result
        speed = p.get('speed')
        result['raw_speed'] = speed
        if not finite(speed) or speed < 0:
            result['reason'] = 'missing_speed'
            return result
        if speed > MAX_SPEED_MS:
            result['reason'] = 'velocity_solution_outlier'
            return result
        state = motion_state(p)
        if state == 'unknown':
            return result
        if state == 'stationary':
            result.update(value=0.0, state='stationary', reason='vibration_stationary')
        else:
            result.update(value=speed, state='moving', reason='gpchcx_speed')
        return result
