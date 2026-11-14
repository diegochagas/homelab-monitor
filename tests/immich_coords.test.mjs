import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const { parseCoordinates } = createRequire(import.meta.url)('../homelab_monitor/immich_review_static/coords.js');

test('pasted pair with comma and space', () => {
  assert.deepEqual(parseCoordinates('-23.933581, -46.328830'), { lat: -23.933581, lon: -46.32883 });
});

test('other separators and surrounding spaces', () => {
  assert.deepEqual(parseCoordinates('  1.5;2.5 '), { lat: 1.5, lon: 2.5 });
  assert.deepEqual(parseCoordinates('1.5 2.5'), { lat: 1.5, lon: 2.5 });
  assert.deepEqual(parseCoordinates('10,20'), { lat: 10, lon: 20 });
});

test('place names, single numbers and out-of-range values are not coordinates', () => {
  assert.equal(parseCoordinates('Curitiba'), null);
  assert.equal(parseCoordinates('12345'), null);
  assert.equal(parseCoordinates('95, 10'), null);
  assert.equal(parseCoordinates('10, 190'), null);
});
