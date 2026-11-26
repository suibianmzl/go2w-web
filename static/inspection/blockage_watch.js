export const FOOTWAY_NOTE = 'position_footway';
export const FOOTWAY_BLOCK_SPEED = 0.3;
export const FOOTWAY_BLOCK_MS = 10000;
export const FOOTWAY_POSE_MAX_GAP_MS = 1500;

export function footwayLowSpeedActive(task, note, pose, threshold = FOOTWAY_BLOCK_SPEED){
  if (!footwayVisitKey(task, note) || !pose?.valid || !Number.isFinite(pose.v)) return false;
  return Math.abs(Number(pose.v)) < threshold;
}

export function footwayVisitKey(task, note){
  if (!task || task.type !== 'route' || task.status !== 'active' || task.back) return '';
  if (String(note || '').trim().toLowerCase() !== FOOTWAY_NOTE) return '';
  return [
    String(task.routeId || ''),
    Number(task.loop) || 1,
    Number(task.point) || 1,
  ].join(':');
}

export function createFootwayBlockageWatch({
  threshold = FOOTWAY_BLOCK_SPEED,
  durationMs = FOOTWAY_BLOCK_MS,
  maxGapMs = FOOTWAY_POSE_MAX_GAP_MS,
} = {}){
  let visitKey = '';
  let slowSince = null;
  let lastObservedAt = null;
  const reported = new Set();

  function resetTimer(){
    visitKey = '';
    slowSince = null;
    lastObservedAt = null;
  }

  return {
    resetRun(){
      resetTimer();
      reported.clear();
    },
    observe(task, note, pose, now = Date.now()){
      if (lastObservedAt !== null && now - lastObservedAt > maxGapMs){
        slowSince = null;
      }
      lastObservedAt = now;
      const key = footwayVisitKey(task, note);
      if (!key || !pose?.valid || !Number.isFinite(pose.v)){
        resetTimer();
        return null;
      }
      if (visitKey !== key){
        visitKey = key;
        slowSince = null;
      }
      if (Math.abs(Number(pose.v)) >= threshold){
        slowSince = null;
        return null;
      }
      if (slowSince === null){
        slowSince = now;
        return null;
      }
      if (now - slowSince < durationMs || reported.has(key)) return null;
      reported.add(key);
      return key;
    },
  };
}
