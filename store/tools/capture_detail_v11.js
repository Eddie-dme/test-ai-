(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));

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

  // classic 档：成品图里显示的是 classic · 2💖
  const sel = document.querySelector('#modeSel');
  if (sel) { sel.value = 'classic'; sel.dispatchEvent(new Event('change')); }

  // 先建会话让聊天工具栏出现（含模式选择器），再切到角色详情
  await startChat(role);
  await wait(900);
  await renderRoleDetail(role);
  await wait(1200);

  return 'detail: name=' + (document.querySelector('.detail h2') || {}).textContent +
         ' ad=' + (document.querySelector('#btnAd') ? 'YES' : 'no') +
         ' sections=' + document.querySelectorAll('.detail .sec').length;
})()
