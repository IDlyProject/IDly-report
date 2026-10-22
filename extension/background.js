// IDly 확장 에이전트. 사용자 크롬의 새 탭에서 탈퇴·해지를 진행한다.
//
// - 이미 로그인된 브라우저를 그대로 쓴다. 비밀번호·쿠키는 브라우저 밖으로 나가지 않는다.
// - 매 단계 화면의 버튼·링크 목록과 글자 일부를 IDly 서버로 보내 다음 행동을 받는다 (AI는 서버에서).
//   비밀번호 칸이 보이는 화면의 글자는 보내지 않고, 사용자에게 로그인을 맡긴다.
// - 되돌릴 수 없는 버튼은 서버가 confirm=true로 표시하고, 사용자가 확정해야 누른다.
// - 진행 상황은 작업 탭 위의 패널과 IDly 웹 정리 내역에 같이 보인다.

const MAX_STEPS = 25
const USER_WAIT_MS = 15 * 60 * 1000
const runs = new Map() // runId -> 작업 상태

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

class Cancelled extends Error {}

const REPORT_ORIGINS = new Set(chrome.runtime.getManifest().content_scripts[0].matches.map((pattern) => new URL(pattern).origin))
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg.type === 'IDLY_PANEL') {
    const run = runs.get(msg.runId)
    if (run && sender.tab?.id === run.tabId && ['resume', 'confirm', 'cancel'].includes(msg.command)) run.local.push(msg.command)
    return
  }
  if (!sender.url || !REPORT_ORIGINS.has(new URL(sender.url).origin)) return
  if (msg.type === 'IDLY_START') {
    if (!msg.run || msg.run.apiBase !== new URL(sender.url).origin || !/^https:\/\//.test(msg.run.startUrl)) return
  }
  if (msg.type === 'IDLY_START' && msg.run && !runs.has(msg.run.id)) startRun(msg.run)
  if (msg.type === 'IDLY_FOCUS') focusRun(msg.runId)
})

// 로그인 팝업·새 탭을 따라간다
chrome.tabs.onCreated.addListener((tab) => {
  for (const r of runs.values()) {
    if (tab.openerTabId === r.tabId && !r.finished) {
      r.previousTabs.push(r.tabId)
      r.tabId = tab.id
    }
  }
})
chrome.tabs.onRemoved.addListener((tabId) => {
  for (const r of runs.values()) {
    if (tabId !== r.tabId || r.finished) continue
    const previous = r.previousTabs.pop()
    if (previous) r.tabId = previous
    else r.local.push('cancel') // 사용자가 작업 탭을 닫으면 취소
  }
})

function startRun(info) {
  const r = { info, tabId: null, previousTabs: [], local: [], step: 0, finished: false, status: '대기', message: '' }
  runs.set(info.id, r)
  runAgent(r)
    .catch(async (e) => {
      if (e instanceof Cancelled) await report(r, '취소됨', '취소했어요')
      else await report(r, '실패', '진행하지 못했어요', String(e.message || e).slice(0, 200))
    })
    .finally(() => {
      r.finished = true
      runs.delete(info.id)
    })
}

async function focusRun(runId) {
  const r = runs.get(runId)
  if (!r?.tabId) return
  const tab = await chrome.tabs.update(r.tabId, { active: true })
  await chrome.windows.update(tab.windowId, { focused: true })
}

// --- IDly 서버 ---

