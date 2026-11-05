// headroom demo fixture: builds an in-memory "monthly report" before asserting on it.
// With the default ROWS it needs well over a gigabyte of heap, which is more than a 2 GB VPS
// running Hermes can spare. ROWS=200000 is the light version.
const test = require("node:test");
const assert = require("node:assert");

const ROWS = Number(process.env.ROWS || 6_000_000);

function buildReport(rows) {
  const lines = [];
  for (let i = 0; i < rows; i++) {
    lines.push({ id: i, customer: `customer-${i % 5000}`, amount: (i * 7919) % 10000, tags: ["a", "b"] });
  }
  return lines;
}

test("monthly report totals add up", () => {
  const report = buildReport(ROWS);
  const total = report.reduce((sum, row) => sum + row.amount, 0);
  assert.ok(total > 0);
  assert.strictEqual(report.length, ROWS);
});
