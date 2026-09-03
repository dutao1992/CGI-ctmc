"""Minute rollups for long-window visualization.

Raw points remain authoritative.  Rollups are deterministic, rebuildable
projections that preserve filtered mean/min/max envelopes and operational
summaries while avoiding a Python pass over hundreds of thousands of rows on
every chart request.
"""
import collections
import json
import math

from . import quality
from .rules import distance


METRICS = ['speed','heading','pitch','roll','gx','gy','gz','ax','ay','az','ve','vn','vu','alt',
           'lat_std','lon_std','alt_std','heading_std','roll_std','pitch_std','age','sat1','sat2',
           've_std','vn_std','vu_std','course','course_std']
ROLLUP_SECONDS = 60
ROLLUP_LEVELS = (60,600)
# Bump when the payload shape or motion-state semantics change; deployment
# rebuilds both levels before serving queries.
ROLLUP_VERSION = quality.VERSION * 100 + 7
TRACK_LIMIT = 20000
STATIONARY_POSITION_CANDIDATE = 'position_candidate'
STATIONARY_POSITION_CANDIDATES = 'position_candidates'
POSITION_CANDIDATE_LIMIT = 32
POSITION_CONTINUITY_TOLERANCE_M = 2.0
# A navigation jump can split one otherwise-contiguous stop into several
# route parts.  Keep those parts on one static anchor when the samples remain
# within the normal 3-second continuity window; a longer gap may be a new stop.
STATIONARY_ANCHOR_MAX_GAP_S = 3.0
ENDPOINT_FIELDS = list(dict.fromkeys(
    ['t','lat','lon','speed','heading','fix_mode','nav_mode','valid_pos','stationary_context','motion_state','lat_std','lon_std'] + METRICS
))


def bucket_start(t, seconds=ROLLUP_SECONDS):
    return int(math.floor(t / seconds) * seconds)


def _endpoint(p):
    return {key:p.get(key) for key in ENDPOINT_FIELDS}


def _transition(prev, current):
    delta = current['t'] - prev['t'] if prev else 0
    contiguous = bool(prev and 0 < delta <= 3)
    jump = bool(contiguous and prev.get('valid_pos') and current.get('valid_pos') and
                quality.navigation_position_drift(prev, current))
    covered = delta if contiguous and prev.get('valid_pos') and current.get('valid_pos') and not jump else 0
    mileage = moving = 0
    prev_state = _motion_state(prev) if prev else 'unknown'
    current_state = _motion_state(current)
    if contiguous and prev_state == 'moving' and current_state == 'moving':
        moving = delta
    if covered and prev_state == 'moving' and current_state == 'moving' and current.get('speed') is not None and prev.get('speed') is not None:
        mileage = (current['speed']+prev['speed'])/2*delta
    return dict(delta=delta,contiguous=contiguous,jump=jump,covered=covered,mileage=mileage,moving=moving,
                gap=bool(prev and delta > 3))


def _motion_state(p):
    state = p.get('motion_state')
    if state in ('moving', 'stationary', 'unknown'):
        return state
    return quality.motion_state(p)


def _position_confidence(p):
    """Rank a position by navigation solution credibility, not displacement."""
    if not p.get('valid_pos') or not all(isinstance(p.get(key), (int, float)) and
                                         math.isfinite(p[key]) for key in ('lat', 'lon')):
        return None
    nav_rank = {2: 3, 1: 2}.get(p.get('nav_mode'), 0)
    fix_rank = {4: 4, 8: 4, 5: 3, 9: 3, 3: 2, 2: 2, 1: 1}.get(p.get('fix_mode'), 0)
    uncertainty = max(p.get('lat_std') or 0, p.get('lon_std') or 0)
    # Navigation mode dominates, then fix type; lower reported uncertainty is
    # only a tie-breaker.  This is deliberately independent of static drift.
    return nav_rank * 100 + fix_rank * 10 - min(float(uncertainty), 100.0) / 100.0


def _position_candidate(p):
    confidence = _position_confidence(p)
    if confidence is None:
        return None
    return dict(lat=p['lat'], lon=p['lon'], t=p['t'], nav_mode=p.get('nav_mode'),
                fix_mode=p.get('fix_mode'), confidence=confidence)