async function api(r, path, body) {
  const res = await fetch(`${r.info.apiBase}${path}`, {
    method: body ? 'POST' : 'GET',
    headers: { Authorization: `Bearer ${r.info.token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => null)
  if (!res.ok) throw new Error(data?.detail || `IDly 서버 오류 (${res.status})`)
  return data
}

async function report(r, status, message, error = null) {
  r.status = status
  r.message = message
  await showPanel(r)
  try {
    await api(r, `/api/agent/${r.info.id}/status`, { status, message, step: r.step, error })
  } catch {
    // 끝난 작업(토큰 만료) 등은 무시
  }
}

// 웹(정리 내역)과 작업 탭 패널에서 온 명령
async function takeCommands(r) {
  const commands = r.local.splice(0)
  try {
    commands.push(...(await api(r, `/api/agent/${r.info.id}/commands`)).commands)
  } catch {
    // 잠깐 연결이 끊겨도 계속
  }
  if (commands.includes('cancel')) throw new Cancelled()
  return commands
}

// --- 에이전트 ---

async function runAgent(r) {
  const { info } = r
  const tab = await chrome.tabs.create({ url: info.startUrl, active: true })
  r.tabId = tab.id
  await waitLoad(r)
  await report(r, '진행 중', `${info.service} 사이트를 열었어요`)
  const history = []

  for (let step = 0; step < MAX_STEPS; step++) {
    r.step = step
    await takeCommands(r)
    const page = await collect(r)
    if (!page) {
      await sleep(1000)
      continue
    }
    // 비밀번호 칸이 보이면 AI에게 묻지 않고 사용자에게 로그인을 맡긴다
    if (page.password) {
      await waitLogin(r, page.url)
      history.push('사용자가 로그인·본인인증을 마침')
      continue
    }

    const d = await api(r, `/api/agent/${info.id}/decide`, {
      url: page.url, text: page.text, elements: page.elements, history, step,
    })
    await report(r, '진행 중', d.reason || '다음 단계를 찾는 중')

    if (d.decision === 'done') {
      await report(r, '완료', `${info.service} ${info.action}을(를) 마쳤어요`)
      return
    }
    if (d.decision === 'fail') throw new Error(d.reason || `${info.action} 방법을 찾지 못했어요`)
    if (d.decision === 'need_login') {
      await waitLogin(r, page.url)
      history.push('사용자가 로그인·본인인증을 마침')
      continue
    }
    if (d.decision === 'skip') {
      history.push(d.reason)
      continue
    }
    if (d.decision === 'goto') {
      await chrome.tabs.update(r.tabId, { url: d.url })
      await waitLoad(r)
      history.push(`주소 이동: ${d.url}`)
      continue
    }
    if (d.decision === 'click') {
      if (d.confirm) {
        await waitConfirm(r, `'${d.label}'을(를) 누르면 ${info.service} ${info.action}이(가) 진행될 수 있어요. 확정할까요?`)
      }
      const clicked = await click(r, d.index)
      await sleep(800)
      await waitLoad(r)
      history.push(clicked ? `클릭: ${d.label}${d.confirm ? ' (사용자 확정)' : ''}` : `'${d.label}'을(를) 찾지 못함`)
      continue
    }
    history.push(`알 수 없는 판단: ${d.decision}`)
  }
  throw new Error('단계가 너무 많아 멈췄어요. 직접 처리로 이어가 주세요.')
}

// 사용자가 이 탭에서 직접 로그인·본인인증하는 동안 기다린다.
// 비밀번호 칸이 사라지거나 주소가 바뀌면 자동으로, 또는 '이어서 진행'을 누르면 이어간다.
async function waitLogin(r, startUrl) {
  await report(r, '입력 필요', `${r.info.service}에 로그인(본인인증)해주세요. 끝나면 자동으로 이어가요.`)
  let sawPassword = false
  const started = Date.now()
  while (Date.now() - started < USER_WAIT_MS) {
    if ((await takeCommands(r)).includes('resume')) break
    const page = await collect(r)
    if (page?.password) sawPassword = true
    if (page && !page.password && (sawPassword || page.url !== startUrl)) break
    await sleep(1000)
  }
  if (Date.now() - started >= USER_WAIT_MS) throw new Error('로그인을 기다리다 시간이 지났어요.')
  await sleep(1000)
  await waitLoad(r)
  await report(r, '진행 중', '로그인을 확인했어요. 이어서 진행할게요.')
}

async function waitConfirm(r, message) {
  await report(r, '확인 필요', message)
  const started = Date.now()
  while (Date.now() - started < USER_WAIT_MS) {
    if ((await takeCommands(r)).includes('confirm')) {
      await report(r, '진행 중', '확정했어요. 진행할게요.')
      return
    }
    await sleep(1000)
  }
  throw new Error('확정을 기다리다 시간이 지났어요.')
}

async function waitLoad(r, timeoutMs = 15000) {
  const started = Date.now()
  while (Date.now() - started < timeoutMs) {
    try {
      const tab = await chrome.tabs.get(r.tabId)
      if (tab.status === 'complete') return
    } catch {
      return
    }
    await sleep(300)
  }
}

// --- 페이지 안에서 도는 함수 (chrome.scripting.executeScript) ---

async function inTab(r, func, args = []) {
  try {
    const [result] = await chrome.scripting.executeScript({ target: { tabId: r.tabId }, func, args })
    return result?.result
  } catch {
    return null // 페이지 이동 중이거나 chrome:// 같은 곳
  }
}

function collect(r) {
  return inTab(r, () => {
    document.querySelectorAll('[data-idly-idx]').forEach((e) => e.removeAttribute('data-idly-idx'))
    const selector =
      'a, button, [role=button], [role=menuitem], [role=tab], [role=checkbox], input[type=submit], input[type=button], summary, label'
    const elements = []
    for (const el of document.querySelectorAll(selector)) {
      if (el.closest('#idly-agent-panel')) continue
      const rect = el.getBoundingClientRect()
      const style = getComputedStyle(el)
      if (rect.width < 4 || rect.height < 4 || style.visibility === 'hidden' || style.display === 'none') continue
      const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 80)
      if (!text) continue
      el.setAttribute('data-idly-idx', String(elements.length))
      elements.push({ i: elements.length, tag: el.tagName.toLowerCase(), text, href: (el.getAttribute('href') || '').slice(0, 120) })
      if (elements.length >= 150) break
    }
    const password = [...document.querySelectorAll('input[type=password]')].some((e) => e.getBoundingClientRect().width > 0)
    // 로그인 화면의 글자는 보내지 않는다
    const text = password ? '' : (document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 2000)
    return { url: location.href, password, elements, text }
  })
}

function click(r, index) {
  return inTab(
    r,
    (i) => {
      const el = document.querySelector(`[data-idly-idx="${i}"]`)
      if (!el) return false
      el.scrollIntoView({ block: 'center' })
      el.click()
      return true
    },
    [index],
  )
}

// 작업 탭 오른쪽 아래에 진행 패널을 띄운다 (페이지 스타일과 섞이지 않게 Shadow DOM)
function showPanel(r) {
  const buttons =
    r.status === '입력 필요'
      ? [['resume', '로그인 끝, 이어서 진행'], ['cancel', '그만두기']]
      : r.status === '확인 필요'
        ? [['confirm', '확정'], ['cancel', '취소']]
        : r.status === '진행 중' || r.status === '대기'
          ? [['cancel', '멈추기']]
          : []
  return inTab(
    r,
    (runId, title, status, message, buttons) => {
      let host = document.getElementById('idly-agent-panel')
      if (!host) {
        host = document.createElement('div')
        host.id = 'idly-agent-panel'
        host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647'
        host.attachShadow({ mode: 'open' })
        document.documentElement.appendChild(host)
      }
      const root = host.shadowRoot
      root.innerHTML = `
        <style>
          .p{font:13px/1.5 system-ui,sans-serif;width:300px;background:#fff;color:#1a1a1a;border:1px solid #1a1a1a;border-radius:6px;padding:12px 14px;box-shadow:0 4px 16px rgba(0,0,0,.15)}
          .t{font-weight:700;display:flex;justify-content:space-between;gap:8px}
          .s{font-size:11px;background:#f3f3f3;border-radius:3px;padding:0 6px}
          .m{margin:8px 0}
          .b{display:flex;gap:6px}
          button{font:inherit;height:32px;padding:0 10px;border:1px solid #1a1a1a;border-radius:6px;background:#fff;cursor:pointer}
          button.primary{background:#1a1a1a;color:#fff}
        </style>
        <div class="p"><div class="t"><span></span><span class="s"></span></div><div class="m"></div><div class="b"></div></div>`
      root.querySelector('.t span').textContent = `IDly · ${title}`
      root.querySelector('.s').textContent = status
      root.querySelector('.m').textContent = message
      buttons.forEach(([command, label], i) => {
        const button = document.createElement('button')
        button.textContent = label
        if (i === 0 && command !== 'cancel') button.className = 'primary'
        button.onclick = () => chrome.runtime.sendMessage({ type: 'IDLY_PANEL', runId, command })
        root.querySelector('.b').appendChild(button)
      })
    },
    [r.info.id, `${r.info.service} ${r.info.action}`, r.status, r.message, buttons],
  )
}
