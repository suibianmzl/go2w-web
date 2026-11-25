// 把未调整、未检查和实际调整明确区分，避免界面凭已保存状态补造优化结论。
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../static/js/taught_geometry.js',import.meta.url),'utf8');
const G = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
assert.equal(typeof G.taughtProcessingSummary,'function','界面需要复用真实处理摘要');
assert.equal(G.taughtProcessingSummary(null),'');
const zero=G.taughtProcessingSummary({input_count:3,output_count:81,interpolation:'cubic_spline',max_offset:0,iterations:0,full_sweep_checked:true});
assert.match(zero,/三次样条/);assert.match(zero,/3.*81/);assert.match(zero,/无需额外调整/);assert.doesNotMatch(zero,/改善|提升|平滑/,'样条生成不暗示发生额外的后处理优化');
const adjusted=G.taughtProcessingSummary({input_count:12,output_count:120,interpolation:'recorded',max_offset:.0123,iterations:4,full_sweep_checked:false});
assert.match(adjusted,/录制采样/);assert.match(adjusted,/1.23 cm/);assert.match(adjusted,/4 次/);assert.match(adjusted,/足迹未核验/);
console.log('PASS 处理摘要显示实际点数、厘米单位、迭代与足迹状态');
