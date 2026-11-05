(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));

  // 登录（截图用临时账号）
  if (document.getElementById('authEmail')) {
    document.getElementById('authEmail').value = 'shot@local.test';
    document.getElementById('authPass').value = 'ShotTest123';
    document.getElementById('authSubmit').click();
    await wait(5500);
  }
  if (typeof state === 'undefined' || !state.roles || !state.roles.length) {
    return 'LOGIN_FAIL';
  }

  await refreshRoles();
  const role = state.roles.find(r => r.name === 'Seraphine') || state.roles[0];

  const sel = document.querySelector('#modeSel');
  sel.value = 'epic';
  sel.dispatchEvent(new Event('change'));

  await startChat(role);
  await wait(600);

  // 只用一轮：epic 档单条回复常达 1200+ 字符，两轮会把视口撑爆、
  // 首轮开场白被推出画面。一轮 = 开场白 + 用户 + 长回复，构图最平衡。
  const turns = [
    "A hundred years on this tower. Does the silence ever wear you down?"
  ];
  for (const t of turns) {
    document.querySelector('#input').value = t;
    await send();
    for (let i = 0; i < 200 && state.sending; i++) {
      await wait(250);
    }
    await wait(1000);
  }

  // startChat 末尾会 switchTab('rooms')，窄屏下那是 overlay（主区变列表）。
  // 必须显式切回聊天视图，否则截到的是会话列表。
  state.tab = 'chat';
  exitOverlay();
  document.getElementById('who').textContent = role.name;
  document.getElementById('sub').textContent = role.personality || '';
  setHero(role.name);
  await wait(700);

  const img = document.querySelector('#heroFace');
  if (img && img.complete && img.naturalWidth > 0) img.style.display = 'block';

  // 滚到顶部：成品图展示的是从第一轮对话开始的内容
  const chat = document.querySelector('#chat');
  chat.scrollTop = 0;
  await wait(900);

  return 'chat: msgs=' + document.querySelectorAll('#chat .msg').length +
         ' hero=' + getComputedStyle(img).display +
         ' ad=' + (document.querySelector('#btnAd') ? 'YES' : 'no') +
         ' affinity=' + ((document.querySelector('#affBadge') || {}).textContent || '');
})()
