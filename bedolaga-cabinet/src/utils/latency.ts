export function median(values: number[]): number | null {
  const v = values.filter((x) => Number.isFinite(x)).slice().sort((a, b) => a - b);
  if (v.length === 0) return null;
  const mid = Math.floor(v.length / 2);
  return v.length % 2 === 0 ? (v[mid - 1] + v[mid]) / 2 : v[mid];
}

// Drop the first sample (TLS/connection setup), then take the median of the rest.
export function effectiveLatency(samples: number[]): number | null {
  if (samples.length === 0) return null;
  const rest = samples.length > 1 ? samples.slice(1) : samples;
  return median(rest);
}

export type LatencyTier = 'fast' | 'ok' | 'slow';

export function latencyTier(ms: number): LatencyTier {
  if (ms < 80) return 'fast';
  if (ms <= 150) return 'ok';
  return 'slow';
}

// Sort reachable targets ascending by latency; unreachable (null) go last.
export function sortByLatency<T extends { latency: number | null }>(items: T[]): T[] {
  return items.slice().sort((a, b) => {
    if (a.latency === null && b.latency === null) return 0;
    if (a.latency === null) return 1;
    if (b.latency === null) return -1;
    return a.latency - b.latency;
  });
}

// Name of the best (lowest-latency, reachable) target, or null.
export function bestTargetName<T extends { name: string; latency: number | null }>(items: T[]): string | null {
  const reachable = items.filter((i) => i.latency !== null) as Array<T & { latency: number }>;
  if (reachable.length === 0) return null;
  return reachable.reduce((best, cur) => (cur.latency < best.latency ? cur : best)).name;
}
