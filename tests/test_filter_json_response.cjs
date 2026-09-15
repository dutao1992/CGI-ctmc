'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const appSource=fs.readFileSync(require.resolve('../static/app.js'),'utf8');
const START=1787328000; // 2026-08-22 00:00:00 Asia/Shanghai
const END=1788494400;   // 2026-09-04 12:00:00 Asia/Shanghai
const AUTH_HTML='<!DOCTYPE html>\n<html>\n<head><title>401 Authorization Required</title></head>\n<body><center><h1>401 Authorization Required</h1></center></body>\n</html>\n';

function apiHarness(){
  let request;
  const context={
    console,
    fetch:async(url,options)=>{
      request={url,options};
      return {
        ok:false,
        status:401,
        headers:{get(name){return name.toLowerCase()==='content-type'?'text/html; charset=utf-8':null;}},
        text:async()=>AUTH_HTML,
        json:async()=>JSON.parse(AUTH_HTML)
      };
    }
  };
  vm.createContext(context);
  const start=appSource.indexOf('async function api');
  const end=appSource.indexOf('\nfunction setView',start);
  vm.runInContext(appSource.slice(start,end)+'\nthis.apiUnderTest=api;',context);
  return {api:context.apiUnderTest,request:()=>request};
}

test('8/22-to-now filter turns an HTML auth response into an actionable error',async()=>{
  const h=apiHarness();
  await assert.rejects(
    ()=>h.api(`query?device=6094510&start=${START}&end=${END}&bins=320`),
    error=>{
      assert.equal(error.message,'登录已过期，请返回质检平台重新登录');
      return true;
    }
  );
  assert.equal(h.request().url,`/vehicle/api/query?device=6094510&start=${START}&end=${END}&bins=320`);
});
