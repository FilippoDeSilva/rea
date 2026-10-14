import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { test } from "vitest";

test("signal verification rejects skipped identical UART bytes and reordered I2C samples", () => {
  const moduleUrl = new URL(
    "../../experiments/analysis-efficiency/signal-entry.mjs",
    import.meta.url,
  ).href;
  const source = `
    import assert from 'node:assert/strict';
    import {verifySignalRows,signalDataRows} from ${JSON.stringify(moduleUrl)};
    const sha256='a'.repeat(64);
    const rows=Array.from({length:8},(_,i)=>({start:i*100+10,end:i*100+80,byte:72}));
    const uart={kind:'uart',sha256,total_samples:2000,rows:rows.map((_,i)=>({start:i*100,end_exclusive:i*100+90,byte:72}))};
    verifySignalRows('uart',rows,uart,sha256);
    assert.throws(()=>verifySignalRows('uart',rows.slice(1).concat({start:810,end:880,byte:72}),uart,sha256),/interval/);
    assert.throws(()=>verifySignalRows('uart',rows.slice(1),uart,sha256),/coverage/);
    assert.throws(()=>verifySignalRows('uart',rows,uart,'b'.repeat(64)),/identity/);
    const data=Array.from({length:18},(_,i)=>({start:i*100+10,end:i*100+90,byte:117}));
    const raw=data.map((_,i)=>({byte:117,first_clock:i*100+20,last_clock:i*100+80}));
    const i2c={kind:'i2c',sha256,total_samples:2000,reads:[{bytes:[{byte:165},...raw]}]};
    verifySignalRows('i2c',data,i2c,sha256);
    assert.throws(()=>verifySignalRows('i2c',data.toReversed(),i2c,sha256),/interval/);
    assert.throws(()=>verifySignalRows('i2c',[{...data[0],byte:118},...data.slice(1)],i2c,sha256),/sample bits/);
    assert.throws(()=>signalDataRows('uart','9007199254740993-9007199254740994 uart-1: 48'),/sample interval/);
  `;
  // Checker semantics only; synthesized rows do not establish provider behavior.
  const result = spawnSync(process.execPath, ["--input-type=module", "-"], {
    input: source,
    encoding: "utf8",
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
});
