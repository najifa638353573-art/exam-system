function startTimer(seconds) {
  const el = document.getElementById('timer');
  let remaining = seconds;

  function fmt(n) {
    const m = Math.floor(n / 60);
    const s = n % 60;
    return String(m).padStart(2, '0') + ":" + String(s).padStart(2, '0');
  }

  function tick() {
    if (!el) return;
    el.textContent = fmt(remaining);
    if (remaining <= 0) {
      const form = document.getElementById('examForm');
      if (form) form.submit();
      return;
    }
    remaining -= 1;
    setTimeout(tick, 1000);
  }

  tick();
}
