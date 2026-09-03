"""Causal, uncertainty-gated horizontal ground speed (not wheel speed).

GPCHCX manual pp. 9-11: Ve/Vn and V_2D in m/s, component std in m/s.
All gates below are engineering acceptance limits, not vendor guarantees.
No coordinate differentiation, acceleration integration, zero-fill or clipping.
"""
from collections import deque
import math
from statistics import median

WINDOW_S = 2.0
MAX_GAP_S = 1.05
MAX_SIGMA_MS = .5
REST_SPEED_MS = .15
REST_SIGMA_MS = .25
QUIET_G = .01
MAX_SPEED_MS = 130 / 3.6
REFERENCE_MIN_S = 1.0
REFERENCE_MAX_GAP_S = .3
REFERENCE_MAX_DISPERSION_MS = .25
REFERENCE_METHOD = 'causal_vector_median_v1'


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def candidate(p):
    """Validate the *current* solution; V_2D is only an internal cross-check."""
    required = ('ve', 'vn', 'vu', 'speed', 've_std', 'vn_std', 'vu_std')
    if not all(finite(p.get(k)) for k in required):
        return None, None, 'missing_velocity_precision'
    speed = math.hypot(p['ve'], p['vn'])
    sigma = math.hypot(p['ve_std'], p['vn_std'])
    if p.get('nav_mode') not in (1, 2) or not p.get('valid_pos') or p.get('fix_mode') not in (1, 2, 4, 5, 6, 7, 8, 9):
        return None, sigma, 'navigation_unavailable'
    if min(p['ve_std'], p['vn_std'], p['vu_std']) < 0 or sigma > MAX_SIGMA_MS:
        return None, sigma, 'velocity_uncertain'
    if abs(p['speed'] - speed) > max(.05, .01 * speed):
        return None, sigma, 'velocity_fields_disagree'
    if speed > MAX_SPEED_MS or abs(p['vu']) > min(5., max(2., .5 * speed)) or p['vu_std'] > 1.:
        return None, sigma, 'velocity_solution_outlier'
    return speed, sigma, None


def quiet(p):
    if not all(finite(p.get(k)) for k in ('ax', 'ay', 'az', 'gx', 'gy', 'gz')):
        return False
    if p.get('warning', 0) & ((1 << 3) | (1 << 4)):
        return False
    return (abs(math.sqrt(sum(p[k]**2 for k in ('ax','ay','az'))) - 1) <= QUIET_G
            and math.sqrt(sum(p[k]**2 for k in ('gx','gy','gz'))) <= 1.)


def valid_reference(estimate):
    """Validate the display contract, including untrusted imported CSV JSON."""
    ref = estimate.get('reference')
    if ref is None:
        return True
    if (not isinstance(ref, dict) or estimate.get('value') is not None or
            estimate.get('state') != 'unknown' or
            estimate.get('reason') not in ('motion_unresolved', 'warming_up')):
        return False
    if not all(finite(ref.get(k)) and not isinstance(ref[k], bool) for k in
               ('value', 'sigma_ms', 'lower_ms', 'upper_ms', 'window_s', 'samples')):
        return False
    return (ref.get('method') == REFERENCE_METHOD and 0 <= ref['value'] <= MAX_SPEED_MS and
            0 <= ref['sigma_ms'] <= MAX_SIGMA_MS and REFERENCE_MIN_S <= ref['window_s'] <= WINDOW_S and
            ref['samples'] >= 5 and ref['samples'] == int(ref['samples']) and
            math.isclose(ref['lower_ms'], max(0., ref['value']-3*ref['sigma_ms']), abs_tol=1e-9) and
            math.isclose(ref['upper_ms'], ref['value']+3*ref['sigma_ms'], abs_tol=1e-9))


