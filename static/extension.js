// PoC의 웹 ↔ 확장 메시지 계약. 실행·명령은 기존 api()로 리포트 소유권을 확인한다.
(() => {
  let reportId = null;
  let timer = null;
  let generation = 0;
  let installed = !!document.documentElement.dataset.idlyExtension;
  const terminal = new Set(['완료', '실패', '취소됨']);
  const post = (type, extra = {}) => window.postMessage({ source: 'idly-web', type, ...extra }, location.origin);

  window.addEventListener('message', (event) => {
    if (event.source !== window || event.origin !== location.origin || event.data?.source !== 'idly-ext' || event.data.type !== 'READY') return;
    installed = true;
    updateAvailability();
  });

  function updateAvailability() {
    document.querySelectorAll('.rp-extension-start').forEach((button) => { button.disabled = !installed; });
    const hint = document.querySelector('#extension-hint');
    if (hint) hint.textContent = installed
      ? '내 크롬에서 진행합니다. 로그인은 직접 하고, 최종 해지·탈퇴는 확정 버튼을 눌러야 진행돼요.'
      : '크롬 확장이 필요해요. chrome://extensions에서 개발자 모드를 켜고 IDly-report의 extension 폴더를 로드한 뒤 이 페이지를 새로고침해주세요.';
  }

  function button(label, onClick) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'rp-action';
    node.textContent = label;
    node.onclick = onClick;
    return node;
  }

  async function refresh(id, current) {
    if (current !== generation) return;
    try {
      const { runs } = await api(`/api/reports/${id}/extension-runs`);
      if (current !== generation) return;
      const list = document.querySelector('#extension-runs');
      if (!list) return;
      list.replaceChildren();
      for (const run of runs) {
        const row = document.createElement('div');
        row.className = 'rp-extension-run';
        const text = document.createElement('p');
        text.textContent = `${run.service} ${run.action} · ${run.status} · ${run.message}${run.error ? ` (${run.error})` : ''}`;
        row.append(text);
        if (!terminal.has(run.status)) {
          row.append(button('작업 탭 보기', () => post('FOCUS', { runId: run.id })));
          const choices = run.status === '확인 필요' ? [['confirm', '확정']] : run.status === '입력 필요' ? [['resume', '이어서 진행']] : [];
          choices.push(['cancel', '취소']);
          for (const [type, label] of choices) row.append(button(label, async () => {
            try {
              await api(`/api/reports/${id}/extension-runs/${run.id}/commands`, { method: 'POST', body: JSON.stringify({ type }) });
              await refresh(id, current);
            } catch (error) { showError('#extension-error', error.message); }
          }));
        }
        list.append(row);
      }
      showError('#extension-error', '');
    } catch (error) {
      if (current === generation) showError('#extension-error', error.message);
    }
  }

  window.mountExtension = (id, enabled) => {
    clearTimeout(timer);
    generation++;
    reportId = id;
    if (!enabled) return;
    const current = generation;
    const panel = document.createElement('section');
    panel.className = 'rp rp-card';
    panel.innerHTML = '<h3>브라우저 확장 작업</h3><p id="extension-hint"></p><p id="extension-error" role="alert" hidden></p><div id="extension-runs" aria-live="polite"></div>';
    document.querySelector('#report-body').append(panel);
    document.querySelectorAll('#report-body [data-extension-action]').forEach((link) => {
      const start = button('내 크롬에서 진행', async () => {
        if (!installed || !window.confirm('확장이 현재 사이트의 화면 글자 일부와 버튼·링크 목록을 IDly 서버와 OpenAI로 보내 해지·탈퇴를 진행합니다. 비밀번호·쿠키는 보내지 않으며, 로그인과 최종 확정은 직접 합니다. 동의하고 시작할까요?')) return;
        start.disabled = true;
        try {
          const run = await api(`/api/reports/${id}/extension-runs`, {
            method: 'POST', body: JSON.stringify({ account_id: link.dataset.accountId, action: link.dataset.extensionAction, consented: true }),
          });
          post('START', { run });
          await refresh(id, current);
        } catch (error) { showError('#extension-error', error.message); }
        finally { start.disabled = !installed; }
      });
      start.classList.add('rp-extension-start');
      link.after(start);
    });
    updateAvailability();
    post('PING');
    const tick = async () => {
      if (current !== generation || reportId !== id || document.querySelector('#report').hidden) return;
      await refresh(id, current);
      if (current === generation) timer = setTimeout(tick, 2000);
    };
    // renderResult()가 report 화면을 표시한 뒤 시작한다.
    timer = setTimeout(tick, 0);
  };
})();