def _bounded_position_candidates(candidates):
    """Keep credible and temporally distributed static fixes in a rollup."""
    candidates = [dict(item) for item in candidates if item]
    if len(candidates) <= POSITION_CANDIDATE_LIMIT:
        return candidates
    ranked = sorted(candidates, key=lambda item: (-item.get('confidence', -math.inf), item.get('t', math.inf)))
    temporal = sorted(candidates, key=lambda item: item.get('t', math.inf))
    keep = ranked[:POSITION_CANDIDATE_LIMIT // 2]
    for index in range(POSITION_CANDIDATE_LIMIT // 2):
        choice = temporal[round(index * (len(temporal) - 1) /
                                max(1, POSITION_CANDIDATE_LIMIT // 2 - 1))]
        if not any(choice.get('t') == item.get('t') for item in keep):
            keep.append(choice)
    return keep[:POSITION_CANDIDATE_LIMIT]


def _prefer_position_candidate(left, right):
    """Return the more credible candidate with deterministic tie-breaking."""
    if right is None:
        return left
    if left is None:
        return right
    return right if (right.get('confidence', -math.inf), -right.get('t', math.inf)) > \
        (left.get('confidence', -math.inf), -left.get('t', math.inf)) else left


def _specific_force_magnitude(p):
    """Return the gravity-included three-axis specific-force magnitude."""
    return quality.tri_axis_resultant(p)


def _vibration_stats(points):
    """Compact min/max/sum/sum-of-squares/count stats for one time bucket."""
    values = [value for _, value in points if isinstance(value, (int, float)) and math.isfinite(value)]
    if not values:
        return [None, None, 0.0, 0.0, 0]
    return [min(values), max(values), sum(values), sum(value * value for value in values), len(values)]


class RollupBuilder:
    """Build one independent time bucket from ordered joined point rows."""
    def __init__(self, device_id, start, seconds=ROLLUP_SECONDS):
        self.device_id = device_id
        self.start = int(start)
        self.seconds = int(seconds)
        self.count = self.fixed = self.valid = 0
        self.first = self.last = self.prev = None
        self.max_speed = None
        self.fix_counts = collections.Counter()
        self.metrics = {key:[None,None,0.0,0,None] for key in METRICS}
        self.quality_groups = collections.Counter()
        self.covered = self.mileage = self.moving = 0.0
        self.gaps = []
        self.segments = []
        self.current = None
        self.track = []
        self.route_break = True
        # Keep one preferred protocol per timestamp so GPCHC + GPCHCX pairs
        # cannot double-count the same vibration sample.  The map is bounded
        # by the current source bucket (60 s online; offline can promote to
        # the bounded 10/60/600/1800 s source levels).
        self.vibration_points = {}

    def add(self, row):
        raw = dict(row)
        q_version = raw.get('q_version')
        q_mask = raw.get('q_mask') or 0
        q_reasons = raw.get('q_reasons') or 0
        self.quality_groups[(q_version,q_mask,q_reasons)] += 1
        p = quality.project(raw)
        self.count += 1
        self.first = self.first or _endpoint(p)
        self.last = _endpoint(p)
        self.fixed += int(p['fix_mode'] in (4,8))
        self.valid += int(bool(p['valid_pos']))
        self.fix_counts[str(p['fix_mode'])] += 1
        # Confirmed stationary samples contribute zero even without a valid
        # position. Moving speeds still require a valid navigation solution.
        if p['speed'] is not None:
            self.max_speed = max(self.max_speed or 0,p['speed']*3.6)
        for metric in METRICS:
            value = p[metric]
            if value is None:
                continue
            a = self.metrics[metric]
            a[0] = value if a[0] is None else min(a[0],value)
            a[1] = value if a[1] is None else max(a[1],value)
            a[2] += value
            a[3] += 1
            a[4] = value

        magnitude = _specific_force_magnitude(p)
        if magnitude is not None:
            previous = self.vibration_points.get(p['t'])
            if previous is None or p.get('protocol') == 'GPCHCX':
                self.vibration_points[p['t']] = (p.get('protocol'), magnitude)

        transition = _transition(self.prev,p)
        self.covered += transition['covered']
        self.mileage += transition['mileage']
        self.moving += transition['moving']
        if transition['gap']:
            self.gaps.append([self.prev['t'],p['t']])
        state = _motion_state(p)
        if not self.current or state != self.current['state'] or not transition['contiguous'] or transition['jump']:
            if self.current:
                self.segments.append(self.current)
            self.current = {'start':p['t'],'end':p['t'],'state':state,'distance_m':0,
                            'max_kmh':p['speed']*3.6 if p['speed'] is not None else None,
                            'break_before':not transition['contiguous'] or transition['jump']}
        else:
            self.current['end'] = p['t']
            if p['speed'] is not None:
                self.current['max_kmh'] = max(self.current['max_kmh'] or 0,p['speed']*3.6)
        if state == 'stationary':
            candidate = _position_candidate(p)
            self.current[STATIONARY_POSITION_CANDIDATES] = _bounded_position_candidates(
                self.current.get(STATIONARY_POSITION_CANDIDATES, []) + ([candidate] if candidate else []))
            self.current[STATIONARY_POSITION_CANDIDATE] = _prefer_position_candidate(
                self.current.get(STATIONARY_POSITION_CANDIDATE), candidate)
        self.current['distance_m'] += transition['mileage']

        if p['valid_pos']:
            pt = {key:p.get(key) for key in ['t','lat','lon','speed','heading','fix_mode','nav_mode','stationary_context','motion_state']}
            pt['break_before'] = self.route_break
            if self.route_break or not self.track:
                self.track.append(pt)
            elif len(self.track)>1 and self.track[-1].get('tail'):
                self.track[-1] = dict(pt,tail=True)
            else:
                self.track.append(dict(pt,tail=True))
            self.route_break = transition['jump']
        elif self.prev and self.prev.get('valid_pos'):
            self.route_break = True
        self.prev = p

    def snapshot(self):
        if self.current:
            self.segments.append(self.current)
            self.current = None
        return dict(version=ROLLUP_VERSION,device_id=self.device_id,bucket_start=self.start,bucket_s=self.seconds,
                    count=self.count,first=self.first,last=self.last,fixed=self.fixed,valid=self.valid,
                    max_speed=self.max_speed,fix_counts=dict(self.fix_counts),metrics=self.metrics,
                    quality_groups=[[v if v is not None else None,m,r,n] for (v,m,r),n in self.quality_groups.items()],
                    covered_s=self.covered,mileage_m=self.mileage,moving_s=self.moving,
                    vibration=_vibration_stats(self.vibration_points.values()),
                    gaps=self.gaps,segments=self.segments,track=self.track)


class QueryCombiner:
    """Merge ordered raw/rollup snapshots into the public query projection."""
    def __init__(self, device_id, start, end, bins):
        self.device_id,self.start,self.end,self.bins = device_id,start,end,bins
        self.groups = {}
        self.count = self.fixed = self.valid = 0
        self.first = self.last = self.prev = None
        self.first_point = None
        self.max_speed = None
        self.fix_counts = collections.Counter()
        self.quality_groups = collections.Counter()
        self.covered = self.mileage = self.moving = 0.0
        self.gaps = []
        self.segments = []
        self.track = []
        self.track_truncated = False

    @staticmethod
    def _empty_vibration_stats():
        return [None, None, 0.0, 0.0, 0]

    def _key(self, t):
        return min(self.bins-1,max(0,int((t-self.start)/(self.end-self.start)*self.bins)))

    def _merge_metrics(self, snap):
        key = self._key((snap['first']['t']+snap['last']['t'])/2)
        group = self.groups.setdefault(key,{'t':snap['first']['t'],
                                            'values':{k:[None,None,0.0,0,None] for k in METRICS},
                                            'vibration':self._empty_vibration_stats(),
                                            'state_counts':collections.Counter()})
        group['t'] = min(group['t'],snap['first']['t'])
        for metric, source in snap['metrics'].items():
            target = group['values'][metric]
            lo,hi,total,n,last = source
            if n:
                target[0] = lo if target[0] is None else min(target[0],lo)
                target[1] = hi if target[1] is None else max(target[1],hi)
                target[2] += total
                target[3] += n
                target[4] = last
        source_vibration = snap.get('vibration') or self._empty_vibration_stats()
        target_vibration = group['vibration']
        lo, hi, total, squares, count = source_vibration
        if count:
            target_vibration[0] = lo if target_vibration[0] is None else min(target_vibration[0],lo)
            target_vibration[1] = hi if target_vibration[1] is None else max(target_vibration[1],hi)
            target_vibration[2] += total
            target_vibration[3] += squares
            target_vibration[4] += count
        for part in snap.get('segments', ()):
            state = part.get('state', 'unknown')
            duration = max(0.0, float(part.get('end', 0)) - float(part.get('start', 0)))
            # A one-sample segment still contributes a small, deterministic
            # vote; longer intervals dominate isolated sensor noise in the
            # display bucket without changing the raw per-sample decision.
            group['state_counts'][state] += max(duration, 0.1)

    def _merge_segments(self, snap, transition):
        parts = [dict(part) for part in snap['segments']]
        if not parts:
            return
        parts[0]['break_before'] = not transition['contiguous'] or transition['jump']
        parts[0]['distance_m'] += transition['mileage']
        for part in parts:
            if self.segments and not part.get('break_before') and self.segments[-1]['state'] == part['state']:
                previous = self.segments[-1]
                previous['end'] = part['end']
                previous['distance_m'] += part['distance_m']
                if previous['state'] == 'stationary':
                    previous[STATIONARY_POSITION_CANDIDATES] = _bounded_position_candidates(
                        previous.get(STATIONARY_POSITION_CANDIDATES, []) +
                        part.get(STATIONARY_POSITION_CANDIDATES, []))
                    previous[STATIONARY_POSITION_CANDIDATE] = _prefer_position_candidate(
                        previous.get(STATIONARY_POSITION_CANDIDATE),
                        part.get(STATIONARY_POSITION_CANDIDATE))
                if part.get('max_kmh') is not None:
                    previous['max_kmh'] = max(previous.get('max_kmh') or 0,part['max_kmh'])
            else:
                self.segments.append(part)

    @staticmethod
    def _stationary_anchor(segment, track):
        """Choose one static coordinate using neighboring motion and confidence."""
        candidates = segment.get(STATIONARY_POSITION_CANDIDATES) or []
        if not candidates and segment.get(STATIONARY_POSITION_CANDIDATE):
            candidates = [segment[STATIONARY_POSITION_CANDIDATE]]
        if not candidates:
            return None
        before = next((p for p in reversed(track)
                       if p['t'] < segment['start'] and p.get('motion_state') == 'moving' and
                       isinstance(p.get('lat'), (int, float)) and isinstance(p.get('lon'), (int, float))), None)
        after = next((p for p in track
                      if p['t'] > segment['end'] and p.get('motion_state') == 'moving' and
                      isinstance(p.get('lat'), (int, float)) and isinstance(p.get('lon'), (int, float))), None)
        # A stop should connect to the position occupied at both sides.  Use
        # their midpoint when both are available; this avoids following static
        # GNSS wander while retaining the selected in-segment fix.
        if before and after:
            target = dict(lat=(before['lat'] + after['lat']) / 2,
                          lon=(before['lon'] + after['lon']) / 2)
        elif before or after:
            target = before or after
        else:
            target = candidates[0]
        # The candidate is selected from the static samples.  Continuity first
        # rejects implausible drift; among fixes within a small physical
        # tolerance, navigation confidence wins.  This prevents a low-quality
        # fix from replacing a credible one merely because it is a fraction
        # closer to the midpoint of two moving samples.
        distances = {id(candidate): distance(candidate, target) for candidate in candidates}
        nearest = min(distances.values())
        eligible = [candidate for candidate in candidates
                    if distances[id(candidate)] <= nearest + POSITION_CONTINUITY_TOLERANCE_M]
        selected = max(eligible, key=lambda p: (p.get('confidence', -math.inf),
                                                  -distances[id(p)], -p['t']))
        return dict(lat=selected['lat'], lon=selected['lon'])

    @classmethod
    def _anchor_stationary_track(cls, track, segments):
        anchored = [dict(point) for point in track]
        index = 0
        while index < len(segments):
            segment = segments[index]
            if segment.get('state') != 'stationary':
                index += 1
                continue
            # A severe navigation jump marks a route break for rendering, but
            # it does not end a stationary interval.  Combine adjacent
            # stationary parts before selecting an anchor so static GNSS drift
            # cannot become a second (or third) displayed position.
            group = [segment]
            group_end = segment['end']
            next_index = index + 1
            while next_index < len(segments):
                candidate_part = segments[next_index]
                if candidate_part.get('state') != 'stationary':
                    break
                gap = float(candidate_part['start']) - float(group_end)
                if gap < 0 or gap > STATIONARY_ANCHOR_MAX_GAP_S:
                    break
                group.append(candidate_part)
                group_end = max(group_end, candidate_part['end'])
                next_index += 1
            combined = dict(start=group[0]['start'], end=group_end, state='stationary')
            candidates = []
            preferred = None
            for part in group:
                candidates.extend(part.get(STATIONARY_POSITION_CANDIDATES) or [])
                preferred = _prefer_position_candidate(
                    preferred, part.get(STATIONARY_POSITION_CANDIDATE))
            if candidates:
                combined[STATIONARY_POSITION_CANDIDATES] = _bounded_position_candidates(candidates)
            if preferred:
                combined[STATIONARY_POSITION_CANDIDATE] = preferred
            anchor = cls._stationary_anchor(combined, anchored)
            if not anchor:
                index = next_index
                continue
            for point in anchored:
                if combined['start'] <= point['t'] <= combined['end'] and point.get('motion_state') == 'stationary':
                    point['lat'], point['lon'] = anchor['lat'], anchor['lon']
                    point['position_source'] = 'stationary_anchor'
            index = next_index
        return anchored

    def _append_track(self, snap, transition):
        points = [dict(p) for p in snap['track']]
        if not points:
            return
        connectable = bool(snap['first'].get('valid_pos') and points[0]['t'] == snap['first']['t'])
        points[0]['break_before'] = not (connectable and transition['contiguous'] and not transition['jump'] and self.prev and self.prev.get('valid_pos'))
        for p in points:
            key = self._key(p['t'])
            # Preserve every source-bucket endpoint instead of collapsing a
            # long window to one point per display bucket.  This gives replay
            # enough anchors to interpolate smoothly while still keeping the
            # payload bounded.  Duplicate timestamps can occur where a raw
            # head/tail window meets a rollup; keep the latest projection.
            if self.track and self.track[-1]['t'] == p['t']:
                self.track[-1] = dict(p,_bucket=key)
            else:
                self.track.append(dict(p,_bucket=key))
        if len(self.track) > TRACK_LIMIT:
            self.track_truncated = True

    def add(self, snap):
        if not snap or not snap.get('count'):
            return
        transition = _transition(self.prev,snap['first'])
        self.first = self.first or snap['first']['t']
        self.first_point = self.first_point or snap['first']
        self.last = snap['last']['t']
        self.count += snap['count']; self.fixed += snap['fixed']; self.valid += snap['valid']
        self.max_speed = max(self.max_speed or 0,snap['max_speed'] or 0) if snap['max_speed'] is not None else self.max_speed
        self.fix_counts.update(snap['fix_counts'])
        for version,mask,reasons,n in snap['quality_groups']:
            self.quality_groups[(version,mask,reasons)] += n
        self.covered += snap['covered_s'] + transition['covered']
        self.mileage += snap['mileage_m'] + transition['mileage']
        self.moving += snap['moving_s'] + transition['moving']
        if transition['gap']:
            self.gaps.append([self.prev['t'],snap['first']['t']])
        self.gaps.extend(snap['gaps'])
        self._merge_metrics(snap)
        self._merge_segments(snap,transition)
        self._append_track(snap,transition)
        self.prev = snap['last']

    def snapshot(self, start, seconds):
        """Return a mergeable derived bucket, used to build coarser levels."""
        values = self.groups.get(0,{'values':{k:[None,None,0.0,0,None] for k in METRICS}})['values']
        vibration = self.groups.get(0,{}).get('vibration',self._empty_vibration_stats())
        track = []
        for point in self.track:
            item = dict(point);item.pop('_bucket',None);item.pop('tail',None);track.append(item)
        track = self._anchor_stationary_track(track, self.segments)
        return dict(version=ROLLUP_VERSION,device_id=self.device_id,bucket_start=int(start),bucket_s=int(seconds),
                    count=self.count,first=self.first_point,last=self.prev,fixed=self.fixed,valid=self.valid,
                    max_speed=self.max_speed,fix_counts=dict(self.fix_counts),metrics=values,
                    quality_groups=[[version,mask,reasons,n] for (version,mask,reasons),n in self.quality_groups.items()],
                    covered_s=self.covered,mileage_m=self.mileage,moving_s=self.moving,
                    vibration=vibration,
                    gaps=self.gaps,segments=self.segments,track=track)

    def quality_summary(self, contexts):
        reason_counts, field_counts = collections.Counter(),collections.Counter()
        excluded = anomalies = unavailable = status = pending = 0
        for (version,mask,reasons),n in self.quality_groups.items():
            if version != quality.VERSION:
                pending += n
                continue
            excluded += n if mask else 0
            anomalies += n if reasons & quality.ANOMALY_BITS else 0
            unavailable += n if reasons & quality.UNAVAILABLE_BITS else 0
            status += n if reasons & quality.STATUS_BITS else 0
            for key,bit in quality.REASON_BITS.items():
                if reasons & bit: reason_counts[key] += n
            for key,bit in quality.BITS.items():
                if mask & bit: field_counts[key] += n
        scopes = [s for s in contexts if s['start']<=self.end and quality.context_end(s)>=self.start]
        return dict(version=quality.VERSION,total=self.count,excluded_samples=excluded,anomaly_samples=anomalies,
                    unavailable_samples=unavailable,status_samples=status,pending_samples=pending,excluded_fields=dict(field_counts),
                    reasons=[dict(code=k,label=label,category=kind,count=reason_counts[k]) for k,(label,kind) in quality.REASONS.items()],
                    contexts=scopes,
                    policy=quality.policy_description())

    def finish(self, contexts, source, source_resolution_s, elapsed_ms):
        series = {key:[] for key in METRICS}
        motion_states = []
        vibration_series = []
        vibration_samples = 0
        vibration_sum = 0.0
        vibration_dynamic_squares = 0.0
        vibration_peak = None
        vibration_peak_to_peak = None
        for key in sorted(self.groups):
            group = self.groups[key]
            for metric,(lo,hi,total,n,last) in group['values'].items():
                mean = last if metric in ('heading','course') else total/n if n else None
                series[metric].append([group['t']*1000,mean,lo,hi])
            state_counts = group.get('state_counts') or {}
            if state_counts:
                state = max(state_counts, key=lambda value: (state_counts[value], value == 'moving'))
            else:
                state = 'unknown'
            lo,hi,total,squares,count = group.get('vibration',self._empty_vibration_stats())
            if count:
                mean = total / count
                variance = max(0.0, squares / count - mean * mean)
                rms = math.sqrt(variance)
                # The decision signal is absolute deviation from the 1 g
                # gravity resultant, not deviation from a moving bucket mean.
                peak = max(abs(lo - 1.0), abs(hi - 1.0))
                if not state_counts:
                    state = 'moving' if peak > quality.MOTION_IMPACT_THRESHOLD_G else 'stationary'
                vibration_series.append([
                    round(group['t'] * 1000), round(rms, 7), round(peak, 7),
                    round(mean, 7), round(lo, 7), round(hi, 7), count,
                ])
                vibration_samples += count
                vibration_sum += total
                vibration_dynamic_squares += max(0.0, squares - total * total / count)
                vibration_peak = peak if vibration_peak is None else max(vibration_peak, peak)
                span = hi - lo
                vibration_peak_to_peak = span if vibration_peak_to_peak is None else max(vibration_peak_to_peak, span)
                motion_states.append([round(group['t'] * 1000), state, round(peak, 7)])
            elif state_counts:
                motion_states.append([round(group['t'] * 1000), state, None])
        def bounded_track(points):
            if len(points) <= TRACK_LIMIT:
                selected = points
            else:
                # Uniformly retain the whole route, including its final point,
                # rather than slicing off the tail of a long selection.
                selected = [points[round(i*(len(points)-1)/(TRACK_LIMIT-1))] for i in range(TRACK_LIMIT)]
            return [dict(p) for p in selected]

        track = []
        for p in bounded_track(self.track):
            p = dict(p); p.pop('_bucket',None); p.pop('tail',None); track.append(p)
        track = self._anchor_stationary_track(track, self.segments)
        segments = []
        for segment in self.segments:
            item = dict(segment); item.pop('break_before',None)
            item.pop(STATIONARY_POSITION_CANDIDATE, None)
            item.pop(STATIONARY_POSITION_CANDIDATES, None)
            if item['end']-item['start'] >= 30:
                segments.append(item)
        span = self.last-self.first if self.first is not None else 0
        range_metrics = {
            'rms_g': round(math.sqrt(vibration_dynamic_squares / vibration_samples), 7) if vibration_samples else None,
            'peak_g': round(vibration_peak, 7) if vibration_peak is not None else None,
            'peak_to_peak_g': round(vibration_peak_to_peak, 7) if vibration_peak_to_peak is not None else None,
            'mean_g': round(vibration_sum / vibration_samples, 7) if vibration_samples else None,
        }
        vibration_range = dict(
            available=bool(vibration_series), start=self.start, end=self.end,
            reason=None if vibration_series else ('所选时段无采样' if not self.count else '筛选时段没有包含三轴比力的有效值'),
            samples=vibration_samples, buckets=len(vibration_series),
            series_fields=['timestamp_ms','rms_g','peak_g','mean_g','min_g','max_g','count'],
            series=vibration_series,
            metrics=range_metrics,
            method='筛选时间内按等时桶统计三轴比力合成模长；RMS 为桶内去均值动态幅值，峰值为桶内极值偏差',
            source=source, source_resolution_s=source_resolution_s,
            capability='10 Hz 仅用于 0-4 Hz 低频载体振动观察；聚合曲线用于趋势和取值，不用于轴承、齿轮等高频故障诊断',
        )
        return dict(device_id=self.device_id,start=self.start,end=self.end,total=self.count,track=track,
                    gaps=self.gaps[:2000],track_truncated=self.track_truncated or len(self.track)>TRACK_LIMIT,
                    series=series,motion_states=motion_states,quality=self.quality_summary(contexts),segments=segments[:1000],
                    vibration_range=vibration_range,
            summary=dict(first_t=self.first,last_t=self.last,distance_km=self.mileage/1000,moving_s=self.moving,
                         covered_s=self.covered,max_kmh=self.max_speed,fixed_pct=100*self.fixed/self.count if self.count else 0,
                         valid_pct=100*self.valid/self.count if self.count else 0,gap_count=len(self.gaps),
                         track_points=len(track),
                         speed_samples=sum(g['values']['speed'][3] for g in self.groups.values()),
                         speed_coverage_pct=100*sum(g['values']['speed'][3] for g in self.groups.values())/self.count if self.count else 0,
                         fix_counts=dict(self.fix_counts),sample_hz=(self.count-1)/span if span else 0),
                    aggregation=dict(buckets=len(self.groups),bucket_s=(self.end-self.start)/self.bins,
                                     source=source,source_resolution_s=source_resolution_s,query_ms=round(elapsed_ms,1),cache_hit=False,
                                     track_points=len(track),track_limit=TRACK_LIMIT,track_truncated=self.track_truncated or len(self.track)>TRACK_LIMIT,
                    method=quality.policy_description()+' 长窗口分层读取可重建的 60 秒或 10 分钟聚合，首尾读取原始采样。',
                                     timezone='Asia/Shanghai',coordinates='WGS84 原始坐标；前端高德底图单独转换为 GCJ-02 展示',
                                     mileage='可信运动且定位连续时的地速梯形积分；缺测不外推，不是轮速/CAN 里程'))


def encode(snapshot):
    return json.dumps(snapshot,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def decode(payload):
    return json.loads(payload)
