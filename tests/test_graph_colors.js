/* Run with node tests/test_graph_colors.js. No service or credentials required. */
'use strict';
const assert=require('node:assert/strict'),colors=require('../static/graph_colors.js');
const rgb=value=>value.match(/\d+/g).map(Number);
const light=value=>rgb(value).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
let checks=0;
for(const [,base] of [...colors.PRESETS,['gray','#888888'],['black','#000000'],['white','#ffffff'],['custom','#15adf2']]){
 const scale=colors.createScale([0,10,100],'cited',base),stops=colors.stops(base);
 assert.equal(scale.color(0),'rgb('+stops[0].join(',')+')');
 assert.equal(scale.color(100),'rgb('+stops[2].join(',')+')');
 let previous=-Infinity;
 for(const n of [0,1,5,10,30,60,100]){const current=scale.color(n);assert(rgb(current).every(v=>v>=0&&v<=255));assert(light(current)>=previous-1);previous=light(current);}
 assert.equal(scale.color(null),'#68778a');assert.equal(scale.color(NaN),'#68778a');assert.equal(scale.color(-1),'#68778a');checks++;
}
assert.equal(colors.createScale([null,NaN,Infinity],'year').color(2026),'#68778a');checks++;
assert(!colors.createScale([2026,2026],'year').color(2026).includes('NaN'));checks++;
const years=colors.createScale([2020,2026],'year');assert(light(years.color(2020))<light(years.color(2026)));checks++;
assert.equal(colors.setBase('url(invalid)'),false);assert.equal(colors.setBase('#19A2BD'),true);assert.equal(colors.getBase(),'#19a2bd');checks++;
assert.deepEqual(colors.stops('invalid'),colors.stops(colors.DEFAULT));assert.equal(colors.CATEGORIES.length,6);checks++;
console.log(JSON.stringify({checks,failed:0}));
