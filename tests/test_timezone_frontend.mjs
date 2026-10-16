import assert from 'node:assert/strict';
import { formatTimestamp } from '../web/dates.js';

assert.equal(formatTimestamp('2026-03-08T07:59:59Z', 'America/Los_Angeles'), '2026-03-07 23:59');
assert.equal(formatTimestamp('2026-03-08T09:59:59Z', 'America/Los_Angeles', true), '01:59:59');
assert.equal(formatTimestamp('2026-03-08T10:00:00Z', 'America/Los_Angeles', true), '03:00:00');
assert.equal(formatTimestamp('2026-10-16T00:00:00Z', 'Asia/Kolkata'), '2026-10-16 05:30');
assert.equal(formatTimestamp('2026-10-16T00:00:00Z', 'UTC'), '2026-10-16 00:00');
assert.equal(formatTimestamp('', 'UTC'), '');
assert.equal(formatTimestamp('invalid', 'UTC'), '');
console.log('Timezone formatting checks passed');
