"""Read-only reproducible profile of the captured vehicle database."""
import argparse,json,sqlite3
from pathlib import Path
import numpy as np

def profile(path, device='6094510'):
    c=sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    cols=['t','lat','lon','alt','speed','ve','vn','vu','gx','gy','gz','ax','ay','az','heading','pitch','roll','lat_std','lon_std','alt_std','heading_std','sat1','sat2','valid_pos','nav_mode']
    rows=c.execute('SELECT '+','.join(cols)+' FROM points WHERE device_id=? ORDER BY t',(device,)).fetchall()
    if not rows:raise ValueError('No samples for selected device')
    a=np.array([tuple(r) for r in rows],dtype=float);ix={k:i for i,k in enumerate(cols)}
    usable=(a[:,ix['valid_pos']]==1)&(a[:,ix['nav_mode']]!=0)
    med=np.nanmedian(a[usable],axis=0)
    north=(a[:,ix['lat']]-med[ix['lat']])*111195
    east=(a[:,ix['lon']]-med[ix['lon']])*111195*np.cos(np.radians(med[ix['lat']]))
    radius=np.hypot(north,east)
    radius[~usable]=np.nan # (0,0) initialization is unavailable, not a 12,000 km excursion.
    output={'rows':len(rows),'device_id':device,'valid_navigation':int(usable.sum()),'devices':[dict(x) for x in c.execute('SELECT id,first_t,last_t,point_count FROM devices WHERE id=?',(device,))],
            'statuses':[dict(x) for x in c.execute('SELECT status_text,count(*) n FROM points WHERE device_id=? GROUP BY status_text',(device,))],
            'median_anchor':{'lat':med[ix['lat']],'lon':med[ix['lon']]},'stats':{}}
    for key,values in [(k,a[:,i]) for k,i in ix.items() if k!='t']+[('radius_m',radius),('gyro_norm',np.linalg.norm(a[:,[ix[k] for k in ['gx','gy','gz']]],axis=1)),('accel_norm',np.linalg.norm(a[:,[ix[k] for k in ['ax','ay','az']]],axis=1))]:
        middle=np.nanmedian(values);output['stats'][key]=dict(zip(['min','p01','p05','p50','p95','p99','p999','max'],np.nanquantile(values,[0,.01,.05,.5,.95,.99,.999,1]).tolist()),mad=float(np.nanmedian(abs(values-middle))))
    output['counts']={'radius_gt_5m':int((radius>5).sum()),'radius_gt_10m':int((radius>10).sum()),'radius_gt_20m':int((radius>20).sum()),'speed_gt_0_3ms':int((a[:,ix['speed']]>.3).sum()),'speed_gt_1ms':int((a[:,ix['speed']]>1).sum())}
    output['hourly']=[dict(x) for x in c.execute("SELECT strftime('%Y-%m-%d %H:00',t,'unixepoch','+8 hours') hour,count(*) n,AVG(speed)*3.6 avg_kmh,MAX(speed)*3.6 max_kmh,MIN(lat) min_lat,MAX(lat) max_lat,MIN(lon) min_lon,MAX(lon) max_lon FROM points WHERE device_id=? GROUP BY hour",(device,))]
    c.close();return output

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('db');parser.add_argument('--output');parser.add_argument('--device',default='6094510');args=parser.parse_args()
    data=json.dumps(profile(args.db,args.device),ensure_ascii=False,indent=2)
    if args.output:Path(args.output).write_text(data+'\n')
    print(data)
