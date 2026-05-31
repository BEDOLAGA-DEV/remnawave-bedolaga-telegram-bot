import { describe, expect, it } from 'vitest';
import { median, effectiveLatency, latencyTier, sortByLatency, bestTargetName } from './latency';

describe('latency utils', () => {
  it('median odd/even', () => {
    expect(median([3, 1, 2])).toBe(2);
    expect(median([4, 1, 2, 3])).toBe(2.5);
    expect(median([])).toBeNull();
  });
  it('effectiveLatency drops first sample', () => {
    expect(effectiveLatency([100, 20, 22, 24])).toBe(22);
    expect(effectiveLatency([50])).toBe(50);
    expect(effectiveLatency([])).toBeNull();
  });
  it('latencyTier thresholds', () => {
    expect(latencyTier(50)).toBe('fast');
    expect(latencyTier(120)).toBe('ok');
    expect(latencyTier(200)).toBe('slow');
  });
  it('sortByLatency puts unreachable last', () => {
    const r = sortByLatency([{ latency: 100 }, { latency: null }, { latency: 30 }]);
    expect(r.map((x) => x.latency)).toEqual([30, 100, null]);
  });
  it('bestTargetName ignores unreachable', () => {
    expect(bestTargetName([{ name: 'a', latency: null }, { name: 'b', latency: 40 }])).toBe('b');
    expect(bestTargetName([{ name: 'a', latency: null }])).toBeNull();
  });
});