class Estimator:
    """Only the preceding 2 s of raw data matter; restart seeds that window.

    Held GNSS frames do not imply zero displacement/speed. A full good window
    is required after invalid navigation. No retrospective filling of warm-up.
    """
    def __init__(self):
        self.window = deque()

    def observe(self, p):
        result = self._observe_trusted(p)
        # A reference is a display-only estimate, never evidence of motion.
        # The two channels share a navigation solution, so do not average them
        # or claim independent-sensor precision gains.
        if result['value'] is not None or result['reason'] not in ('motion_unresolved', 'warming_up'):
            return result
        good = []
        for x in reversed(self.window):
            if (x['speed'] is None or x['nav'] != p.get('nav_mode') or
                    (good and good[-1]['t']-x['t'] > REFERENCE_MAX_GAP_S+1e-6)):
                break
            good.append(x)
        span = p['t']-good[-1]['t'] if good else 0
        if span < REFERENCE_MIN_S-1e-6 or len(good) < 5:
            return result
        ve, vn = median(x['ve'] for x in good), median(x['vn'] for x in good)
        residuals = sorted(math.hypot(x['ve']-ve, x['vn']-vn) for x in good)
        dispersion = 1.4826*median(residuals)
        # MAD alone misses a near-50/50 bimodal direction flip (11 vs 10 held
        # frames). The upper residual tail must also agree with the center.
        tail = residuals[math.ceil(.9*len(residuals))-1]
        if (dispersion > REFERENCE_MAX_DISPERSION_MS or
                tail > max(.3, 3*max(x['sigma'] for x in good)) or
                math.hypot(p['ve']-ve, p['vn']-vn) > max(.3, 3*result['sigma_ms'])):
            return result
        value = math.hypot(ve, vn)
        # Correlated/held frames cannot shrink uncertainty by sqrt(N).
        sigma = max(dispersion, max(x['sigma'] for x in good))
        result['reference'] = dict(value=value, sigma_ms=sigma,
                                  lower_ms=max(0., value-3*sigma), upper_ms=value+3*sigma,
                                  samples=len(good), window_s=round(span, 3), method=REFERENCE_METHOD)
        return result

    def _observe_trusted(self, p):
        t = p['t']
        if self.window and (t < self.window[-1]['t'] or t-self.window[-1]['t'] > MAX_GAP_S):
            self.window.clear()
        # Do not count two enabled protocols at one timestamp as two samples.
        if self.window and t == self.window[-1]['t']:
            self.window.pop()
        while self.window and self.window[0]['t'] < t-WINDOW_S-1e-6:
            self.window.popleft()
        speed, sigma, reason = candidate(p)
        item = dict(t=t, speed=speed, sigma=sigma, quiet=quiet(p),
                    ve=p.get('ve'), vn=p.get('vn'), nav=p.get('nav_mode'))
        prior = list(self.window)
        self.window.append(item)
        result = dict(value=None, state='unknown', reason=reason,
                      sigma_ms=sigma, source={1:'GNSS', 2:'GNSS/INS'}.get(p.get('nav_mode'), 'unavailable'),
                      window_s=round(t-self.window[0]['t'], 3))
        if reason:
            return result
        # Use a continuous, current-mode, quality-qualified suffix only.
        good = []
        for x in reversed(self.window):
            if x['speed'] is None:
                break
            good.append(x)
        span = t-good[-1]['t'] if good else 0
        if span >= WINDOW_S-1e-6 and len(good) >= 3 and all(
                x['speed'] <= REST_SPEED_MS and x['sigma'] <= REST_SIGMA_MS and x['quiet'] for x in good):
            result.update(value=0., state='stationary', reason='sustained_rest')
            return result
        if speed <= REST_SPEED_MS or speed <= 3*sigma:
            result['reason'] = 'motion_unresolved'
            return result
        motion = []
        for x in good:
            if x['speed'] <= REST_SPEED_MS or x['speed'] <= 3*x['sigma']:
                break
            motion.append(x)
        if not motion or t-motion[-1]['t'] < .5-1e-6 or len(motion) < 3:
            result['reason'] = 'warming_up'
            return result
        # Reject a sudden solution jump; never clamp it to the permitted rate.
        # Compare with the preceding 0.5 s, not 0.1 s coordinate differences.
        recent = [x for x in prior if x['speed'] is not None and t-x['t'] <= .5+1e-6 and x['nav'] == item['nav']]
        if recent:
            dv = math.hypot(p['ve']-median(x['ve'] for x in recent),
                            p['vn']-median(x['vn'] for x in recent))
            allowance = 3*max(.1, t-median(x['t'] for x in recent)) + 3*math.hypot(sigma, median(x['sigma'] for x in recent))
            if dv > max(.3, allowance):
                result['reason'] = 'velocity_jump'
                return result
        result.update(value=speed, state='moving', reason='qualified_velocity')
        return result
