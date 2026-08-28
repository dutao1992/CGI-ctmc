"""CGI-430 V2.4.4 pp.5-12. Strict ASCII checksum, no binary guesswork."""
import datetime as dt
import math
import re
import zlib

FRAME = re.compile(rb'([$#][A-Z][A-Z0-9]+,[\x20-\x7e]{1,4096}?\*[0-9a-fA-F]{2,8})\r?\n')
GPS_EPOCH = dt.datetime(1980, 1, 6, tzinfo=dt.timezone.utc).timestamp()
LEAPS = ['1981-07-01','1982-07-01','1983-07-01','1985-07-01','1988-01-01',
         '1990-01-01','1991-01-01','1992-07-01','1993-07-01','1994-07-01',
         '1996-01-01','1997-07-01','1999-01-01','2006-01-01','2009-01-01',
         '2012-07-01','2015-07-01','2017-01-01']
LEAP_UTC = [dt.datetime.fromisoformat(x).replace(tzinfo=dt.timezone.utc).timestamp() for x in LEAPS]
BASE = ['heading','pitch','roll','gx','gy','gz','ax','ay','az','lat','lon','alt',
        've','vn','vu','speed','sat1','sat2']
EXT = ['lat_std','lon_std','alt_std','ve_std','vn_std','vu_std','roll_std','pitch_std','heading_std']
TAIL = ['course','course_std','lever_x','lever_y','lever_z','mount_x','mount_y','mount_z','antenna_angle','visible1','visible2']
NUMERIC = BASE + ['age'] + EXT + TAIL
NAV = {0:'初始化',1:'卫导模式',2:'组合导航',3:'纯惯导'}
FIX = {0:'无定位',1:'单点定位 / 定向',2:'伪距差分 / 定向',3:'组合推算',4:'RTK 固定解 / 定向',
       5:'RTK 浮点解 / 定向',6:'单点定位 / 未定向',7:'伪距差分 / 未定向',8:'RTK 固定解 / 未定向',9:'RTK 浮点解 / 未定向'}
WARN = {0:'GNSS 中断（保留位）',1:'轮速数据中断',2:'PPS 中断（保留位）',3:'陀螺异常（保留位）',
        4:'加表异常（保留位）',5:'主天线短路',6:'主天线断路',7:'副天线短路',8:'副天线断路',
        9:'移动网络（保留位）',10:'SIM（保留位）',11:'CORS（保留位）',12:'倒车状态',13:'CPU 降频',14:'CPU 高温'}
ACTIVE_WARNING_MASK = sum(1 << x for x in [1,5,6,7,8,13,14])


def gps_to_unix(week, seconds):
    if not 0 <= week <= 8191 or not 0 <= seconds < 604800:
        raise ValueError('GPS 时间越界')
    gps = GPS_EPOCH + week * 604800 + seconds
    offset = sum(gps >= t + n for n, t in enumerate(LEAP_UTC, 1))
    return round(gps - offset, 3)


def checksum(frame):
    body, check = frame[1:].rsplit(b'*', 1)
    if frame[:1] == b'$':
        value = 0
        for byte in body:
            value ^= byte
        return len(check) == 2 and value == int(check, 16)
    return len(check) == 8 and (zlib.crc32(body, 0xffffffff) ^ 0xffffffff) == int(check, 16)


def parse(frame, bound_sn=None):
    if not checksum(frame):
        raise ValueError('checksum')
    fields = frame[1:].split(b'*')[0].decode('ascii').split(',')
    protocol = fields[0]
    if protocol not in ('GPCHC', 'GPCHCX'):
        return None
    extended = protocol == 'GPCHCX'
    if len(fields) != (46 if extended else 24):
        raise ValueError('field_count')
    sn = fields[45] if extended else bound_sn
    if not sn:
        raise ValueError('unidentified_device')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', sn):
        raise ValueError('invalid_sn')
    week = int(fields[1])
    tow = float(fields[2])
    if not math.isfinite(tow):
        raise ValueError('invalid_time')
    result = dict(device_id=sn, protocol=protocol, week=week, tow=tow, t=gps_to_unix(week, tow))
    result.update({key: float(value) for key, value in zip(BASE, fields[3:21])})
    # Wire status 61 = high nibble 6 (single point, no heading), low nibble 1.
    if not re.fullmatch('[0-9A-Fa-f]{2}', fields[21]):
        raise ValueError('invalid_status')
    status = int(fields[21], 16)
    result.update(status_text=fields[21], fix_mode=status >> 4, nav_mode=status & 15,
                  age=float(fields[22]), warning=int(fields[23], 16))
    if result['nav_mode'] > 3 or result['fix_mode'] > 9 or not 0 <= result['warning'] <= 65535:
        raise ValueError('invalid_status')
    result.update({key: None for key in EXT + TAIL})
    if extended:
        if fields[33] != 'X':
            raise ValueError('separator')
        result.update({key: float(value) for key, value in zip(EXT, fields[24:33])})
        result.update({key: float(value) for key, value in zip(TAIL, fields[34:45])})
    if any(value is not None and not math.isfinite(value) for key, value in result.items() if key in NUMERIC):
        raise ValueError('nonfinite')
    if not (-90 <= result['lat'] <= 90 and -180 <= result['lon'] <= 180):
        raise ValueError('coordinate_range')
    if not (0 <= result['heading'] < 360 and -90 <= result['pitch'] <= 90 and -180 <= result['roll'] <= 180):
        raise ValueError('attitude_range')
    if not (0 <= result['speed'] <= 300 and 0 <= result['sat1'] <= 255 and 0 <= result['sat2'] <= 255):
        raise ValueError('range')
    if result['age'] < 0 or any(result[k] is not None and result[k] < 0 for k in EXT):
        raise ValueError('negative_quality')
    result['valid_pos'] = int(result['fix_mode'] != 0 and (result['lat'] != 0 or result['lon'] != 0))
    return result


def describe(row):
    return {**dict(row), 'nav_label': NAV.get(row['nav_mode'], '未知'),
            'fix_label': FIX.get(row['fix_mode'], '未知'),
            'warning_labels': [label for bit, label in WARN.items() if row['warning'] & (1 << bit)]}
