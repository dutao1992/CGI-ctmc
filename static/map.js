/* Display-only coordinate adapter. Raw telemetry and exports remain WGS84. */
(function(root,factory){
  if(typeof module==='object'&&module.exports)module.exports=factory(require('./vendor/coordtransform.js'));
  else root.CTMCMap=factory(root.coordtransform);
})(typeof self!=='undefined'?self:this,function(transform){
  'use strict';
  function latLng(point){
    if(!Number.isFinite(point.lat)||!Number.isFinite(point.lon)||Math.abs(point.lat)>90||Math.abs(point.lon)>180)throw new Error('无效地图坐标');
    const [lon,lat]=transform.wgs84togcj02(point.lon,point.lat);
    return [lat,lon];
  }
  function tileLayer(L,notice){
    const layer=L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}',{
      subdomains:'1234',minZoom:3,maxZoom:18,updateWhenIdle:true,keepBuffer:2,
      attribution:'© <a href="https://www.amap.com/" target="_blank" rel="noopener">高德地图</a>'
    });
    const failed=new Set();
    const refresh=()=>{notice.hidden=failed.size===0;};
    layer.on('loading',()=>{failed.clear();refresh();});
    layer.on('tileerror',e=>{failed.add(e.tile);refresh();});
    layer.on('tileload',e=>{failed.delete(e.tile);refresh();});
    layer.on('tileunload',e=>{failed.delete(e.tile);refresh();});
    return layer;
  }
  return Object.freeze({latLng,tileLayer});
});
