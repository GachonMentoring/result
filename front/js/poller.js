// 주기 실행기
// - 이전 실행이 끝난 뒤에 다음 타이머를 건다 (요청이 겹치지 않음)
// - 브라우저 탭이 숨겨지면 멈추고, 다시 보이면 즉시 실행
export function createPoller(task, intervalMs) {
  let timer = null;
  let running = false;
  let again = false;   // 실행 중에 refreshNow()가 불리면 끝나자마자 한 번 더
  let stopped = true;

  async function tick() {
    if (stopped) return;
    clearTimeout(timer);
    timer = null;
    running = true;
    try {
      await task();
    } catch (e) {
      console.error(e);
    } finally {
      running = false;
    }
    if (stopped || document.hidden) return;
    if (again) {
      again = false;
      tick();
    } else {
      timer = setTimeout(tick, intervalMs);
    }
  }

  function onVisibility() {
    if (document.hidden) {
      clearTimeout(timer);
      timer = null;
    } else if (!stopped && !running) {
      tick();
    }
  }

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      document.addEventListener("visibilitychange", onVisibility);
      tick();
    },
    stop() {
      stopped = true;
      clearTimeout(timer);
      timer = null;
      document.removeEventListener("visibilitychange", onVisibility);
    },
    refreshNow() {
      if (stopped) return;
      if (running) again = true;
      else tick();
    },
  };
}
