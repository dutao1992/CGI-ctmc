"""Transparent engineering rules. Not legal violation or ISO comfort certification."""
import math
from .protocol import ACTIVE_WARNING_MASK

DEFAULTS = {'version': 1, 'speed_kmh': 80, 'accel_ms2': 3, 'brake_ms2': 3.5,
            'roll_deg': 15, 'pitch_deg': 12, 'shock_g': 0.5, 'age_s': 10,
            'position_std_m': 2, 'gap_s': 3, 'dwell_s': 2}
LABELS = {'fix_degraded':'定位质量降级','heading_unavailable':'惯导 / 定向未就绪',
          'hardware_warning':'设备告警','diff_age':'差分延迟超限','position_std':'位置不确定度偏大',
          'overspeed':'速度超业务阈值','acceleration':'急加速候选','braking':'急减速候选',
          'roll':'横滚超业务阈值','pitch':'俯仰超业务阈值','shock':'合加速度冲击候选',
          'data_gap':'数据时间断档','out_of_order':'设备时间倒序','position_jump':'位置跳变候选'}


def distance(a, b):
    p1, p2 = math.radians(a['lat']), math.radians(b['lat'])
    dlat, dlon = p2-p1, math.radians(b['lon']-a['lon'])
    h = math.sin(dlat/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlon/2)**2
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1, h)))


def conditions(p, previous, baseline, rules, mount_confirmed):
    """kind -> (value, threshold, severity, minimum persistence seconds)."""
    out = {}
    def add(kind, value, threshold, severity='warning', dwell=0):
        out[kind] = (round(value, 5), threshold, severity, dwell)
    if p['fix_mode'] not in (4, 8):
        add('fix_degraded', p['fix_mode'], 4, 'info')
    if p['nav_mode'] != 2 or p['fix_mode'] in (0,6,7,8,9):
        add('heading_unavailable', p['nav_mode'], 2, 'info')
    if p['warning'] & ACTIVE_WARNING_MASK:
        add('hardware_warning', p['warning'] & ACTIVE_WARNING_MASK, 0)
    if p['age'] is not None and p['age'] > rules['age_s']:
        add('diff_age', p['age'], rules['age_s'])
    std = max(p.get('lat_std') or 0, p.get('lon_std') or 0)
    if std > rules['position_std_m']:
        add('position_std', std, rules['position_std_m'], 'info')
    if previous:
        delta = p['t'] - previous['t']
        if delta < 0:
            add('out_of_order', -delta, 0)
        elif delta > rules['gap_s']:
            add('data_gap', delta, rules['gap_s'])
        elif delta > 0 and p['valid_pos'] and previous['valid_pos']:
            jump = distance(previous, p)
            tolerance = max(20, 5 * std, 5 * (previous.get('lat_std') or 0), 5 * (previous.get('lon_std') or 0))
            if jump > (max(p['speed'] or 0, previous['speed'] or 0) + 15) * delta + tolerance and not p.get('stationary_context'):
                add('position_jump', jump, tolerance)
    # Business dynamics only with fused navigation and acceptable position quality.
    qualified = p['valid_pos'] and p['nav_mode'] == 2 and std <= rules['position_std_m'] and p['speed'] is not None and not p.get('stationary_context')
    if qualified and p['speed'] * 3.6 > rules['speed_kmh']:
        add('overspeed', p['speed']*3.6, rules['speed_kmh'], 'warning', rules['dwell_s'])
    if qualified and baseline and baseline['speed'] is not None and not baseline.get('stationary_context') and baseline['nav_mode'] == 2 and baseline['valid_pos'] and 'position_jump' not in out and max(baseline.get('lat_std') or 0,baseline.get('lon_std') or 0) <= rules['position_std_m']:
        delta = p['t'] - baseline['t']
        if 0.8 <= delta <= 2.5:
            accel = (p['speed'] - baseline['speed']) / delta
            if accel > rules['accel_ms2']:
                add('acceleration', accel, rules['accel_ms2'], 'warning', 0.3)
            if -accel > rules['brake_ms2']:
                add('braking', -accel, rules['brake_ms2'], 'warning', 0.3)
    if qualified and mount_confirmed and p['speed'] > 2:
        for key in ('roll','pitch'):
            if p[key] is not None and abs(p[key]) > rules[key+'_deg']:
                add(key, abs(p[key]), rules[key+'_deg'], 'warning', rules['dwell_s'])
        shock = abs(math.sqrt(sum(p[k]**2 for k in ('ax','ay','az'))) - 1) if all(p[k] is not None for k in ('ax','ay','az')) else None
        if shock is not None and shock > rules['shock_g']:
            add('shock', shock, rules['shock_g'], 'warning', 0.1)
    return out
